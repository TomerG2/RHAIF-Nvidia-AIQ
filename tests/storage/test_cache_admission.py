import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("admission", ROOT / "charts/all/model-cache-platform/files/admission.py")
admission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admission)
CONFIG = {"modelCache": {"jobNamespace": "redhat-ods-applications"},
          "serving": {"tolerations": [{"key": "odh-notebook", "operator": "Exists", "effect": "NoSchedule"}]},
          "modelTools": {"image": "internal/helper@sha256:abc"}, "rhoai": {"localModelAgentImage": "agent@sha256:def"}}


def pod(namespace="redhat-ods-applications", account="aiq-model-reader"):
    return {"namespace": namespace, "kind": {"kind": "Pod"}, "object": {
        "metadata": {"ownerReferences": [{"kind": "Job"}]},
        "spec": {"serviceAccountName": account, "containers": [
            {"name": "storage-initializer", "image": CONFIG["modelTools"]["image"]}]}}}


def test_job_tolerations_preserve_existing_and_are_idempotent():
    request = pod()
    existing = {"key": "existing", "operator": "Exists"}
    request["object"]["spec"]["tolerations"] = [existing]
    patch = admission.patches(request, CONFIG)
    assert patch == [{"op": "add", "path": "/spec/tolerations", "value": [existing, *CONFIG["serving"]["tolerations"]]}]
    request["object"]["spec"]["tolerations"] = patch[0]["value"]
    assert admission.patches(request, CONFIG) == []


@pytest.mark.parametrize("change", [
    lambda r: r.update(namespace="other"),
    lambda r: r["object"]["spec"].update(serviceAccountName="operator"),
    lambda r: r["object"]["metadata"].update(ownerReferences=[]),
    lambda r: r["object"]["spec"]["containers"][0].update(image="unrelated"),
])
def test_unrelated_pods_are_unchanged(change):
    request = pod()
    change(request)
    assert admission.patches(request, CONFIG) == []


def test_config_preserves_operator_fields_and_limits_patch_scope():
    request = {"namespace": CONFIG["modelCache"]["jobNamespace"], "kind": {"kind": "ConfigMap"},
               "object": {"metadata": {"name": "inferenceservice-config"}, "data": {
                   "localModel": json.dumps({"enabled": True, "futureField": 42}), "unrelated": "keep"}}}
    original = copy.deepcopy(request)
    patch = admission.patches(request, CONFIG)
    assert request == original
    assert patch[0]["path"] == "/data/localModel"
    desired = json.loads(patch[0]["value"])
    assert desired["futureField"] == 42 and desired["enabled"] is True
    assert desired["jobNamespace"] == CONFIG["modelCache"]["jobNamespace"]
    request["object"]["data"]["localModel"] = patch[0]["value"]
    assert admission.patches(request, CONFIG) == []
