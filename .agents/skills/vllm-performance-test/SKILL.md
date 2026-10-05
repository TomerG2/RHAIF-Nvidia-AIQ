---
name: vllm-performance-test
description: >
  Benchmark deployed vLLM directly with repeatable throughput and latency tests,
  recording deployed AI-Q commits, GPU hardware, runtime/model versions, response
  validity, and comparisons with earlier runs. Use for direct vLLM performance
  checks across commits; use aiq-research-check for end-to-end research tests.
---

# vLLM performance test

Benchmark the existing OpenShift vLLM deployment and report which deployed commit,
hardware, runtime, and model produced every result. This skill belongs to the AI-Q
repository and uses its shared `scripts/performance_audit.py` helper.

## Run

Resolve `SKILL_DIR` to this skill's folder, then run:

```bash
python3 "${SKILL_DIR}/scripts/run.py"
```

Defaults: `aiq-inference`, service `vllm-inference-service`, 100 requests each at
concurrency 1 then 8, 512 input / 128 output tokens, seed 0, temperature 0, fixed
length sampling, and ignored EOS. Each workload has one excluded warmup. Preserve
the deployed prefix-cache settings; this is a warm-cache check, not a cold-start
measurement. The benchmark client runs in the serving leader container and can
compete for its CPU; preserve that placement when comparing runs.

Overrides: `--namespace`, `--name`, `--pod`, `--num-prompts`, `--concurrency`,
`--input-len`, `--output-len`, `--seed`, `--timeout`, and `--history-dir`.
Require `--pod` when multiple ready leaders exist. All serving pods must be ready.
The runner checks installed CLI flags before requests, discovers the served model,
and runs two response-validity probes before benchmark load. These check response
structure and nonempty text, including reasoning; they do not grade factual accuracy.

The runner uses installed `vllm bench serve` and `/mnt/models` as the tokenizer;
it does not install packages, change serving configuration, restart pods, deploy
commits, or provision hardware. A workload has a 30-minute remote deadline by
default. On failures or interruption, inspect preserved artifacts; do not rerun
automatically. An interrupted/disconnected benchmark can continue remotely until
its deadline. Do not run both test skills concurrently against the same server.

## Audit and compare

The runner prints its directory under git-ignored `artifacts/performance/`.
Read `summary.md`, `run.json`, probe responses, and each workload's raw result.
Keep partial results and failures. The audit records local SHA/dirty status
separately from Argo CD's resolved deployment revisions; never label local `HEAD`
as deployed. Hardware is discovered from actual serving pods/nodes and NVIDIA
tools, not assumed from a profile. Node GPU capacity and GPU allocations are
separate. NVIDIA-SMI's CUDA version describes driver capability, not the installed
CUDA toolkit. Missing evidence includes an explicit discovery error.

Compare against the latest successful run with the same workload, hardware,
model, runtime, serving configuration, and client placement. Different commits
may compare when those conditions match. Rollouts, pod restarts, incomplete
provenance, and changed configurations prevent automatic comparison; report why.
Percentage changes are observations, not threshold-based regression verdicts.

To inspect chronological history, resolving `REPO_ROOT` to this repository:

```bash
python3 "${REPO_ROOT}/scripts/performance_audit.py" history
```

In the final reply after every run, state deployed commit(s), GPU model/count and
memory, runtime/model revision, successful/failed requests, output tokens/second,
median/p95 latency and TTFT/TPOT for each concurrency, probe status, baseline and
percentage changes (or absence of a comparable baseline), and link to `summary.md`.
If history needs a visual exploration, follow the available visualization workflow.
