#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026, Red Hat, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Collect 10 shallow and 3 deep AI-Q answers through the frontend HTTP proxy."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import sys
from threading import Event
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts"))
import performance_audit as audit


API_PATH = "/api/v1/jobs/async"
HTTP_TIMEOUT = 120
JOB_TIMEOUT = 3600
POLL_INTERVAL = 15
AGENTS = {"shallow": "shallow_researcher", "deep": "deep_researcher"}
SUCCESS_STATES = {"completed", "success"}
FAILURE_STATES = {"failed", "failure", "cancelled", "interrupted"}
QUESTIONS = (
    (
        "Compare Kubernetes Deployments and StatefulSets. When should each be used?",
        "Deployments suit interchangeable replicas; StatefulSets provide stable pod "
        "identities and persistent-volume associations for stateful workloads.",
    ),
    (
        "Compare HTTP polling, Server-Sent Events (SSE), and WebSockets for "
        "delivering progress and results from a long-running research job.",
        "Polling repeatedly requests updates; SSE streams server-to-client events; "
        "WebSockets support bidirectional messages over a persistent connection. "
        "Discuss tradeoffs for job progress without claiming a universal best choice.",
    ),
    (
        "Explain concurrency versus parallelism, and how asynchronous I/O helps "
        "an HTTP server handle many requests.",
        "Concurrency permits overlapping tasks; parallelism executes work at the "
        "same time. Async I/O allows other tasks to run while waiting for I/O; it "
        "does not automatically make CPU-bound work faster.",
    ),
    ("What is the capital of France?", "Paris."),
    ("What does HTTP stand for?", "Hypertext Transfer Protocol."),
    ("In what year did Apollo 11 land on the Moon?", "1969."),
    ("What is the chemical formula for water?", "H2O, also written H₂O."),
    ("Who wrote Romeo and Juliet?", "William Shakespeare."),
    (
        "What is the purpose of DNS when accessing a website?",
        "The Domain Name System resolves domain names to records such as IP "
        "addresses, allowing clients to locate services.",
    ),
    (
        "Explain the main difference between a container and a virtual machine.",
        "Containers generally share the host kernel; virtual machines virtualize "
        "hardware and run their own guest operating system and kernel.",
    ),
)


class HTTPFailure(RuntimeError):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body}")
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(base_url, method, path, *, payload=None, timeout=HTTP_TIMEOUT):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + API_PATH + path,
        data=data,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        raise HTTPFailure(error.code, error.read().decode("utf-8", errors="replace")) from error
    except urllib.error.URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TimeoutError("HTTP request timed out") from error
        raise
    if not isinstance(result, dict):
        raise ValueError("Expected a JSON object from the frontend API")
    return result


def write_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect(base_url, record, path, stop):
    started = time.monotonic()
    deadline = started + JOB_TIMEOUT

    def call(method, endpoint, payload=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Job exceeded its completion deadline")
        return request_json(
            base_url, method, endpoint, payload=payload,
            timeout=min(HTTP_TIMEOUT, remaining),
        )

    try:
        if stop.is_set():
            record["status"] = "interrupted"
            return
        submitted = call("POST", "/submit", {"agent_type": record["agent"], "input": record["question"]})
        record["submission"] = submitted
        record["job_id"] = submitted.get("job_id")
        # Persist the returned ID before any polling or parsing can fail.
        record["status"] = "running"
        write_json(path, record)
        job_id = str(uuid.UUID(record["job_id"]))
        endpoint = f"/job/{job_id}"
        while not stop.is_set():
            status = call("GET", endpoint)
            record["job_status"] = status
            state = status.get("status")
            if not isinstance(state, str) or not state:
                raise ValueError("Job status response is missing its status")
            state = state.lower()
            if state in SUCCESS_STATES:
                report = call("GET", endpoint + "/report")
                record["report"] = report
                answer = report.get("report")
                if report.get("has_report") is False or not isinstance(answer, str) or not answer.strip():
                    record["status"] = "missing_answer"
                    record["error"] = "Completed job returned no nonempty text report"
                else:
                    record["status"] = "completed"
                    record["answer"] = answer
                return
            if state in FAILURE_STATES:
                record["status"] = "failed"
                record["error"] = status.get("error") or f"Job ended with status {state}"
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Job exceeded its completion deadline")
            stop.wait(min(POLL_INTERVAL, remaining))
        record["status"] = "interrupted"
    except HTTPFailure as error:
        record["http_status"] = error.status
        record["status"] = "rejected" if error.status == 429 and not record["job_id"] else "error"
        record["error"] = str(error)
    except TimeoutError as error:
        record["status"] = "timed_out"
        record["error"] = str(error)
    except Exception as error:
        record["status"] = "error"
        record["error"] = f"{type(error).__name__}: {error}"
    finally:
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_json(path, record)
        print(f"{record['request_id']}: {record['status']}", file=sys.stderr, flush=True)


def run_batch(base_url, output_dir, *, audit_run=None):
    agents = request_json(base_url, "GET", "/agents")
    if not isinstance(agents.get("agents"), list):
        raise ValueError("Frontend preflight: expected an agents list")
    available = {
        entry.get("agent_type") for entry in agents.get("agents", []) if isinstance(entry, dict)
    }
    missing = set(AGENTS.values()) - available
    if missing:
        raise ValueError("Frontend preflight: missing agents: " + ", ".join(sorted(missing)))

    if audit_run is None:
        output_dir.mkdir(parents=True, exist_ok=False)
    elif output_dir != audit_run.path or (output_dir / "results.json").exists():
        raise ValueError("Audit output must be a fresh run directory")
    batch_started = time.monotonic()
    records = []
    for label, count in (("shallow", 10), ("deep", 3)):
        for index, (question, expected) in enumerate(QUESTIONS[:count], 1):
            record = {
                "request_id": f"{label}-{index:02d}", "question": question,
                "expected_key_points": expected, "agent": AGENTS[label],
                "job_id": None, "status": "pending", "answer": None,
                "elapsed_seconds": None, "error": None,
            }
            records.append(record)
            write_json(output_dir / f"{record['request_id']}.json", record)

    print(f"Frontend: {base_url}{API_PATH}", file=sys.stderr)
    print(f"Artifacts: {output_dir}", file=sys.stderr, flush=True)
    stop = Event()
    interrupted = False
    with ThreadPoolExecutor(max_workers=13) as executor:
        try:
            futures = [
                executor.submit(collect, base_url, record, output_dir / f"{record['request_id']}.json", stop)
                for record in records
            ]
            for future in as_completed(futures):
                future.result()
        except KeyboardInterrupt:
            interrupted = True
            stop.set()
            print("Interrupted; saving job IDs. An in-flight HTTP call can take up to 120 seconds.",
                  file=sys.stderr, flush=True)

    summary = {
        agent: dict(Counter(record["status"] for record in records if record["agent"] == agent))
        for agent in AGENTS.values()
    }
    outstanding = [
        record["job_id"] for record in records if record["job_id"]
        and record["status"] in {"error", "timed_out", "interrupted"}
    ]
    write_json(output_dir / "results.json", {
        "frontend_url": base_url, "summary": summary,
        "batch_duration_seconds": round(time.monotonic() - batch_started, 3),
        "potentially_outstanding_job_ids": outstanding, "results": records,
    })
    print(json.dumps(summary), file=sys.stderr)
    print(output_dir, flush=True)
    return 130 if interrupted else int(any(record["status"] != "completed" for record in records))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-url", required=True, help="Frontend origin, e.g. https://aiq.example.com")
    parser.add_argument("--history-dir", type=Path, default=audit.HISTORY)
    parser.add_argument("--namespace", default="aiq-inference", help="Serving namespace for audit discovery")
    parser.add_argument("--name", default="vllm-inference-service")
    parser.add_argument("--app-namespace", default="aiq")
    args = parser.parse_args()
    base_url = args.frontend_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        parser.error("--frontend-url must be an HTTP(S) origin without a path, credentials, query, or fragment")
    workload = {"version": 1, "questions": QUESTIONS, "shallow": 10, "deep": 3,
                "concurrency": 13, "poll_interval_seconds": POLL_INTERVAL,
                "job_timeout_seconds": JOB_TIMEOUT, "http_timeout_seconds": HTTP_TIMEOUT,
                "frontend_host": parsed.hostname}
    run_audit = audit.Audit("research", workload, {"placement": "frontend-http-proxy", "origin": base_url},
                            root=args.history_dir, namespace=args.namespace, name=args.name,
                            app_namespace=args.app_namespace)
    code, error = 1, None
    try:
        code = run_batch(base_url, run_audit.path, audit_run=run_audit)
    except KeyboardInterrupt:
        code = 130
        error = "Interrupted before batch completion; inspect saved request IDs before any new run"
    except (OSError, ValueError, RuntimeError) as exc:
        error = str(exc)
        print(f"ERROR: {error}", file=sys.stderr)
    metrics = {}
    results_path = run_audit.path / "results.json"
    if results_path.exists():
        collected = json.loads(results_path.read_text())
        metrics["batch_duration_seconds"] = collected["batch_duration_seconds"]
        for agent in AGENTS.values():
            records = [r for r in collected["results"] if r["agent"] == agent]
            successes = [r for r in records if r["status"] == "completed"]
            elapsed = [r["elapsed_seconds"] for r in successes]
            metrics.update({f"{agent}.successful_requests": len(successes),
                            f"{agent}.failed_requests": len(records) - len(successes),
                            f"{agent}.median_job_elapsed_seconds": audit.percentile(elapsed, 50),
                            f"{agent}.p95_job_elapsed_seconds": audit.percentile(elapsed, 95)})
    status = "interrupted" if code == 130 else "awaiting_review" if code == 0 else "failed"
    run_audit.finish(status, metrics, {"review_status": "pending", "timing": "submit, poll, and fetch through frontend; includes polling delay"}, error)
    if not results_path.exists():
        print(run_audit.path, flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
