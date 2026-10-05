"""Publish immutable HF snapshots and populate KServe's per-node warm caches.

Dependencies are supplied by the digest-pinned storage-initializer image. No
package installation or HF access is performed in serving/cache pods.
"""
import concurrent.futures
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


READY = "_READY.json"
LOCAL_READY = ".aiq-ready.json"
RESERVE = int(os.environ.get("DISK_RESERVE_BYTES", str(1024**3)))
CONCURRENCY = int(os.environ.get("TRANSFER_CONCURRENCY", "4"))


def event(stage, **fields):
    print(json.dumps({"stage": stage, **fields}), flush=True)


def client():
    endpoint = os.environ["S3_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise ValueError("S3_ENDPOINT must use TLS")
    return boto3.session.Session().client(
        "s3", endpoint_url=endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        verify=os.environ["AWS_CA_BUNDLE"],
        config=Config(s3={"addressing_style": "path"},
                      retries={"mode": "standard", "max_attempts": 8},
                      max_pool_connections=max(10, CONCURRENCY)),
    )


def location(uri):
    parsed = urlparse(uri)
    prefix = parsed.path.strip("/")
    if parsed.scheme != "s3" or not parsed.netloc or not prefix or parsed.query or parsed.fragment:
        raise ValueError("Expected s3://bucket/immutable-prefix")
    safe_path(prefix)
    return parsed.netloc, prefix


def safe_path(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or "\\" in name
            or str(path) != name or name == READY or any(p.startswith(".aiq-") for p in path.parts)):
        raise ValueError(f"Unsafe or reserved artifact path: {name!r}")
    return name


def digest(stream, algorithm="sha256", prefix=b""):
    h = hashlib.new(algorithm, prefix)
    while block := stream.read(8 * 1024**2):
        h.update(block)
    return h.hexdigest()


def disk_check(directory, required):
    directory.mkdir(parents=True, exist_ok=True)
    # os.access is not sufficient on ACL/SELinux-constrained mounted volumes.
    with tempfile.TemporaryFile(dir=directory) as probe:
        probe.write(b"permission-check")
        probe.flush()
        os.fsync(probe.fileno())
    available = shutil.disk_usage(directory).free
    if available < required + RESERVE:
        raise RuntimeError(f"Insufficient actual free space: need {required + RESERVE}, have {available}")


def read_json(s3, bucket, key):
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        with response["Body"] as stream:
            return json.load(stream)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise


def verify_remote(s3, bucket, key, entry):
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return False
        raise
    with response["Body"] as stream:
        return response["ContentLength"] == entry["size"] and digest(stream) == entry["sha256"]


def validate_manifest(manifest):
    if not manifest or manifest.get("schema") != 1 or not manifest.get("files"):
        raise ValueError("Publication is not complete or has an invalid manifest")
    if not re.fullmatch(r"[0-9a-f]{40}", manifest.get("revision", "")):
        raise ValueError("Manifest revision is not an immutable HF commit")
    names = set()
    for entry in manifest["files"]:
        name = safe_path(entry["path"])
        if name in names or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) or entry["size"] < 0:
            raise ValueError("Invalid or duplicate manifest entry")
        names.add(name)
    if sum(e["size"] for e in manifest["files"]) != manifest["bytes"]:
        raise ValueError("Manifest size mismatch")
    return manifest


def verify_publication(s3, bucket, prefix, manifest):
    validate_manifest(manifest)
    def verify(entry):
        if not verify_remote(s3, bucket, prefix + "/" + entry["path"], entry):
            raise RuntimeError(f"Published artifact is missing or corrupt: {entry['path']}")
    with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as executor:
        list(executor.map(verify, manifest["files"]))


def publish(s3, uri, repo, revision, directory):
    # The optional Xet client stages large reconstruction buffers per file.
    # Use HF's streaming HTTP downloader inside the bounded publication pod.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from huggingface_hub import HfApi, hf_hub_download

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Pin model.revision to a full Hugging Face commit before deployment")
    bucket, prefix = location(uri)
    existing = read_json(s3, bucket, prefix + "/" + READY)
    if existing:
        if existing.get("repo") != repo or existing.get("revision") != revision:
            raise ValueError("Immutable publication prefix belongs to a different snapshot")
        verify_publication(s3, bucket, prefix, existing)
        event("publication_reused", revision=revision, bytes=existing["bytes"])
        return existing
    info = HfApi().model_info(repo, revision=revision, files_metadata=True)
    if info.sha != revision:
        raise ValueError("Resolved HF revision differs from the configured immutable revision")
    files = info.siblings
    if not files or any(f.size is None for f in files):
        raise ValueError("Hugging Face did not return a complete snapshot inventory")
    total = sum(f.size for f in files)
    if total > int(os.environ["MODEL_MAX_BYTES"]):
        raise ValueError("Snapshot exceeds configured cache modelSize; increase it before publishing")
    # Only CONCURRENCY files are staged at once, then removed after verification.
    disk_check(directory, sum(sorted((f.size for f in files), reverse=True)[:CONCURRENCY]))
    started = time.monotonic()

    def upload(file):
        name = safe_path(file.rfilename)
        with tempfile.TemporaryDirectory(dir=directory) as staging:
            path = Path(hf_hub_download(repo, name, revision=revision, local_dir=staging))
            with path.open("rb") as stream:
                sha256 = digest(stream)
            if path.stat().st_size != file.size:
                raise ValueError(f"HF file size mismatch: {name}")
            lfs = file.lfs
            expected = (lfs.get("sha256") if isinstance(lfs, dict) else getattr(lfs, "sha256", None)) if lfs else None
            if expected and sha256 != expected:
                raise ValueError(f"HF LFS checksum mismatch: {name}")
            if not lfs:
                with path.open("rb") as stream:
                    git_hash = digest(stream, "sha1", f"blob {file.size}\0".encode())
                if git_hash != file.blob_id:
                    raise ValueError(f"HF git checksum mismatch: {name}")
            entry = {"path": name, "size": file.size, "sha256": sha256}
            key = prefix + "/" + name
            if not verify_remote(s3, bucket, key, entry):
                # SDK multipart handles >5GiB files; unfinished multipart uploads
                # are garbage-collected by the bucket lifecycle rule.
                from boto3.s3.transfer import TransferConfig
                s3.upload_file(str(path), bucket, key,
                               Config=TransferConfig(max_concurrency=1),
                               ExtraArgs={"Metadata": {"sha256": sha256}})
                if not verify_remote(s3, bucket, key, entry):
                    raise RuntimeError(f"S3 read-back verification failed: {name}")
            event("artifact_verified", path=name, bytes=file.size)
            return entry

    with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as executor:
        entries = list(executor.map(upload, files))
    manifest = {"schema": 1, "repo": repo, "revision": revision,
                "bytes": total, "files": sorted(entries, key=lambda e: e["path"])}
    # The only availability signal. No consumer lists a partially uploaded prefix.
    body = json.dumps(manifest, sort_keys=True).encode()
    try:
        s3.put_object(Bucket=bucket, Key=prefix + "/" + READY, Body=body,
                      ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("PreconditionFailed", "412"):
            raise
        if read_json(s3, bucket, prefix + "/" + READY) != manifest:
            raise RuntimeError("Concurrent publication has different contents") from exc
    event("publication_ready", revision=revision, bytes=total,
          download_seconds=time.monotonic() - started)
    return manifest


def local_valid(path, entry):
    if path.is_symlink() or not path.is_file() or path.stat().st_size != entry["size"]:
        return False
    with path.open("rb") as stream:
        return digest(stream) == entry["sha256"]


def download(s3, uri, directory):
    bucket, prefix = location(uri)
    manifest = validate_manifest(read_json(s3, bucket, prefix + "/" + READY))
    if not prefix.endswith("/" + manifest["repo"] + "/" + manifest["revision"]):
        raise ValueError("Publication identity does not match the immutable source URI")
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    # The platform may retry duplicate download jobs for the same node/cache.
    with (directory / ".aiq-lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (directory / LOCAL_READY).unlink(missing_ok=True)
        # Recover space left by a killed downloader before reserving more space.
        for partial in directory.rglob(".aiq-part-*"):
            if partial.is_file() and not partial.is_symlink():
                partial.unlink()
        missing = []
        for entry in manifest["files"]:
            path = directory / entry["path"]
            if not path.resolve().is_relative_to(directory.resolve()):
                raise ValueError("Cache path escapes through a symlink")
            if not local_valid(path, entry):
                missing.append(entry)
        disk_check(directory, sum(e["size"] for e in missing))

        def fetch(entry):
            path = directory / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".aiq-part-", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as output:
                    response = s3.get_object(Bucket=bucket, Key=prefix + "/" + entry["path"])
                    with response["Body"] as stream:
                        shutil.copyfileobj(stream, output, 8 * 1024**2)
                    output.flush()
                    os.fsync(output.fileno())
                if not local_valid(Path(temporary), entry):
                    raise ValueError(f"Downloaded artifact checksum mismatch: {entry['path']}")
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
                event("cache_file_ready", path=entry["path"], bytes=entry["size"])
            finally:
                Path(temporary).unlink(missing_ok=True)

        with concurrent.futures.ThreadPoolExecutor(CONCURRENCY) as executor:
            list(executor.map(fetch, missing))
        marker = directory / ".aiq-part-ready"
        marker.write_text(json.dumps({"uri": uri, "revision": manifest["revision"]}))
        marker.chmod(0o644)
        os.replace(marker, directory / LOCAL_READY)
    event("cache_ready", uri=uri, downloaded_files=len(missing),
          download_seconds=time.monotonic() - started)
    return manifest


if __name__ == "__main__":
    if sys.argv[1] == "publish":
        publish(client(), os.environ["MODEL_URI"], os.environ["HF_REPO"],
                os.environ["HF_REVISION"], Path("/work"))
    else:
        # KServe replaces container args with [sourceURI, destination].
        download(client(), sys.argv[1], Path(sys.argv[2]))
