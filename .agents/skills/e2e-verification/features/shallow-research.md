# Shallow research

Shallow research returns a bounded, faster cited answer from the
`shallow_researcher` agent (in-cluster Nemotron Lightning on this pattern).

## Sub-features

- `shallow-submit` creates an async job with `agent_type=shallow_researcher`.
- `shallow-poll` waits until success and fetches the report.
- `shallow-verdict` judges whether the answer makes sense for the question asked.

## How to get to it (user POV)

- Submit an async job selecting the shallow researcher (API, debug console, or
  verification helper).
- In the web UI, ask a quick factual question that the orchestrator routes to
  shallow research (UI path is secondary; this map drives the explicit agent).

## Driving it with aiq.py

Preconditions:

- Port-forward and doctor as in the feature map baseline.
- InferenceService `vllm-inference-service` in `aiq-inference` is Ready when
  possible.
- Question: `What is the capital of France?` (or `VERIFY_AIQ_QUESTION`).

- **Submit.** Run
  `python3 .agents/skills/e2e-verification/scripts/aiq.py submit "What is the capital of France?" shallow_researcher`.
  Exit code `0`; stdout JSON includes `job_id`.
- **Poll.** Run
  `python3 .agents/skills/e2e-verification/scripts/aiq.py research_poll <job_id>`.
  Exit code `0`; stdout is the report JSON.
- **Judge.** Answer makes sense if it clearly names Paris. Write the verdict next
  to any saved report artifact.
- **Proof.** Save the exact question in `question.txt`, submit JSON, job id, and
  report JSON as `shallow-report.json`. Run
  `python3 .agents/skills/e2e-verification/scripts/save-responses.py <artifact-dir>`
  to save the complete final answer in `shallow-response.md` and `responses.md`.
  Retain these files with the one-line verdict.

## Gotchas

- Shallow depends on in-cluster vLLM. Backend health alone is not enough if the
  InferenceService is not Ready.
- Headless mode skips the clarifier (`X-AIQ-Mode: headless` is set by `aiq.py`).
- Do not cancel a shallow job belonging to another session.
