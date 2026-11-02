"""Synthetic PyTorch training example, NOT a model-quality evaluation."""
import argparse
import json
import os
from pathlib import Path
import time
import torch
from muse_telemetry import phase

parser = argparse.ArgumentParser()
parser.add_argument("--steps", type=int, default=100)
args = parser.parse_args()
torch.manual_seed(7)
device = "cuda"
model = torch.nn.Linear(8, 1).to(device)
optimizer = torch.optim.Adam(model.parameters(), lr=.01)
x = torch.randn(256, 8, device=device)
y = x.sum(dim=1, keepdim=True)
phase(representation="synthetic-8d", objective="MSE smoke", seed=7, fold=0, epoch=0, batch_size=256)
started = time.monotonic()
for step in range(args.steps):
    phase(step=step)
    optimizer.zero_grad()
    loss = ((model(x)-y)**2).mean()
    loss.backward()
    optimizer.step()
torch.cuda.synchronize()
elapsed = time.monotonic()-started
output = Path(os.environ["MUSE_OUTPUT_DIR"])
torch.save(model.state_dict(), output / "synthetic-checkpoint.pt")
(output / "metrics.json").write_text(json.dumps({"loss": loss.item(), "steps": args.steps, "elapsed_s": elapsed, "steps_per_s": args.steps/elapsed, "claim": "synthetic execution only; generalization NOT VERIFIED"}), encoding="utf-8")
print("Synthetic training completed; this is not an evaluation of a real model.")
