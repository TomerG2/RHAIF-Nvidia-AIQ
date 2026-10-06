---
name: e2e-verification
description: >
  Run end-to-end smoke verification of deployed NVIDIA AI-Q on OpenShift with the
  same question for shallow_researcher and deep_researcher. Save the exact prompt
  and each agent's complete final response, then judge whether both answers make
  sense. Use for post-deploy E2E checks and hybrid Lightning routing verification.
---

# E2E Verification (AI-Q shallow + deep research)

This skill drives a **deployed** AI-Q backend (Validated Pattern on OpenShift), not a
local blueprint checkout. Primary surface: HTTP async job API via `scripts/aiq.py`.
Secondary surfaces (frontend Route, interactive CLI) exist but are out of scope here.

## Locked smoke question

Default question (override with `VERIFY_AIQ_QUESTION`):

```text
What is the capital of France?
```

Alternatives that stay easy to judge:

1. `What does HTTP stand for?`
2. `In what year did Apollo 11 land on the Moon?`
3. `What is the chemical formula for water?`
4. `Who wrote Romeo and Juliet?`

Keep one question for both agents in a single run.
This is a two-job smoke test. Report its scope explicitly; do not describe it as
a larger research batch or a broad research-quality audit.

## Launch

Preconditions: `oc` logged into the cluster; pattern installed so `svc/aiq-backend`
exists in namespace `aiq`; for shallow success the in-cluster InferenceService
`vllm-inference-service` in `aiq-inference` should be Ready; deep research needs
`NVIDIA_API_KEY` already in the deployment secrets.

```bash
SKILL_DIR=".agents/skills/e2e-verification"

# Starts oc port-forward (only this run's PID) and prints export lines.
# Capture first so a failed launch is not hidden by eval of empty stdout.
LAUNCH_OUT="$(bash ${SKILL_DIR}/scripts/launch-port-forward.sh)" || exit 1
eval "${LAUNCH_OUT}"
```

Ready when `scripts/aiq.py health` returns JSON (the launch script waits for this).

Teardown: `VERIFY_AIQ_STATE_DIR=... bash ${SKILL_DIR}/scripts/cleanup.sh` (see Cleanup).

The existing `VERIFY_AIQ_*` environment variables remain supported, including
`VERIFY_AIQ_ARTIFACT_DIR` for choosing where evidence is saved.

**Isolate:** This pattern uses one shared cluster backend. Do not start a second
verification run against the same port-forward or cancel jobs you did not submit.
Two agents in one run (shallow then deep) is intentional; concurrent E2E verification
sessions on the same `AIQ_SERVER_URL` are not.

## Doctor

Run first whenever anything looks off:

```bash
bash ${SKILL_DIR}/scripts/doctor.sh
```

Pass criteria:

- `health` succeeds against `AIQ_SERVER_URL` (default `http://127.0.0.1:8000`)
- `agents` lists both `shallow_researcher` and `deep_researcher`
- Optional: warns if InferenceService is not Ready (shallow will likely fail)

## Drive

```bash
# Same question → shallow, then deep. Writes evidence; prints artifact dir path.
OUT="$(bash ${SKILL_DIR}/scripts/run-pair.sh)" || exit 1
```

Harness commands (also usable alone):

```bash
python3 ${SKILL_DIR}/scripts/aiq.py health
python3 ${SKILL_DIR}/scripts/aiq.py agents
python3 ${SKILL_DIR}/scripts/aiq.py submit "<question>" shallow_researcher
python3 ${SKILL_DIR}/scripts/aiq.py research_poll <job_id>
python3 ${SKILL_DIR}/scripts/aiq.py submit "<question>" deep_researcher
python3 ${SKILL_DIR}/scripts/aiq.py research_poll <job_id>
```

Always submit with an explicit `agent_type`. Do not use `chat` for this proof — chat
routes depth via the orchestrator and can skip the agent you meant to test.

Deep research can take many minutes. Keep the port-forward alive until both polls finish.

## Evidence

Artifacts land in the repository's ignored run directory by default:

```text
artifacts/e2e-verification/<run-id>/
```

Expected files:

| File | Purpose |
|---|---|
| `question.txt` | Exact question sent |
| `env.txt` | `AIQ_SERVER_URL` and run id |
| `shallow-submit.json` / `shallow-job-id.txt` / `shallow-report.json` | Shallow path |
| `deep-submit.json` / `deep-job-id.txt` / `deep-report.json` | Deep path |
| `shallow-response.md` / `deep-response.md` | Exact final response text, including citations and sources |
| `responses.md` | Question plus available agent responses and job IDs in one readable file |
| `verdict.md` | Agent-written reasonableness judgment (create this) |

`run-pair.sh` saves each response as soon as its report arrives. If the second job
fails, the first response remains available. Empty or missing responses fail the
run; they do not count as successful verification.

To generate readable responses from an existing run without submitting new jobs:

```bash
python3 ${SKILL_DIR}/scripts/save-responses.py "<artifact-dir>"
```

### Proof standards

1. Exercise the real async job path (`submit` + `research_poll`), not mocks.
2. Capture submit response (job id), raw final report, and readable final response
   for each agent. Preserve the complete response verbatim; do not replace it with
   a summary, verdict, or only the first sentence.
3. Reasonableness bar is intentionally coarse: read each report and decide only
   **makes sense** or **does not make sense**, with one short why.
4. For the default question, "makes sense" means the answer clearly identifies
   **Paris** as the capital (extra prose or citations are fine).
5. Write `verdict.md` in the artifact dir after both reports exist. Include the
   question and a link to each saved response. When reporting results, link
   `responses.md` so the user can inspect the actual prompt and answers. Example:

```markdown
# Reasonableness verdict

Question: What is the capital of France?

## Shallow
- Verdict: makes sense
- One-line why: States Paris is the capital of France.
- Response: [shallow-response.md](shallow-response.md)

## Deep
- Verdict: makes sense
- One-line why: Report concludes Paris; citations do not contradict that.
- Response: [deep-response.md](deep-response.md)
```

Cleanup must not delete this directory.

## Cleanup

```bash
# Cancels only job IDs recorded in this run's state dir; kills only the
# port-forward PID started by launch-port-forward.sh.
bash ${SKILL_DIR}/scripts/cleanup.sh
```

Requires `VERIFY_AIQ_STATE_DIR` from launch (and jobs file populated by `run-pair.sh`).
Never kill by process name. Never remove `artifacts/`.

## Helpers

Invoke from repo root unless noted.

| Helper | Invocation |
|---|---|
| Port-forward | `LAUNCH_OUT="$(bash .agents/skills/e2e-verification/scripts/launch-port-forward.sh)" || exit 1; eval "${LAUNCH_OUT}"` |
| Doctor | `bash .agents/skills/e2e-verification/scripts/doctor.sh` |
| Paired drive | `OUT="$(bash .agents/skills/e2e-verification/scripts/run-pair.sh)"` |
| Cleanup | `bash .agents/skills/e2e-verification/scripts/cleanup.sh` |
| Save readable responses | `python3 .agents/skills/e2e-verification/scripts/save-responses.py <artifact-dir>` |
| Raw client | `python3 .agents/skills/e2e-verification/scripts/aiq.py <command>` |

`scripts/aiq.py` is the NVIDIA AI-Q research helper (stdlib HTTP only). It expects
`REQUIRE_AUTH=false` on the backend (pattern default for this smoke path) and
`AIQ_SERVER_URL` pointing at localhost via port-forward.

## Feature map

See [features/README.md](features/README.md).
