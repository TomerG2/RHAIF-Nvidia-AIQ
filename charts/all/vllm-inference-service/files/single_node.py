"""Start vLLM only from a verified cache, including on replacement nodes."""
import json
import os
from pathlib import Path
import sys


if __name__ == "__main__":
    marker = json.loads(Path("/mnt/models/.aiq-ready.json").read_text())
    if marker["uri"] != os.environ["AIQ_MODEL_URI"]:
        raise RuntimeError("Mounted cache does not match the selected model revision")
    # vLLM validates architecture, quantization, tensor dimensions, and memory.
    os.execvp("python", ["python", "-m", "vllm.entrypoints.openai.api_server", *sys.argv[1:]])
