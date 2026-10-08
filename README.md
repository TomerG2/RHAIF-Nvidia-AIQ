# NVIDIA AI-Q on OpenShift (Validated Pattern)

OpenShift [Validated Pattern](https://validatedpatterns.io/) that deploys the
[NVIDIA AI-Q Blueprint](https://github.com/NVIDIA-AI-Blueprints/aiq) with GitOps.
This repository is the pattern wrapper (values, charts, overlays, GPU provisioning).
It does not contain AI-Q application source. Runtime images come from NGC
(`nvcr.io/nvidia/blueprint/aiq-agent:2.2.0` and `aiq-frontend:2.2.0`).

## What this pattern deploys

| Component | How |
|---|---|
| NFD + NVIDIA GPU Operator config | `charts/all/nfd-config`, `charts/all/nvidia-gpu-config` |
| OpenShift AI (KServe serving only) | `charts/all/rhods` |
| In-cluster vLLM (Nemotron 3.5 Lightning) | `charts/all/vllm-inference-service` |
| Hybrid workflow ConfigMap | `charts/aiq-workflow-config` |
| AI-Q backend + frontend | `charts/aiq2-web` + `overrides/` |

Profile: intent + shallow research on in-cluster vLLM; clarifier + deep research on
NVIDIA API Catalog (Nemotron 3 Ultra).

Model artifacts are published to a TLS-protected RustFS store and cached on each
selected serving node. Profiles support one-node serving, independent replicas,
and configurable tensor/pipeline distribution through LeaderWorkerSet. See
[model storage and serving](docs/source/deployment/model-storage-and-serving.md)
for disk preparation, topology settings, migration, rollback, and validation status.

## Deploying the demo

Prerequisites: OpenShift cluster with `oc` logged in (cluster-admin or equivalent);
[Podman](https://podman.io/) for `./pattern.sh`; this branch pushed to a Git remote
Argo CD can clone.

### Configure secrets

```bash
cp values-secret.yaml.template ~/values-secret-aiq.yaml
# Fill in keys. Do not commit this file.
```

`./pattern.sh make install` loads `~/values-secret-aiq.yaml` into Vault.

### Provision a GPU node (required before install)

See [GPU_provisioning.md](GPU_provisioning.md) for AWS/Azure MachineSet steps and
for labeling an existing GPU node.
Prepare local cache disk and apply the cache-node label described in the
[storage prerequisites](docs/source/deployment/model-storage-and-serving.md#prepare-the-platform)
before installation.

### Deploy the pattern

```bash
./pattern.sh make install
# BF16 / catalog token limits:
./pattern.sh make install PROFILE=bf16
# Same BF16 limits, tensor-parallel across 4 GPUs:
./pattern.sh make install PROFILE=bf16-tp4
# B200 profile:
./pattern.sh make install PROFILE=b200
```

`PROFILE` selects `variants/<name>/` and `profiles/<name>.yaml`. The default is `nvfp4` (NVFP4 checkpoint, small token budgets). `bf16-tp4` is the `bf16` checkpoint with `--tensor-parallel-size=4`.

### Check readiness

```bash
oc wait --for=condition=Ready inferenceservice/vllm-inference-service -n aiq-inference --timeout=45m
oc get route -n aiq
python scripts/model-cache.py status
```

Open the frontend Route and try a shallow research query. For upgrade, uninstall,
and troubleshooting, see
[docs/source/deployment/validated-patterns.md](docs/source/deployment/validated-patterns.md).

## License

This Validated Pattern wrapper is copyright Red Hat, Inc., and licensed under
Apache-2.0. See [LICENSE](LICENSE). AI-Q application source, NGC containers, and
upstream blueprint code remain under their NVIDIA licenses.
