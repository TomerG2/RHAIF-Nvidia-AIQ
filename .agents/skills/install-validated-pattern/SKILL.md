---
name: install-validated-pattern
description: >-
  Install this repository's NVIDIA AI-Q Validated Pattern on OpenShift: discover
  NVIDIA GPUs, select or propose an adapted serving profile, offer an AWS worker
  when GPUs are absent, deploy through GitOps, then run E2E and vLLM performance
  verification sequentially. Use for installing or deploying this pattern;
  existing installations require developer direction before proceeding.
---

# Install the AI-Q Validated Pattern

Run from this repository's root, using the current authenticated `oc` context
unless the developer specifies another cluster. Resolve paths relative to the
checkout, not a particular user's home directory.

The sequence is discovery → approved preparation → installation and readiness →
E2E verification → performance test. Finish each stage before starting the next.
An invocation to install authorizes the normal installer and verification; the
approval points below cover additional decisions agreed with the developer.

## 1. Discover the cluster and NVIDIA GPUs

Read [QUICKSTART.md](../../../QUICKSTART.md) and the
[deployment guide](../../../docs/source/deployment/validated-patterns.md).
Record the cluster context/API, cloud platform, repository remote and branch,
and local revision/dirty status. Verify `oc` authentication and sufficient
permissions, Podman, and an Argo-readable deployment source. Do not print tokens
or credential-bearing remote URLs. An access error is not evidence of absence.

Start with read-only inventory, for example:

```bash
oc config current-context
oc whoami --show-server
oc get infrastructure cluster -o json
oc get nodes -o json
```

For each candidate node, collect GPU product, physical count and memory when
available, node Ready/schedulable status, CPU/RAM, taints, placement labels, GPU
capacity/allocatable, and GPU reservations by existing workloads. Allocatable
is not the number of GPUs currently free. Account for MIG/time-sharing rather
than treating advertised slices as independent full GPUs.

Use GPU Operator/NFD labels and existing NVIDIA tooling for hardware evidence.
If these are absent, inspect Machine API/provider instance types and available
hardware evidence. Missing `nvidia.com/gpu` can mean the driver/device plugin has
not been installed; the pattern installs that stack. Distinguish **no NVIDIA
hardware**, **hardware present but not registered**, and **unknown**. Resolve
unknown inventory before selecting hardware or offering additional capacity.
Recheck actual GPU registration after installation.

Also inspect Pattern CRs, Argo applications and AI-Q workloads in `aiq` and
`aiq-inference` (and any discovered custom namespaces). A shared operator or
empty namespace alone does not establish an AI-Q installation. If an existing
or partial AI-Q pattern deployment is found, summarize the evidence, ask the
developer what to do, and wait. Do not choose upgrade, reinstall, cleanup, repair,
or testing on their behalf. This applies to resuming an earlier partial install.

## 2. Offer a worker if no NVIDIA GPUs are present

Read [GPU_provisioning.md](../../../GPU_provisioning.md). Propose **one AWS
`g6.12xlarge` worker with four NVIDIA L4 GPUs**, a 500 GiB root volume, and
`bf16-tp4`. The instance specification is documented by
[AWS](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html); verify current
SKU specifications and region availability when preparing the proposal.

Explain the target cluster, region/zone, instance count/type, storage and added
cloud resource cost. Prepare the exact provisioning command and **wait for the
developer's approval before creating or scaling anything**. Do not invent prices.
For an AWS cluster with Machine API, the starting command is:

```bash
./pattern.sh make create-gpu-machineset PROFILE=bf16-tp4 \
  GPU_REPLICAS=1 GPU_INSTANCE_TYPE=g6.12xlarge \
  GPU_COUNT=4 GPU_VCPU=48 GPU_MEMORY_MB=196608 GPU_ROOT_VOLUME_SIZE=500
```

Pass an approved `OVERRIDE_ZONE` only when needed. Explicit overrides matter:
the generic default provisions one L4, and `bf16-tp4.mk` has a different GPU SKU.
Inspect existing Machines/MachineSets so a retry cannot silently create duplicate
workers. On a non-AWS cluster or one without Machine API, explain why this AWS
worker cannot simply be attached and ask the developer how to proceed; do not
provision an unrelated standalone instance.

After approval, provision once and wait for the Machine and node to become
Running/Ready, then repeat hardware discovery. Report capacity/quota failures
and ask before changing the approved zone/SKU/count or retrying provisioning.

## 3. Match the profile; obtain acceptance for adaptations

Inspect `profiles/`, `variants/`, [Makefile](../../../Makefile), and
[model storage and serving](../../../docs/source/deployment/model-storage-and-serving.md).
Match **GPU model, memory, counts per node and available nodes**, not the product
name alone. Use these existing starting points, verifying their current contents:

- Four L4 GPUs on one node: `bf16-tp4` (TP=4).
- One L4: `nvfp4`, with its smaller context/output budgets.
- A supported single 80 GiB GPU: `bf16`, subject to model/runtime compatibility.
- Four nodes with eight B200 GPUs each: `b200`, four independent replicas, TP=8,
  PP=1. This preset requires real hardware validation; configuration tests do not
  establish runtime or performance support.

There is no assumed profile literally named `l4`. Other NVIDIA hardware or
layouts require a proposed adaptation. Prefer a homogeneous eligible node pool;
do not mix GPU models in one parallel group or evict unrelated GPU consumers.
If no compatible capacity is available, explain the constraint and ask for a
capacity/allocation decision instead of forcing an unschedulable deployment.

Prepare the proposed changes locally and validate them before asking for
acceptance. Show the diff and explain selected nodes, GPUs per node, replicas,
TP/PP, model/runtime compatibility, context/output budgets and cache capacity.
Adapt placement, resources and token limits together. Keep shared presets intact
when a new deployment-specific profile/variant is appropriate. A new profile
needs `profiles/<name>.yaml`, `profiles/<name>.mk`, and its matching variant;
include provider values where the selected platform needs them.

Ensure variants and all affected Argo applications actually load the adapted
values. For distributed layouts, follow the supported TP=GPUs-per-node and
PP=nodes-per-replica constraints. Do not infer cross-node support from GPU count.
Render the affected charts in their real values order and run relevant existing
checks in `tests/deploy/`; local rendering alone does not publish GitOps changes.

**Ask the developer to accept the concrete adaptation before deploying it.**
Explain that Argo CD must be able to fetch these changes. Show the intended files,
remote and branch and **ask for approval before committing or pushing**. Profile
acceptance and commit/push approval may be collected together if both actions
are explicit. Preserve unrelated edits; do not include them in a deployment
commit. Reuse approval for unchanged actions already authorized in this session.
If the developer publishes the changes, verify the remote revision before install.
An unchanged profile on an already published branch needs no new Git approval.

## 4. Resolve credentials and platform prerequisites

Reuse credentials already supplied in the conversation, an explicitly provided
local file, the environment, or `~/values-secret-aiq.yaml`. Check presence without
printing values; empty values and template placeholders do not count as supplied.
Request only missing credentials from this list, together in one concise prompt:

- `NVIDIA_API_KEY`: required for hosted clarification/deep-research models.
- `TAVILY_API_KEY`: required for this workflow's research verification.
- `hftoken`: ask only if absent and the selected model requires authenticated or
  gated Hugging Face access. Public model access does not require a token.

Never ask again for a supplied value unless authentication demonstrates it is
unusable. Direct the developer to put missing values in a local secret file,
then recheck it; do not require secrets in chat. If a supplied value exists only
in the environment/session, ensure it reaches the installer's secret file using
safe structured handling, without echoing it or putting it in command arguments.

Use [values-secret.yaml.template](../../../values-secret.yaml.template) for
schema/field mapping. Preserve an existing file and generated passwords. Keep
secret files outside Git with restricted permissions; never stage them. Let
Vault generate DB and RustFS passwords, and keep the local-vLLM placeholder.
The RustFS `AWS_*` fields are internal object-store credentials, not cloud keys.
Do not expand the credential questionnaire beyond the three keys above. If
cluster/Git/registry access is unavailable, report the failing prerequisite so
the developer can configure access externally.

Let the normal installer manage its included operators and application resources.
Check existing operator configuration for conflicts instead of replacing shared
configuration. Inspect storage classes, internal registry availability and each
selected node's usable disk under `/var/lib/kserve/models`. The default cache
capacity is 350Gi for BF16 or 200Gi for NVFP4; a configured capacity or 500 GiB
root volume does not prove that much space is free.

For additional node labels/taints, directories, disk preparation, or network
changes, prepare a concrete plan and obtain approval before applying it. Include
any destructive disk operation explicitly in that approval. Label a node
`aiq.rhai.redhat.com/model-cache=true` only after verifying its cache disk.
Set `global.serving.nodeNames` when needed to limit placement/cache checks to the
approved nodes. Fold configuration changes back into the profile/Git review.

## 5. Install and wait for readiness

Recheck that the context, inventory, accepted configuration and published source
still match the plan. Use the repository wrapper and secret loader:

```bash
./pattern.sh make validate-prereq
./pattern.sh make validate-cluster
./pattern.sh make show
./pattern.sh make install PROFILE=<accepted-profile>
./pattern.sh make argo-healthcheck
```

Replace the placeholder with the accepted profile; stop on a failed command.
The repository installer initializes Vault and preserves generated passwords;
do not bypass it with the upstream secret-loading playbook.

Verify Argo sync/health and record its resolved deployed revision(s), separately
from local HEAD. Wait for model publication/cache readiness, all serving replicas
and workers, and the AI-Q backend/frontend. Use `python3 scripts/model-cache.py
status` and the serving kind selected by the profile (`InferenceService` for
single-node replicas; `LLMInferenceService` for distributed serving). The guide's
45-minute inference-readiness timeout is the starting bound; do not wait forever.
Verify the real allocated GPUs and model endpoint after drivers register.

On install/readiness failure or timeout, collect focused events, status and logs,
preserve artifacts, explain the blocker and stop. Do not automatically repair,
roll back, delete resources or rerun installation. Ask the developer for direction.

## 6. Run E2E, then performance, with a pass gate

1. Read and follow [e2e-verification](../e2e-verification/SKILL.md). Use its
   existing launch, doctor, paired-run and cleanup helpers. Preserve launch
   environment/state across commands. Submit the same default question, “What is
   the capital of France?”, to shallow and then deep research unless overridden.
2. Wait for both jobs to finish. Preserve their complete final responses, inspect
   each answer, and write the required reasonableness verdict. Both jobs must
   succeed, both responses must be nonempty, and both must make sense. Script
   exit status alone is insufficient. Finish cleanup of this run's jobs and
   port-forward without touching other runs.
3. **Only after E2E passes and is finished**, read and follow
   [vllm-performance-test](../vllm-performance-test/SKILL.md). Run its existing
   runner with default workloads (100 requests at concurrency 1, then 8). If
   multiple leaders are ready, explicitly select a representative leader with
   `--pod` and report that scope; a single-leader benchmark is not total cluster
   throughput. All serving pods must be ready before benchmarking.
4. Wait for the performance runner and inspect its validity probes, results and
   provenance. Preserve the skill's comparison rules and metrics; do not invent
   a performance pass threshold. Do not overlap these tests or start a second
   run against the same serving deployment while an earlier run remains active.

If E2E fails, times out, is interrupted, or either answer fails the reasonableness
check, stop and report **performance skipped because E2E did not pass**. Do not
automatically repair or retry. Likewise preserve performance failures without
rerunning; an interrupted remote benchmark may remain active until its deadline.

## Report

State the cluster, discovered/allocated GPU hardware, selected profile and any
accepted adaptation, deployed revision(s), installation/readiness status and
HTTPS frontend Route. Report E2E and performance separately, including skipped
stages and the reason. Link the E2E `responses.md` and verdict, and the performance
`summary.md`; retain full artifacts in the existing ignored run directories.
Include the benchmark metrics/provenance required by its skill. Claim successful
completion only when installation, E2E and performance have all succeeded.
