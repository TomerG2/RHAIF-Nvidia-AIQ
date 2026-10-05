---
name: aiq-research-check
description: >
  Check AI-Q research answers with a small concurrent batch through the frontend
  HTTP API: ten shallow requests and the same first three sent to deep research.
  Record deployed commits, vLLM hardware, end-to-end timing, and answer verdicts
  for comparisons across runs. Use for paired end-to-end checks; use
  vllm-performance-test for direct inference throughput and latency.
---

# AI-Q research check

Run **10 shallow + 3 deep requests**, then read every returned answer and judge
whether it makes sense for its question. Use the frontend HTTP proxy; no browser
automation or evaluation service is needed. This skill assumes the deployment's
authentication-disabled configuration.

## Collect answers

Use the frontend URL supplied by the user. Otherwise discover it with:

```bash
oc -n aiq get route aiq-frontend -o jsonpath='{.spec.host}'
```

Use `https://` for the discovered host. If discovery fails, request the frontend
URL; do not substitute the backend or start a backend port-forward.

Run the helper from any directory, resolving `SKILL_DIR` to this skill's folder:

```bash
python3 "${SKILL_DIR}/scripts/run.py" --frontend-url "${FRONTEND_URL}"
```

The script checks `/api/v1/jobs/async/agents` before any submission and requires
both explicit agent types. It submits all 13 requests concurrently, polls every
15 seconds, and allows one hour per job, with a 120-second limit per HTTP call.
It never retries submissions, follows redirects, or falls back to another API.
The first three questions are identical across the two agents.

The script prints its artifact directory under the repository's git-ignored
`artifacts/performance/`, shared with `vllm-performance-test`. Each `shallow-*.json` or `deep-*.json`
contains the question, expected key points, job ID, status, answer, elapsed time,
error, and raw API evidence. `results.json` contains all 13 results, status counts
per agent, and potentially outstanding job IDs. Artifacts are ignored by git.

`run.json` and `summary.md` record the local checkout SHA/dirty status separately
from the serving, workflow, and application revisions resolved by Argo CD. They
also record actual serving nodes, allocated GPU resources, GPU model/memory,
driver/CUDA and vLLM versions, image digests, model revision, and serving arguments.
Unknown evidence stays unknown with discovery errors; never substitute local
`HEAD` for the deployed commit or profile recommendations for actual hardware.
Deployment changes or incomplete evidence prevent automatic comparison.

The existing `--frontend-url` interface is preserved. Optional audit settings:
`--namespace`, `--name`, `--app-namespace`, and `--history-dir`. Missing cluster
access does not prevent a frontend check but makes its audit incomplete.
Do not run this batch concurrently with the direct vLLM performance skill.

A nonzero exit can mean partial results: read the saved files even when individual
requests fail. Never rerun the batch automatically to replace failures. On
interruption, the script saves known job IDs and stops polling; server jobs may
still run. Do not cancel unrelated jobs. A submission whose HTTP response was lost
may have created a job with an unknown ID; report that uncertainty.

## Review answers

Read **every** result. For each nonempty returned answer, assign exactly
`make sense` or `not` and give one short reason. Judge relevance to the question,
coherence, and factual correctness using the embedded expected key points as
anchors, not exact wording requirements. Verify questionable factual claims with
appropriate sources. Extra detail in a deep report is acceptable.

Keep errors, timeouts, capacity rejections, interruptions, and missing answers
separate from answer-quality verdicts. Do not invent a verdict for a missing
answer, judge from HTTP success alone, or call a capacity rejection nonsensical.
This is a coarse reasonableness review, not an exhaustive factual audit.

Write `verdicts.md` inside the printed artifact directory. Include counts per
agent for `make sense`, `not`, and execution failures, followed by one entry per
request with its question, agent, verdict or execution status, and short reason.
Compare the paired first three answers when there is a meaningful difference.

Also write `verdicts.json` as a list with exactly one entry for every nonempty
returned answer: `request_id`, `verdict` (`make sense` or `not`), and a nonempty
`reason`. Missing answers and execution failures have no quality verdict.
Finalize the audit with the shared helper, resolving `REPO_ROOT` and `RUN_DIR`:

```bash
python3 "${REPO_ROOT}/scripts/performance_audit.py" review "${RUN_DIR}"
```

This validates complete verdict coverage and updates `run.json` and `summary.md`.
A collected batch awaits review and cannot become a successful baseline until
all answers pass review and all requests completed. Compare only against the
latest successful run with matching questions, hardware, model, runtime, serving
and workflow configuration, and client placement. Changed configurations remain
in history with an explanation instead of a misleading percentage comparison.
Use `python3 "${REPO_ROOT}/scripts/performance_audit.py" history` to read history.

Report total batch duration, per-agent successful/failed request counts, and
median/p95 successful-job elapsed times. These are frontend end-to-end timings
that include submission, polling delay (up to the existing 15-second interval),
and report retrieval; they are not vLLM latency measurements. Shallow uses the
in-cluster vLLM; deep uses the NVIDIA API and has an external-service confound.
Report percentage changes as observations without regression thresholds.

In the final reply, report deployed commit(s), actual vLLM GPU model/count/memory,
runtime/model revision, timings, verdict counts, failed or nonsensical results,
and baseline changes (or why none is comparable). Link to `summary.md` and
`verdicts.md`. Keep all collected evidence. Running this skill does
not authorize changing application code, authentication, or admission limits.
