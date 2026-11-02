"""Copied into each approved Modal image. No local credentials are injected."""
import contextlib
import csv
import io
import json
import math
import os
from pathlib import Path
import runpy
import statistics
import subprocess
import sys
import threading
import time
import traceback
import zipfile

_context = {k: None for k in ("representation", "objective", "seed", "fold", "epoch", "step", "group_id", "group_images", "batch_size")}
_sampler = None


def phase(**values):
    """Optional training hook: from muse_telemetry import phase; phase(fold=0, step=1)."""
    if _sampler and "fold" in values and values["fold"] != _context["fold"]:
        _sampler.sample()
        torch = sys.modules.get("torch")
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.reset_peak_memory_stats()
    _context.update({key: value for key, value in values.items() if key in _context})


class Sampler:
    def __init__(self, root, run_id):
        self.root, self.run_id = root, run_id
        self.stop = threading.Event()
        self.rows = []
        self.lock = threading.RLock()
        self.previous_host = None

    def sample(self):
        with self.lock:
            row = {"timestamp": time.time(), "run_id": self.run_id, **_context,
                   "source_gpu": "nvidia-smi", "source_host": "procfs", "source_torch": "same-process torch.cuda"}
            fields = ["gpu_util_pct", "nvml_vram_used_mib", "nvml_vram_free_mib", "gpu_temp_c", "gpu_power_w", "gpu_power_limit_w", "gpu_clock_mhz", "gpu_mem_clock_mhz", "gpu_total_mib", "pstate", "throttle_reasons"]
            query = "utilization.gpu,memory.used,memory.free,temperature.gpu,power.draw,power.limit,clocks.sm,clocks.mem,memory.total,pstate,clocks_event_reasons.active"
            try:
                values = subprocess.check_output(["nvidia-smi", "--query-gpu="+query, "--format=csv,noheader,nounits"], timeout=4, text=True).splitlines()[0].split(",")
                for key, value in zip(fields, values):
                    try:
                        row[key] = float(value.strip())
                    except ValueError:
                        row[key] = value.strip()
            except Exception:
                row.update({key: None for key in fields})
            try:
                mem = {line.split(":")[0]: float(line.split()[1])/1024 for line in Path("/proc/meminfo").read_text().splitlines() if ":" in line and len(line.split()) > 1}
                row["host_ram_used_mib"] = mem["MemTotal"]-mem["MemAvailable"]
                row["host_ram_available_mib"] = mem["MemAvailable"]
                for line in Path("/proc/self/status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        row["process_rss_mib"] = float(line.split()[1])/1024
                row["host_load_1m"] = os.getloadavg()[0]
                now=time.monotonic()
                ticks=[float(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
                process_stat=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
                process_cpu=(float(process_stat[11])+float(process_stat[12]))/os.sysconf('SC_CLK_TCK')
                io_stat={line.split(':')[0]:float(line.split()[1]) for line in Path('/proc/self/io').read_text().splitlines()}
                current=(now,sum(ticks),ticks[3]+ticks[4],process_cpu,io_stat.get('read_bytes',0),io_stat.get('write_bytes',0))
                if self.previous_host:
                    old=self.previous_host
                    seconds=now-old[0]
                    total=current[1]-old[1]
                    row['cpu_util_pct']=(1-(current[2]-old[2])/total)*100 if total>0 else None
                    row['process_cpu_pct']=(current[3]-old[3])/seconds*100 if seconds>0 else None
                    row['disk_read_mbps']=(current[4]-old[4])/seconds/1048576 if seconds>0 else None
                    row['disk_write_mbps']=(current[5]-old[5])/seconds/1048576 if seconds>0 else None
                self.previous_host=current
            except Exception:
                pass
            row.update({"windows_dedicated_gpu_mib": None, "windows_shared_gpu_mib": None})
            for name in ('cpu_util_pct','process_cpu_pct','disk_read_mbps','disk_write_mbps'):
                row.setdefault(name,None)
            torch_fields = ("torch_allocated_mib", "torch_reserved_mib", "torch_max_allocated_mib", "torch_max_reserved_mib", "torch_cuda_free_mib", "torch_cuda_total_mib")
            row.update({key: None for key in torch_fields})
            torch = sys.modules.get("torch")
            try:
                if torch is not None and torch.cuda.is_initialized():
                    row['torch_device_name']=torch.cuda.get_device_name()
                    row['torch_device_total_mib']=torch.cuda.get_device_properties(0).total_memory/1048576
                    free, total = torch.cuda.mem_get_info()
                    for key, value in zip(torch_fields, [torch.cuda.memory_allocated(), torch.cuda.memory_reserved(), torch.cuda.max_memory_allocated(), torch.cuda.max_memory_reserved(), free, total]):
                        row[key] = value/1048576
            except Exception:
                pass
            self.rows.append(row)

    def monitor(self):
        while not self.stop.wait(2):
            self.sample()

    def finish(self, elapsed, exit_code):
        self.stop.set()
        self.sample()
        keys = list(dict.fromkeys(key for row in self.rows for key in row))
        with (self.root / "hardware-telemetry.csv").open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self.rows)
        def numeric(rows, key):
            return [r[key] for r in rows if isinstance(r.get(key), (float, int))]
        def peak(rows, key):
            values = numeric(rows, key)
            return max(values) if values else None
        folds = []
        for fold in dict.fromkeys(row.get("fold") for row in self.rows):
            rows = [r for r in self.rows if r.get("fold") == fold]
            util = sorted(numeric(rows, "gpu_util_pct"))
            free = numeric(rows, "torch_cuda_free_mib")
            total = peak(rows, "gpu_total_mib")
            used = peak(rows, "nvml_vram_used_mib")
            ratio = used/total if total and used is not None else None
            verdict = "unavailable" if ratio is None else ("NEAR-LIMIT" if ratio >= .95 else "HIGH" if ratio >= .85 else "MODERATE" if ratio >= .60 else "LOW")
            folds.append({"fold": fold, "peak_torch_allocated_mib": peak(rows, "torch_max_allocated_mib"), "peak_torch_reserved_mib": peak(rows, "torch_max_reserved_mib"), "min_cuda_free_mib": min(free) if free else None,
                "peak_nvml_vram_mib": used, "peak_host_ram_mib": peak(rows, "host_ram_used_mib"), "avg_gpu_util": statistics.mean(util) if util else None,
                "p95_gpu_util": util[max(0, math.ceil(len(util)*.95)-1)] if util else None, "peak_temp_c": peak(rows, "gpu_temp_c"), "peak_power_w": peak(rows, "gpu_power_w"),
                "vram_status_observed": verdict, "accumulation": "NOT VERIFIED: compare repeated phases/epochs with identical workloads", "train_speed": None})
        summary = {"duration_s": elapsed, "exit_code": exit_code, "folds": folds, "sampling_interval_s": 2, "monitor_overhead": "NOT VERIFIED; no extra benchmark was launched",
            "limitations": ["NVIDIA samples are device-wide, not PyTorch allocator metrics", "PyTorch allocator values cover this Python process only; subprocess trainers are unavailable", "Windows WDDM counters unavailable on the remote Linux host", "Short runs may have too few samples for pressure/accumulation conclusions", "No automatic limit probing"]}
        (self.root / "hardware-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        def size(value):
            return "NA" if value is None else f"{value:.1f} MiB ({value/1024:.3f} GiB)"
        report = ["# Hardware report", "", "| Fold | Peak Torch Alloc | Peak Torch Reserved | Min CUDA Free | Peak NVIDIA VRAM | Peak Dedicated VRAM | Peak Shared GPU | Peak Host RAM | Avg GPU Util | Peak Temp | Train Speed |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for fold in folds:
            report.append(f"| {fold['fold']} | {size(fold['peak_torch_allocated_mib'])} | {size(fold['peak_torch_reserved_mib'])} | {size(fold['min_cuda_free_mib'])} | {size(fold['peak_nvml_vram_mib'])} | NA | NA | {size(fold['peak_host_ram_mib'])} | {fold['avg_gpu_util']} | {fold['peak_temp_c']} | NA |")
        report.extend(["", *summary["limitations"], "", "Monitoring overhead: NOT VERIFIED. VRAM-limited behavior, paging, per-group peaks, accumulation, I/O bottlenecks and safe headroom require workload-specific analysis; not inferred from a smoke test."])
        (self.root / "hardware-report.md").write_text("\n".join(report), encoding="utf-8")
        return summary


class Tee(io.TextIOBase):
    def __init__(self, target):
        self.target, self.buffer = target, io.StringIO()
    def write(self, text):
        self.target.write(text)
        self.target.flush()
        if self.buffer.tell() < 2_000_000:
            self.buffer.write(text[:10000])
        return len(text)
    def flush(self):
        self.target.flush()


def execute_payload(spec):
    global _sampler
    os.chdir("/workspace")
    root = Path("/muse-results") / spec["id"] if spec.get("volume") else Path("/output")
    root.mkdir(parents=True, exist_ok=True)
    os.environ["MUSE_OUTPUT_DIR"] = str(root)
    sys.path.insert(0, "/workspace")
    sys.modules["muse_telemetry"] = sys.modules[__name__]
    _context.update(spec.get("telemetry_context", {}))
    _sampler = Sampler(root, spec["id"])
    _sampler.sample()
    thread = threading.Thread(target=_sampler.monitor, daemon=True)
    thread.start()
    output = Tee(sys.stdout)
    started = time.monotonic()
    exit_code = 0
    try:
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            if spec["program"] in ("python", "pytorch"):
                sys.argv = [spec["entry"], *spec.get("args", [])]
                runpy.run_path(spec["entry"], run_name="__main__")
            else:
                if spec["program"] == "blender":
                    command = blender_command(spec, root)
                else:
                    command = spec["command"]
                process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
                for line in process.stdout:
                    output.write(line)
                exit_code = process.wait()
                process.stdout.close()
    except SystemExit as error:
        exit_code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
    except BaseException:
        output.write(traceback.format_exc())
        exit_code = 1
    elapsed = time.monotonic()-started
    summary = _sampler.finish(elapsed, exit_code)
    thread.join(timeout=5)
    (root / "run.log").write_text(output.buffer.getvalue(), encoding="utf-8")
    stream = io.BytesIO()
    total, skipped, retained = 0, [], []
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(root.rglob("*"), key=lambda f: (not f.name.startswith(("hardware-", "run.log")), str(f))):
            if file.is_symlink() or not file.is_file() or not file.resolve().is_relative_to(root):
                continue
            retained.append({"path": file.relative_to(root).as_posix(), "size": file.stat().st_size})
            if total + file.stat().st_size > 32_000_000:
                skipped.append(file.relative_to(root).as_posix())
                continue
            total += file.stat().st_size
            archive.write(file, file.relative_to(root).as_posix())
    return {"exit_code": exit_code, "logs":output.buffer.getvalue(), "summary": summary, "bundle": stream.getvalue(), "skipped_large_outputs": skipped, "retained_outputs": retained if spec.get("volume") else [], "volume": spec.get("volume", ""), "volume_path": spec["id"] if spec.get("volume") else None}


def blender_command(spec, root):
    if spec['entry'].endswith('.py'):
        return ['/opt/blender/blender','-b','--python-exit-code','1','--python',spec['entry'],'--',*spec.get('args',[])]
    return ['/opt/blender/blender','-b',spec['entry'],'-E','CYCLES','-o',str(root/'frame_'),'-F','PNG','-f',str(spec.get('frame',1)),'--','--cycles-device','CUDA']
