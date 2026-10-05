# Serving review verification, 2026-10-05

The goal is RustFS-backed model publication, separate node caches, configurable
serving, and a safe migration. These verdicts cover the five supplied review
findings. Previously removed BF16 memory and DSC selector findings remain removed.

## 1. Distributed cache mounts and labels: WRONG

> Distributed pods never declare `/mnt/models` (or serving labels on the pod template)

The template intentionally supplies a PodSpec, not a PodTemplateSpec. KServe adds
model storage afterward and propagates `LLMInferenceService.spec.labels` onto both
pod templates. Adding `metadata.labels` inside these PodSpec fields is invalid;
adding a second cache mount would duplicate native integration.

The matching [RHOAI controller implementation](https://github.com/red-hat-data-services/kserve/blob/189d548dabf58420620b34f001cae2330ef450a2/pkg/controller/v1alpha2/llmisvc/workload_multi_node.go#L213)
attaches artifacts for both roles and copies their labels. Its
[storage implementation](https://github.com/red-hat-data-services/kserve/blob/189d548dabf58420620b34f001cae2330ef450a2/pkg/controller/v1alpha2/llmisvc/workload_storage.go#L107)
rewrites the S3 model URI to the cache PVC.

A live, deliberately unschedulable probe confirmed the installed controller
injected the serving label and read-only `/mnt/models` cache mount into both
leader and worker templates, with no remote storage initializer. The probe and
its temporary configs were removed. Local evidence is
`tmp/deploy/review-llm-probe-lws.json` (git-ignored).

### Separate pipeline preset defect: RIGHT, fixed

The probe initially failed with `ConfigNotFound`: RHOAI 3.5.1 automatically looks
up `v3-5-1-kserve-config-llm-worker-pipeline-parallel`, even when explicit
`baseRefs` refer to `aiq-tensor-pipeline`. The chart now supplies our runtime under
the expected name, configured by `global.rhoai.pipelineConfigName`, and references
that config. The live probe reconciled into a LeaderWorkerSet after this fix.
This verifies controller integration, not multi-node GPU inference.

## 2. Publication retry: RIGHT, fixed

> Failed `publish-*` Job is never retried by Argo CD

After Kubernetes retries or the deadline are exhausted, an unchanged normal Job
remains failed on later syncs. Publication now uses `Sync` and
`BeforeHookCreation`, preserving wave 5 and the persistent scratch claim.
Completed snapshots are verified and reused without another Hugging Face
download. Latest Job logs remain until the next full sync. Selective syncs skip
hooks; this is not a way to bypass publication gates.

## 3. Cache footprint: RIGHT, fixed with explicit placement

> Cache readiness waits for every eligible labeled node, not the replica footprint

Simply requiring fewer ready copies is unsafe: native cache PV affinity selects
the entire node group and does not exclude cold nodes. Automatic topology
selection is outside the initial serving scope.

`global.serving.nodeNames` now provides an explicit deployment footprint within
the labeled pool. The gate and required pod affinity use the same allowlist.
Every listed node must be eligible and downloaded, and enough distinct topology
domains must exist. Cache failures outside this footprint no longer block it.
The conservative all-eligible-node behavior remains when no allowlist is supplied.
Tests cover failed spare nodes, missing copies, duplicate topology domains, and
a temporarily ineligible cold node that must not slip into scheduling later.

## 4. GPU preflight: RIGHT for ownership and init accounting, fixed

> GPU preflight credits the old workload only when the name is literally `vllm-inference-service`

The gate now uses its injected `SERVICE_NAME`, preserving the namespace check.
GPU accounting follows Kubernetes scheduling semantics: the maximum of steady
application/sidecar requests and sequential init-container peaks, plus overhead.
Limits supply the request when a container omits an explicit request.

The alleged truncation of an unbounded list was not observed: the original
request did not request chunking. The new list helper explicitly requests pages
and follows encoded continuation tokens for pods, nodes, and operator versions.
Regression tests include occupancy on the second page and custom release names.

## 5. RustFS SCC namespace test: RIGHT as a coverage defect, fixed

> RustFS SCC unit test asserts the wrong namespace

The production chart already uses `.Release.Namespace` correctly. The render
helper now accepts a namespace, and both RustFS modes are tested in
`aiq-model-storage`, including their SCC service-account subjects.

## Remaining validation

The active cluster has one GPU node. Distributed GPU inference, worker recovery,
and B200 validation remain open. The existing AI-Q research failures recorded in
`TODO.md` are separate application issues; a healthy serving deployment does not
establish that all research requests succeed.
