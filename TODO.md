# Deployment follow-ups

## `bf16` on-cluster verification

**Status:** profile is in git (`./pattern.sh make install PROFILE=bf16`). Cluster checks below are still open.

`profiles/bf16.yaml` serves the BF16 checkpoint
`nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-BF16` with catalog-parity token limits
on one GPU that can hold the weights. The `nvfp4` profile remains the quantized bootstrap.

### Checklist

- [ ] Provision an 80 GiB worker: `./pattern.sh make create-gpu-machineset PROFILE=bf16 GPU_INSTANCE_TYPE=<80GiB-SKU>`
- [ ] `./pattern.sh make install PROFILE=bf16` on a branch Argo CD can clone
- [ ] `oc wait --for=condition=Ready inferenceservice/vllm-inference-service -n aiq-inference`
- [ ] `curl` the vLLM `/v1/models` endpoint and confirm `max_model_len` is `65536` and the served id is `nemotron-3.5-lightning-30b-a3b-bf16`
- [ ] Run shallow-research smoke via `skills/aiq-research/scripts/aiq.py`

### Profile values (reference)

| Component | `nvfp4` | `bf16` |
|---|---|---|
| Checkpoint | NVFP4 | BF16 |
| vLLM `--max-model-len` | `4096` | `65536` |
| vLLM `--max-num-batched-tokens` | `8192` | `32768` |
| `nemotron_lightning_agent_llm.max_tokens` | `1536` | `32768` |
| `extra_body.thinking_token_budget` | `512` | omitted |
| Model-cache PVC | `80Gi` | `150Gi` |
| Example GPU | L4 24 GiB | H100/A100 80 GiB |
