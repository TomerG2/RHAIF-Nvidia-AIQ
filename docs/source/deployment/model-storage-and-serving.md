# RustFS models and configurable serving

The supplied `nvfp4`, `bf16`, and `bf16-tp4` profiles publish a complete pinned
Hugging Face snapshot to RustFS, verify it, and populate OpenShift AI model caches
before changing serving. The former shared RWO PVC remains protected for rollback;
serving does not mount it. Each selected node keeps its own copy under the
platform-managed `/var/lib/kserve/models` path. Replacement pods reuse that copy;
replacement nodes download from RustFS.

The compatibility baseline is OpenShift AI **3.5.1**. In the inspected cluster,
`LLMInferenceService` and `LLMInferenceServiceConfig` use
`serving.kserve.io/v1alpha2`. The published documentation's older
`inference.rhaieng.openshift.io/v1alpha1` example does not match these installed
CRDs. The pattern installs the Leader Worker Set operator and its `cluster`
operand, plus cert-manager for internal storage TLS.
The RHOAI subscription starts at `rhods-operator.3.5.1` on `stable-3.5`.
When upgrading the operator, update the compatibility baseline and runtime digest
together after validation; the preflight gate rejects an unreviewed version.

## Settings and presets

Choose the existing preset with `./pattern.sh make install PROFILE=bf16-tp4` (or
`nvfp4`, the default, or `bf16`). Presets keep the existing model IDs, served
names, model arguments, shallow token budgets, and remote deep-research calls.
The two BF16 presets share the same model revision. The RHOAI runtime digest now
matches the 3.5.1 operator's installed template.

`global.serving` is the topology input in `values-global.yaml`:

```yaml
global:
  serving:
    replicas: 1
    nodesPerReplica: 2
    gpusPerNode: 2
    tensorParallel: 2
    pipelineParallel: 2
    runtimeImage: ""  # uses the pinned global.rhoai.vllmImage
    cpu: "8"
    memory: 64Gi
    shmSize: 8Gi
    nodeSelector: {}
    topologyKey: kubernetes.io/hostname
    networkAttachments: []
    rdmaResources: {}
    ncclEnv: {NCCL_DEBUG: INFO}
    ipcLock: false
```

One-node layouts use `InferenceService`, including multiple independent replicas.
More than one node per replica uses `LLMInferenceService` with a namespace-scoped
`aiq-tensor-pipeline` configuration. Every pod in a replica uses the same runtime
and `/mnt/models`. The launcher passes `--nnodes`, `--node-rank`, `--master-addr`,
and `--headless` for workers. KServe derives LWS size from `parallelism.pipeline`
when data parallelism is absent and uses `RecreateGroupOnPodRestart`.

GPU requests and TP/PP flags are generated together. Helm rejects inconsistent
arithmetic and duplicate arguments that would override it. For distributed
layouts, this initial implementation requires **TP = GPUs per node, PP = nodes
per replica**. Cross-node tensor-only layouts, expert parallelism, and separate
prefill/decode pools are not implemented. The runtime checks the actual model's
pipeline support and visible GPU count before launching workers. A healthy-leader
Service supplies the distributed AI-Q endpoint; worker pods do not receive API
requests. AI-Q waits for the selected endpoint and restarts when model/topology
configuration changes.

Examples are in `overrides/values-serving-replicated.yaml` and
`overrides/values-serving-distributed.yaml`. The latter is a **configuration
example**, not a validated 4 × 8 B200 result. Change model, resources, network, and
placement together for the actual platform.

For GitOps, put shared topology/cache changes in your variant's values or
`values-global.yaml`. If using another values file, add it **after the profile**
in `extraValueFiles` for `vllm-inference-service`, `aiq-workflow-config`, `aiq`, and
`openshift-ai`. Applying `helm template -f` locally does not configure Argo CD.
Apply the distributed RustFS overlay only to the `rustfs` application's
`extraValueFiles`; storage placement is independent of GPU placement.

## Prepare the platform

1. Prepare sufficient local disk on each cache node at `/var/lib/kserve/models`.
   The operator supplies the local-volume/permissions integration. Label only
   nodes whose disk is ready:

   ```bash
   oc label node <gpu-node> aiq.rhai.redhat.com/model-cache=true
   ```

   Set `global.modelCache.capacity` to cover the current revision plus retained
   rollback revisions. The defaults are 200Gi for NVFP4 and 350Gi for BF16.
   `global.model.size` is a conservative reservation (80Gi or 150Gi), not an
   assertion about the actual artifact size or available disk. Every download
   tests an actual write, checks free bytes with `statvfs`, and verifies checksums.
   Node replacement uses the label selector automatically.

2. Have working NVIDIA drivers, GPU device plugins, and enough distinct eligible
   nodes for `replicas × nodesPerReplica`. Configure CPU/memory/shm for the model.
   The prerequisite gate reports GPUs requested by existing pods and rejects
   layouts that cannot fit after releasing this workload's old GPUs. It also
   rejects missing cache/serving/LWS APIs and network attachment definitions.

3. If using RDMA, provision the physical network, device plugin, NADs, and topology
   labels first. Configure `networkAttachments` (namespace/name), `rdmaResources`
   (extended resource name/count), and NCCL/GLOO/UCX/FI variables. Set `ipcLock`
   only when your platform grants `IPC_LOCK` to the `aiq-model-reader` service
   account. The pattern does not provision a B200 platform, RDMA fabric, or a
   privileged serving SCC. TCP without RDMA is supported at lower throughput.
   The default ingress policy restricts distributed transport to this workload's
   serving pods and HTTP to configured `allowedClientNamespaces` plus readiness
   gates. Ordinary NetworkPolicy may not cover secondary interfaces: verify the
   fabric is isolated and set `secondaryNetworkIsolated: true` for distributed
   layouts with network attachments. Merely creating a NAD is insufficient.

4. Ensure the OpenShift internal image registry is available. The `model-tools`
   application imports the digest-pinned KServe storage-initializer image with
   `PreserveOriginal` and `referencePolicy: Local`. Publication/cache/monitoring
   workloads pull that exact digest through the internal registry. It packages
   Python 3.11.15, boto3 1.38.33, and huggingface-hub 0.32.4; no startup `pip install`
   occurs. The repository's Python entrypoints are mounted from versioned
   ConfigMaps or embedded in the cluster storage-container command. The internal
   registry namespace/digest are explicit `global.modelTools` settings, including
   for disconnected mirrors.

5. Load the new secrets from `values-secret.yaml.template`. Vault generates each
   password once. The administrator is used only by RustFS/bootstrap; publisher
   credentials can read/write model objects; readers can only list/read the
   configured artifact prefix. Serving and cache jobs receive only the reader
   identity. The HF token is used only by the publication Job.

## RustFS and publication

The official chart **1.0.1** is vendored unchanged. Its archive SHA-256 and the
`quay.io/rustfs/rustfs:1.0.1` image digest are recorded in
`charts/all/rustfs/UPSTREAM.md`. Standalone is the default. The distributed overlay
uses four storage nodes with one PVC each and EC:2, required pod anti-affinity,
and a one-pod disruption budget. Choose the storage class, capacity per drive,
CPU/memory, and node placement under `rustfs.*`. Do not change a populated
standalone installation into distributed storage by editing replica count; use
an explicit storage migration and backup procedure.

The image uses UID/GID 10001 and owns `/data` and `/logs` with mode 0750. The
`aiq-rustfs` SCC grants only that identity to the RustFS service account, with no
host mounts, privilege escalation, or capabilities. Cert-manager issues internal
server certificates. Bootstrap copies the CA to the inference/download namespaces
and a 15-minute reconciliation CronJob refreshes trust and IAM credentials. Do not
rotate the root CA without a trust-overlap maintenance procedure; clients reject
untrusted certificates. Credentials and transport are independently verified.
The upstream chart mounts server certificates with `subPath`. The reconciliation
Job fingerprints the mounted public certificates, rolls only the named RustFS
Deployment/StatefulSet when they change, and waits for updated replicas and a
verified HTTPS health response before copying client trust. Its RBAC permits
get/patch only on that workload; Argo preserves its fingerprint annotation.
Standalone storage has a brief interruption during certificate rollout.

RustFS PVCs have Helm keep and Argo `Prune=false,Delete=false` protection; StatefulSet
claims retain the Kubernetes default Retain behavior. Use a StorageClass with
`reclaimPolicy: Retain` when disks must also survive manual claim/namespace deletion.
Namespace deletion is outside Argo's per-resource prune protection. Keep external
backups for durable artifacts; standalone mode has no storage-node redundancy.

Publication resolves the configured full HF commit and inventories every file,
including tokenizer/configuration/code files. It checks upstream LFS SHA-256 or
Git blob SHA-1, uploads to:

```text
s3://aiq-models/snapshots/<HF organization>/<HF model>/<40-character revision>
```

It streams every object back to verify SHA-256, then conditionally writes
`_READY.json`, the manifest and sole availability marker. Failed uploads leave
no availability marker. Retry reuses verified objects and completes the snapshot;
repeat installations verify and reuse a completed publication without HF access.
The bucket versions objects and aborts abandoned multipart uploads after one day.
Logical immutable prefixes are enforced by the publication protocol; administrative
or publisher credential holders can still alter objects. Restrict those credentials.

Change models by pinning a new `global.model.revision` (never `main`) and adjusting
`global.model.size` and model arguments as needed. `publication.scratchSize` bounds
ephemeral staging and requests schedulable disk; publication stages up to
`global.modelTools.transferConcurrency` files at once. Node download jobs run
concurrently; the same setting bounds file-transfer concurrency within each job.

## Order, migration, and rollback

Argo application waves alone do not ensure readiness of separate applications.
The serving application therefore has its own gates: prerequisites → successful
publication Job → immutable `LocalModelCache` → selected-node readiness Job →
serving resource → HTTP health Job. The storage wait also verifies TLS/credentials.
Cache admission must match the exact S3 URI. A fallback download initializer fails
visibly if admission misses the cache. Serving entrypoints require the local
verification marker, so a replacement node cannot serve incomplete files.

Before the first migration, record the previous Git commit and legacy PVC size,
and protect the existing claim before any automated prune:

```bash
oc annotate pvc vllm-inference-service-model-cache -n aiq-inference \
  argocd.argoproj.io/sync-options=Prune=false,Delete=false \
  helm.sh/resource-policy=keep --overwrite
```

`migration.retainLegacyPVC` defaults to true. The retained claim is never mounted
by the new workload. A fresh installation can set it false. Keep the old claim's
size in `migration.legacyPVCSize` and keep its storage class while changing profiles; Kubernetes cannot shrink PVCs.
Publication reads the pinned HF revision, so it does not attach the active claim
on another node. The single-node rolling strategy releases one old replica before
starting its replacement, allowing an intentional maintenance interruption when
all GPUs are occupied. The baseline LWS default similarly uses zero surge and
one unavailable replica group.

To retain a previous cached revision for fast rollback, add it before changing
the selected revision:

```yaml
global:
  modelCache:
    retainedRevisions:
      - hfRepo: nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4
        revision: bee7596271d1495f6992ae224aefde4410e816b8
        size: 80Gi
```

Switching the current model back to that same entry requires removing its duplicate
from `retainedRevisions`. For rollback within the new storage path, restore the
previous profile/revision and keep the newer revision in that list. For rollback
to the old PVC path, restore the saved Git revision of serving/workflow/application
configuration and sync; the old chart remounts the retained claim. When switching
between `InferenceService` and `LLMInferenceService`, the wave-18 backend-switch
Job retires the opposite backend with the same release name in the inference
namespace, only after publication/cache readiness. It waits for foreground
deletion before creating the new backend, releasing occupied GPUs. Its RBAC
allows deleting only these two named serving resources; it cannot delete PVCs.
Set `migration.replaceBackend: false` to require an operator to retire the previous
backend explicitly instead. This transition may interrupt serving.

Remove unused revisions from `retainedRevisions`, then inspect an explicit cleanup:

```bash
python scripts/model-cache.py cleanup --cache <cache-name>
python scripts/model-cache.py cleanup --cache <cache-name> --execute
```

The script refuses cache URIs referenced by any serving resource and deletes only
the named cache CR/node copies. RustFS objects/versions and the old PVC are not
deleted. Delete those only as a separate, intentional storage cleanup after a
backup and after confirming rollback is no longer needed. No scheduled model
revision garbage collection runs.

## Observe and validate

```bash
python scripts/model-cache.py status
oc get jobs -n aiq-inference
oc get localmodelcache -o yaml
oc get pods -n redhat-ods-applications
oc get leaderworkerset -n aiq-inference
```

Publication logs report each verified file and total duration. Cache status reports
each node separately. The metrics exporter exposes `aiq_model_download_seconds`
from completion logs, `aiq_model_cache_copies`, `aiq_serving_pod_ready`, and
`aiq_warm_startup_seconds` (pod start to Ready, including model loading). Missing
timing observations stay absent. Enable OpenShift user-workload monitoring to
scrape the supplied PodMonitors. vLLM supplies request-latency histograms and token
counters; recording rules produce `aiq:request_latency_seconds:p95` and
`aiq:generation_tokens_per_second`.

RHOAI 3.5.1 forces cache jobs into `redhat-ods-applications`. The
`model-cache-platform` application supplies the pinned downloader settings and
preserves GPU tolerations on the platform cache agent and its jobs through a
scoped admission webhook. It has no Kubernetes API token; it mutates only the
named cache DaemonSet/configuration and identified cache pods in that namespace.
The reader SCC allows group 1000 for just the two reader service accounts, without
host access or privilege. Verify the cache files remain readable across namespace
SELinux boundaries before accepting a deployment as ready.

Local checks:

```bash
python -m pytest tests/deploy tests/storage
scripts/test-rustfs.sh
```

The Python tests need pytest, PyYAML, boto3, and huggingface-hub. The RustFS test
uses Podman, temporary files/storage, generated test TLS certificates, a tiny
snapshot fixture, and the actual pinned RustFS/downloader images. It cleans up
its container and temporary directory automatically.

On an installed GPU workload:

```bash
python scripts/verify-serving.py
python scripts/verify-serving.py --restart-pod
python scripts/verify-serving.py --distributed
python scripts/verify-serving.py --distributed --restart-worker
python scripts/verify-serving.py --distributed --aiq-url https://<frontend-route>
```

Restart options delete one serving pod and test recovery and inference afterwards.
The distributed check requires at least two actual GPU nodes and checks both
leader/worker cache mounts, ranks/group size, group recovery policy, model markers,
and current-pod Multi-Attach events. Run it before and after a revision update and
rollback. To test cold replacement, replace a platform-managed node through your
normal infrastructure procedure, apply the cache label to its replacement, wait
for `NodeDownloaded`, and rerun verification. Record hardware type/node count with
the results; B200 and larger topologies are separate validation entries.

## Verification record (2026-10-05)

- Existing profiles, replicated layouts, two-node and four-node layouts rendered;
  68 focused unit/render tests passed. Invalid GPU arithmetic, unsafe overrides,
  missing operators/NADs, cache readiness, checksums, and filesystem checks tested.
- The cluster accepted InferenceService, ServingRuntime, ClusterStorageContainer,
  LLMInferenceService, and LLMInferenceServiceConfig with server-side dry-run.
  No cluster deployment was changed.
- RustFS 1.0.1 passed the local TLS/IAM/publication/interruption/idempotency/integrity/
  concurrent-cache/warm-reuse/cold-copy integration test, using UID/GID 10001,
  a read-only root filesystem, and all capabilities dropped.
- **Not yet validated:** real GPU inference with these new manifests, OpenShift
  cache admission and permissions on leaders/workers, worker failure on two GPU
  nodes, model update/rollback on GPUs, cold physical-node replacement, and an
  end-to-end AI-Q request. The inspected cluster has one GPU node with four GPUs;
  it cannot validate the multi-node path. B200 and 4 × 8 validation are pending.

Sources: [OpenShift AI model caching](https://docs.redhat.com/en/documentation/red_hat_openshift_ai_self-managed/3.5/html/deploying_models/precache_models_for_faster_deployment),
[vLLM 0.24 distributed multiprocessing](https://docs.vllm.ai/en/v0.24.0/serving/parallelism_scaling/),
[Leader Worker Set operator](https://github.com/openshift/lws-operator), and
[RustFS IAM](https://docs.rustfs.com/en/security-compliance/iam/policies).
