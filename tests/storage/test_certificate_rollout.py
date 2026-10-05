import hashlib
import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "charts/all/rustfs/files/bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


@pytest.fixture
def certificate(tmp_path, monkeypatch):
    (tmp_path / "tls.crt").write_bytes(b"renewed-server-certificate")
    (tmp_path / "ca.crt").write_bytes(b"trusted-ca")
    monkeypatch.setattr(bootstrap, "Path", lambda name: tmp_path / Path(name).name)
    monkeypatch.setenv("STORAGE_NAMESPACE", "aiq-model-storage")
    monkeypatch.setenv("STORAGE_WORKLOAD_KIND", "deployments")
    monkeypatch.setenv("S3_ENDPOINT", "https://rustfs:9000")
    return hashlib.sha256(b"renewed-server-certificatetrusted-ca").hexdigest()


def workload(fingerprint=None):
    return {"metadata": {"generation": 2}, "spec": {"replicas": 1, "template": {
        "metadata": {"annotations": {"aiq.rhai.redhat.com/tls-fingerprint": fingerprint}}}},
        "status": {"observedGeneration": 2, "updatedReplicas": 1, "readyReplicas": 1}}


def test_renewal_rolls_only_named_storage_workload(certificate, monkeypatch):
    api = Mock(return_value=workload("old-certificate"))
    monkeypatch.setattr(bootstrap, "kube", api)
    with pytest.raises(RuntimeError, match="certificate rollout"):
        bootstrap.reconcile_tls()
    path, method, body = api.call_args.args
    assert path == "/apis/apps/v1/namespaces/aiq-model-storage/deployments/rustfs"
    assert method == "PATCH"
    assert body["spec"]["template"]["metadata"]["annotations"]["aiq.rhai.redhat.com/tls-fingerprint"] == certificate


def test_unchanged_certificate_checks_tls_without_restarting(certificate, monkeypatch):
    api = Mock(return_value=workload(certificate))
    monkeypatch.setattr(bootstrap, "kube", api)
    monkeypatch.setattr(bootstrap.ssl, "create_default_context", Mock())
    response = Mock()
    response.__enter__ = Mock(return_value=Mock(status=200))
    response.__exit__ = Mock(return_value=False)
    connection = Mock(return_value=response)
    monkeypatch.setattr(bootstrap, "urlopen", connection)
    bootstrap.reconcile_tls()
    api.assert_called_once()
    assert connection.call_args.args[0] == "https://rustfs:9000/health/ready"


def test_unfinished_rollout_blocks_client_trust_update(certificate, monkeypatch):
    current = workload(certificate)
    current["status"]["updatedReplicas"] = 0
    monkeypatch.setattr(bootstrap, "kube", Mock(return_value=current))
    with pytest.raises(RuntimeError, match="not ready"):
        bootstrap.reconcile_tls()
