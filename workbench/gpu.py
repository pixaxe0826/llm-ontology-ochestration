import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from .providers import error_message
from .security import KEY_NAMES, contains_key, protected, redact, refresh_keys, scoped_path
from .workspace import Workspace
from .gpu_launch import image_lines, classify_failure, startup_failure

GPUS = {"T4": 16, "L4": 24, "A10": 24, "L40S": 48, "A100": 40, "A100-80GB": 80, "H100": 80, "H200": 141, "B200": 192, "B300": 288}
PROGRAMS = ("python", "pytorch", "blender", "custom")


def normalize_spec(spec):
    spec = dict(spec)
    if spec.get("program") not in PROGRAMS:
        raise ValueError("Python, PyTorch, Blender 또는 custom을 선택하세요.")
    spec["timeout"] = max(30, min(int(spec.get("timeout", 600)), 21600))
    spec["args"] = spec.get("args", [])
    spec["files"] = spec.get("files", [])
    spec["packages"] = spec.get("packages", [])
    spec["volume"] = spec.get("volume", "muse-workbench-results")
    if spec["volume"] and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{1,63}", spec["volume"]):
        raise ValueError("결과 Volume 이름은 영문·숫자·밑줄·하이픈의 2~64자로 지정하세요.")
    if not isinstance(spec["files"], list) or not spec["files"]:
        raise ValueError("클라우드에 전송할 파일·폴더를 명시하세요.")
    if not isinstance(spec["args"], list) or any(not isinstance(x, str) for x in spec["args"]):
        raise ValueError("프로그램 인수는 문자열 배열이어야 합니다.")
    if not isinstance(spec["packages"], list) or len(spec["packages"]) > 30 or any(not isinstance(p, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\[\],!<>=~+\-]*", p) for p in spec["packages"]):
        raise ValueError("패키지는 PyPI 이름·버전만 지정할 수 있습니다. URL과 설치 옵션은 사용할 수 없습니다.")
    if spec.get("gpu", "auto") == "auto":
        minimum = max(float(spec.get("min_vram_gb", 0)), 24 if spec["program"] in ("blender", "pytorch") else 16)
        spec["gpu"] = next((gpu for gpu, size in GPUS.items() if size >= minimum), None)
    if spec.get("gpu") not in GPUS:
        raise ValueError("지원하는 GPU 범위를 초과했습니다.")
    if spec["program"] == "custom":
        image = spec.get("image", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@\-]+", image):
            raise ValueError("custom 프로그램은 공개 컨테이너 이미지 이름이 필요합니다.")
        command = spec.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(c, str) or "\x00" in c for c in command):
            raise ValueError("custom 명령은 실행 파일과 인수의 문자열 배열입니다.")
    else:
        entry = spec.get("entry", "")
        if not entry:
            raise ValueError("실행할 .py 또는 .blend 파일을 지정하세요.")
        if spec["program"] in ("python", "pytorch") and not entry.endswith(".py"):
            raise ValueError("Python 진입 파일은 .py여야 합니다.")
        if spec["program"] == "blender" and not entry.endswith((".blend", ".py")):
            raise ValueError("Blender entry must be a workspace .blend file or procedural .py script.")
    if contains_key(json.dumps(spec)):
        raise ValueError("작업 설정에 비밀값을 넣을 수 없습니다.")
    return spec


class GPUManager:
    def __init__(self, store):
        self.store = store
        self.processes, self.cancels = {}, {}
        self.active = set()
        self.launch_aborts = set()
        self.lock = threading.RLock()

    def _cancel_remote(self, job_id):
        job = self.store.get('gpu', job_id)
        if not job.get('call_id'):return False
        import modal
        modal.FunctionCall.from_id(job['call_id']).cancel(terminate_containers=True)
        return True

    def _abort_launch(self, job_id, failure):
        with self.lock:
            if job_id in self.launch_aborts:return
            self.launch_aborts.add(job_id)
        self.store.update('gpu',job_id,failure=failure,automatic_retry=False)
        process = self.processes.get(job_id)
        if process and process.poll() is None:process.terminate()
        try:
            confirmed=self._cancel_remote(job_id)
            self.store.update('gpu',job_id,remote_cancel_confirmed=confirmed)
        except Exception as error:
            self.store.update('gpu',job_id,remote_cancel_confirmed=False,remote_cancel_error=error_message(error))

    def preview(self, root, spec):
        spec = normalize_spec(spec)
        ws = Workspace(root, self.store.root)
        files = {}
        for selected in spec["files"]:
            path = ws.path(selected, True)
            if path.is_file():
                entries = [path]
            else:
                listing = ws.files(selected, depth=30, limit=2100)
                if listing["truncated"]:
                    raise ValueError("전송 폴더에 항목이 너무 많습니다. 필요한 파일·하위 폴더만 지정하세요.")
                entries = [ws.path(row["path"], True) for row in listing["files"] if not row["directory"]]
            for file in entries:
                relative = file.relative_to(ws.root).as_posix()
                if any(protected(part) for part in file.relative_to(ws.root).parts):
                    continue
                size = file.stat().st_size
                if size > 100_000_000:
                    raise ValueError("파일 전송 한도 100 MB를 초과했습니다.")
                content = file.read_bytes()
                if any(os.environ.get(key) and os.environ[key].encode() in content for key in KEY_NAMES):
                    raise ValueError("등록된 키가 포함된 파일은 업로드할 수 없습니다.")
                files[relative] = {"size": size, "sha256": hashlib.sha256(content).hexdigest()}
        if len(files) > 2000 or sum(info["size"] for info in files.values()) > 100_000_000:
            raise ValueError("전송 한도는 2000개 파일, 합계 100 MB입니다. 필요한 하위 폴더만 선택하세요.")
        if not files:
            raise ValueError("전송할 수 있는 파일이 없습니다.")
        if spec["program"] != "custom" and spec["entry"].replace("\\", "/") not in files:
            raise ValueError("진입 파일을 전송 목록에 포함하세요.")
        spec["entry"] = spec.get("entry", "").replace("\\", "/")
        preview = {"spec": spec, "files": [{"path": key, **value} for key, value in sorted(files.items())], "total_bytes": sum(value["size"] for value in files.values()), "workspace": str(ws.root)}
        preview["digest"] = hashlib.sha256(json.dumps(preview,sort_keys=True).encode()).hexdigest()
        return preview

    def submit(self, preview, task_id=None):
        refresh_keys()
        if not os.environ.get("MODAL_TOKEN_ID") or not os.environ.get("MODAL_TOKEN_SECRET"):
            raise ValueError("Modal API 토큰을 먼저 설정하세요.")
        owner = self.store.get("task",task_id) if task_id else {}
        shared = owner.get("share_memory",True) if task_id else preview["spec"].get("share_memory",True)
        links = {key:owner[key] for key in ('goal_id','plan_item_id') if owner.get(key)}
        job = self.store.save("gpu", {"workspace": preview["workspace"], "spec": preview["spec"], "manifest": preview["files"], "input_hashes":{f['path']:f['sha256'] for f in preview['files']}, "total_bytes": preview["total_bytes"], "status": "queued", "logs": "", "task_id": task_id,"share_memory":shared,**links})
        cancel = threading.Event()
        self.cancels[job["id"]] = cancel
        self.active.add(job["id"])
        threading.Thread(target=self._run, args=(job["id"],), daemon=True).start()
        return job

    def _run(self, job_id):
        folder = self.store.root / "gpu-jobs" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        stage = folder / "input"
        stage.mkdir(exist_ok=True)
        try:
            job = self.store.get("gpu", job_id)
            if self.cancels[job_id].is_set():
                return
            spec = {**job["spec"], "id": job_id}
            ws = Workspace(job["workspace"], self.store.root)
            for file in job["manifest"]:
                source = ws.path(file["path"], True)
                content = source.read_bytes()
                if hashlib.sha256(content).hexdigest() != file["sha256"]:
                    raise ValueError("승인 이후 입력 파일이 변경됐습니다. 다시 확인·제출하세요.")
                destination = stage / file["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            runtime = stage / ".muse-runtime"
            runtime.mkdir(exist_ok=True)
            shutil.copy2(Path(__file__).with_name("remote_runner.py"), runtime / "runner.py")
            driver = folder / "modal_driver.py"
            lines = ["import json, sys", "from pathlib import Path", "import modal", f"SPEC = {spec!r}", f"OUT = Path({str(folder)!r})", "app = modal.App('muse-workbench-job')"]
            lines += image_lines(spec)
            lines += [f"image = image.add_local_dir({str(stage)!r}, '/workspace')"]
            volumes = "{}"
            if spec["volume"]:
                lines.append(f"volume = modal.Volume.from_name({spec['volume']!r}, create_if_missing=True, version=2)")
                volumes = "{'/muse-results': volume}"
            lines += [f"@app.function(image=image, gpu={spec['gpu']!r}, timeout={spec['timeout']}, startup_timeout=90, retries=0, max_containers=1, volumes={volumes})",
                "def execute():", "    import sys", "    sys.path.insert(0, '/workspace/.muse-runtime')", "    from runner import execute_payload", "    result = execute_payload(SPEC)",
                "    volume.commit()" if spec["volume"] else "    pass", "    return result",
                "@app.local_entrypoint()", "def main():", "    call = execute.spawn()", "    print('MUSE_CALL_ID:' + call.object_id, flush=True)",
                "    try:", f"        result = call.get(timeout={spec['timeout']+90})", "    except BaseException:", "        call.cancel(terminate_containers=True)", "        raise",
                "    (OUT / 'artifacts.zip').write_bytes(result.pop('bundle'))", "    (OUT / 'result.json').write_text(json.dumps(result), encoding='utf-8')"]
            driver.write_text("\n".join(lines)+"\n", encoding="utf-8")
            if self.cancels[job_id].is_set():
                return
            self.store.update("gpu", job_id, status="running", started=time.time())
            proc = subprocess.Popen([sys.executable, "-m", "modal", "run", str(driver)], cwd=folder,
                env={**os.environ, "PYTHONIOENCODING": "utf-8", "MODAL_LOGLEVEL": "WARNING"}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.processes[job_id] = proc
            watchdog = threading.Timer(spec['timeout']+180,lambda:self._abort_launch(job_id,{
                'category':'allocation','code':'launch_deadline','phase':'launch','reason':'Build/scheduling/execution wall-time limit reached; no retry.','automatic_retry':False}))
            watchdog.daemon=True
            watchdog.start()
            for line in proc.stdout:
                if line.startswith("MUSE_CALL_ID:"):
                    self.store.update("gpu", job_id, call_id=line.strip().split(":", 1)[1])
                else:
                    current = self.store.get("gpu", job_id)
                    self.store.update("gpu", job_id, logs=(current.get("logs", "") + redact(line))[-120000:])
                    if startup_failure(line):
                        self._abort_launch(job_id,classify_failure(self.store.get('gpu',job_id)['logs'],startup=True))
                        break
                if self.cancels[job_id].is_set():
                    break
            code = proc.wait(timeout=30)
            if self.cancels[job_id].is_set():
                self.store.update("gpu", job_id, status="cancelled", finished=time.time())
                return
            if code or not (folder / "result.json").exists():
                raise ValueError("Modal 실행이 실패했습니다. 작업 로그를 확인하세요. 자동 재시도하지 않습니다.")
            result = json.loads((folder / "result.json").read_text(encoding="utf-8"))
            if result['exit_code']:
                result['failure']=classify_failure(result.get('logs','')+'\ncommand exited with exit_code '+str(result['exit_code']))
            artifacts = folder / "artifacts"
            artifacts.mkdir(exist_ok=True)
            with zipfile.ZipFile(folder / "artifacts.zip") as archive:
                total = 0
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > 32_000_000 or len(archive.infolist()) > 3000:
                        raise ValueError("결과 파일 한도를 초과했습니다.")
                    target = scoped_path(artifacts, info.filename)
                    data = archive.read(info)
                    if any(os.environ.get(key) and os.environ[key].encode() in data for key in KEY_NAMES):
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
            paths = [p.relative_to(artifacts).as_posix() for p in artifacts.rglob("*") if p.is_file()]
            self.store.update("gpu", job_id, status="succeeded" if result["exit_code"] == 0 else "failed", result=result, failure=result.get('failure'), automatic_retry=False, artifacts=paths, finished=time.time())
        except Exception as error:
            current=self.store.get('gpu',job_id)
            failure=current.get('failure') or classify_failure(current.get('logs','')+'\n'+redact(str(error)))
            detail=f"[{failure['category']}/{failure['code']}] {failure['reason']} "+error_message(error)
            self.store.update("gpu", job_id, status="cancelled" if self.cancels[job_id].is_set() else "failed", error=detail, failure=failure, automatic_retry=False, finished=time.time())
        finally:
            if 'watchdog' in locals():watchdog.cancel()
            process = self.processes.pop(job_id, None)
            if process and process.stdout:
                process.stdout.close()
            with self.lock:
                self.active.discard(job_id)

    def cancel(self, job_id):
        job = self.store.get("gpu", job_id)
        if job["status"] not in ("running", "queued", "interrupted"):
            return job
        self.cancels.setdefault(job_id, threading.Event()).set()
        if job.get("call_id"):
            try:
                import modal
                modal.FunctionCall.from_id(job["call_id"]).cancel(terminate_containers=True)
            except Exception as error:
                return self.store.update("gpu", job_id, status="interrupted", error="원격 취소 확인 실패: " + error_message(error))
        proc = self.processes.get(job_id)
        if proc and proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.terminate()
        return self.store.update("gpu", job_id, status="cancelled", finished=time.time())

    def artifact(self, job_id, path):
        self.store.get("gpu", job_id)
        return scoped_path(self.store.root / "gpu-jobs" / job_id / "artifacts", path, True)

    def remote_artifact(self, job_id, path):
        job = self.store.get("gpu", job_id)
        result = job.get("result", {})
        entry = next((item for item in result.get("retained_outputs", []) if item["path"] == path), None)
        if not entry or not result.get("volume"):
            raise ValueError("보관된 결과 파일을 찾을 수 없습니다.")
        scoped_path(self.store.root, path)
        import modal
        volume = modal.Volume.from_name(result["volume"])
        return entry, volume.read_file("/"+job_id+"/"+path)
