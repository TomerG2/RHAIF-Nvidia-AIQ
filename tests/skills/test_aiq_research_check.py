# SPDX-FileCopyrightText: Copyright (c) 2026, Red Hat, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the batch collector against a local frontend HTTP API."""

from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
from threading import Event, Lock, Thread
import time
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / ".cursor/skills/aiq-research-check/scripts/run.py"
PREFIX = "/api/v1/jobs/async"


@pytest.fixture
def runner(monkeypatch):
    spec = importlib.util.spec_from_file_location("aiq_research_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "POLL_INTERVAL", 0.01)
    monkeypatch.setattr(module, "JOB_TIMEOUT", 5)
    return module


@contextmanager
def frontend(*, agents=None, behavior=None, pending=False, delay=0.03):
    state = {
        "requests": [], "submissions": [], "jobs": {}, "active": 0, "peak": 0,
        "all_submitted": Event(),
    }
    lock = Lock()
    behaviors = behavior or {}
    available = agents if agents is not None else ["shallow_researcher", "deep_researcher"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, body, status=200, *, raw=False):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            try:
                self.wfile.write(body.encode() if raw else json.dumps(body).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            with lock:
                state["requests"].append(("POST", self.path))
            if self.path != PREFIX + "/submit":
                return self.respond({"error": "Wrong proxy path"}, 404)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["submissions"].append(payload)
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(delay)
            try:
                if behaviors.get(payload["input"]) == "reject":
                    return self.respond({"error": "Capacity reached"}, 429)
                job_id = str(uuid.uuid4())
                with lock:
                    state["jobs"][job_id] = {"payload": payload, "polls": 0}
                self.respond({"job_id": job_id, "status": "submitted"})
            finally:
                with lock:
                    state["active"] -= 1
                    if len(state["submissions"]) == 13 and state["active"] == 0:
                        state["all_submitted"].set()

        def do_GET(self):
            with lock:
                state["requests"].append(("GET", self.path))
            if self.path == PREFIX + "/agents":
                return self.respond({"agents": [{"agent_type": agent} for agent in available]})
            if not self.path.startswith(PREFIX + "/job/"):
                return self.respond({"error": "Wrong proxy path"}, 404)
            job_id = self.path.removeprefix(PREFIX + "/job/").split("/")[0]
            with lock:
                job = state["jobs"][job_id]
                question = job["payload"]["input"]
                job["polls"] += 1
                polls = job["polls"]
            action = behaviors.get(question)
            if self.path.endswith("/report"):
                if action == "bad_json":
                    return self.respond("invalid JSON", raw=True)
                if action == "missing":
                    return self.respond({"has_report": False, "report": ""})
                return self.respond({"has_report": True, "report": "Answer to: " + question})
            if action == "fail":
                return self.respond({"status": "failed", "error": "Research tool failed"})
            if pending or polls == 1:
                return self.respond({"status": "running"})
            self.respond({"status": "completed"})

    class Server(ThreadingHTTPServer):
        request_queue_size = 32
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_batch_pairs_questions_and_collects_concurrently(runner, tmp_path):
    output = tmp_path / "run"
    with frontend() as (url, state):
        assert runner.run_batch(url, output) == 0
    assert state["peak"] > 1
    assert Counter(item["agent_type"] for item in state["submissions"]) == {
        "shallow_researcher": 10, "deep_researcher": 3,
    }
    shallow = {item["input"] for item in state["submissions"] if item["agent_type"] == "shallow_researcher"}
    deep = {item["input"] for item in state["submissions"] if item["agent_type"] == "deep_researcher"}
    assert len(shallow) == 10
    assert deep == {question for question, _ in runner.QUESTIONS[:3]}
    assert deep <= shallow
    assert all(path.startswith(PREFIX + "/") for _, path in state["requests"])
    assert state["requests"][0] == ("GET", PREFIX + "/agents")
    results = json.loads((output / "results.json").read_text())
    assert len(results["results"]) == 13
    assert results["potentially_outstanding_job_ids"] == []
    for record in results["results"]:
        assert record["status"] == "completed"
        assert record["answer"] == "Answer to: " + record["question"]
        assert record["report"]["report"] == record["answer"]
        assert record["job_status"]["status"] == "completed"
        assert record["elapsed_seconds"] >= 0
        assert json.loads((output / f"{record['request_id']}.json").read_text()) == record


def test_missing_agent_stops_before_submission(runner, tmp_path):
    output = tmp_path / "run"
    with frontend(agents=["shallow_researcher"]) as (url, state):
        with pytest.raises(ValueError, match="missing agents: deep_researcher"):
            runner.run_batch(url, output)
    assert state["submissions"] == []
    assert not output.exists()


def test_failures_are_preserved_without_resubmitting(runner, tmp_path):
    output = tmp_path / "run"
    behavior = {
        runner.QUESTIONS[5][0]: "reject",
        runner.QUESTIONS[6][0]: "missing",
        runner.QUESTIONS[7][0]: "bad_json",
        runner.QUESTIONS[8][0]: "fail",
    }
    with frontend(behavior=behavior) as (url, state):
        assert runner.run_batch(url, output) == 1
    assert len(state["submissions"]) == 13
    records = {record["request_id"]: record for record in json.loads((output / "results.json").read_text())["results"]}
    assert records["shallow-06"]["status"] == "rejected"
    assert records["shallow-06"]["http_status"] == 429
    assert records["shallow-06"]["job_id"] is None
    assert records["shallow-07"]["status"] == "missing_answer"
    assert records["shallow-08"]["status"] == "error"
    assert records["shallow-09"]["status"] == "failed"
    assert records["shallow-09"]["error"] == "Research tool failed"
    assert sum(record["status"] == "completed" for record in records.values()) == 9


def test_job_deadline_preserves_outstanding_ids(runner, monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "JOB_TIMEOUT", 0.5)
    output = tmp_path / "run"
    with frontend(pending=True, delay=0) as (url, state):
        assert runner.run_batch(url, output) == 1
    results = json.loads((output / "results.json").read_text())
    assert len(state["submissions"]) == 13
    assert all(record["status"] == "timed_out" for record in results["results"])
    assert set(results["potentially_outstanding_job_ids"]) == set(state["jobs"])


def test_interruption_keeps_submitted_ids(runner, monkeypatch, tmp_path):
    output = tmp_path / "run"
    with frontend(pending=True, delay=0) as (url, state):
        def interrupt(futures):
            assert state["all_submitted"].wait(3)
            # Wait for workers to persist the received IDs before interruption.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                saved = list(output.glob("shallow-*.json")) + list(output.glob("deep-*.json"))
                if len(saved) == 13 and all(json.loads(path.read_text())["job_id"] for path in saved):
                    raise KeyboardInterrupt
                time.sleep(0.01)
            pytest.fail("Submitted job IDs were not persisted")

        monkeypatch.setattr(runner, "as_completed", interrupt)
        assert runner.run_batch(url, output) == 130
    results = json.loads((output / "results.json").read_text())
    assert len(state["submissions"]) == 13
    assert all(record["status"] == "interrupted" for record in results["results"])
    assert set(results["potentially_outstanding_job_ids"]) == set(state["jobs"])
