# Deploy and use NVIDIA AI-Q on a demo platform

Use your AI harness to deploy the Validated Pattern on the demo platform's
OpenShift cluster, then run research through AI-Q.

- [This pattern repository](https://github.com/validatedpatterns-sandbox/RHAIF-Nvidia-AIQ)
- [Upstream NVIDIA AI-Q project](https://github.com/NVIDIA-AI-Blueprints/aiq)

## 1. Have these ready

- An OpenShift cluster on the demo platform, with `oc` logged in as cluster-admin.
- Podman installed and this repository cloned; the deployment branch pushed to
  a Git remote Argo CD can read.
- Persistent storage and a working OpenShift internal image registry.
- NVIDIA API Catalog and Tavily API keys for research.

Copy the secrets template, then fill in `NVIDIA_API_KEY` and `TAVILY_API_KEY`:

```bash
cp values-secret.yaml.template ~/values-secret-aiq.yaml
```

Keep this file outside Git. Leave generated database and storage passwords unset;
the installer creates them in Vault. Add a Hugging Face token only if required.

## 2. Ask your AI harness to deploy

### Case A: Provision GPUs on AWS

The cluster needs Machine API and AWS quota/capacity for one
[`g6.12xlarge` worker with four NVIDIA L4 GPUs](https://docs.aws.amazon.com/ec2/latest/instancetypes/ac.html).
Use `bf16-tp4` for tensor parallelism across those four GPUs.

> Provision one AWS OpenShift GPU worker with four NVIDIA L4 GPUs
> (`g6.12xlarge`). Follow `GPU_provisioning.md`, use a 500 GiB root volume,
> prepare and verify 350 GiB of usable model-cache disk at
> `/var/lib/kserve/models`, and apply the required GPU/cache labels and taint.
> Then run `./pattern.sh make install PROFILE=bf16-tp4` on this cluster.

### Case B: Use four existing B200 nodes, eight GPUs each

The working cluster already has 32 GPUs. The `b200` profile runs four copies of
the BF16 model, one per node, with TP=8 and PP=1. It allows a 262,144-token
context and a 65,536-token shallow output budget. B200 startup and performance
still require validation on the actual hardware.

> Install the pattern on this existing OpenShift cluster using its four B200
> nodes with eight GPUs each. Use `PROFILE=b200`: four replicas, one per node,
> with TP=8 within each node and PP=1. Confirm model/runtime support and the
> `nvidia.com/gpu.product=NVIDIA-B200` label. Prepare and verify 350 GiB of usable
> model-cache disk at `/var/lib/kserve/models` and required labels on all four
> nodes. Validate the configuration and push the deployment branch.
> Then run `./pattern.sh make install PROFILE=b200`.

The installer deploys the GPU stack, OpenShift AI, model storage, vLLM, and AI-Q
through GitOps. Initial model download and startup can take tens of minutes.

## 3. Ask it to verify

> Check Argo CD health, model-cache status, and serving readiness. Confirm four
> GPUs on the L4 worker or eight GPUs on each of the four B200 nodes, depending
> on the deployment. Run “What is the capital of France?” with both
> `shallow_researcher` and `deep_researcher`. Show both complete answers, judge
> whether they make sense, and return the HTTPS AI-Q frontend URL.

## 4. Use AI-Q

Open the frontend URL and enter a research question, for example:

> Compare Kubernetes Deployments and StatefulSets, with sources.

- **Shallow research:** uses in-cluster Nemotron Lightning.
- **Deep research:** uses NVIDIA API Catalog Nemotron Ultra; clarifying questions
  also use the catalog.

For configuration changes, ask your harness to update the pattern values in Git
and push the deployment branch; Argo CD applies them.

Details: [deployment](docs/source/deployment/validated-patterns.md),
[GPU provisioning](GPU_provisioning.md), and
[model storage](docs/source/deployment/model-storage-and-serving.md).
