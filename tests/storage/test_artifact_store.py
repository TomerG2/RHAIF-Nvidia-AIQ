import hashlib
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"charts/all/vllm-inference-service/files/{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


store = load("artifact_store")
gate = load("gate")
distributed = load("distributed")


def test_publication_recovers_only_its_abandoned_staging(tmp_path, monkeypatch):
    abandoned = tmp_path / ".aiq-publish-abandoned"
    abandoned.mkdir()
    (abandoned / "partial").write_bytes(b"incomplete")
    other = tmp_path / "unrelated"
    other.mkdir()
    target = Mock(return_value="published")
    monkeypatch.setattr(store, "_publish", target)
    assert store.publish(None, "uri", "repo", "revision", tmp_path) == "published"
    assert not abandoned.exists() and other.is_dir()
    target.assert_called_once_with(None, "uri", "repo", "revision", tmp_path)


@pytest.mark.parametrize("name", ["../secret", "/absolute", "folder/../secret", "./file", "a//b", "a\\b", "_READY.json", ".aiq-ready.json", "x/.aiq-part-file"])
def test_rejects_unsafe_manifest_paths(name):
    with pytest.raises(ValueError):
        store.safe_path(name)


def manifest(data=b"weights"):
    return {"schema": 1, "repo": "test/tiny", "revision": "a" * 40, "bytes": len(data), "files": [
        {"path": "model.bin", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]}


@pytest.mark.parametrize("change", [
    lambda m: m.update(bytes=999),
    lambda m: m.update(revision="main"),
    lambda m: m.update(files=m["files"] * 2),
    lambda m: m["files"][0].update(sha256="wrong"),
    lambda m: m.update(files=[]),
])
def test_incomplete_or_corrupt_manifest_is_rejected(change):
    data = manifest()
    change(data)
    with pytest.raises(ValueError):
        store.validate_manifest(data)


def test_corrupt_download_revokes_old_ready_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "RESERVE", 0)
    old = tmp_path / store.LOCAL_READY
    old.write_text("old marker")
    (tmp_path / ".aiq-part-abandoned").write_bytes(b"abandoned")
    data = manifest()
    client = Mock()
    client.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(
        json.dumps(data).encode() if kw["Key"].endswith(store.READY) else b"bad")}
    with pytest.raises(ValueError, match="checksum"):
        store.download(client, "s3://models/snapshots/test/tiny/" + "a" * 40, tmp_path)
    assert not old.exists()
    assert not list(tmp_path.rglob(".aiq-part-*"))


def test_disk_check_uses_real_filesystem_free_space(tmp_path, monkeypatch):
    monkeypatch.setattr(store.shutil, "disk_usage", lambda _: Mock(free=12))
    monkeypatch.setattr(store, "RESERVE", 3)
    store.disk_check(tmp_path, 9)
    with pytest.raises(RuntimeError, match="actual free space"):
        store.disk_check(tmp_path, 10)


def test_model_cache_requires_each_selected_node():
    nodes = [{"metadata": {"name": "one"}}, {"metadata": {"name": "two"}}]
    cache = {"status": {"nodeStatus": {"one": "NodeDownloaded"}, "copies": {"available": 1}}}
    with pytest.raises(RuntimeError, match="two"):
        gate.check_cache(nodes, cache)
    cache["status"]["nodeStatus"]["two"] = "NodeDownloaded"
    cache["status"]["copies"]["available"] = 2
    gate.check_cache(nodes, cache)
    with pytest.raises(RuntimeError):
        gate.check_cache([], {"status": {"copies": {"available": 0}}})


def test_missing_operator_is_a_deployment_blocker(monkeypatch):
    config = {"serving": {"nodesPerReplica": 2}}
    def fake_api(path):
        if "leaderworkersets" in path:
            raise RuntimeError("LWS operator missing")
        return {"status": {"conditions": [{"type": "Established", "status": "True"}]}}
    monkeypatch.setattr(gate, "api", fake_api)
    with pytest.raises(RuntimeError, match="LWS operator missing"):
        gate.preflight(config)


def test_missing_network_attachment_is_a_deployment_blocker(monkeypatch):
    import yaml
    config = yaml.safe_load((ROOT / "values-global.yaml").read_text())["global"]
    config["serving"]["networkAttachments"] = ["aiq-inference/rdma"]
    monkeypatch.setattr(gate, "selected_nodes", lambda _: [])
    monkeypatch.setattr(gate, "check_nodes", lambda *_: [])
    def fake_api(path):
        if "network-attachment-definitions" in path:
            raise RuntimeError("RDMA network attachment missing")
        if "clusterserviceversions" in path:
            return {"items": [{"metadata": {"name": "rhods-operator.3.5.1"}, "status": {"phase": "Succeeded"}}]}
        if "datascienceclusters/" in path:
            return {"spec": {"components": {"kserve": {"modelCache": {"managementState": "Managed"}}}}}
        return {"status": {"conditions": [{"type": "Established", "status": "True"}]}}
    monkeypatch.setattr(gate, "api", fake_api)
    with pytest.raises(RuntimeError, match="RDMA network attachment missing"):
        gate.preflight(config)


@pytest.mark.parametrize("rank", [0, 1, 3])
def test_distributed_launch_assigns_rank_and_headless_workers(rank):
    env = {"AIQ_NODES": "4", "AIQ_GPUS": "8", "AIQ_TP": "8", "AIQ_PP": "4",
           "LWS_WORKER_INDEX": str(rank), "LWS_LEADER_ADDRESS": "replica-0", "AIQ_SERVED_NAME": "model"}
    args = distributed.command(env, ["--max-model-len=4096"])
    assert f"--node-rank={rank}" in args
    assert "--master-addr=replica-0" in args
    assert "--nnodes=4" in args and "--pipeline-parallel-size=4" in args
    assert ("--headless" in args) == bool(rank)
    assert args[2] == "/mnt/models"


def test_rank_outside_replica_is_rejected():
    env = {"AIQ_NODES": "2", "AIQ_GPUS": "1", "AIQ_TP": "1", "AIQ_PP": "2", "LWS_WORKER_INDEX": "2"}
    with pytest.raises(ValueError, match="invalid worker rank"):
        distributed.command(env, [])


def test_backend_transition_deletes_only_opposite_named_cr(monkeypatch):
    calls = []
    def fake_api(path, method="GET", body=None):
        calls.append((path, method, body))
        return {"metadata": {"uid": "old-uid"}}
    monkeypatch.setattr(gate, "api", fake_api)
    monkeypatch.setenv("SERVICE_NAME", "vllm-inference-service")
    monkeypatch.setenv("REPLACE_BACKEND", "true")
    config = {"serving": {"nodesPerReplica": 2}, "inference": {"namespace": "aiq-inference"}}
    with pytest.raises(RuntimeError, match="release its GPUs"):
        gate.run("switch", config)
    assert len(calls) == 2
    assert calls[1][0] == "/apis/serving.kserve.io/v1beta1/namespaces/aiq-inference/inferenceservices/vllm-inference-service"
    assert calls[1][1] == "DELETE"
    assert calls[1][2]["preconditions"] == {"uid": "old-uid"}


def test_backend_transition_can_require_explicit_retirement(monkeypatch):
    monkeypatch.setattr(gate, "api", lambda _: {"metadata": {"uid": "old-uid"}})
    monkeypatch.setenv("SERVICE_NAME", "vllm-inference-service")
    monkeypatch.setenv("REPLACE_BACKEND", "false")
    config = {"serving": {"nodesPerReplica": 1}, "inference": {"namespace": "aiq-inference"}}
    with pytest.raises(RuntimeError, match="retire it explicitly"):
        gate.run("switch", config)
