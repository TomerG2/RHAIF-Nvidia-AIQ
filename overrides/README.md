# OpenShift Helm value overlays

Validated Patterns applies these files on top of the AI-Q umbrella chart at
`charts/aiq2-web`. Argo CD loads them through `clusterGroup.applications.aiq.extraValueFiles`
in `values-global.yaml`.

## Configuration layers

| Layer | Files | What it controls |
|---|---|---|
| Pattern globals | `values-global.yaml` | Argo CD mode, Vault, default model IDs, shared applications |
| Serving profile | `variants/<name>/values-<name>.yaml` and `profiles/<name>.yaml` | GPU, checkpoint, vLLM args, shallow Lightning token budgets |
| OpenShift overlays | `overrides/values-openshift-*.yaml` | Platform tweaks for the AI-Q umbrella chart (Route, PVC, workflow mount) |

The `values-openshift-*` overlays adjust how the published AI-Q images run on
OpenShift. Additional topology/storage examples are described below.

## Overlay files

| File | Purpose |
|---|---|
| `values-openshift-base.yaml` | OpenShift Route instead of Ingress, Postgres PVC |
| `values-openshift-hybrid-lightning.yaml` | Mount `config_hybrid_lightning.yml` (in-cluster vLLM + NVIDIA API Ultra) |

Helm list values replace rather than merge. Always apply base plus the hybrid overlay.

`values-serving-replicated.yaml` selects independent replicas;
`values-serving-distributed.yaml` illustrates four nodes with eight GPUs each.
Apply shared serving overrides after the profile for the serving, workflow,
AI-Q, and OpenShift AI applications. `values-rustfs-distributed.yaml` selects
four storage nodes independently and applies only to the RustFS application.
See [the serving guide](../docs/source/deployment/model-storage-and-serving.md)
for prerequisites and the exact GitOps configuration path. These examples do
not provision GPU hardware or physical storage/network infrastructure.

## Credentials

- `NVIDIA_API_KEY` — Nemotron 3 Ultra on the NVIDIA API Catalog (clarifier + deep research).
- In-cluster vLLM (Lightning) does not need a real API key. The workflow config defaults a placeholder token.

Copy `values-secret.yaml.template` to `~/values-secret-aiq.yaml`. `make load-secrets` writes Vault KV under `secret/data/hub/`. See [validated-patterns.md](../docs/source/deployment/validated-patterns.md).
