# Paired shallow + deep smoke

Runs the same easy research question through `shallow_researcher` and
`deep_researcher`, then records whether each answer makes sense.

## Sub-features

- `pair-shallow` submits and polls `shallow_researcher` for the locked question.
- `pair-deep` submits and polls `deep_researcher` for the same question.
- `pair-verdict` writes a coarse reasonableness judgment for each report.

## How to get to it (user POV)

- Operator/agent runs the verification helpers against a port-forwarded backend
  (not the browser UI).
- Equivalent manual path: submit two async jobs in the AI-Q API/debug console with
  agent types `shallow_researcher` and `deep_researcher` and the same prompt.

## Driving it with aiq.py

Preconditions:

- Port-forward launch succeeded (`LAUNCH_OUT="$(.../launch-port-forward.sh)" || exit 1; eval "${LAUNCH_OUT}"`).
- `.agents/skills/verify-aiq/scripts/doctor.sh` lists both agent types.
- Question is `What is the capital of France?` unless `VERIFY_AIQ_QUESTION` is set.

- **Run pair.** Drive both agents. Run
  `OUT=$(.agents/skills/verify-aiq/scripts/run-pair.sh)`. Exit code `0` and `$OUT`
  points at an artifact directory containing `shallow-report.json` and
  `deep-report.json`.
- **Read shallow report.** Open `$OUT/shallow-report.json`. The payload contains
  research answer text (report body / message content). Note the `job_id` in
  `$OUT/shallow-job-id.txt`.
- **Read deep report.** Open `$OUT/deep-report.json`. Note `$OUT/deep-job-id.txt`.
- **Judge reasonableness.** For each agent, decide only **makes sense** or
  **does not make sense**. For the default question, makes sense means the answer
  clearly identifies Paris. Write `$OUT/verdict.md` with both verdicts and a
  one-line why each.
- **Proof.** Keep `$OUT` intact after cleanup. Proof is incomplete without
  `verdict.md` plus both report JSON files.

## Gotchas

- `chat` may escalate or choose depth automatically; do not use it for this feature.
- Deep research is slow; do not treat a long `running` status as failure until the
  job reaches a terminal state or the backend returns an error.
- If InferenceService is not Ready, shallow may fail while deep still succeeds —
  report shallow as failed, do not skip writing the deep verdict.
- Cleanup cancels only job IDs in this run's state file; foreign jobs stay running.
