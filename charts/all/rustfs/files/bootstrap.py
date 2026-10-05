"""Bootstrap the model bucket and scoped IAM users; refresh client CA bundles."""
import hashlib
import json
import os
from pathlib import Path
import ssl
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError


def kube(path, method="GET", body=None):
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    request = Request("https://kubernetes.default.svc" + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + token,
                               "Content-Type": "application/merge-patch+json"})
    with urlopen(request, context=context, timeout=30) as response:
        return json.load(response)


def reconcile_tls():
    """Restart the named storage workload when its subPath certificate changes."""
    fingerprint = hashlib.sha256(Path("/tls/tls.crt").read_bytes()
                                 + Path("/tls/ca.crt").read_bytes()).hexdigest()
    path = ("/apis/apps/v1/namespaces/" + os.environ["STORAGE_NAMESPACE"]
            + "/" + os.environ["STORAGE_WORKLOAD_KIND"] + "/rustfs")
    workload = kube(path)
    annotations = workload["spec"]["template"]["metadata"].get("annotations", {})
    key = "aiq.rhai.redhat.com/tls-fingerprint"
    if annotations.get(key) != fingerprint:
        kube(path, "PATCH", {"spec": {"template": {"metadata": {
            "annotations": {key: fingerprint}}}}})
        print("Storage certificate rollout requested", flush=True)
        raise RuntimeError("Waiting for storage certificate rollout")
    generation = workload["metadata"]["generation"]
    status = workload.get("status", {})
    replicas = workload["spec"].get("replicas", 1)
    if (status.get("observedGeneration", 0) < generation
            or status.get("updatedReplicas", 0) != replicas
            or status.get("readyReplicas", 0) != replicas):
        raise RuntimeError("Storage certificate rollout is not ready")
    # Verify the replacement endpoint before distributing new trust to clients.
    context = ssl.create_default_context(cafile="/tls/ca.crt")
    with urlopen(os.environ["S3_ENDPOINT"] + "/health/ready", context=context, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError("Storage TLS readiness failed")


def sync_ca():
    ca = Path("/tls/ca.crt").read_text()
    if "BEGIN CERTIFICATE" not in ca:
        raise ValueError("Missing RustFS CA certificate")
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    for namespace in os.environ["CLIENT_NAMESPACES"].split(","):
        name = os.environ["CA_CONFIGMAP"]
        url = f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}/configmaps/{name}"
        request = Request(url, method="PATCH", data=json.dumps({"data": {"ca.crt": ca}}).encode(),
                          headers={"Authorization": "Bearer " + token,
                                   "Content-Type": "application/merge-patch+json"})
        with urlopen(request, context=context, timeout=30):
            pass
    print("Client CA bundles synchronized", flush=True)


def admin(endpoint, access, secret, operation, params, body):
    url = endpoint + "/rustfs/admin/v3/" + operation + "?" + urlencode(params)
    payload = json.dumps(body).encode()
    request = AWSRequest(method="PUT", url=url, data=payload, headers={
        "Content-Type": "application/json", "X-Amz-Content-SHA256": hashlib.sha256(payload).hexdigest()})
    SigV4Auth(Credentials(access, secret), "s3", os.environ["AWS_DEFAULT_REGION"]).add_auth(request)
    request = Request(url, method="PUT", data=payload, headers=dict(request.headers))
    with urlopen(request, context=ssl.create_default_context(cafile="/tls/ca.crt"), timeout=60):
        pass


def bootstrap():
    endpoint, bucket = os.environ["S3_ENDPOINT"], os.environ["S3_BUCKET"]
    prefix = os.environ["S3_PREFIX"].strip("/")
    access, secret = os.environ["RUSTFS_ACCESS_KEY"], os.environ["RUSTFS_SECRET_KEY"]
    s3 = boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=access,
                      aws_secret_access_key=secret, region_name=os.environ["AWS_DEFAULT_REGION"],
                      verify="/tls/ca.crt", config=Config(s3={"addressing_style": "path"}))
    try:
        s3.create_bucket(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
            raise
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_lifecycle_configuration(Bucket=bucket, LifecycleConfiguration={"Rules": [{
        "ID": "abort-interrupted-publications", "Status": "Enabled", "Filter": {"Prefix": prefix + "/"},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}]})
    for identity in ("reader", "publisher"):
        key = Path(f"/credentials/{identity}/AWS_ACCESS_KEY_ID").read_text().strip()
        password = Path(f"/credentials/{identity}/AWS_SECRET_ACCESS_KEY").read_text().strip()
        policy_name = "aiq-model-" + identity
        actions = ["s3:GetObject"]
        if identity == "publisher":
            actions += ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [f"arn:aws:s3:::{bucket}"]},
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{bucket}"],
             "Condition": {"StringLike": {"s3:prefix": [prefix + "/*"]}}},
            {"Effect": "Allow", "Action": actions, "Resource": [f"arn:aws:s3:::{bucket}/{prefix}/*"]},
        ]}
        admin(endpoint, access, secret, "add-canned-policy", {"name": policy_name}, policy)
        admin(endpoint, access, secret, "add-user", {"accessKey": key}, {"secretKey": password, "status": "enabled"})
        admin(endpoint, access, secret, "set-user-or-group-policy",
              {"policyName": policy_name, "userOrGroup": key, "isGroup": "false"}, {})
    print("Model bucket and publisher/reader policies ready", flush=True)


if __name__ == "__main__":
    # Jobs may start before cert-manager, ESO, or the object store is ready.
    deadline = time.monotonic() + 1800
    while True:
        try:
            reconcile_tls()
            sync_ca()
            if os.environ.get("CA_ONLY") != "true":
                bootstrap()
            break
        except Exception as error:
            if time.monotonic() >= deadline:
                raise
            # Do not log request headers, bodies, or credentials.
            print(f"Waiting for storage prerequisites ({type(error).__name__})", flush=True)
            time.sleep(15)
