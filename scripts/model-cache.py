#!/usr/bin/env python3
"""Inspect model preparation, or explicitly delete an unused LocalModelCache."""
import argparse
import json
import subprocess


def get(*args):
    return json.loads(subprocess.check_output(["oc", "get", *args, "-o", "json"], text=True, stderr=subprocess.PIPE))


def active_uris(resources):
    uris = []
    for obj in resources:
        spec = obj.get("spec", {})
        uri = spec.get("model", {}).get("uri") or spec.get("predictor", {}).get("model", {}).get("storageUri")
        if uri:
            uris.append(uri)
    return uris


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "cleanup"])
    parser.add_argument("--namespace", default="aiq-inference")
    parser.add_argument("--cache")
    parser.add_argument("--execute", action="store_true", help="Delete the named unused cache CR (and its node copies)")
    args = parser.parse_args()
    if args.command == "cleanup":
        if not args.cache:
            parser.error("cleanup requires --cache")
        # Fail closed if either serving API cannot be inspected.
        caches = get("localmodelcache")["items"]
        services = get("inferenceservice,llminferenceservice", "-A")["items"]
        cache = next(c for c in caches if c["metadata"]["name"] == args.cache)
        uri = cache["spec"]["sourceModelUri"].rstrip("/")
        if any(active == uri or active.startswith(uri + "/") for active in active_uris(services)):
            raise SystemExit("Refusing to delete a cache referenced by a serving workload")
        print(json.dumps({"cache": args.cache, "uri": uri, "delete": args.execute}))
        if args.execute:
            subprocess.run(["oc", "delete", "localmodelcache", args.cache], check=True)
        return
    errors = {}
    def status(section, *query):
        try:
            return get(*query)["items"]
        except subprocess.CalledProcessError as error:
            errors[section] = error.stderr.strip()
            return []
    caches = status("cache", "localmodelcache")
    services = status("serving", "inferenceservice,llminferenceservice", "-A")
    jobs = status("publication", "jobs", "-n", args.namespace)
    pods = status("workers", "pods", "-n", args.namespace, "-l", "aiq.rhai.redhat.com/serving")
    nodes = status("gpu_capacity", "nodes", "-l", "nvidia.com/gpu.present=true")
    report = {
        "unavailable": errors,
        "publication": [{"name": j["metadata"]["name"], "status": j.get("status", {})} for j in jobs
                        if j["metadata"]["name"].startswith("publish-")],
        "cache": [{"name": c["metadata"]["name"], "uri": c["spec"]["sourceModelUri"], "status": c.get("status", {})} for c in caches],
        "gpu_capacity": [{"node": n["metadata"]["name"], "gpus": n["status"]["allocatable"].get("nvidia.com/gpu", "0")} for n in nodes],
        "workers": [{"name": p["metadata"]["name"], "node": p["spec"].get("nodeName"), "status": p.get("status", {})} for p in pods],
        "serving": [{"kind": s["kind"], "name": s["metadata"]["name"], "namespace": s["metadata"]["namespace"], "status": s.get("status", {})} for s in services],
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
