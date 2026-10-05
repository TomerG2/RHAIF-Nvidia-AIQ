"""vLLM multiprocessing entrypoint for a KServe LeaderWorkerSet replica."""
import json
import os
from pathlib import Path
import socket
import sys
import time


def command(env, arguments):
    nodes, gpus = int(env["AIQ_NODES"]), int(env["AIQ_GPUS"])
    tensor, pipeline = int(env["AIQ_TP"]), int(env["AIQ_PP"])
    rank = int(env.get("LWS_WORKER_INDEX", "0"))
    if not (nodes == pipeline and gpus == tensor and 0 <= rank < nodes):
        raise ValueError("Unsupported tensor/pipeline layout or invalid worker rank")
    leader = env["LWS_LEADER_ADDRESS"]
    result = ["vllm", "serve", "/mnt/models", "--distributed-executor-backend=mp",
              f"--nnodes={nodes}", f"--node-rank={rank}", f"--master-addr={leader}",
              "--master-port=29500", f"--tensor-parallel-size={tensor}",
              f"--pipeline-parallel-size={pipeline}", "--port=8080",
              "--served-model-name=" + env["AIQ_SERVED_NAME"], *arguments]
    if rank:
        result.append("--headless")
    return result


def validate_runtime():
    import torch
    from vllm.config import ModelConfig
    from vllm.model_executor.models import ModelRegistry

    path = Path("/mnt/models")
    # This marker is only produced after the manifest and every file are verified.
    marker = json.loads((path / ".aiq-ready.json").read_text())
    if marker["uri"] != os.environ["AIQ_MODEL_URI"]:
        raise RuntimeError("Mounted cache is for a different model revision")
    if torch.cuda.device_count() != int(os.environ["AIQ_GPUS"]):
        raise RuntimeError("Visible CUDA device count differs from requested GPUs")
    config = ModelConfig(model=str(path))
    model_info, architecture = ModelRegistry.inspect_model_cls(config.hf_config.architectures, config)
    if int(os.environ["AIQ_PP"]) > 1 and not model_info.supports_pp:
        raise RuntimeError(f"Runtime/model combination does not support pipeline parallelism: {architecture}")


if __name__ == "__main__":
    validate_runtime()
    args = command(os.environ, sys.argv[1:])
    deadline = time.monotonic() + 300
    while True:
        try:
            socket.getaddrinfo(os.environ["LWS_LEADER_ADDRESS"], 29500)
            break
        except socket.gaierror:
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)
    print(json.dumps({"stage": "worker_start", "rank": os.environ.get("LWS_WORKER_INDEX", "0")}), flush=True)
    os.execvp(args[0], args)
