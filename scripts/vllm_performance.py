#!/usr/bin/env python3
"""Benchmark an existing serving leader; never install, restart, or deploy it."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

import performance_audit as audit


DISCOVER_MODEL = '''import json,urllib.request
data=json.load(urllib.request.urlopen("http://localhost:8080/v1/models",timeout=30))
assert len(data["data"])==1,"Select a deployment serving exactly one model"
print(json.dumps(data["data"][0]))'''

PROBES = '''import json,urllib.request
model=MODEL
results=[]
for prompt in ["Reply with the word ready.","What is 2 + 2? Reply briefly."]:
    item={"prompt":prompt,"passed":False}
    try:
        payload={"model":model,"messages":[{"role":"user","content":prompt}],"max_tokens":128,"temperature":0}
        req=urllib.request.Request("http://localhost:8080/v1/chat/completions",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"})
        result=json.load(urllib.request.urlopen(req,timeout=120))
        item["response"]=result
        message=result["choices"][0]["message"]
        item["passed"]=any(isinstance(message.get(k),str) and message[k].strip() for k in ("content","reasoning_content","reasoning"))
        if not item["passed"]: item["error"]="No nonempty content or reasoning text"
    except Exception as e: item["error"]=str(e)
    results.append(item)
print(json.dumps(results))'''

# Kill the remote benchmark process group on timeout; return evidence on failure too.
EXECUTE = '''import json,os,pathlib,signal,subprocess,sys,tempfile
argv=json.loads(sys.argv[1]); timeout=int(sys.argv[2]); filename="result.json"
with tempfile.TemporaryDirectory(prefix="aiq-perf-") as directory:
    argv += ["--result-dir",directory,"--result-filename",filename]
    p=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    timed_out=False
    try: out,err=p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out=True; os.killpg(p.pid,signal.SIGKILL); out,err=p.communicate()
    result=None; result_error=None
    try: result=json.loads((pathlib.Path(directory)/filename).read_text())
    except Exception as e: result_error=str(e)
    print(json.dumps({"command":argv,"returncode":p.returncode,"timed_out":timed_out,"stdout":out,"stderr":err,"result":result,"result_error":result_error}))'''


def choose_leader(snapshot, selected=None):
    pods = snapshot["pods"]
    if not pods or not all(p["ready"] for p in pods):
        raise ValueError("All serving pods must be ready before benchmarking")
    leaders = [p for p in pods if p["labels"].get("leaderworkerset.sigs.k8s.io/worker-index", "0") == "0"]
    if selected:
        leaders = [p for p in leaders if p["name"] == selected]
    if len(leaders) != 1:
        raise ValueError("Select exactly one ready leader with --pod; candidates: " + ", ".join(p["name"] for p in leaders))
    pod = leaders[0]
    containers = [c for c in pod["containers"] if c["name"] in ("main", "kserve-container")]
    if len(containers) != 1:
        raise ValueError("Expected main or kserve-container on the serving leader")
    return pod, containers[0]["name"]


def benchmark_command(args, model, concurrency):
    return ["vllm", "bench", "serve", "--backend", "openai-chat", "--base-url", "http://localhost:8080",
            "--endpoint", "/v1/chat/completions", "--model", model, "--tokenizer", "/mnt/models",
            "--dataset-name", "random", "--random-input-len", str(args.input_len),
            "--random-output-len", str(args.output_len), "--random-range-ratio", "0",
            "--num-prompts", str(args.num_prompts), "--max-concurrency", str(concurrency),
            "--request-rate", "inf", "--seed", str(args.seed), "--temperature", "0",
            "--ignore-eos", "--num-warmups", "1", "--save-result", "--save-detailed",
            "--percentile-metrics", "ttft,tpot,e2el", "--metric-percentiles", "50,95", "--disable-tqdm"]


def replica_hardware(snapshot, leader):
    """Compare the tested replica, including its workers, on heterogeneous fleets."""
    group_key = "leaderworkerset.sigs.k8s.io/group-index"
    group = leader["labels"].get(group_key)
    members = [p for p in snapshot["pods"] if p["labels"].get(group_key) == group] if group is not None else [leader]
    result = []
    for pod in members:
        runtime = snapshot["runtimes"].get(pod["name"]) or {}
        node = snapshot["nodes"].get(pod["node"], {})
        result.append({"node": {k: node.get(k) for k in ("instance_type", "gpu_product", "capacity", "allocatable")},
                       "gpus": sorted([row.split(",")[:3] for row in (runtime.get("gpus") or "").splitlines()]),
                       "resources": [c["resources"] for c in pod["containers"]]})
    return sorted(result, key=lambda r: json.dumps(r, sort_keys=True))


def parse_metrics(result, count, expected_output_len=None):
    if not isinstance(result, dict):
        raise ValueError("Benchmark produced no JSON result")
    required = ("duration", "completed", "request_throughput", "output_throughput",
                "median_e2el_ms", "p95_e2el_ms", "median_ttft_ms", "p95_ttft_ms", "median_tpot_ms", "p95_tpot_ms")
    for key in required:
        value = result.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Benchmark metric missing or invalid: {key}")
    completed = result["completed"]
    if not isinstance(completed, int) or completed > count:
        raise ValueError("Invalid completed request count")
    failed = result.get("failed", count - completed)
    if not isinstance(failed, int) or isinstance(failed, bool) or failed < 0 or completed + failed != count:
        raise ValueError("Benchmark result does not account for every request")
    if result["duration"] <= 0:
        raise ValueError("Benchmark duration must be positive")
    output_lens = result.get("output_lens")
    if (not isinstance(output_lens, list) or len(output_lens) != count
            or any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in output_lens)
            or sum(n > 0 for n in output_lens) != completed):
        raise ValueError("Detailed output lengths do not account for successful requests")
    unexpected = sum(n > 0 and n != expected_output_len for n in output_lens) if expected_output_len is not None else 0
    return {"successful_requests": completed, "failed_requests": failed,
            "unexpected_output_lengths": unexpected,
            "duration_seconds": result["duration"], "requests_per_second": result["request_throughput"],
            "output_tokens_per_second": result["output_throughput"],
            "median_request_latency_ms": result["median_e2el_ms"], "p95_request_latency_ms": result["p95_e2el_ms"],
            "median_ttft_ms": result["median_ttft_ms"], "p95_ttft_ms": result["p95_ttft_ms"],
            "median_tpot_ms": result["median_tpot_ms"], "p95_tpot_ms": result["p95_tpot_ms"]}


def run(args):
    workload = {"version": 1, "dataset": "random", "input_tokens": args.input_len,
                "output_tokens": args.output_len, "num_prompts": args.num_prompts,
                "concurrency": args.concurrency, "seed": args.seed, "temperature": 0,
                "ignore_eos": True, "range_ratio": 0, "warmups_per_workload": 1,
                "cache_mode": "warm; preserve deployed cache settings", "timeout_seconds": args.timeout}
    run_audit = audit.Audit("vllm", workload,
                            {"placement": "serving-leader-container", "endpoint": "localhost:8080"},
                            root=args.history_dir, namespace=args.namespace, name=args.name)
    print(f"Artifacts: {run_audit.path}", file=sys.stderr, flush=True)
    metrics, validation = {}, {"response_validity_only": True, "workloads": {}}
    status, error, code = "failed", None, 1
    try:
        pod, container = choose_leader(run_audit.record["before"], args.pod)
        execute = lambda argv, timeout=120: audit.remote(pod["name"], args.namespace, container, argv, timeout)
        help_text = execute(["vllm", "bench", "serve", "--help"])
        (run_audit.path / "benchmark-help.txt").write_text(help_text)
        planned = benchmark_command(args, "placeholder", args.concurrency[0])
        flags = {a for a in planned if a.startswith("--")} | {"--result-dir", "--result-filename"}
        missing = sorted(f for f in flags if f not in help_text)
        if missing:
            raise ValueError("Installed benchmark CLI lacks required flags: " + ", ".join(missing))
        model = json.loads(execute(["python", "-c", DISCOVER_MODEL]))
        audit.write_json(run_audit.path / "models.json", model)
        run_audit.record["client"]["vllm_version"] = (run_audit.record["before"]["runtimes"].get(pod["name"]) or {}).get("vllm_version")
        run_audit.record["client"]["replica_hardware"] = replica_hardware(run_audit.record["before"], pod)
        run_audit.record["workload"]["served_model"] = model["id"]
        run_audit.record["selected_pod"] = pod["name"]
        audit.write_json(run_audit.path / "run.json", run_audit.record)
        probes = json.loads(execute(["python", "-c", PROBES.replace("MODEL", repr(model["id"]))], 260))
        audit.write_json(run_audit.path / "probes.json", probes)
        validation["probes"] = probes
        if not probes or not all(p["passed"] for p in probes):
            raise ValueError("Response-validity probes failed; benchmark load was not submitted")
        for concurrency in args.concurrency:
            label = f"concurrency_{concurrency}"
            argv = benchmark_command(args, model["id"], concurrency)
            audit.write_json(run_audit.path / f"{label}-command.json", argv)
            print(f"Benchmark: {args.num_prompts} requests, concurrency {concurrency}", file=sys.stderr, flush=True)
            payload = json.loads(execute(["python", "-c", EXECUTE, json.dumps(argv), str(args.timeout)], args.timeout + 60))
            audit.write_json(run_audit.path / f"{label}-raw.json", payload)
            (run_audit.path / f"{label}-stdout.log").write_text(payload["stdout"])
            (run_audit.path / f"{label}-stderr.log").write_text(payload["stderr"])
            if payload["result"] is not None:
                values = parse_metrics(payload["result"], args.num_prompts, args.output_len)
                metrics.update({f"{label}.{k}": v for k, v in values.items()})
                validation["workloads"][label] = {"failed_requests": values["failed_requests"],
                                                   "unexpected_output_lengths": values["unexpected_output_lengths"]}
            if payload["timed_out"] or payload["returncode"] != 0 or payload["result"] is None:
                raise ValueError(f"{label} failed (returncode={payload['returncode']}, timeout={payload['timed_out']}); see raw artifacts")
            if values["failed_requests"]:
                raise ValueError(f"{label}: {values['failed_requests']} benchmark requests failed")
            if values["unexpected_output_lengths"]:
                raise ValueError(f"{label}: successful requests did not generate the fixed requested output length")
            run_audit.record.update(metrics=metrics, validation=validation)
            audit.write_json(run_audit.path / "run.json", run_audit.record)
        status, code = "passed", 0
    except KeyboardInterrupt:
        status, code = "interrupted", 130
        error = "Interrupted; a remote benchmark may continue until its recorded timeout. Do not automatically rerun."
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        error = str(exc)
    finally:
        run_audit.finish(status, metrics, validation, error)
        print(run_audit.path, flush=True)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)
    return code


def positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="aiq-inference")
    parser.add_argument("--name", default="vllm-inference-service")
    parser.add_argument("--pod", help="Select a leader when serving multiple replicas")
    parser.add_argument("--num-prompts", type=positive, default=100)
    parser.add_argument("--concurrency", type=positive, nargs="+", default=[1, 8])
    parser.add_argument("--input-len", type=positive, default=512)
    parser.add_argument("--output-len", type=positive, default=128)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=positive, default=1800, help="Remote deadline per workload in seconds")
    parser.add_argument("--history-dir", type=Path, default=audit.HISTORY)
    args = parser.parse_args()
    if len(set(args.concurrency)) != len(args.concurrency):
        parser.error("--concurrency values must be distinct")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
