"""Minimal integration check: no training, no benchmark."""
import json
import os
from pathlib import Path
import subprocess

output = Path(os.environ["MUSE_OUTPUT_DIR"])
gpu = subprocess.check_output(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"], text=True).strip()
(output / "result.json").write_text(json.dumps({"gpu": gpu, "status": "ok", "purpose": "connection smoke only"}), encoding="utf-8")
print("GPU connection:", gpu)
