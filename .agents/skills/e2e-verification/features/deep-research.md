# Deep research

Deep research runs the multi-step `deep_researcher` agent and returns a longer
citation-backed report (Nemotron Ultra on the NVIDIA API Catalog in this pattern).

## Sub-features

- `deep-submit` creates an async job with `agent_type=deep_researcher`.
- `deep-poll` waits until success and fetches the final report.
- `deep-verdict` judges whether the report makes sense for the question asked.

## How to get to it (user POV)

- Submit an async job selecting the deep researcher (API, debug console, or
  verification helper).
- In the web UI, ask for thorough / deep research on a topic (UI path is
  secondary; this map drives the explicit agent).

## Driving it with aiq.py

Preconditions:

- Port-forward and doctor as in the feature map baseline.
- Deployment has a valid `NVIDIA_API_KEY` for Ultra.
- Question: `What is the capital of France?` (or `VERIFY_AIQ_QUESTION`).

- **Submit.** Run
  `python3 .agents/skills/e2e-verification/scripts/aiq.py submit "What is the capital of France?" deep_researcher`.
  Exit code `0`; stdout JSON includes `job_id`.
- **Poll.** Run
  `python3 .agents/skills/e2e-verification/scripts/aiq.py research_poll <job_id>`.
  Exit code `0`; stdout is the report JSON. Allow a long wait.
- **Judge.** Report makes sense if it clearly identifies Paris as the capital.
  Extra sections and citations are fine. Write a one-line verdict.
- **Proof.** Save the exact question in `question.txt`, submit JSON, job id, and
  report JSON as `deep-report.json`. Run
  `python3 .agents/skills/e2e-verification/scripts/save-responses.py <artifact-dir>`
  to save the complete final answer in `deep-response.md` and `responses.md`.
  Retain these files with the verdict.

## Gotchas

- Deep jobs are asynchronous and continue server-side if local polling stops;
  resume with `research_poll <job_id>`.
- Admission limits may reject submit under load; surface the API error and stop.
- A verbose report that never states Paris fails the reasonableness bar for the
  default question.
