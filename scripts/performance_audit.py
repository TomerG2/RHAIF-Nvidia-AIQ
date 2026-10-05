#!/usr/bin/env python3
"""Shared, local run history for direct vLLM and AI-Q research checks."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "artifacts" / "performance"
APP_PATHS = {"serving": "charts/all/vllm-inference-service",
             "workflow": "charts/aiq-workflow-config", "application": "charts/aiq2-web"}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(path)


def command(argv, timeout=25):
    return subprocess.check_output(argv, text=True, stderr=subprocess.PIPE, timeout=timeout).strip()


def oc_json(*args):
    return json.loads(command(["oc", *args, "-o", "json", "--request-timeout=15s"]))


def remote(pod, namespace, container, argv, timeout=120):
    return command(["oc", "exec", "-n", namespace, pod, "-c", container, "--", *argv], timeout)


def attempt(errors, label, fn):
    try:
        return fn()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", None) or str(error)
        errors.append(f"{label}: {detail.strip()[:1500]}")
        return None


def ready(pod):
    return not pod["metadata"].get("deletionTimestamp") and any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in pod.get("status", {}).get("conditions", []))


def serving_container(pod):
    candidates = [c for c in pod["spec"]["containers"]
                  if c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu")
                  or c.get("resources", {}).get("requests", {}).get("nvidia.com/gpu")]
    if len(candidates) != 1:
        raise ValueError(f"Expected one GPU container in {pod['metadata']['name']}")
    return candidates[0]


def pod_record(pod):
    statuses = {c["name"]: c for c in pod.get("status", {}).get("containerStatuses", [])}
    return {
        "name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
        "node": pod["spec"].get("nodeName"), "ready": ready(pod),
        "labels": {k: v for k, v in pod["metadata"].get("labels", {}).items()
                   if k.startswith(("leaderworkerset.", "aiq.rhai.", "app.kubernetes.io/"))},
        "containers": [{"name": c["name"], "image": c["image"],
                        "image_id": statuses.get(c["name"], {}).get("imageID"),
                        "restart_count": statuses.get(c["name"], {}).get("restartCount"),
                        "args": c.get("args", []), "resources": c.get("resources", {}),
                        "serving_env": {e["name"]: e.get("value") for e in c.get("env", [])
                                        if e["name"].startswith(("AIQ_", "NCCL_", "VLLM_"))
                                        and not any(s in e["name"] for s in ("KEY", "TOKEN", "SECRET"))}}
                       for c in pod["spec"]["containers"]],
    }


def revisions(apps, namespace, kind, errors, app_namespace="aiq"):
    result = {}
    roles = ("serving",) if kind == "vllm" else tuple(APP_PATHS)
    for role in roles:
        destination = namespace if role == "serving" else app_namespace
        matches = [a for a in apps if any(s.get("path", "").rstrip("/") == APP_PATHS[role]
                   for s in a["spec"].get("sources", [a["spec"].get("source", {})]))
                   and a["spec"].get("destination", {}).get("namespace") == destination]
        if len(matches) != 1:
            errors.append(f"{role} revision: expected one matching Argo application; found {len(matches)}")
            result[role] = None
            continue
        app = matches[0]
        sync = app.get("status", {}).get("sync", {})
        sources = app["spec"].get("sources", [app["spec"].get("source", {})])
        resolved = sync.get("revisions", [sync.get("revision")])
        # sync.revision is a comparison target during drift/rollout, not proof of deployment.
        operation_phase = app.get("status", {}).get("operationState", {}).get("phase")
        verified = sync.get("status") == "Synced" and operation_phase not in {"Running", "Failed", "Error", "Terminating"}
        source_records = [{"repo": s.get("repoURL"), "path": s.get("path"), "ref": s.get("ref"),
                           "revision": resolved[i] if verified and i < len(resolved) else None,
                           "compared_revision": resolved[i] if i < len(resolved) else None}
                          for i, s in enumerate(sources)]
        result[role] = {"name": app["metadata"]["name"], "namespace": app["metadata"]["namespace"],
                        "sync_status": sync.get("status"),
                        "operation_phase": operation_phase,
                        "health": app.get("status", {}).get("health", {}).get("status"),
                        "sources": source_records}
    return result


# Runs outside the measurement window; importing torch does not allocate CUDA tensors.
RUNTIME_PROBE = '''import importlib.metadata,json,pathlib,re,subprocess
result={"vllm_version":importlib.metadata.version("vllm")}
try:
    import torch
    result["cuda_runtime_version"]=torch.version.cuda
except Exception as e: result["cuda_runtime_version"]=None; result["cuda_runtime_version_error"]=str(e)
for key,args in [("gpus",["nvidia-smi","--query-gpu=name,memory.total,driver_version,uuid","--format=csv,noheader,nounits"]),("cuda",["nvidia-smi"])]:
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=10)
        result[key]=p.stdout if p.returncode==0 else None
        if p.returncode: result[key+"_error"]=p.stderr
        if key=="cuda" and p.returncode==0:
            match=re.search(r"CUDA Version:\\s*([\\d.]+)",p.stdout)
            result[key]="CUDA Version: "+match.group(1) if match else None
            if not match: result[key+"_error"]="NVIDIA-SMI returned no CUDA driver capability"
    except Exception as e: result[key]=None; result[key+"_error"]=str(e)
try: result["model_marker"]=json.loads(pathlib.Path("/mnt/models/.aiq-ready.json").read_text())
except Exception as e: result["model_marker"]=None; result["model_marker_error"]=str(e)
print(json.dumps(result))'''


def capture(kind, namespace="aiq-inference", name="vllm-inference-service", app_namespace="aiq"):
    errors = []
    local = {"sha": attempt(errors, "local commit", lambda: command(["git", "-C", str(ROOT), "rev-parse", "HEAD"])),
             "status": attempt(errors, "local status", lambda: command(["git", "-C", str(ROOT), "status", "--porcelain"]))}
    local["dirty"] = bool(local["status"]) if local["status"] is not None else None
    pods = attempt(errors, "serving pods", lambda: oc_json("get", "pods", "-n", namespace,
                   "-l", "aiq.rhai.redhat.com/serving=" + name))
    apps = attempt(errors, "Argo applications", lambda: oc_json("get", "applications.argoproj.io", "-A"))
    deployed = revisions((apps or {}).get("items", []), namespace, kind, errors, app_namespace)
    records, nodes, runtimes = [], {}, {}
    for pod in sorted((pods or {}).get("items", []), key=lambda p: p["metadata"]["name"]):
        records.append(pod_record(pod))
        node_name = pod["spec"].get("nodeName")
        if node_name and node_name not in nodes:
            node = attempt(errors, "node " + node_name, lambda: oc_json("get", "node", node_name))
            if node:
                labels = node["metadata"].get("labels", {})
                nodes[node_name] = {"instance_type": labels.get("node.kubernetes.io/instance-type"),
                                    "gpu_product": labels.get("nvidia.com/gpu.product"),
                                    "capacity": node["status"].get("capacity", {}),
                                    "allocatable": node["status"].get("allocatable", {}),
                                    "node_info": node["status"].get("nodeInfo", {})}
        container = attempt(errors, "GPU container", lambda: serving_container(pod))
        if container and ready(pod):
            runtime = attempt(errors, "runtime " + pod["metadata"]["name"], lambda: json.loads(remote(
                pod["metadata"]["name"], namespace, container["name"], ["python", "-c", RUNTIME_PROBE])))
            runtimes[pod["metadata"]["name"]] = runtime
            if runtime:
                errors.extend(f"runtime {pod['metadata']['name']} {k}: {v}" for k, v in runtime.items() if k.endswith("_error"))
    application = None
    if kind == "research":
        app_pods = attempt(errors, "application pods", lambda: oc_json("get", "pods", "-n", app_namespace))
        config = attempt(errors, "workflow config", lambda: oc_json("get", "configmap", "aiq-workflow-config", "-n", app_namespace))
        route = attempt(errors, "frontend route", lambda: oc_json("get", "route", "aiq-frontend", "-n", app_namespace))
        application = {"pods": [pod_record(p) for p in sorted((app_pods or {}).get("items", []), key=lambda p: p["metadata"]["name"])],
                       "frontend_host": (route or {}).get("spec", {}).get("host"),
                       "workflow_hash": digest(config.get("data", {})) if config else None}
    return {"local": local, "deployed": deployed, "namespace": namespace, "service": name,
            "pods": records, "nodes": nodes, "runtimes": runtimes,
            "application": application, "errors": errors}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def hardware_summary(snapshot):
    allocated = 0
    devices = []
    for pod in snapshot["pods"]:
        for container in pod["containers"]:
            resources = container["resources"]
            allocated += int(resources.get("requests", {}).get("nvidia.com/gpu",
                             resources.get("limits", {}).get("nvidia.com/gpu", 0)))
        runtime = snapshot["runtimes"].get(pod["name"]) or {}
        for row in (runtime.get("gpus") or "").strip().splitlines():
            parts = [p.strip() for p in row.split(",")]
            if len(parts) == 4:
                devices.append({"pod": pod["name"], "model": parts[0], "memory_mib": parts[1],
                                "driver_version": parts[2], "uuid": parts[3]})
    return {"allocated_gpus": allocated if snapshot["pods"] else None,
            "serving_nodes": len({p["node"] for p in snapshot["pods"] if p["node"]}),
            "visible_devices": devices}


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * percent / 100
    low, high = math.floor(index), math.ceil(index)
    return round(values[low] + (values[high] - values[low]) * (index - low), 6)


def identity(snapshot):
    # Include pod UID/restarts and config, but exclude discovery errors and local edits.
    return {k: snapshot.get(k) for k in ("deployed", "pods", "nodes", "runtimes", "application")}


def comparison_key(run):
    snapshot = run["before"]
    problems = []
    for role, app in snapshot["deployed"].items():
        if not app or app.get("sync_status") != "Synced" or app.get("health") != "Healthy":
            problems.append(f"{role}: deployed revision is unavailable or application is not synced/healthy")
        elif not app["sources"] or any(not re.fullmatch(r"[0-9a-f]{40}", s.get("revision") or "")
                                       for s in app["sources"] if s.get("path") or s.get("ref")):
            problems.append(f"{role}: immutable source revision unavailable")
    pods = snapshot["pods"]
    if not pods or not all(p["ready"] for p in pods):
        problems.append("serving pods missing or not ready")
    hardware, config = [], []
    for pod in pods:
        node = snapshot["nodes"].get(pod["node"])
        runtime = snapshot["runtimes"].get(pod["name"])
        if (not node or not runtime or not runtime.get("gpus") or not runtime.get("model_marker")
                or not runtime.get("vllm_version") or not runtime.get("cuda_runtime_version")):
            problems.append("hardware/runtime/model evidence incomplete")
            continue
        gpu_rows = [r.split(",") for r in runtime["gpus"].strip().splitlines()]
        if any(len(row) != 4 or not all(part.strip() for part in row) for row in gpu_rows):
            problems.append("GPU identity/memory/driver evidence malformed")
        if not re.fullmatch(r"[0-9a-f]{40}", runtime["model_marker"].get("revision", "")):
            problems.append("immutable model revision unavailable")
        cuda_match = re.search(r"CUDA Version:\s*([\d.]+)", runtime.get("cuda") or "")
        if not cuda_match:
            problems.append("CUDA driver capability unavailable")
        hardware.append({"node": {k: node[k] for k in ("instance_type", "gpu_product", "capacity", "allocatable")},
                         # Exclude physical GPU UUID: equivalent replacement hardware can compare.
                         "gpus": sorted([[s.strip() for s in r[:3]] for r in gpu_rows]),
                         "cuda_driver_capability": cuda_match.group(1) if cuda_match else None,
                         "cuda_runtime_version": runtime["cuda_runtime_version"]})
        containers = [dict(c) for c in pod["containers"] if c["name"] in ("main", "kserve-container")]
        if not containers or any(not re.search(r"@sha256:[0-9a-f]{64}$", c["image_id"] or "") for c in containers):
            problems.append("actual serving image digest unavailable")
        config.append({"containers": containers, "model": runtime["model_marker"], "version": runtime["vllm_version"]})
    # A pod name/restart count must not make otherwise equivalent historical runs differ.
    for item in config:
        for c in item["containers"]:
            c.pop("restart_count", None)
    app_config = None
    if run["kind"] == "research":
        app = snapshot.get("application") or {}
        if not app.get("workflow_hash") or not app.get("pods") or not app.get("frontend_host"):
            problems.append("application/workflow evidence incomplete")
        if run["workload"].get("frontend_host") != app.get("frontend_host"):
            problems.append("frontend URL is not the discovered deployment route")
        app_config = {"workflow_hash": app.get("workflow_hash"), "pods": []}
        for p in app.get("pods", []):
            if not p["ready"] or any(not c["image_id"] for c in p["containers"]):
                problems.append("application pods missing readiness/image evidence")
            app_config["pods"].append([{k: v for k, v in c.items() if k != "restart_count"} for c in p["containers"]])
    if not run.get("stable"):
        problems.append("deployment changed or could not be verified after the run")
    if problems:
        return None, sorted(set(problems))
    if app_config:
        app_config["pods"] = sorted(app_config["pods"], key=lambda x: json.dumps(x, sort_keys=True))
    normalized = lambda items: sorted(items, key=lambda x: json.dumps(x, sort_keys=True))
    return digest({"kind": run["kind"], "workload": run["workload"], "client": run["client"],
                   "hardware": normalized(hardware), "serving": normalized(config), "application": app_config}), []


def history(root=HISTORY):
    records = []
    for path in Path(root).glob("*/run.json"):
        try:
            record = json.loads(path.read_text())
            record["artifact_dir"] = str(path.parent)
            records.append(record)
        except (OSError, ValueError):
            continue
    return sorted(records, key=lambda r: r["started_at"])


def compare(run, root):
    key, reasons = comparison_key(run)
    run["comparison_key"] = key
    if not key:
        return {"baseline": None, "reasons": reasons, "changes_percent": {}}
    candidates = [r for r in history(root) if r["id"] != run["id"] and r.get("comparison_key") == key
                  and r.get("status") == "passed" and r["started_at"] < run["started_at"]]
    if not candidates:
        return {"baseline": None, "reasons": ["No previous successful run with matching workload, hardware, and configuration"], "changes_percent": {}}
    baseline = candidates[-1]
    changes = {k: round((v - baseline["metrics"][k]) / baseline["metrics"][k] * 100, 3)
               for k, v in run["metrics"].items()
               if isinstance(v, (int, float)) and not isinstance(v, bool)
               and isinstance(baseline["metrics"].get(k), (int, float)) and baseline["metrics"][k] != 0}
    return {"baseline": baseline["id"], "baseline_artifacts": baseline["artifact_dir"],
            "baseline_deployed": baseline["before"]["deployed"], "reasons": [], "changes_percent": changes}


def summary(run):
    lines = [f"# {run['kind']} run {run['id']}", "", f"Status: {run['status']}; deployment stable: {run.get('stable')}",
             f"Local checkout: {run['before']['local']['sha']} (dirty: {run['before']['local']['dirty']})", "", "## Deployed commits", ""]
    for role, app in run["before"]["deployed"].items():
        sources = app["sources"] if app else []
        lines.append(f"- {role}: " + (", ".join(f"{s['repo']} @ {s['revision']}" for s in sources) or "unknown"))
        local_sha = run["before"]["local"]["sha"]
        matches = [s["revision"] == local_sha for s in sources if s.get("path") or s.get("ref")]
        relation = "unknown" if not local_sha or not matches or any(not s.get("revision") for s in sources) else "matches local checkout" if all(matches) else "differs from local checkout"
        lines.append(f"  - Deployment revision {relation}.")
    lines += ["", "## Hardware and runtime", ""]
    hardware = hardware_summary(run["before"])
    lines.append(f"Allocated serving GPUs: {hardware['allocated_gpus']}; serving nodes: {hardware['serving_nodes']}")
    lines.append("")
    for pod in run["before"]["pods"]:
        runtime = run["before"]["runtimes"].get(pod["name"]) or {}
        node = run["before"]["nodes"].get(pod["node"], {})
        lines.append(f"- {pod['name']} on {pod['node']}: {node.get('instance_type') or 'instance type unknown'}; "
                     f"node CPU/RAM {node.get('capacity', {}).get('cpu', 'unknown')}/{node.get('capacity', {}).get('memory', 'unknown')}; "
                     f"vLLM {runtime.get('vllm_version', 'unknown')}; model {runtime.get('model_marker')}")
        for c in pod["containers"]:
            lines.append(f"  - {c['name']}: {c['image_id'] or c['image']}; allocated resources {json.dumps(c['resources'])}")
        lines.append("  - Visible GPUs (model, memory MiB, driver, UUID): " + (runtime.get("gpus") or "unknown").strip().replace("\n", "; "))
        lines.append(f"  - CUDA runtime (PyTorch): {runtime.get('cuda_runtime_version') or 'unknown'}")
    if not run["before"]["pods"]:
        lines.append("- Hardware unavailable; see discovery errors in run.json.")
    lines += ["", "## Results", ""]
    lines += [f"- {k}: {v}" for k, v in run["metrics"].items()]
    lines += ["", "## Validation", "", json.dumps(run["validation"], ensure_ascii=False), "", "## Comparison", ""]
    if run["kind"] == "research":
        lines.insert(lines.index("## Validation"), "Timings include frontend submission, polling, and report retrieval. Deep research uses the external NVIDIA API.")
    comparison = run["comparison"]
    if comparison["baseline"]:
        lines.append(f"Baseline: {comparison['baseline']} ({comparison['baseline_artifacts']})")
        lines.extend(f"- {k}: {v:+.3f}%" for k, v in comparison["changes_percent"].items())
        lines.append("Changes are observations; no regression threshold is enforced.")
    else:
        lines.extend(f"- {reason}" for reason in comparison["reasons"])
    if run["before"]["errors"]:
        lines += ["", "## Discovery errors", ""] + [f"- {e}" for e in run["before"]["errors"]]
    if run.get("after", {}).get("errors"):
        lines += ["", "## Post-run discovery errors", ""] + [f"- {e}" for e in run["after"]["errors"]]
    if run.get("error"):
        lines += ["", f"Run error: {run['error']}"]
    return "\n".join(lines) + "\n"


class Audit:
    def __init__(self, kind, workload, client, *, root=HISTORY, namespace="aiq-inference",
                 name="vllm-inference-service", app_namespace="aiq"):
        self.root = Path(root).resolve()
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.path = self.root / self.id
        self.path.mkdir(parents=True, exist_ok=False)
        self.capture_args = (kind, namespace, name, app_namespace)
        self.record = {"schema_version": 1, "id": self.id, "kind": kind,
                       "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                       "workload": workload, "client": client, "metrics": {}, "validation": {},
                       "before": capture(*self.capture_args)}
        self.record["hardware"] = hardware_summary(self.record["before"])
        write_json(self.path / "run.json", self.record)

    def finish(self, status, metrics, validation, error=None):
        self.record.update(status=status, metrics=metrics, validation=validation, error=error,
                           finished_at=datetime.now(timezone.utc).isoformat())
        # Persist terminal evidence before post-run discovery, which may be interrupted.
        write_json(self.path / "run.json", self.record)
        self.record["after"] = capture(*self.capture_args)
        before, after = self.record["before"], self.record["after"]
        known = bool(before["pods"] and after["pods"] and all(before["deployed"].values()) and all(after["deployed"].values()))
        self.record["stable"] = identity(before) == identity(after) if known else None
        self.save()

    def save(self):
        self.record["comparison"] = compare(self.record, self.root)
        write_json(self.path / "run.json", self.record)
        (self.path / "summary.md").write_text(summary(self.record))
        print(f"Audit: {self.path / 'summary.md'}", file=sys.stderr)


def review(path):
    path = Path(path).resolve()
    run = json.loads((path / "run.json").read_text())
    results = json.loads((path / "results.json").read_text())["results"]
    verdicts = json.loads((path / "verdicts.json").read_text())
    expected = {r["request_id"]: r for r in results if r.get("answer")}
    if (not isinstance(verdicts, list) or not all(isinstance(v, dict) for v in verdicts)
            or len(verdicts) != len(expected) or {v.get("request_id") for v in verdicts} != set(expected)):
        raise ValueError("Provide exactly one verdict for every returned answer")
    counts = {}
    for v in verdicts:
        if v.get("verdict") not in {"make sense", "not"} or not isinstance(v.get("reason"), str) or not v["reason"].strip():
            raise ValueError("Each verdict needs 'make sense' or 'not' and a nonempty reason")
        agent = expected[v["request_id"]]["agent"]
        counts.setdefault(agent, Counter())[v["verdict"]] += 1
    run["validation"].update(review_status="completed", verdicts=verdicts, counts=counts,
                             execution_failures=sum(r["status"] != "completed" for r in results))
    if run["status"] != "interrupted":
        run["status"] = "passed" if all(v["verdict"] == "make sense" for v in verdicts) and all(r["status"] == "completed" for r in results) else "failed"
    run["comparison"] = compare(run, path.parent)
    write_json(path / "run.json", run)
    (path / "summary.md").write_text(summary(run))
    print(path / "summary.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    listing = sub.add_parser("history", help="List chronological run records as JSON")
    listing.add_argument("--history-dir", type=Path, default=HISTORY)
    reviewing = sub.add_parser("review", help="Validate verdicts.json and finalize a research audit")
    reviewing.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    if args.action == "history":
        print(json.dumps(history(args.history_dir), indent=2))
    else:
        review(args.run_dir)


if __name__ == "__main__":
    main()
