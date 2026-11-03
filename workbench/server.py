import argparse
from collections import defaultdict
from contextlib import nullcontext
import gzip,hashlib,time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
from urllib.parse import parse_qs, urlparse
import webbrowser

from .ui_metrics import RequestMetrics, span, response_metrics, accepts_gzip
from .ui_cache import ViewResponses
from .agent import Agents
from .gpu import GPUS, PROGRAMS, GPUManager
from .memory import Memory, uid
from .providers import Providers, error_message
from .security import clean_data, redact, refresh_keys
from .store import Store
from .workspace import Workspace
from .goals import GoalManager
from .notifications import Notifications
from .product_proxy import proxy_error_message
from .product_images import MAX_REQUEST_BYTES
from .goal_history import GoalHistory, assert_unarchived
from .goal_models import BUILDER_MODELS, DEFAULT_BUILDER_MODEL, builder_catalog
from .goal_watchdog import GoalWatchdog
from .sam_proxy import SAM_REQUEST_BYTES
from .chat_attachments import ChatAttachments, UPLOAD_LIMIT


class LocalHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = os.name != "nt"
    allow_reuse_port = False

    def server_bind(self):
        if os.name == "nt" and hasattr(socket,"SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
        super().server_bind()


class Application:
    def __init__(self, state):
        refresh_keys()
        self.ui_metrics = RequestMetrics(state)
        self.ui_responses = ViewResponses()
        self.store = Store(state)
        self.providers = Providers(self.store)
        self.gpu = GPUManager(self.store)
        self.memory = Memory(self.store)
        self.memory.vault.ui_graph_cache=self.ui_metrics.config.get("memory_cache",True)
        self.notifications = Notifications(self.store)
        self.store.on_save = self.record_saved
        self.attachments=ChatAttachments(self.store)
        self.agents = Agents(self.store, self.providers, self.gpu,self.memory,self.attachments)
        self.token = secrets.token_urlsafe(32)
        for kind in ("task", "gpu"):
            for row in self.store.items(kind):
                if row["status"] in ("running", "waiting_approval", "queued"):
                    marker={'restart_interruption':{'kind':'server_restart','created':time.time(),'generation':row.get('generation')}} if kind=='task' and row.get('goal_id') else {}
                    self.store.update(kind, row["id"], status="interrupted", approval=None, error="서버가 재시작됐습니다. 실행 상태를 확인한 뒤 계속하거나 중지하세요.",**marker)
                elif row["status"] in ("succeeded","failed","paused","cancelled","interrupted"):
                    self.memory.enqueue(kind,row)
        self.goals = GoalManager(self.store,self.providers,self.gpu,self.memory)
        self.goal_history = GoalHistory(self)
        self.notifications.ledger=self.goals.ledger
        self.goals.notifier=self.notifications
        self.notifications.watchdog=GoalWatchdog(self.goals,self.notifications)
        self.resume_timer=None
        self.agents.usage_callback = self.goals.record_chat_usage

    def record_saved(self,kind,record):
        self.memory.enqueue(kind,record)
        self.notifications.observe(kind,record)

    def public_task(self, task, rows=None):
        if rows is None:rows=[r for r in self.goals.ledger.rows() if r.get('task_id')==task['id']]
        cost={'known':bool(rows),'estimated_usd':sum(r.get('estimated_usd',0) for r in rows),
              'reasoning_tokens':sum(r.get('reasoning_tokens',0) for r in rows),'reasoning_tokens_known':any(r.get('reasoning_tokens_known',False) for r in rows),
              'image_count':sum(r.get('image_count',0) for r in rows)}
        return {**{key: value for key, value in task.items() if key != "messages"},
                'cost':cost,'goal_context':self.goals.executions.context('task',task['id']),"history_busy": task["id"] in self.agents.active or task['id'] in self.goals.builders.active}

    def public_tasks(self, compact=False):
        tasks=self.store.items('task',None)
        if compact:
            fields=('id','title','provider','mode','status','archived','updated','created','goal_id','plan_item_id','task_id')
            return [{**{k:t[k] for k in fields if k in t},'goal_context':self.goals.executions.context('task',t['id']),
                     'history_busy':t['id'] in self.agents.active or t['id'] in self.goals.builders.active} for t in tasks]
        grouped=defaultdict(list)
        for row in self.goals.ledger.rows():grouped[row.get('task_id')].append(row)
        return [self.public_task(t,grouped[t['id']]) for t in tasks]

    def public_jobs(self):
        return [{**job,'goal_context':self.goals.executions.context('gpu',job['id']), "history_busy":job["id"] in self.gpu.active} for job in self.store.items("gpu", None)]

    def stop(self):
        if self.resume_timer:self.resume_timer.cancel()
        self.notifications.stop()
        self.goals.shutdown()
        for task in self.store.items("task", None):
            if task["status"] in ("running", "queued", "waiting_approval"):
                self.agents.cancel(task["id"])
        for job in self.store.items("gpu", None):
            if job["status"] in ("running", "queued"):
                self.gpu.cancel(job["id"])
        self.memory.pending.join()

    def manage_history(self, data):
        kind, record_id, action = data.get("kind"), data.get("id"), data.get("action")
        if kind not in ("task", "gpu") or action not in ("archive", "restore", "delete"):
            raise ValueError("기록과 작업을 확인하세요.")
        if action == "delete" and data.get("confirmed") is not True:
            raise ValueError("기록과 연결된 공유 메모리 삭제를 확인하세요.")
        # Same order as memory sync; running/cancelling workers cannot recreate deleted records.
        with self.agents.lock, self.gpu.lock, self.memory.lock, self.store.lock:
            record = self.store.get(kind, record_id)
            active = self.agents.active if kind == "task" else self.gpu.active
            linked = [j for j in self.store.items("gpu", None) if j.get("task_id") == record_id] if kind == "task" else []
            if (record["status"] in ("running", "queued", "waiting_approval") or record_id in active
                    or any(j["status"] in ("running", "queued") or j["id"] in self.gpu.active for j in linked)):
                raise ValueError("실행 중인 작업이 끝난 뒤 기록을 변경하세요.")
            if action != "delete":
                self.store.update(kind, record_id, archived=action == "archive")
            else:
                source_id = uid(kind, record_id)
                if kind=='task':self.attachments.delete(record_id)
                if kind=='task':
                    for item in self.store.items('memory_source',None):
                        if item.get('kind')=='task_response' and item.get('source_id')==record_id:
                            self.memory.vault.delete(item['id']);self.store.delete('memory_source',item['id'])
                self.memory.vault.delete(source_id)
                self.store.save("deleted_history", {"id":source_id})
                self.store.delete("memory_source", source_id)
                self.store.delete(kind, record_id)
        return {"ok":True}


    def handle(self, method, path, data, query):
        scope=self.store.read_cache() if self.ui_metrics.config.get('request_snapshot',True) else nullcontext()
        with scope:return self._handle(method,path,data,query)

    def _handle(self, method, path, data, query):
        if path=='/api/provider-registry' and method=='GET':
            from .provider_checks import ProviderChecks
            return ProviderChecks(self.goals).status()
        if path=='/api/provider-registry' and method=='POST':return self.goals.model_registry.save(data)
        if path=='/api/provider-registry/test' and method=='POST':
            from .provider_checks import ProviderChecks
            return ProviderChecks(self.goals).test(data)
        if path=='/api/provider-registry/models' and method=='POST':
            from .security import contains_key
            with self.providers.goal_client(data['provider']) as client:
                return {'models':sorted({r.id for r in client.models.list().data if isinstance(r.id,str) and len(r.id)<=200 and not contains_key(r.id)})}
        if path=='/api/goals/item-route' and method=='POST':return self.goals.update_item_route(data['goal_id'],data)
        if method=='GET' and path=='/api/ui/history':
            return {'tasks':self.public_tasks(compact=self.ui_metrics.config.get('compact_lists',True)),
                    'jobs':self.public_jobs(),'goals':self.store.items('goal',None)}
        if method=='GET' and path=='/api/execution-index':
            return self.goals.executions.report()
        if method=='GET' and path=='/api/notifications':
            if self.notifications.thread is None:self.notifications.tick()
            return self.notifications.inbox()
        if method=='POST' and path=='/api/notifications/settings':
            return self.notifications.configure(data)
        if method=='POST' and path=='/api/notifications/read':
            return self.notifications.read(data.get('id'))
        if method=='POST' and path=='/api/notifications/resolve':
            return self.notifications.resolve(data['id'])
        if method=='POST' and path=='/api/notifications/claim':
            return self.notifications.claim(data['id'],data['seq'],data['channel'])
        if method == 'GET' and path == '/api/muse-open':
            return self.goals.muse_routes.status()
        if method == 'POST' and path == '/api/muse-open':
            return self.goals.muse_routes.save(data)
        if method == 'POST' and path == '/api/muse-open/models':
            self.goals.muse_routes.discover()
            return self.goals.muse_routes.status()
        if method == 'GET' and path == '/api/goals':
            return self.store.items('goal',None)
        if method == 'GET' and path == '/api/goals/config':
            from .generation import settings
            return {'prices':self.goals.ledger.prices,'registry':self.goals.model_registry.public(),'limits':settings(),'defaults':self.goals.defaults(),'muse_open':self.goals.muse_routes.status(),
                    'builder_models':list(BUILDER_MODELS),'builder_default':DEFAULT_BUILDER_MODEL}
        if method=='POST' and path=='/api/goals/models':
            return builder_catalog(self.providers.models('meta'))
        if method=='GET' and path=='/api/goals/history':
            return self.goal_history.preview(query['goal_id'][0])
        if method=='POST' and path=='/api/goals/history':
            return self.goal_history.manage(data)
        if method=='POST' and path.startswith('/api/goals/') and data.get('goal_id'):
            assert_unarchived(self.store.get('goal',data['goal_id']))
        if method == 'GET' and path.startswith('/api/goals/evidence/'):
            return self.store.get('goal_evidence',path.rsplit('/',1)[1])
        if method == 'GET' and path.startswith('/api/goals/'):
            return self.goals.detail(path.rsplit('/',1)[1])
        if method == 'POST' and path == '/api/goals':
            return self.goals.create(data)
        if method=='POST' and path=='/api/goals/message':return self.goals.submit_directive(data['goal_id'],data)
        if method == 'POST' and path == '/api/goals/control':
            return self.goals.control(data['goal_id'],data['action'])
        if method == 'POST' and path == '/api/goals/settings':
            return self.goals.update(data['goal_id'],data)
        if method == 'POST' and path == '/api/goals/title':
            return self.goals.rename(data['goal_id'],data)
        if method == 'POST' and path == '/api/goals/reasoning':
            return self.goals.set_reasoning(data)
        if method == 'POST' and path == '/api/goals/approve':
            return self.goals.decide(data['goal_id'],data['approval_id'],data['approve'],data.get('answer',''))
        if method == 'POST' and path == '/api/goals/decision':
            return self.goals.answer_decision(data['goal_id'],data)
        if method == 'POST' and path == '/api/goals/dependencies':
            return self.goals.update_dependencies(data['goal_id'],data)
        if method == "GET" and path == "/api/bootstrap":
            return {"application":"muse-platform", "project_root":str(Path(__file__).resolve().parent.parent), "token": self.token, "providers": self.providers.settings(), "modal_ready": bool(os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET")), "gpus": GPUS, "programs": PROGRAMS,
                    "tasks": self.public_tasks(compact=query.get("view")==["compact"] and self.ui_metrics.config.get("compact_lists",True)), "jobs": self.public_jobs(), "version": "0.1.0", "ui_settings":self.ui_metrics.config}
        if method == "GET" and path == "/api/tasks":
            return self.public_tasks()
        if method == "GET" and path.startswith("/api/tasks/"):
            return self.public_task(self.store.get("task", path.rsplit("/",1)[1]))
        if method == "GET" and path == "/api/jobs":
            return self.public_jobs()
        if method == "GET" and path == "/api/memory":
            return self.memory.graph()
        if method == "GET" and path == "/api/memory/source":
            return self.memory.source(query["id"][0])
        if method == "POST" and path == "/api/memory/search":
            return self.memory.search(data["query"])
        if method == "POST" and path == "/api/memory/concepts":
            return self.memory.definitions(data["concepts"])
        if method == "POST" and path == "/api/memory/share":
            return self.memory.share(data["id"],data["shared"],data.get('attachment_confirmed',False))
        if method == "POST" and path == "/api/memory/note":
            return self.memory.add_note(data.get("title","메모"),data["text"],data.get("concepts",[]))
        if method == "POST" and path == "/api/memory/import":
            return self.memory.import_folder(data["root"])
        if method == "POST" and path == "/api/settings":
            return self.providers.save(data)
        if method == "POST" and path == "/api/models":
            return self.providers.models(data["provider"])
        if method == "POST" and path == "/api/workspace":
            ws = Workspace(data["root"], self.store.root)
            result = {"root": str(ws.root), **ws.files(depth=3)}
            self.store.save("workspace", {"id": "last", "root": str(ws.root)})
            return result
        if method == "GET" and path == "/api/workspace":
            try:
                return self.store.get("workspace", "last")
            except ValueError:
                return {"root": ""}
        if method == "POST" and path == "/api/pick-folder":
            script = "import tkinter as t; from tkinter import filedialog; r=t.Tk(); r.withdraw(); r.attributes('-topmost',True); p=filedialog.askdirectory(title='Muse Workbench 작업 폴더'); print(p); r.destroy()"
            proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW",0), env={**os.environ,"PYTHONIOENCODING":"utf-8"})
            if proc.returncode:
                raise ValueError("폴더 선택 창을 열지 못했습니다. 경로를 직접 입력하세요.")
            return {"root": proc.stdout.strip()}
        if method == "POST" and path == "/api/file":
            ws = Workspace(data["root"], self.store.root)
            if data.get("action") == "write":
                return ws.write(data["path"], data["content"], data.get("expected_sha256"))
            return ws.read(data["path"])
        if method == "POST" and path == "/api/history":
            return self.manage_history(data)
        if method == "POST" and path == "/api/task":
            if data.get('task_id') and self.store.get('task',data['task_id']).get('goal_id'):
                raise ValueError('Continue this Builder through Goal mode.')
            return self.agents.start(data.get("root",""), data["prompt"], data.get("provider","meta"), data.get("model"), data.get("allow_write",True), data.get("max_steps",16), data.get("task_id"),data.get("mode","work"),data.get("use_memory",True),data.get("share_memory",True),attachments=data.get('attachments'),conversation_id=data.get('conversation_id'))
        if method == 'POST' and path == '/api/attachments/conversation':
            return self.attachments.conversation(data.get('task_id'))
        if method == "POST" and path == "/api/approve":
            task=self.store.get('task',data['task_id'])
            if task.get('goal_id'):
                return self.goals.decide(task['goal_id'],data['approval_id'],data['approve'])
            self.agents.decide(data["task_id"], data["approval_id"], data["approve"])
            return {"ok": True}
        if method == "POST" and path == "/api/cancel":
            return self.agents.cancel(data["task_id"])
        if method == "POST" and path == "/api/gpu/preview":
            return self.gpu.preview(data["root"], data["spec"])
        if method == "POST" and path == "/api/gpu/submit":
            # Recompute a manifest, never accept client-supplied absolute staging paths.
            preview = self.gpu.preview(data["root"], data["spec"])
            if not data.get("confirmed"):
                raise ValueError("전송 파일과 과금 GPU 실행을 확인하세요.")
            if data.get("preview_digest") != preview["digest"]:
                raise ValueError("입력 파일 또는 설정이 변경됐습니다. 전송 내용을 다시 확인하세요.")
            return self.gpu.submit(preview)
        if method == "POST" and path == "/api/gpu/cancel":
            return self.gpu.cancel(data["job_id"])
        if method == "POST" and path == "/api/restore":
            task = self.store.get("task", data["task_id"])
            change = next((e["change"] for e in task["events"] if e["kind"] == "change" and e["change"].get("backup_id") == data["backup_id"]), None)
            if not change:
                raise ValueError("복구할 백업이 없습니다.")
            ws = Workspace(task["workspace"], self.store.root)
            current = ws.read(change["path"])
            if current["sha256"] != change["sha256"]:
                raise ValueError("파일이 이후 변경됐습니다. 현재 내용을 확인한 뒤 직접 복구하세요.")
            content = (self.store.root / "backups" / change["backup_id"]).read_text(encoding="utf-8-sig")
            result = ws.write(change["path"], content, current["sha256"])
            self.store.event(task["id"], "change", change=result)
            return result
        raise ValueError("요청 경로를 찾을 수 없습니다.")


def make_handler(app):
    static = Path(__file__).parent / "static"
    class Handler(BaseHTTPRequestHandler):
        server_version = "MuseWorkbench"

        def handle_one_request(self):
            if getattr(app,"ui_metrics",None):
                with app.ui_metrics.request(self):return super().handle_one_request()
            return super().handle_one_request()

        def log_message(self, *_):
            pass

        def allowed(self):
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host", "") not in hosts:
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://"+h for h in hosts}:
                return False
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return False
            return True

        def send(self, status, body, content_type="application/json; charset=utf-8", filename=None, headers=None):
            if not isinstance(body, bytes):
                with span("serialize"):
                    body = json.dumps(clean_data(body), ensure_ascii=False).encode("utf-8")
            original_size=len(body);headers=dict(headers or {})
            compressible=content_type.startswith(('application/json','text/','application/javascript'))
            if status!=304 and not self.path.startswith('/proxy/') and not filename and compressible and len(body)>1024 and getattr(app,'ui_metrics',None) and app.ui_metrics.config.get('gzip',True) and accepts_gzip(self.headers.get('Accept-Encoding','')):
                body=gzip.compress(body,compresslevel=1,mtime=0);headers['Content-Encoding']='gzip'
            if compressible:headers['Vary']='Accept-Encoding'
            timing=response_metrics(status,original_size,len(body))
            self.send_response(status)
            if timing:self.send_header("Server-Timing",timing)
            self.send_header("Content-Type", content_type)
            if status!=304:self.send_header("Content-Length", str(len(body)))
            cache_control=headers.pop('Cache-Control','no-store')
            for name,value in (headers or {}).items():self.send_header(name,value)
            self.send_header("Cache-Control", cache_control)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if filename:
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''"+__import__('urllib.parse',fromlist=['quote']).quote(filename))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed():
                return self.send(403, {"error": "로컬 출처만 허용합니다."})
            parsed = urlparse(self.path)
            try:
                if parsed.path == '/proxy/v1/models':
                    return self.do_proxy(list_models=True)
                if parsed.path == "/api/artifact":
                    query = parse_qs(parsed.query)
                    file = app.gpu.artifact(query["job_id"][0],query["path"][0])
                    if query.get('preview')==['1']:
                        if file.suffix.lower()!='.png' or file.stat().st_size>5*1024*1024:
                            raise ValueError('PNG preview is limited to 5 MiB local artifacts.')
                        payload=file.read_bytes()
                        if not payload.startswith(b'\x89PNG\r\n\x1a\n'):raise ValueError('Invalid PNG artifact.')
                        return self.send(200,payload,'image/png')
                    return self.send(200,file.read_bytes(),"application/octet-stream",file.name)
                if parsed.path == "/api/remote-artifact":
                    query = parse_qs(parsed.query)
                    entry, chunks = app.gpu.remote_artifact(query["job_id"][0],query["path"][0])
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(entry["size"]))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    from urllib.parse import quote
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''"+quote(Path(entry["path"]).name))
                    self.end_headers()
                    try:
                        for chunk in chunks:
                            self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if parsed.path.startswith("/api/"):
                    if getattr(app,'ui_responses',None) and app.ui_responses.eligible(parsed.path):
                        body,tag=app.ui_responses.response(app,parsed.path,parse_qs(parsed.query))
                        conditional=app.ui_metrics.config.get('conditional_get',True)
                        if conditional and self.headers.get('If-None-Match')==tag:
                            return self.send(304,b'',headers={'ETag':tag})
                        return self.send(200,body,headers={'ETag':tag} if conditional else None)
                    return self.send(200,app.handle("GET",parsed.path,{},parse_qs(parsed.query)))
                relative = "index.html" if parsed.path == "/" else parsed.path.lstrip("/")
                file = (static / relative).resolve()
                if not file.is_relative_to(static.resolve()) or not file.is_file():
                    return self.send(404,{"error":"파일 없음"})
                with span("file"):payload=file.read_bytes()
                tag='W/"'+hashlib.sha256(payload).hexdigest()+'"'
                conditional=getattr(app,'ui_metrics',None) and app.ui_metrics.config.get('conditional_get',True)
                if conditional and self.headers.get('If-None-Match')==tag:return self.send(304,b'',headers={'ETag':tag,'Cache-Control':'private, no-cache'})
                return self.send(200,payload,mimetypes.guess_type(file.name)[0] or "application/octet-stream",headers={'ETag':tag,'Cache-Control':'private, no-cache'} if conditional else None)
            except Exception as error:
                self.send(400,{"error":error_message(error)})

        def do_POST(self):
            if urlparse(self.path).path == '/proxy/v1/sam/segment':return self.do_proxy(sam=True)
            if urlparse(self.path).path == '/proxy/v1/chat/completions':
                return self.do_proxy()
            if not self.allowed() or not secrets.compare_digest(self.headers.get("X-Local-Token",""),app.token):
                return self.send(403,{"error":"출처 또는 로컬 세션 토큰이 일치하지 않습니다. 페이지를 새로 고치세요."})
            try:
                app.goals.proxy_base=f'http://127.0.0.1:{self.server.server_address[1]}/proxy/v1'
                size = int(self.headers.get("Content-Length",0))
                if urlparse(self.path).path == '/api/attachments/upload':
                    if not 0<size<=UPLOAD_LIMIT:raise ValueError('첨부 크기 한도를 초과했습니다 (최대 15 MiB).')
                    if self.headers.get('Content-Type','').split(';')[0]!='application/octet-stream':raise ValueError('Binary upload required.')
                    from urllib.parse import unquote
                    self.connection.settimeout(45)
                    value=self.rfile.read(size)
                    if len(value)!=size:raise ValueError('Incomplete attachment upload.')
                    return self.send(200,app.attachments.upload(self.headers.get('X-Conversation-Id',''),unquote(self.headers.get('X-File-Name','')),value))
                if size <= 0 or size > 2_000_000:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                if self.headers.get("Content-Type","").split(";")[0] != "application/json":
                    raise ValueError("JSON 요청만 허용합니다.")
                data = json.loads(self.rfile.read(size))
                if urlparse(self.path).path == "/api/shutdown":
                    if data.get("confirmed") is not True:
                        raise ValueError("프로그램 종료를 확인하세요.")
                    self.send(200,{"ok":True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
                result = app.handle("POST",urlparse(self.path).path,data,{})
                self.send(200,result)
            except Exception as error:
                self.send(400,{"error":error_message(error)})

        def do_proxy(self,list_models=False,sam=False):
            if not self.allowed():return self.send(403,{'error':{'message':'Local origin required.','type':'permission_error'}})
            auth=self.headers.get('Authorization','')
            if not auth.startswith('Bearer '):return self.send(401,{'error':{'message':'Goal proxy token required.','type':'authentication_error'}})
            with app.goals.long_commands.proxy_scope(auth[7:]):
                return self.proxy_request(auth,list_models,sam)

        def proxy_request(self,auth,list_models=False,sam=False):
            try:
                if list_models:
                    return self.send(200,app.goals.proxy_models(auth[7:]))
                app.goals.proxy_goal(auth[7:])
                size=int(self.headers.get('Content-Length',0))
                maximum=SAM_REQUEST_BYTES if sam else MAX_REQUEST_BYTES
                if not 0<size<=maximum or self.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('JSON request required; proxy request size limit exceeded.')
                data=json.loads(self.rfile.read(size))
                if not isinstance(data,dict):raise ValueError('JSON object required.')
                if sam:return self.send(200,app.goals.proxy_sam(auth[7:],data))
                result=app.goals.proxy(auth[7:],data)
                adjustment=result.get('muse_proxy_adjustment')
                headers={'X-Muse-Proxy-Adjusted':f"max_tokens {adjustment['requested']}->{adjustment['effective']}"} if adjustment else None
                if not data.get('stream'):return self.send(200,result,headers=headers)
                # MVP streaming is buffered: one compatible completion chunk and [DONE].
                choice=result['choices'][0]
                delta={**choice['message']}
                if delta.get('tool_calls'):delta['tool_calls']=[{'index':i,**call} for i,call in enumerate(delta['tool_calls'])]
                chunk={**result,'object':'chat.completion.chunk','choices':[{'index':0,'delta':delta,'finish_reason':choice['finish_reason']}]}
                payload=('data: '+json.dumps(clean_data(chunk),ensure_ascii=False)+'\n\ndata: [DONE]\n\n').encode('utf-8')
                return self.send(200,payload,'text/event-stream; charset=utf-8',headers=headers)
            except PermissionError:
                self.send(401,{'error':{'message':'Invalid Goal proxy token.','type':'authentication_error'}})
            except Exception as error:
                from .goal_costs import GoalLimit
                app.goals.long_commands.proxy_denied(error)
                self.send(429 if isinstance(error,GoalLimit) else 400,{'error':{'message':proxy_error_message(error),'type':'goal_proxy_error'}})
    return Handler


def main():
    parser = argparse.ArgumentParser(description="Muse Workbench local WebUI")
    parser.add_argument("--port",type=int,default=8765)
    parser.add_argument("--state-dir",default=str(Path(__file__).resolve().parent.parent / ".workbench"))
    parser.add_argument("--no-browser",action="store_true")
    args = parser.parse_args()
    try:
        server = LocalHTTPServer(("127.0.0.1",args.port),BaseHTTPRequestHandler)
    except OSError:
        from urllib.request import urlopen
        try:
            url = f"http://127.0.0.1:{args.port}"
            with urlopen(url+"/api/bootstrap",timeout=3) as response:
                existing = json.load(response)
            if existing.get("application") != "muse-platform":
                raise ValueError("Another service is using this port")
            print("Muse Platform is already running: "+url,flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            return
        except Exception:
            raise SystemExit("Port is in use. Choose another port with --port.") from None
    app = Application(args.state_dir)
    server.RequestHandlerClass = make_handler(app)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_address[1]}"
    app.notifications.start(url)
    app.resume_timer=threading.Timer(.5,app.goals.resume_unattended);app.resume_timer.daemon=True;app.resume_timer.start()
    print("Muse Platform: " + url,flush=True)
    if not args.no_browser:
        threading.Timer(.8,lambda:webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()
        server.server_close()


if __name__ == "__main__":
    main()
