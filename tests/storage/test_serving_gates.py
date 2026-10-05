"""Regression checks for rollout capacity and cache prerequisites."""
import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "serving_gate", ROOT / "charts/all/vllm-inference-service/files/gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


def container(gpus, **fields):
    return {"resources": {"requests": {"nvidia.com/gpu": str(gpus)}}, **fields}


def node(name="gpu-node", domain="gpu-node"):
    return {"metadata": {"name": name, "labels": {"kubernetes.io/hostname": domain}},
            "status": {"allocatable": {"nvidia.com/gpu": "4"},
                       "conditions": [{"type": "Ready", "status": "True"}]}}


@pytest.mark.parametrize("applications,inits,expected", [
    ([container(1)], [container(2), container(3)], 3),
    ([container(2)], [container(1, restartPolicy="Always"), container(2)], 3),
    ([container(1)], [container(3), container(2, restartPolicy="Always")], 3),
    ([container(2)], [container(1, restartPolicy="Always"), container(3)], 4),
])
def test_gpu_request_matches_init_peak_and_running_sidecars(applications, inits, expected):
    assert gate.pod_gpu_request({"spec": {"containers": applications,
                                         "initContainers": inits}}) == expected


def test_gpu_request_defaults_to_limit_and_adds_overhead():
    pod = {"spec": {"containers": [{"resources": {"limits": {"nvidia.com/gpu": "2"}}}],
                    "overhead": {"nvidia.com/gpu": "1"}}}
    assert gate.pod_gpu_request(pod) == 3


def test_paginated_list_preserves_filters_and_encodes_continuation(monkeypatch):
    calls = []
    def api(path):
        calls.append(path)
        query = parse_qs(urlsplit(path).query)
        assert query["labelSelector"] == ["cache=true"]
        assert query["limit"] == ["500"]
        if "continue" not in query:
            return {"items": ["first"], "metadata": {"continue": "a+/= token"}}
        assert query["continue"] == ["a+/= token"]
        return {"items": ["second"], "metadata": {}}
    monkeypatch.setattr(gate, "api", api)
    assert gate.api_items("/api/v1/nodes?labelSelector=cache%3Dtrue") == ["first", "second"]
    assert len(calls) == 2


def preflight_api(monkeypatch, pods):
    config = yaml.safe_load((ROOT / "values-global.yaml").read_text())["global"]
    config["serving"].update(gpusPerNode=4)
    monkeypatch.setenv("SERVICE_NAME", "custom-serving")
    monkeypatch.setattr(gate, "selected_nodes", lambda _: [node()])
    def api(path):
        if "clusterserviceversions" in path:
            return {"items": [{"metadata": {"name": "rhods-operator.3.5.1"},
                               "status": {"phase": "Succeeded"}}]}
        if "datascienceclusters/" in path:
            return {"spec": {"components": {"kserve": {"modelCache": {"managementState": "Managed"}}}}}
        if path.startswith("/api/v1/pods"):
            if "continue=" in path:
                return {"items": pods[1:]}
            return {"items": pods[:1], "metadata": {"continue": "next"}}
        return {"status": {"conditions": [{"type": "Established", "status": "True"}]}}
    monkeypatch.setattr(gate, "api", api)
    return config


@pytest.mark.parametrize("label", ["aiq.rhai.redhat.com/serving", "serving.kserve.io/inferenceservice"])
def test_custom_release_gets_credit_for_its_old_workload(monkeypatch, label):
    pod = {"metadata": {"namespace": "aiq-inference", "labels": {label: "custom-serving"}},
           "spec": {"nodeName": "gpu-node", "containers": [container(4)]}}
    gate.preflight(preflight_api(monkeypatch, [pod]))


def test_other_release_on_second_page_does_not_get_gpu_credit(monkeypatch):
    pods = [{"metadata": {"namespace": "aiq-inference"}, "spec": {"containers": []}},
            {"metadata": {"namespace": "aiq-inference", "labels": {
                "aiq.rhai.redhat.com/serving": "vllm-inference-service"}},
             "spec": {"nodeName": "gpu-node", "containers": [], "initContainers": [container(1)]}}]
    with pytest.raises(RuntimeError, match="Other workloads occupy"):
        gate.preflight(preflight_api(monkeypatch, pods))


def test_explicit_cache_footprint_ignores_failed_spare_nodes():
    config = yaml.safe_load((ROOT / "values-global.yaml").read_text())["global"]
    config["serving"].update(replicas=2, nodeNames=["one", "two"])
    selected = gate.check_nodes(config, [node("one", "one"), node("two", "two"), node("spare", "spare")])
    assert [n["metadata"]["name"] for n in selected] == ["one", "two"]
    cache = {"status": {"nodeStatus": {"one": "NodeDownloaded", "two": "NodeDownloaded", "spare": "DownloadFailed"},
                        "copies": {"available": 2, "failed": 1}}}
    gate.check_cache(selected, cache)
    cache["status"]["nodeStatus"]["two"] = "DownloadFailed"
    with pytest.raises(RuntimeError, match="two"):
        gate.check_cache(selected, cache)


def test_cache_footprint_requires_distinct_topology_domains():
    config = yaml.safe_load((ROOT / "values-global.yaml").read_text())["global"]
    config["serving"].update(replicas=2, nodeNames=["one", "two"])
    with pytest.raises(RuntimeError, match="distinct topology domains"):
        gate.check_nodes(config, [node("one", "same"), node("two", "same")])


def test_allowlist_cannot_skip_a_temporarily_ineligible_cold_node():
    config = yaml.safe_load((ROOT / "values-global.yaml").read_text())["global"]
    config["serving"]["nodeNames"] = ["warm", "cold"]
    cold = node("cold", "cold")
    cold["status"]["conditions"][0]["status"] = "False"
    with pytest.raises(RuntimeError, match="Every allowlisted node must be eligible.*cold"):
        gate.check_nodes(config, [node("warm", "warm"), cold])
