"""Deployment prerequisites, cache/health gates, and scoped backend migration."""
import json
import os
from pathlib import Path
import ssl
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError


def api(path, method="GET", body=None):
    token = Path("/var/run/secrets/kubernetes.io/serviceaccount/token").read_text()
    context = ssl.create_default_context(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
    request = Request("https://kubernetes.default.svc" + path, method=method,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urlopen(request, context=context, timeout=30) as response:
        return json.load(response)


def quantity(value):
    import re
    match = re.fullmatch(r"([0-9]+)(Ki|Mi|Gi|Ti)?", str(value))
    if not match:
        raise ValueError("Use integer bytes, Ki, Mi, Gi or Ti for model/cache sizes")
    return int(match[1]) * {None: 1, "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}[match[2]]


def selected_nodes(config):
    selector = {**config["modelCache"]["nodeSelector"], **config["serving"]["nodeSelector"]}
    query = urlencode({"labelSelector": ",".join(f"{key}={value}" for key, value in selector.items())})
    return api("/api/v1/nodes?" + query)["items"]


def check_nodes(config, nodes):
    serving = config["serving"]
    required = serving["nodesPerReplica"] * serving["replicas"]
    eligible = []
    for node in nodes:
        ready = any(c["type"] == "Ready" and c["status"] == "True" for c in node["status"].get("conditions", []))
        if not ready or node.get("spec", {}).get("unschedulable"):
            continue
        def tolerated(taint):
            return any((not t.get("effect") or t["effect"] == taint["effect"])
                       and (t.get("key", "") == taint["key"] or not t.get("key"))
                       and (t.get("operator") == "Exists" or t.get("value", "") == taint.get("value", ""))
                       for t in serving["tolerations"])
        if any(t["effect"] in ("NoSchedule", "NoExecute") and not tolerated(t)
               for t in node.get("spec", {}).get("taints", [])):
            continue
        allocatable = node["status"]["allocatable"]
        if int(allocatable.get("nvidia.com/gpu", "0")) < serving["gpusPerNode"]:
            continue
        if any(int(allocatable.get(key, 0)) < int(value) for key, value in serving["rdmaResources"].items()):
            continue
        labels = node["metadata"]["labels"]
        if serving["topologyKey"] not in labels:
            continue
        eligible.append(node)
    domains = {n["metadata"]["labels"][serving["topologyKey"]] for n in eligible}
    if len(domains) < required:
        raise RuntimeError(f"Need {required} ready GPU nodes in distinct topology domains; found {len(domains)}")
    return eligible


def check_cache(nodes, cache):
    expected = {n["metadata"]["name"] for n in nodes}
    statuses = cache.get("status", {}).get("nodeStatus", {})
    missing = {name: statuses.get(name, "Missing") for name in expected if statuses.get(name) != "NodeDownloaded"}
    copies = cache.get("status", {}).get("copies", {})
    print(json.dumps({"stage": "cache", "nodes": statuses, "copies": copies}), flush=True)
    if not expected or missing or copies.get("failed", 0) or copies.get("available", 0) < len(expected):
        raise RuntimeError(f"Cache not ready on selected nodes: {missing}")


def preflight(config):
    required = ["inferenceservices.serving.kserve.io", "localmodelcaches.serving.kserve.io",
                "localmodelnodegroups.serving.kserve.io", "clusterstoragecontainers.serving.kserve.io"]
    if config["serving"]["nodesPerReplica"] > 1:
        required += ["leaderworkersets.leaderworkerset.x-k8s.io", "llminferenceservices.serving.kserve.io",
                     "llminferenceserviceconfigs.serving.kserve.io"]
    for name in required:
        crd = api("/apis/apiextensions.k8s.io/v1/customresourcedefinitions/" + name)
        if not any(c["type"] == "Established" and c["status"] == "True" for c in crd.get("status", {}).get("conditions", [])):
            raise RuntimeError(f"CRD not established: {name}")
    csvs = api("/apis/operators.coreos.com/v1alpha1/namespaces/redhat-ods-operator/clusterserviceversions")["items"]
    if not any(c["metadata"]["name"] == "rhods-operator." + config["rhoai"]["version"]
               and c.get("status", {}).get("phase") == "Succeeded" for c in csvs):
        raise RuntimeError("Configured RHOAI compatibility baseline is not installed")
    dsc = api("/apis/datasciencecluster.opendatahub.io/v1/datascienceclusters/default-dsc")
    if dsc["spec"]["components"]["kserve"].get("modelCache", {}).get("managementState") != "Managed":
        raise RuntimeError("Enable modelCache in the DataScienceCluster")
    nodes = check_nodes(config, selected_nodes(config))
    if (config["serving"]["nodesPerReplica"] > 1 and config["serving"]["networkAttachments"]
            and not config["serving"].get("secondaryNetworkIsolated")):
        raise RuntimeError("Distributed secondary networks require explicit isolation; set secondaryNetworkIsolated after platform verification")
    for reference in config["serving"]["networkAttachments"]:
        namespace, name = reference.split("/") if "/" in reference else (config["inference"]["namespace"], reference)
        api(f"/apis/k8s.cni.cncf.io/v1/namespaces/{namespace}/network-attachment-definitions/{name}")
    total_size = quantity(config["model"]["size"]) + sum(quantity(r["size"]) for r in config["modelCache"]["retainedRevisions"])
    if total_size > quantity(config["modelCache"]["capacity"]):
        raise RuntimeError("Current and retained model sizes exceed declared cache capacity")
    # Capacity is only an initial check. The downloader checks statvfs and a real
    # write on its mounted volume before transferring any files.
    pods = api("/api/v1/pods")["items"]
    used, old_workload = {}, {}
    for pod in pods:
        if pod.get("status", {}).get("phase") in ("Succeeded", "Failed"):
            continue
        node = pod.get("spec", {}).get("nodeName")
        count = sum(int(c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                    for c in pod.get("spec", {}).get("containers", []))
        used[node] = used.get(node, 0) + count
        labels = pod["metadata"].get("labels", {})
        if (pod["metadata"]["namespace"] == config["inference"]["namespace"]
                and (labels.get("aiq.rhai.redhat.com/serving") == "vllm-inference-service"
                     or labels.get("serving.kserve.io/inferenceservice") == "vllm-inference-service")):
            old_workload[node] = old_workload.get(node, 0) + count
    print(json.dumps({"stage": "gpu_availability", "nodes": [
        {"name": n["metadata"]["name"], "allocatable": n["status"]["allocatable"].get("nvidia.com/gpu"),
         "requested": used.get(n["metadata"]["name"], 0)} for n in nodes]}), flush=True)
    usable = [n for n in nodes if int(n["status"]["allocatable"].get("nvidia.com/gpu", 0))
              - used.get(n["metadata"]["name"], 0) + old_workload.get(n["metadata"]["name"], 0)
              >= config["serving"]["gpusPerNode"]]
    if len({n["metadata"]["labels"][config["serving"]["topologyKey"]] for n in usable}) < config["serving"]["nodesPerReplica"] * config["serving"]["replicas"]:
        raise RuntimeError("Other workloads occupy the GPUs required by this layout")


def run(stage, config):
    if stage == "preflight":
        preflight(config)
    elif stage == "cache":
        nodes = check_nodes(config, selected_nodes(config))
        cache = api("/apis/serving.kserve.io/v1alpha1/localmodelcaches/" + os.environ["CACHE_NAME"])
        check_cache(nodes, cache)
    elif stage == "storage":
        from artifact_store import client
        # List is scoped to the publication prefix and verifies credentials/TLS.
        client().list_objects_v2(Bucket=config["modelStore"]["bucket"],
                                 Prefix=config["modelStore"]["prefix"] + "/", MaxKeys=1)
    elif stage == "serving":
        with urlopen(os.environ["SERVING_URL"] + "/health", timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("Serving health check failed")
    elif stage == "switch":
        # This runs only after publication and selected-node cache readiness.
        distributed = config["serving"]["nodesPerReplica"] > 1
        version, resource = ("v1beta1", "inferenceservices") if distributed else ("v1alpha2", "llminferenceservices")
        path = f'/apis/serving.kserve.io/{version}/namespaces/{config["inference"]["namespace"]}/{resource}/{os.environ["SERVICE_NAME"]}'
        try:
            old = api(path)
        except HTTPError as error:
            if error.code == 404:
                return
            raise
        if os.environ.get("REPLACE_BACKEND") != "true":
            raise RuntimeError("Previous serving backend still exists; enable migration.replaceBackend or retire it explicitly")
        if not old["metadata"].get("deletionTimestamp"):
            api(path, method="DELETE", body={"apiVersion": "v1", "kind": "DeleteOptions",
                "propagationPolicy": "Foreground", "preconditions": {"uid": old["metadata"]["uid"]}})
        raise RuntimeError("Waiting for the previous serving backend to release its GPUs")
    else:
        raise ValueError("Unknown gate stage")


if __name__ == "__main__":
    config = json.loads(Path("/scripts/config.json").read_text())
    deadline = time.monotonic() + int(os.environ.get("TIMEOUT_SECONDS", "3600"))
    while True:
        try:
            run(sys.argv[1], config)
            print(json.dumps({"stage": sys.argv[1], "ready": True}), flush=True)
            break
        except Exception as error:
            print(json.dumps({"stage": sys.argv[1], "ready": False, "reason": str(error)}), flush=True)
            if time.monotonic() >= deadline:
                raise
            time.sleep(15)
