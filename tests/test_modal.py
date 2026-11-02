"""Run once with: modal run tests/test_modal.py"""

import modal

app = modal.App("meta-hackathon-gpu-smoke")
image = modal.Image.debian_slim(python_version="3.12")


@app.function(image=image, gpu="T4", timeout=120, retries=0, max_containers=1)
def gpu_info() -> str:
    import subprocess

    return subprocess.check_output(["nvidia-smi"], text=True, timeout=30)


@app.local_entrypoint()
def main():
    output = gpu_info.remote()
    print(output, flush=True)
    if "T4" not in output:
        raise RuntimeError("FAIL: nvidia-smi output did not identify a T4 GPU")
    print("PASS: Modal T4 nvidia-smi (one remote invocation).", flush=True)
