#!/usr/bin/env python3
"""Repository skill entrypoint for the shared benchmark runner."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts"))
from vllm_performance import main

if __name__ == "__main__":
    sys.exit(main())
