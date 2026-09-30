---
name: aiq-research-check
description: >
  Check AI-Q research answers with a small concurrent batch through the frontend
  HTTP API: ten shallow requests and the same first three sent to deep research.
  Use when asked to run this paired batch and review whether the answers make sense.
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

The script prints its artifact directory. Each `shallow-*.json` or `deep-*.json`
contains the question, expected key points, job ID, status, answer, elapsed time,
error, and raw API evidence. `results.json` contains all 13 results, status counts
per agent, and potentially outstanding job IDs. Artifacts are ignored by git.

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

In the final reply, report the counts, identify failed or nonsensical results,
and link to `verdicts.md`. Keep all collected evidence. Running this skill does
not authorize changing application code, authentication, or admission limits.
