#!/usr/bin/env python3
"""Opt-in checks on an installed GPU workload; never provisions hardware.

--restart-worker and --restart-pod explicitly authorize deleting one serving pod.
The default performs read-only inspection plus a small inference request.
"""
import argparse
import json
import subprocess
import time
from urllib.request import Request, urlopen
import uuid


def oc(*args):
    return subprocess.check_output(["oc", *args], text=True)


def get(*args):
    return json.loads(oc("get", *args, "-o", "json"))


def ready(pod):
    return any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="aiq-inference")
    parser.add_argument("--name", default="vllm-inference-service")
    parser.add_argument("--distributed", action="store_true")
    parser.add_argument("--restart-worker", action="store_true")
    parser.add_argument("--restart-pod", action="store_true")
    parser.add_argument("--aiq-url", help="Frontend origin for one end-to-end shallow research request")
    args = parser.parse_args()
    selector = "aiq.rhai.redhat.com/serving=" + args.name
    pods = lambda: get("pods", "-n", args.namespace, "-l", selector)["items"]
    current = pods()
    assert current and all(ready(p) for p in current), "Serving pods are not all ready"
    namespace_nodes = {p["spec"]["nodeName"] for p in current}
    caches = get("localmodelcache")["items"]
    service = get("llminferenceservice" if args.distributed else "inferenceservice", args.name, "-n", args.namespace)
    uri = service["spec"]["model"]["uri"] if args.distributed else service["spec"]["predictor"]["model"]["storageUri"]
    cache = next(c for c in caches if c["spec"]["sourceModelUri"] == uri)
    if args.distributed:
        assert len(namespace_nodes) >= 2, "Distributed validation requires at least two actual GPU nodes"
        lws = get("leaderworkerset", args.name + "-kserve-mn", "-n", args.namespace)
        template = lws["spec"]["leaderWorkerTemplate"]
        assert template["size"] == service["spec"]["parallelism"]["pipeline"]
        assert template["restartPolicy"] == "RecreateGroupOnPodRestart"
    def verify_mounts(current):
        cache_status = get("localmodelcache", cache["metadata"]["name"])["status"]
        for pod in current:
            assert cache_status["nodeStatus"][pod["spec"]["nodeName"]] == "NodeDownloaded"
            pvcs = [v["persistentVolumeClaim"]["claimName"] for v in pod["spec"]["volumes"] if "persistentVolumeClaim" in v]
            assert pvcs and args.name + "-model-cache" not in pvcs, "Pod is not using the platform cache"
            assert not any(c["name"] == "storage-initializer" for c in pod["spec"].get("initContainers", [])), "Serving pod has a remote download initializer"
            container = "main" if args.distributed else "kserve-container"
            marker = json.loads(oc("exec", pod["metadata"]["name"], "-n", args.namespace, "-c", container,
                                  "--", "cat", "/mnt/models/.aiq-ready.json"))
            assert marker["uri"] == uri, "Leader/worker mounted the wrong model revision"
            if args.distributed:
                rank = oc("exec", pod["metadata"]["name"], "-n", args.namespace, "-c", container,
                          "--", "python", "-c", 'import os; print(os.environ.get("LWS_WORKER_INDEX", "0"))').strip()
                assert rank == pod["metadata"]["labels"]["leaderworkerset.sigs.k8s.io/worker-index"]
    verify_mounts(current)
    leaders = [p for p in current if not args.distributed or p["metadata"]["labels"].get("leaderworkerset.sigs.k8s.io/worker-index") == "0"]
    probe = '''import json,urllib.request
origin="http://localhost:8080"
model=json.load(urllib.request.urlopen(origin+"/v1/models"))["data"][0]["id"]
request=urllib.request.Request(origin+"/v1/chat/completions",headers={"Content-Type":"application/json"},data=json.dumps({"model":model,"messages":[{"role":"user","content":"Reply with the word ready."}],"max_tokens":32}).encode())
result=json.load(urllib.request.urlopen(request,timeout=120))
assert result.get("choices"),result
print(json.dumps({"model":model,"usage":result.get("usage")}))'''
    for leader in leaders:
        print(oc("exec", leader["metadata"]["name"], "-n", args.namespace, "-c", "main" if args.distributed else "kserve-container", "--", "python", "-c", probe).strip())
    if args.restart_worker or args.restart_pod:
        if args.restart_worker:
            assert args.distributed, "--restart-worker requires --distributed"
            target = next(p for p in current if p["metadata"]["labels"].get("leaderworkerset.sigs.k8s.io/worker-index") not in (None, "0"))
            group = target["metadata"]["labels"]["leaderworkerset.sigs.k8s.io/group-index"]
            previous = {p["metadata"]["uid"] for p in current if p["metadata"]["labels"].get("leaderworkerset.sigs.k8s.io/group-index") == group}
        else:
            target, previous = current[0], {current[0]["metadata"]["uid"]}
        oc("delete", "pod", target["metadata"]["name"], "-n", args.namespace, "--wait=false")
        deadline = time.monotonic() + 1800
        while True:
            replacements = pods()
            if (len(replacements) == len(current) and all(ready(p) for p in replacements)
                    and not previous.intersection(p["metadata"]["uid"] for p in replacements)):
                break
            if time.monotonic() > deadline:
                raise TimeoutError("Worker group did not recover")
            time.sleep(10)
        verify_mounts(replacements)
        print("Serving pod/group recovered with the expected local cache mounts.")
        recovered_leaders = [p for p in pods() if not args.distributed or p["metadata"]["labels"].get("leaderworkerset.sigs.k8s.io/worker-index") == "0"]
        for leader in recovered_leaders:
            print(oc("exec", leader["metadata"]["name"], "-n", args.namespace, "-c", "main" if args.distributed else "kserve-container", "--", "python", "-c", probe).strip())
    events = get("events", "-n", args.namespace)["items"]
    uids = {p["metadata"]["uid"] for p in pods()}
    assert not any(e.get("involvedObject", {}).get("uid") in uids and "Multi-Attach" in e.get("message", "") for e in events)
    if args.aiq_url:
        base = args.aiq_url.rstrip("/") + "/api/v1/jobs/async"
        def request(path, payload=None):
            req = Request(base + path, data=json.dumps(payload).encode() if payload else None,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=120) as response:
                return json.load(response)
        job = request("/submit", {"agent_type": "shallow_researcher", "input": "Briefly compare Kubernetes Deployments and StatefulSets."})
        job_id = str(uuid.UUID(job["job_id"]))
        deadline = time.monotonic() + 1800
        while True:
            status = request("/job/" + job_id)["status"].lower()
            if status in ("completed", "success"):
                assert request("/job/" + job_id + "/report"), "Empty AI-Q report"
                break
            if status in ("failed", "failure", "cancelled", "interrupted") or time.monotonic() > deadline:
                raise RuntimeError(f"AI-Q request did not complete: {status}")
            time.sleep(15)
        print(json.dumps({"aiq_job": job_id, "status": status}))
    print(json.dumps({"result": "passed", "distributed": args.distributed, "nodes": len(namespace_nodes), "uri": uri}))


if __name__ == "__main__":
    main()
