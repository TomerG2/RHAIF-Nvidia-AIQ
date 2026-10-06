<!--
SPDX-FileCopyrightText: Copyright (c) 2026, Red Hat, Inc.
SPDX-License-Identifier: Apache-2.0
-->

# OpenShift (Validated Patterns)

Red Hat authored this Validated Pattern wrapper for deploying AI-Q on OpenShift
through the [Validated Patterns](https://validatedpatterns.io/learn/) GitOps framework.
Scaffolding was generated with [patternizer](https://validatedpatterns.io/learn/creating-patterns-with-patternizer/). This path is single-cluster only: no ACM hub/spoke. HashiCorp Vault stores secret values. The External Secrets Operator copies them into Kubernetes Secrets named `aiq-credentials` and `huggingface-secret`.

The pattern ships the AI-Q umbrella Helm chart at `charts/aiq2-web` (NGC `aiq-agent` / `aiq-frontend` images) and applies OpenShift value overlays under `overrides/` (see `overrides/README.md`). Blueprint application source lives in the [NVIDIA AI-Q repository](https://github.com/NVIDIA-AI-Blueprints/aiq), not in this pattern repo.

## Deployment profile

The Validated Pattern ships hybrid Lightning (GPU stack + in-cluster vLLM) with selectable serving profiles. Profiles are named by serving config (checkpoint and parallelism), not by GPU SKU. `main.variant` in `values-global.yaml` defaults to `nvfp4`. `./pattern.sh make install PROFILE=<name>` sets `TARGET_VARIANT` and loads `variants/<name>/values-<name>.yaml`, which points vLLM and the workflow chart at `profiles/<name>.yaml`.

| Profile | Install | Checkpoint | Shallow Lightning | Example worker |
|---|---|---|---|---|
| `nvfp4` (default) | `./pattern.sh make install` | NVFP4 (`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4`) | `max_tokens: 1536`, `thinking_token_budget: 512`, vLLM `--max-model-len=4096` | 1× L4 24 GiB (`g6.2xlarge`) |
| `bf16` | `./pattern.sh make install PROFILE=bf16` | BF16 (`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16`) | `max_tokens: 32768`, no thinking budget, vLLM `--max-model-len=65536` | 1× A100 or H100 80 GiB |
| `bf16-tp4` | `./pattern.sh make install PROFILE=bf16-tp4` | same BF16 checkpoint as `bf16` | same token limits as `bf16`, vLLM `--tensor-parallel-size=4` | 4× 24 GiB (`g5.12xlarge`) |

OpenShift overlays stay `values-openshift-base.yaml` + `values-openshift-hybrid-lightning.yaml`. LLM routing is the same for every installable profile: intent + shallow → in-cluster vLLM (Nemotron 3.5 Lightning); clarifier + deep → NVIDIA API Catalog (Nemotron 3 Ultra).

The hybrid backend runs `db-init` before `wait-for-local-model` on every pod
start. Database initialization applies the idempotent `init-db.sql` with
`ON_ERROR_STOP=1`, so missing job tables are created on fresh installations and
SQL errors block startup. Keep both containers when overriding
`initContainers`: Helm replaces this list rather than appending to it.

To add a profile, add `profiles/<name>.yaml` (vLLM args, GPU count, workflow token fields, optional `global.model`), `variants/<name>/values-<name>.yaml` (`clusterGroup.name` and `global.hardwareProfile`), and `profiles/<name>.mk` (Phase 0 GPU defaults or required-SKU flags; Make always includes it when `PROFILE` is set). A top-level `placeholder:` key makes `make install` refuse the profile.

Workflow YAML is mounted from ConfigMap `aiq-workflow-config` (`charts/aiq-workflow-config/files/config_hybrid_lightning.yml`). Shallow `max_tokens`, `thinking_token_budget`, and the in-cluster `model_name` are rendered from the selected profile.

### Lightning thinking and token budgets

Nemotron 3.5 Lightning is a reasoning model: thinking is **on by default** unless
`chat_template_kwargs.enable_thinking` is set. Lightning roles use `_type: openai`
against in-cluster vLLM; pass `chat_template_kwargs` (and `thinking_token_budget`)
under `extra_body`, not as top-level LLM fields — `ChatOpenAI` rejects them in
`model_kwargs`. The hybrid workflow mirrors the NVIDIA catalog Lightning profile
(see the upstream AI-Q `config_cli_default.yml`):

| Role | `enable_thinking` | Notes |
|---|---|---|
| Intent (`nemotron_lightning_intent_llm`) | `false` | Structured JSON; reasoning would consume the 1024-token budget |
| Shallow (`nemotron_lightning_agent_llm`) | `true` | Tool-calling research agent |

**Hosted catalog vs in-cluster OpenShift:** the catalog profile uses
`max_tokens: 32768` for shallow Lightning. The `nvfp4` profile caps vLLM at `--max-model-len=4096` with `--enforce-eager` on a single 24 GiB class card, so shallow uses
`max_tokens: 1536` plus `extra_body.thinking_token_budget: 512` so the prompt, reasoning, and
answer fit in the context window. The `bf16` profile serves the BF16 checkpoint on one GPU that can hold the weights (NVIDIA validates about 256K context on one H100 80GB) with catalog-parity `--max-model-len=65536` and shallow `max_tokens: 32768`, and omits `thinking_token_budget`. vLLM reads quantization from `config.json`; do not pass `--quantization`.

Async jobs report `job_status.status: success` when finished (not `completed`).

## Prerequisites

- An OpenShift cluster and `oc` logged in with enough privilege to install operators.
- [Podman](https://podman.io/) (the `./pattern.sh` wrapper runs make targets in the Validated Patterns utility container).
- A Git remote Argo CD can clone and this branch pushed.
- Local secret file `~/values-secret-aiq.yaml` (see below). `make install` loads it before waiting for Argo health.

### GPU workers (Phase 0, hybrid profile)

The hybrid Lightning profile requires one GPU worker before OpenShift AI and vLLM can serve the model. On AWS clusters with Machine API:

```bash
./pattern.sh make create-gpu-machineset
# or explicitly:
./pattern.sh make create-gpu-machineset GPU_INSTANCE_TYPE=g6.2xlarge GPU_REPLICAS=1
# 80 GiB BF16 profile (pass the SKU you have; there is no single default):
./pattern.sh make create-gpu-machineset PROFILE=bf16 GPU_INSTANCE_TYPE=<80GiB-SKU>
```

If AWS returns `InsufficientInstanceCapacity`, retry with a different availability zone:

```bash
./pattern.sh make create-gpu-machineset OVERRIDE_ZONE=us-east-2b
./pattern.sh make create-gpu-machineset OVERRIDE_ZONE=us-east-2c
./pattern.sh make create-gpu-machineset OVERRIDE_ZONE=us-east-2a
```

On Azure clusters with Machine API, the playbook defaults to **two** GPU workers (`GPU_REPLICAS_AZURE=2`). Use one worker for hybrid Lightning:

```bash
./pattern.sh make create-gpu-machineset-azure GPU_REPLICAS_AZURE=1
```

Verify the GPU node:

```bash
oc get machines -n openshift-machine-api | grep gpu
oc get nodes -l node-role.kubernetes.io/odh-notebook=
```

Phase 0 is complete when at least one GPU worker is `Ready`, labeled `node-role.kubernetes.io/odh-notebook`, and tainted `odh-notebook=true:NoSchedule`. `nvidia.com/gpu` allocatable appears after the GPU Operator syncs (Phase 1).

See [GPU_provisioning.md](https://github.com/validatedpatterns-sandbox/RHAIF-Nvidia-AIQ/blob/main/GPU_provisioning.md) for Azure defaults, manual MachineSet, bare-metal, and verification steps. Clusters without Machine API must add GPU nodes outside GitOps.

The default `nvfp4` profile uses **1× `g6.2xlarge`** (NVIDIA L4, 24 GiB VRAM) with the **NVFP4** checkpoint. RustFS stores the complete pinned snapshot; each serving node has its own warm cache. Model reservations are **80Gi** for NVFP4 and **150Gi** for BF16, with cache capacities of **200Gi** and **350Gi** respectively to accommodate rollback. Prepare local disk and label eligible nodes before installation. See [model storage and serving](model-storage-and-serving.md) for prerequisites, replica/distributed settings, storage classes, and retained-PVC migration.

Default destination namespace is `aiq` (application) and `aiq-inference` (vLLM). Override `clusterGroup.namespaces` and each application's `namespace` in `values-global.yaml` only if your cluster requires different project names.

## Configure secrets

Do not commit secrets. Copy the template out of Git and fill in real values:

```bash
cp values-secret.yaml.template ~/values-secret-aiq.yaml
# Set NVIDIA_API_KEY. Leave DB_USER_PASSWORD unset so Vault generates it once.
```

`~/values-secret-aiq.yaml` is the operator-facing secret file. `backingStore: vault` must match `global.secretStore.backend`. `make load-secrets` writes fields to Vault KV `secret/data/hub/<secret name>`. The `hub` prefix is the ansible `vault_hub` default. Do not set `vaultPrefixes` on secret entries. ESO materializes application/Hugging Face credentials and scoped RustFS administrator, model-publisher, and model-reader credentials in the required namespaces. Leave the new storage passwords unset so Vault generates them once.

| Field | Purpose |
|---|---|
| `DB_USER_NAME`, `DB_USER_PASSWORD` | In-cluster Postgres. Leave `DB_USER_PASSWORD` unset in your copy so Vault generates it once. Re-running `load-secrets` does not rotate it. |
| `NVIDIA_API_KEY` | Nemotron 3 Ultra (clarifier + deep research) on NVIDIA API Catalog |
| `TAVILY_API_KEY` | Web search (optional for install-only smoke tests) |
| `VLLM_API_KEY` | Optional. Omit from `~/values-secret-aiq.yaml` to use workflow default (`local-vllm`) |
| `AIQ_LIGHTNING_BASE_URL` | Optional. Omit to use the in-cluster vLLM service URL baked into the workflow config |
| `hftoken` in `huggingface-secret` | Optional Hugging Face token if model download is gated |

In-cluster vLLM does not authenticate callers. The hybrid workflow config supplies a default placeholder token so you do not need a real key for Lightning roles. `NVIDIA_API_KEY` is only for Ultra roles that call the NVIDIA API Catalog.

`./pattern.sh make install` (and `./pattern.sh make load-secrets`) looks for `~/values-secret-aiq.yaml` before falling back to the in-repo template. Vault does not write Kubernetes Secrets into workload namespaces. Argo CD creates those namespaces. ESO creates the Secret objects after the `eso-bindings` applications sync.

`make install` and `make load-secrets` automatically order generated password
fields first in memory before invoking the utility Vault loader. This preserves
existing passwords even with older private secret files: the utility writes the
first field with `vault kv put` and later fields with `patch`, so a static first
field would otherwise erase the password before its generation check. The input
file remains unchanged. Explicit `override: true` still requests rotation.
Use the repository Make targets; invoking the upstream secret-loading playbook
directly bypasses this protection. New GPU nodes receive the existing credentials
through External Secrets and download the published snapshot from RustFS.

Encrypt `~/values-secret-aiq.yaml` with `ansible-vault encrypt` if you want it encrypted at rest.

NGC images on this overlay are public enough for many clusters; add an image-pull secret if your cluster cannot pull `nvcr.io/nvidia/blueprint/*`.

## Install

From the repository root, on the branch Argo CD should track:

```bash
./pattern.sh make validate-prereq
./pattern.sh make validate-cluster
./pattern.sh make show
./pattern.sh make install
# or: ./pattern.sh make install PROFILE=bf16
./pattern.sh make argo-healthcheck
```

`make install` installs the Validated Patterns Operator, OpenShift GitOps, and a `Pattern` custom resource. It waits for Vault, unseals it, and runs `load-secrets` (`global.secretStore.backend: vault`). Argo CD syncs applications in sync-wave order:

```text
wave -30: vault (HashiCorp Vault)
wave -20: golang-external-secrets (External Secrets Operator and ClusterSecretStore vault-backend)
wave -1:  aiq-workflow-config (workflow ConfigMap)
wave 5:   ESO application/storage credentials and internal model-tools image mirror
wave 10:  nfd-config, nvidia-config (GPU operator enablement)
wave 15:  openshift-ai (RHOAI 3.5.1, KServe and node caches), LeaderWorkerSet operand
wave 16:  rustfs (TLS, persistent storage, bucket and scoped IAM bootstrap)
wave 20:  vllm-inference-service (prerequisites, publication, cache readiness, serving)
wave 30:  aiq (umbrella Helm chart)
```

`eso-bindings` applications set `SkipDryRunOnMissingResource=true` so the first sync can wait for the ExternalSecret CRD. `clusterGroup.isHubCluster: true` selects kubernetes-auth `mountPath: hub` and role `hub-role` for every serving profile. The default Pattern cluster group name is `nvfp4`.

`make install` always provisions OpenShift GitOps (`vp-gitops`) when it is missing. `values-global.yaml` sets `global.singleArgoCD: true` so clustergroup Applications are created in that instance instead of a second Argo CD in `aiq-prod`.

## Upgrading from MaaS Granite or older overlays

Earlier pattern revisions used the MaaS Granite profile (`aiq-maas-config`, `values-aiq-openshift.yaml`,
`config_maas_granite.yml`). The current tree ships hybrid Lightning only. After you push this branch and Argo CD
syncs:

1. Delete the legacy Argo CD Application `aiq-maas-config` if it still exists.
2. Delete ConfigMap `aiq-maas-config` in namespace `aiq` when `aiq-workflow-config` is healthy.
3. Update `~/values-secret-aiq.yaml`: remove `OPENAI_API_KEY`, `MAAS_MODEL_NAME`, and
   `AIQ_INFERENCE_BASE_URL` if present; set `NVIDIA_API_KEY` and re-run `./pattern.sh make load-secrets`.
4. Provision a GPU worker (Phase 0) before expecting shallow Lightning traffic to succeed.

Fresh installs can ignore this section.

## Validate

### Infrastructure

The DataScienceCluster enables KServe with headed RawDeployment only. Dashboard, workbenches, data
science pipelines, Kueue, and Ray stay Removed. The pattern does not install the OpenShift Serverless
operator.

```bash
oc get csv -n redhat-ods-operator
oc get csv -n nvidia-gpu-operator
oc get datasciencecluster default-dsc -o jsonpath='{.spec.components.kserve.managementState}{"\n"}'
oc describe node -l node-role.kubernetes.io/odh-notebook= | grep -A2 'nvidia.com/gpu'
oc get inferenceservice -n aiq-inference
oc get pods -n aiq-inference -o wide
```

Wait until the InferenceService is ready before shallow Lightning smoke tests. Model download and GPU
scheduling can take tens of minutes after Argo syncs wave 20:

```bash
oc wait --for=condition=Ready inferenceservice/vllm-inference-service -n aiq-inference --timeout=45m
```

From a debug pod in `aiq`, confirm the vLLM OpenAI API responds:

```bash
curl -sf http://vllm-inference-service-predictor.aiq-inference.svc.cluster.local/v1/models
```

If shallow research fails while `aiq-backend` is healthy, check that the InferenceService above is `Ready`
and that `NVIDIA_API_KEY` is set for Ultra roles.

### AI-Q health

```bash
oc get pods,pvc -n aiq
oc -n aiq port-forward svc/aiq-backend 8000:8000
# in another terminal:
curl -sf http://127.0.0.1:8000/live && echo
curl -sf http://127.0.0.1:8000/health && echo
```

Frontend Ingress is disabled on OpenShift. The overlay enables an OpenShift Route for `svc/aiq-frontend` on port 3000; use `oc get route -n aiq` to find the UI URL.

### Agent smoke proof (hybrid routing)

With the backend port-forwarded (`oc -n aiq port-forward svc/aiq-backend 8000:8000`):

```bash
curl -sf http://127.0.0.1:8000/live && echo
curl -sf http://127.0.0.1:8000/health && echo
curl -sf http://127.0.0.1:8000/v1/models || true
```

Confirm routing in backend or vLLM predictor logs: shallow traffic to `aiq-inference`, deep traffic to `integrate.api.nvidia.com`. For application-level research APIs, use the NVIDIA AI-Q client docs against the same backend URL.
