import difflib
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import threading
import uuid

from .security import child_environment, contains_key, is_link, protected, redact, scoped_path
from .command_cwd import MISSING,command_folder,with_cwd_notice


class Workspace:
    def __init__(self, root, state_root):
        raw = Path(root).expanduser()
        if not raw.is_absolute() or not raw.is_dir() or is_link(raw):
            raise ValueError("존재하는 일반 폴더의 절대 경로를 선택하세요.")
        self.root = raw.resolve()
        if self.root == Path(self.root.anchor) or self.root == Path.home():
            raise ValueError("드라이브 루트나 사용자 홈 전체 대신 작업용 하위 폴더를 선택하세요.")
        if any(protected(part) for part in self.root.parts) or (os.name == "nt" and str(self.root).lower().startswith(str(Path(os.environ.get("SystemRoot", "C:\\Windows"))).lower())):
            raise ValueError("시스템·비밀·내부 폴더는 작업 폴더로 사용할 수 없습니다.")
        self.state = Path(state_root)

    def path(self, path=".", exists=False):
        return scoped_path(self.root, path, exists)

    def files(self, path=".", depth=3, limit=400):
        start = self.path(path, True)
        rows = []
        def walk(folder, level):
            for item in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                if protected(item.name) or is_link(item):
                    continue
                if len(rows) >= limit:
                    return
                rows.append({"path": item.relative_to(self.root).as_posix(), "directory": item.is_dir(), "size": 0 if item.is_dir() else item.stat().st_size})
                if item.is_dir() and level < depth:
                    walk(item, level + 1)
        walk(start, 1)
        return {"files": rows, "truncated": len(rows) >= limit}

    def read(self, path, start_line=None, end_line=None, *, include_lines=False):
        file = self.path(path, True)
        if not file.is_file() or file.stat().st_size > 200_000:
            raise ValueError("200 KB 이하 텍스트 파일만 읽을 수 있습니다.")
        try:
            data = file.read_bytes()
            if len(data)>200_000:raise ValueError('200 KB 이하 텍스트 파일만 읽을 수 있습니다.')
            # Preserve read_text's existing universal-newline contract while hashing
            # the same byte snapshot that supplied this content.
            content = data.decode("utf-8-sig").replace('\r\n','\n').replace('\r','\n')
        except UnicodeError:
            raise ValueError("UTF-8 텍스트 파일이 아닙니다.") from None
        if "\x00" in content or contains_key(content):
            raise ValueError("바이너리 또는 등록된 키가 포함된 파일은 모델에 전달하지 않습니다.")
        result={"path": path, "content": content, "sha256": hashlib.sha256(data).hexdigest()}
        if include_lines or start_line is not None or end_line is not None:
            from .file_views import line_result
            return line_result(result,start_line,end_line)
        return result

    def read_structured(self,path,key_path):
        from .structured_files import structured_result
        return structured_result(self.read(path),Path(path).suffix.lower(),key_path)

    def write(self, path, content, expected_sha256=None, full_diff=False):
        file = self.path(path)
        if not isinstance(content, str) or len(content.encode("utf-8")) > 200_000 or contains_key(content):
            raise ValueError("파일 크기 제한을 넘거나 비밀값이 포함되어 있습니다.")
        old = ""
        backup_id = None
        if file.exists():
            current = self.read(path)
            if not expected_sha256 or current["sha256"] != expected_sha256:
                raise ValueError("기존 파일은 먼저 read_file로 읽고 최신 sha256을 지정해야 합니다.")
            old = current["content"]
            backup_id = uuid.uuid4().hex
            backup = self.state / "backups" / backup_id
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file, backup)
        file.parent.mkdir(parents=True, exist_ok=True)
        self.path(path)  # Recheck after creating parent directories.
        temp = file.with_name(file.name + ".muse-tmp-" + uuid.uuid4().hex[:8])
        try:
            temp.write_text(content, encoding="utf-8", newline="")
            temp.replace(file)
        finally:
            temp.unlink(missing_ok=True)
        diff = "".join(difflib.unified_diff(old.splitlines(True), content.splitlines(True), fromfile=path, tofile=path))
        return {"path": path, "sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "backup_id": backup_id, "diff": diff if full_diff else diff[:40000], "created": not bool(backup_id)}

    def search(self, query, path="."):
        if not query or len(query) > 300:
            raise ValueError("검색어는 1~300자여야 합니다.")
        hits = []
        for entry in self.files(path, depth=8, limit=2000)["files"]:
            if entry["directory"] or entry["size"] > 200_000:
                continue
            try:
                text = self.read(entry["path"])["content"]
            except (ValueError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query.casefold() in line.casefold():
                    hits.append({"path": entry["path"], "line": number, "text": line[:500]})
                    if len(hits) >= 80:
                        return hits
        return hits

    def run(self, argv, cwd=MISSING, timeout=120, cancel=None, extra_env=None, full_output=False, *, timeout_limit=120, on_process=None):
        if not isinstance(argv, list) or not argv or len(argv) > 64 or any(not isinstance(a, str) or "\x00" in a for a in argv):
            raise ValueError("실행 명령은 문자열 배열이어야 합니다.")
        timeout = max(1, min(int(timeout), timeout_limit))
        cwd,folder,notice = command_folder(self,cwd)
        started = time.monotonic()
        chunks, byte_count = ([] if full_output else deque(maxlen=40)), [0]
        environment = child_environment()
        environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        if extra_env:
            if set(extra_env) - {'MUSE_PROXY_BASE_URL','MUSE_PROXY_TOKEN'}:
                raise ValueError('Only Goal proxy variables may be added to a child environment.')
            environment.update(extra_env)
        from .command_files import discover,hashes,python_target
        hints=discover(self,argv,cwd);before_inputs=hashes(self,hints['inputs'])
        before_outputs={p:(self.path(p).stat().st_mtime_ns,self.path(p).stat().st_size) for p in hints['outputs'] if self.path(p).is_file()}
        trace=None;run_argv=argv
        if python_target(argv,folder) or len(argv)>2 and Path(argv[0]).stem.lower().startswith('python') and argv[1]=='-m' and argv[2] in ('pytest','unittest'):
            trace=self.state/'command-traces'/(uuid.uuid4().hex+'.json');trace.parent.mkdir(parents=True,exist_ok=True)
            run_argv=[argv[0],str(Path(__file__).with_name('command_audit_runner.py')),str(trace),str(self.root),*argv[1:]]
        proc = subprocess.Popen(run_argv, cwd=folder, env=environment, shell=False,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=bool(on_process and os.name!='nt'),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        tree=None
        if on_process:
            try:
                from .command_process import ProcessTree
                tree=ProcessTree(proc)
                on_process(proc.pid,lambda:redact(b''.join(list(chunks)).decode('utf-8',errors='replace'))[-8000:])
            except Exception:
                if tree:tree.close()
                proc.kill();proc.wait(timeout=10);proc.stdout.close();raise
        def consume():
            while True:
                chunk = proc.stdout.read1(1024)
                if not chunk:
                    break
                byte_count[0] += len(chunk)
                chunks.append(chunk)
        reader = threading.Thread(target=consume, daemon=True)
        reader.start()
        interrupted = timed_out = cancelled = output_limit = False
        while proc.poll() is None:
            if time.monotonic() - started > timeout or (cancel and cancel.is_set()) or byte_count[0] > 4_000_000:
                interrupted = True
                timed_out = time.monotonic() - started > timeout
                cancelled = bool(cancel and cancel.is_set())
                output_limit = byte_count[0] > 4_000_000
                if tree:tree.close()
                elif os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                else:
                    proc.kill()
                break
            time.sleep(0.15)
        proc.wait(timeout=10)
        if tree:tree.close()
        reader.join(timeout=3)
        proc.stdout.close()
        output = b"".join(chunks)
        raw = (output if full_output else output[-40000:]).decode("utf-8", errors="replace")
        tracking={'inputs':before_inputs,'outputs':{},'method':hints['method']}
        if trace:
            try:
                if trace.stat().st_size>2_000_000:raise ValueError('Trace too large.')
                data=json.loads(trace.read_text(encoding='utf-8'))
                if any(not isinstance(data.get(k),dict) or any(not isinstance(p,str) or not isinstance(h,str) or len(h)!=64 or any(c not in '0123456789abcdef' for c in h) for p,h in data[k].items()) for k in ('inputs','outputs')):raise ValueError('Invalid trace hashes.')
                tracking={'inputs':{**{p:h for p,h in before_inputs.items() if p in hints['native_inputs']},**data['inputs']},'outputs':data['outputs'],'method':'Python audit hook + native path hints'}
            except (ValueError,OSError,KeyError,TypeError):tracking['note']='Runtime IO trace unavailable; static hints only.'
            finally:trace.unlink(missing_ok=True)
        for p in hints['outputs']:
            if self.path(p).is_file() and before_outputs.get(p)!=(self.path(p).stat().st_mtime_ns,self.path(p).stat().st_size):
                tracking['outputs'].update(hashes(self,[p]))
        tracking['inputs']={p:h for p,h in tracking['inputs'].items() if p not in tracking['outputs']}
        # Revalidate observer metadata through the same path boundary before persisting it.
        for paths in (tracking['inputs'],tracking['outputs']):
            for path in list(paths):
                try:self.path(path)
                except (ValueError,OSError):del paths[path]
        return with_cwd_notice({"exit_code": proc.returncode, "output": redact(raw), "interrupted": interrupted,
            "timed_out":timed_out,"cancelled":cancelled,"output_limit_exceeded":output_limit,"timeout_s":timeout,
            "duration_s": round(time.monotonic()-started, 2),'command_files':tracking},notice)
