"""Verify provenance, comparisons, and failure persistence without a live cluster."""
from copy import deepcopy
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import performance_audit as audit
import vllm_performance as bench


@pytest.fixture
def raw_pod():
    return {"metadata": {"name": "serving-0", "uid": "pod-uid", "labels": {}},
            "spec": {"nodeName": "gpu-node", "containers": [{
                "name": "kserve-container", "image": "registry/vllm@sha256:" + "a" * 64,
                "args": ["--tensor-parallel-size=1", "--max-model-len=4096"],
                "env": [{"name": "AIQ_MODEL_URI", "value": "s3://models/repo/" + "b" * 40},
                        {"name": "VLLM_API_KEY", "value": "do-not-save"}],
                "resources": {"limits": {"nvidia.com/gpu": "1", "cpu": "4", "memory": "24Gi"}}}]},
            "status": {"conditions": [{"type": "Ready", "status": "True"}],
                       "containerStatuses": [{"name": "kserve-container", "imageID": "registry/vllm@sha256:" + "a" * 64,
                                              "restartCount": 0}]}}


@pytest.fixture
def snapshot(raw_pod):
    return {"local": {"sha": "c" * 40, "dirty": True, "status": " M file"},
            "deployed": {"serving": {"name": "vllm-inference-service", "namespace": "vp-gitops",
                                     "sync_status": "Synced", "health": "Healthy",
                                     "sources": [{"repo": "https://example.com/aiq.git", "path": audit.APP_PATHS["serving"],
                                                  "ref": None, "revision": "d" * 40}]}},
            "namespace": "aiq-inference", "service": "vllm-inference-service",
            "pods": [audit.pod_record(raw_pod)],
            "nodes": {"gpu-node": {"instance_type": "g6.2xlarge", "gpu_product": "NVIDIA-L4",
                                   "capacity": {"cpu": "8", "memory": "32Gi", "nvidia.com/gpu": "1"},
                                   "allocatable": {"cpu": "7", "memory": "30Gi", "nvidia.com/gpu": "1"}}},
            "runtimes": {"serving-0": {"gpus": "NVIDIA L4, 23034, 580.0, GPU-uuid",
                                        "cuda": "CUDA Version: 13.0", "cuda_runtime_version": "12.8",
                                        "vllm_version": "0.24.0", "model_marker": {"uri": "s3://models/repo/" + "b" * 40,
                                                                                  "revision": "b" * 40}}},
            "application": None, "errors": []}


def run_record(snapshot, identifier="run-2"):
    return {"schema_version": 1, "id": identifier, "kind": "vllm", "started_at": "2026-10-05T10:00:00+00:00",
            "workload": {"seed": 0, "concurrency": [1, 8]}, "client": {"placement": "serving-leader-container"},
            "before": deepcopy(snapshot), "after": deepcopy(snapshot), "stable": True,
            "status": "passed", "metrics": {"requests_per_second": 12.0, "zero": 0}, "validation": {}}


def test_pod_record_records_allocations_and_excludes_api_key(raw_pod):
    record = audit.pod_record(raw_pod)
    assert record["containers"][0]["resources"]["limits"]["nvidia.com/gpu"] == "1"
    assert record["containers"][0]["image_id"].endswith("a" * 64)
    assert "do-not-save" not in json.dumps(record)


def test_runtime_probe_ignores_volatile_nvidia_smi_telemetry(monkeypatch, capsys):
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.24.0")
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(version=SimpleNamespace(cuda="12.8")))
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: json.dumps({"revision": "b" * 40}))
    def run(argv, **kwargs):
        output = "NVIDIA L4, 23034, 580.0, GPU-uuid" if len(argv) > 1 else "Mon Oct 5 10:00 CUDA Version: 13.0 | GPU util 91%"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(subprocess, "run", run)
    exec(audit.RUNTIME_PROBE, {})
    result = json.loads(capsys.readouterr().out)
    assert result["cuda"] == "CUDA Version: 13.0"
    assert result["cuda_runtime_version"] == "12.8"
    assert "GPU util" not in json.dumps(result)


def test_hardware_summary_distinguishes_allocation_from_node_capacity(snapshot):
    snapshot["nodes"]["gpu-node"]["capacity"]["nvidia.com/gpu"] = "4"
    hardware = audit.hardware_summary(snapshot)
    assert hardware["allocated_gpus"] == 1
    assert hardware["visible_devices"][0]["model"] == "NVIDIA L4"
    assert hardware["visible_devices"][0]["memory_mib"] == "23034"


def test_capture_discovers_deployed_commit_and_hardware(monkeypatch, raw_pod):
    app = {"metadata": {"name": "serving-app", "namespace": "vp-gitops"},
           "spec": {"destination": {"namespace": "aiq-inference"}, "sources": [
               {"repoURL": "https://example.com/aiq.git", "path": audit.APP_PATHS["serving"]},
               {"repoURL": "https://example.com/aiq.git", "ref": "values"}]},
           "status": {"sync": {"status": "Synced", "revisions": ["d" * 40, "e" * 40]},
                      "health": {"status": "Healthy"}}}
    def get(*args):
        if args[1] == "pods":
            return {"items": [raw_pod]}
        if args[1] == "applications.argoproj.io":
            return {"items": [app]}
        assert args == ("get", "node", "gpu-node")
        return {"metadata": {"labels": {"node.kubernetes.io/instance-type": "g6.2xlarge"}},
                "status": {"capacity": {"cpu": "8", "memory": "32Gi"}, "allocatable": {"nvidia.com/gpu": "1"}}}
    monkeypatch.setattr(audit, "oc_json", get)
    monkeypatch.setattr(audit, "command", lambda args: "c" * 40 if "rev-parse" in args else " M test")
    monkeypatch.setattr(audit, "remote", lambda *a: json.dumps({"vllm_version": "0.24", "gpus": "NVIDIA L4, 24000, 580, UUID"}))
    record = audit.capture("vllm")
    assert record["local"]["sha"] == "c" * 40
    assert record["local"]["dirty"] is True
    assert [s["revision"] for s in record["deployed"]["serving"]["sources"]] == ["d" * 40, "e" * 40]
    assert record["nodes"]["gpu-node"]["instance_type"] == "g6.2xlarge"
    assert record["runtimes"]["serving-0"]["gpus"].startswith("NVIDIA L4")


def test_cluster_unavailable_does_not_fabricate_deployed_commit(monkeypatch):
    monkeypatch.setattr(audit, "command", lambda args: "c" * 40 if "rev-parse" in args else "")
    def unavailable(*args):
        raise OSError("cluster DNS unavailable")
    monkeypatch.setattr(audit, "oc_json", unavailable)
    record = audit.capture("vllm")
    assert record["local"]["sha"] == "c" * 40
    assert record["deployed"]["serving"] is None
    assert record["pods"] == []
    assert any("DNS unavailable" in e for e in record["errors"])
    key, reasons = audit.comparison_key(run_record(record))
    assert key is None and reasons


@pytest.mark.parametrize("sync_status,phase", [("OutOfSync", "Succeeded"), ("Synced", "Running"), ("Synced", "Failed")])
def test_argo_comparison_target_is_not_labeled_deployed_during_drift(sync_status, phase):
    app = {"metadata": {"name": "serving", "namespace": "vp-gitops"},
           "spec": {"source": {"repoURL": "repo", "path": audit.APP_PATHS["serving"]},
                    "destination": {"namespace": "aiq-inference"}},
           "status": {"sync": {"status": sync_status, "revision": "e" * 40},
                      "operationState": {"phase": phase}}}
    deployed = audit.revisions([app], "aiq-inference", "vllm", [])
    source = deployed["serving"]["sources"][0]
    assert source["revision"] is None
    assert source["compared_revision"] == "e" * 40


def test_comparison_across_commits_uses_latest_success_without_mutating_evidence(snapshot, tmp_path):
    current = run_record(snapshot)
    original = deepcopy(current)
    key, reasons = audit.comparison_key(current)
    assert reasons == []
    assert current == original
    for identifier, timestamp, status, value in [
        ("older", "2026-10-01T10:00:00+00:00", "passed", 8),
        ("latest", "2026-10-03T10:00:00+00:00", "passed", 10),
        ("failed", "2026-10-04T10:00:00+00:00", "failed", 1),
    ]:
        record = run_record(snapshot, identifier)
        record.update(started_at=timestamp, status=status, comparison_key=key, metrics={"requests_per_second": value, "zero": 0})
        record["before"]["deployed"]["serving"]["sources"][0]["revision"] = "f" * 40
        folder = tmp_path / identifier
        folder.mkdir()
        audit.write_json(folder / "run.json", record)
    comparison = audit.compare(current, tmp_path)
    assert comparison["baseline"] == "latest"
    assert comparison["changes_percent"] == {"requests_per_second": 20.0}
    assert comparison["baseline_deployed"]["serving"]["sources"][0]["revision"] == "f" * 40


@pytest.mark.parametrize("changed", ["gpu", "model", "workload", "runtime", "config", "client"])
def test_incompatible_runs_have_distinct_keys(snapshot, changed):
    before = run_record(snapshot)
    after = deepcopy(before)
    if changed == "gpu":
        after["before"]["runtimes"]["serving-0"]["gpus"] = "NVIDIA H100, 80000, 580, GPU-uuid"
    elif changed == "model":
        after["before"]["runtimes"]["serving-0"]["model_marker"]["revision"] = "1" * 40
    elif changed == "workload":
        after["workload"]["seed"] = 42
    elif changed == "runtime":
        after["before"]["runtimes"]["serving-0"]["vllm_version"] = "0.25"
    elif changed == "config":
        after["before"]["pods"][0]["containers"][0]["args"] = ["--max-model-len=8192"]
    else:
        after["client"]["placement"] = "external-client"
    assert audit.comparison_key(before)[0] != audit.comparison_key(after)[0]


def test_rollout_and_missing_runtime_prevent_comparison(snapshot):
    run = run_record(snapshot)
    run["stable"] = False
    assert audit.comparison_key(run)[0] is None
    run["stable"] = True
    run["before"]["runtimes"]["serving-0"]["model_marker"] = None
    assert audit.comparison_key(run)[0] is None


@pytest.mark.parametrize("change", ["pod_uid", "restart", "revision"])
def test_finish_detects_deployment_changes(snapshot, monkeypatch, tmp_path, change):
    after = deepcopy(snapshot)
    if change == "pod_uid":
        after["pods"][0]["uid"] = "replacement"
    elif change == "restart":
        after["pods"][0]["containers"][0]["restart_count"] = 1
    else:
        after["deployed"]["serving"]["sources"][0]["revision"] = "e" * 40
    captures = iter([deepcopy(snapshot), after])
    monkeypatch.setattr(audit, "capture", lambda *args: next(captures))
    instance = audit.Audit("vllm", {}, {}, root=tmp_path)
    instance.finish("passed", {"requests_per_second": 10}, {})
    saved = json.loads((instance.path / "run.json").read_text())
    assert saved["stable"] is False
    assert saved["comparison_key"] is None
    assert (instance.path / "summary.md").exists()


def test_review_requires_complete_verdicts_and_preserves_failures(snapshot, tmp_path):
    run = run_record(snapshot)
    run.update(kind="research", status="awaiting_review")
    audit.write_json(tmp_path / "run.json", run)
    audit.write_json(tmp_path / "results.json", {"results": [
        {"request_id": "shallow-01", "answer": "Paris", "status": "completed", "agent": "shallow_researcher"},
        {"request_id": "deep-01", "answer": None, "status": "timed_out", "agent": "deep_researcher"}]})
    audit.write_json(tmp_path / "verdicts.json", [])
    with pytest.raises(ValueError, match="exactly one"):
        audit.review(tmp_path)
    audit.write_json(tmp_path / "verdicts.json", [{"request_id": "shallow-01", "verdict": "make sense", "reason": "Correct capital"}])
    audit.review(tmp_path)
    saved = json.loads((tmp_path / "run.json").read_text())
    assert saved["status"] == "failed"
    assert saved["validation"]["execution_failures"] == 1
    assert saved["validation"]["counts"] == {"shallow_researcher": {"make sense": 1}}


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(input_len=512, output_len=128, num_prompts=100, concurrency=[1, 8],
                           seed=0, timeout=1800, history_dir=tmp_path, namespace="aiq-inference",
                           name="vllm-inference-service", pod=None)


@pytest.fixture
def benchmark_result():
    return {"duration": 10, "completed": 100, "failed": 0, "request_throughput": 10, "output_throughput": 1280,
            "median_e2el_ms": 100, "p95_e2el_ms": 120, "median_ttft_ms": 10, "p95_ttft_ms": 12,
            "median_tpot_ms": 2, "p95_tpot_ms": 3, "output_lens": [128] * 100}


def fake_remote(args, result, *, fail_second=False, interrupt_second=False, flags_available=True):
    calls = []
    def execute(pod, namespace, container, argv, timeout=120):
        calls.append(argv)
        if argv[:3] == ["vllm", "bench", "serve"]:
            return " ".join(bench.benchmark_command(args, "model", 1) + ["--result-dir", "--result-filename"]) if flags_available else "--model"
        if argv[2] == bench.DISCOVER_MODEL:
            return json.dumps({"id": "model"})
        if len(argv) == 3:
            return json.dumps([{"prompt": "ready", "passed": True}, {"prompt": "arithmetic", "passed": True}])
        command = json.loads(argv[3])
        concurrency = command[command.index("--max-concurrency") + 1]
        if concurrency == "8" and interrupt_second:
            raise KeyboardInterrupt
        payload = {"command": command, "stdout": "benchmark log", "stderr": "", "returncode": 0,
                   "timed_out": False, "result": deepcopy(result), "result_error": None}
        if concurrency == "8" and fail_second:
            payload["result"].update(completed=90, failed=10, output_lens=[128] * 90 + [0] * 10)
        return json.dumps(payload)
    return execute, calls


@pytest.mark.parametrize("mode,expected", [("passed", 0), ("partial", 1), ("interrupted", 130)])
def test_runner_saves_success_partial_and_interrupted_runs(args, snapshot, benchmark_result, monkeypatch, mode, expected):
    monkeypatch.setattr(audit, "capture", lambda *a: deepcopy(snapshot))
    execute, calls = fake_remote(args, benchmark_result, fail_second=mode == "partial", interrupt_second=mode == "interrupted")
    monkeypatch.setattr(audit, "remote", execute)
    assert bench.run(args) == expected
    record = audit.history(args.history_dir)[0]
    assert record["metrics"]["concurrency_1.successful_requests"] == 100
    assert record["status"] == {"passed": "passed", "partial": "failed", "interrupted": "interrupted"}[mode]
    if mode == "partial":
        assert record["metrics"]["concurrency_8.failed_requests"] == 10
    if mode == "interrupted":
        assert "remote benchmark may continue" in record["error"]
    assert sum(len(c) == 5 and c[2] == bench.EXECUTE for c in calls) == 2
    assert (Path(record["artifact_dir"]) / "summary.md").exists()


def test_unsupported_cli_stops_before_sending_requests(args, snapshot, benchmark_result, monkeypatch):
    monkeypatch.setattr(audit, "capture", lambda *a: deepcopy(snapshot))
    execute, calls = fake_remote(args, benchmark_result, flags_available=False)
    monkeypatch.setattr(audit, "remote", execute)
    assert bench.run(args) == 1
    assert len(calls) == 1
    assert "lacks required flags" in audit.history(args.history_dir)[0]["error"]


def test_multiple_replicas_require_leader_selection(snapshot):
    other = deepcopy(snapshot["pods"][0])
    other["name"] = "serving-1"
    snapshot["pods"].append(other)
    with pytest.raises(ValueError, match="Select exactly one"):
        bench.choose_leader(snapshot)
    assert bench.choose_leader(snapshot, "serving-1")[0]["name"] == "serving-1"


def test_selected_replica_hardware_changes_on_heterogeneous_fleet(snapshot):
    other = deepcopy(snapshot["pods"][0])
    other.update(name="serving-1", node="h100-node")
    snapshot["pods"].append(other)
    snapshot["nodes"]["h100-node"] = {"gpu_product": "NVIDIA-H100"}
    snapshot["runtimes"]["serving-1"] = {"gpus": "NVIDIA H100, 80000, 580, uuid"}
    assert bench.replica_hardware(snapshot, snapshot["pods"][0]) != bench.replica_hardware(snapshot, other)


def test_fixed_output_length_validation(benchmark_result):
    benchmark_result["output_lens"][0] = 127
    metrics = bench.parse_metrics(benchmark_result, 100, 128)
    assert metrics["unexpected_output_lengths"] == 1


@pytest.mark.parametrize("bad", [None, float("nan"), -1, True])
def test_invalid_benchmark_metrics_are_rejected(benchmark_result, bad):
    benchmark_result["duration"] = bad
    with pytest.raises(ValueError, match="missing or invalid"):
        bench.parse_metrics(benchmark_result, 100)


def test_remote_timeout_returns_partial_evidence_without_hanging(tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\nprint('started', flush=True)\ntime.sleep(10)\n")
    output = subprocess.check_output([sys.executable, "-c", bench.EXECUTE,
                                      json.dumps([sys.executable, str(script)]), "1"], text=True, timeout=5)
    payload = json.loads(output)
    assert payload["timed_out"] is True
    assert payload["returncode"] != 0
    assert "started" in payload["stdout"]


def test_skill_entrypoints_expose_performance_audit_options():
    for path in (ROOT / ".agents/skills/aiq-research-check/scripts/run.py",
                 ROOT / ".agents/skills/vllm-performance-test/scripts/run.py"):
        help_text = subprocess.check_output([sys.executable, str(path), "--help"], text=True)
        assert "--history-dir" in help_text
