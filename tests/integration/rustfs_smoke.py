"""Run inside the pinned downloader image against an isolated local RustFS.

See scripts/test-rustfs.sh. Hugging Face is replaced with a tiny snapshot fixture;
S3, TLS, IAM, read-back verification, and all local disk operations are real.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError, SSLError

sys.path.insert(0, "/source/charts/all/vllm-inference-service/files")
sys.path.insert(0, "/source/charts/all/rustfs/files")
import artifact_store as store
import bootstrap


def main():
    bootstrap.bootstrap()
    bootstrap.bootstrap()  # Repeat-install and credential reconciliation.
    bucket, repo, revision = "aiq-test-models", "test/tiny", "a" * 40
    uri = f"s3://{bucket}/snapshots/{repo}/{revision}"
    prefix = store.location(uri)[1]
    credentials = {
        "aws_access_key_id": "aiq-test-admin",
        "aws_secret_access_key": "isolated-integration-test-admin",
    }
    admin = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"],
                        verify=os.environ["AWS_CA_BUNDLE"], **credentials)
    os.environ["AWS_ACCESS_KEY_ID"] = "aiq-test-publisher"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "isolated-integration-test-publisher"
    publisher = store.client()
    store.CONCURRENCY = 1  # Deterministic interruption after the first file.
    content = {"config.json": b'{"architectures":["Test"]}', "nested/model.bin": b"weights" * 4096}
    siblings = [SimpleNamespace(rfilename=name, size=len(data), lfs=None,
                  blob_id=hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest())
                for name, data in content.items()]

    def hf_download(repo, filename, revision, local_dir):
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content[filename])
        return str(target)

    with tempfile.TemporaryDirectory() as work:
        root = Path(work)
        with patch("huggingface_hub.HfApi") as api, patch("huggingface_hub.hf_hub_download", hf_download):
            api.return_value.model_info.return_value = SimpleNamespace(sha=revision, siblings=siblings)
            original_upload = publisher.upload_file

            def interrupt(filename, bucket, key, **kwargs):
                if key.endswith("model.bin"):
                    raise ConnectionError("Injected upload interruption")
                return original_upload(filename, bucket, key, **kwargs)

            with patch.object(publisher, "upload_file", interrupt):
                try:
                    store.publish(publisher, uri, repo, revision, root / "publish")
                except ConnectionError:
                    pass
                else:
                    raise AssertionError("Expected interruption")
            assert store.read_json(publisher, bucket, prefix + "/" + store.READY) is None
            manifest = store.publish(publisher, uri, repo, revision, root / "publish")
            assert len(manifest["files"]) == 2
        # Reuse must work without any HF call, and without another upload.
        with patch.object(publisher, "upload_file", side_effect=AssertionError("unexpected upload")):
            assert store.publish(publisher, uri, repo, revision, root / "publish") == manifest
        os.environ["AWS_ACCESS_KEY_ID"] = "aiq-test-reader"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "isolated-integration-test-reader"
        reader = store.client()
        try:
            reader.put_object(Bucket=bucket, Key=prefix + "/forbidden", Body=b"x")
        except ClientError as error:
            assert error.response["Error"]["Code"] == "AccessDenied"
        else:
            raise AssertionError("Reader can write")
        store.download(reader, uri, root / "node-1")
        # Independent concurrent copies, including a replacement node.
        with store.concurrent.futures.ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda n: store.download(reader, uri, root / n), ["node-2", "replacement-node"]))
        # Warm cache: only the small availability manifest is fetched.
        original_get = reader.get_object
        with patch.object(reader, "get_object", side_effect=lambda **kw: original_get(**kw)
                          if kw["Key"].endswith(store.READY) else (_ for _ in ()).throw(AssertionError("full download"))):
            store.download(reader, uri, root / "node-1")
        # Corruption is detected, never marked cache-ready.
        admin.put_object(Bucket=bucket, Key=prefix + "/nested/model.bin", Body=b"corrupt")
        try:
            store.download(reader, uri, root / "corrupt-node")
        except ValueError:
            assert not (root / "corrupt-node" / store.LOCAL_READY).exists()
        else:
            raise AssertionError("Corruption was accepted")
        try:
            store.disk_check(root, 10**30)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Disk capacity check did not fail")
    untrusted = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], verify=True, **credentials)
    try:
        untrusted.list_buckets()
    except SSLError:
        pass
    else:
        raise AssertionError("Untrusted certificate was accepted")
    print(json.dumps({"rustfs": "1.0.1", "result": "passed", "checks": [
        "TLS trust", "scoped reader credentials", "complete publication", "interrupted upload recovery",
        "idempotency", "integrity", "concurrent caches", "warm reuse", "cold node", "real disk space"]}))


if __name__ == "__main__":
    main()
