"""Shared, source-grounded ontology adapted from the user's Muse Spark Chat ZIP."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import queue
import threading
from uuid import NAMESPACE_URL, uuid4, uuid5

from .knowledge import KnowledgeVault, VaultError, add_references, words
from .security import clean_data, contains_key

FINISHED = {"succeeded", "failed", "cancelled", "paused", "interrupted"}


def uid(kind, source_id):
    return str(uuid5(NAMESPACE_URL, "muse-workbench:"+kind+":"+source_id))


class Memory:
    def __init__(self, store):
        self.store = store
        self.vault = KnowledgeVault(store.root / "memory")
        self.pending = queue.Queue()
        self.lock = threading.RLock()
        if not (self.vault.knowledge_dir / "concepts.json").exists():
            self.vault.set_definitions([
                {"label":"GPU", "aliases":["CUDA", "VRAM", "T4", "L4", "H100"]},
                {"label":"학습", "aliases":["training", "PyTorch", "epoch"]},
                {"label":"렌더링", "aliases":["rendering", "Blender", "Cycles"]},
            ])
        threading.Thread(target=self._worker, daemon=True).start()

    def enqueue(self, kind, record):
        if kind in ("task", "gpu") and record.get("status") in FINISHED:
            self.pending.put((kind, record))

    def _worker(self):
        while True:
            kind, record = self.pending.get()
            try:
                self.sync(kind, record)
            except Exception:
                self.store.save("memory_status", {"id":"latest", "error":"공유 메모리 동기화 실패. 작업 원문은 유지됩니다."})
            finally:
                self.pending.task_done()

    def sync(self, kind, record):
        source_id = uid(kind, record["id"])
        with self.lock:
            try:
                self.store.get("deleted_history", source_id)
                return
            except ValueError:
                pass
            messages = []
            if kind == "task":
                service = "baseten" if record.get("provider") == "baseten" else "meta"
                if record.get('attachment_context_start') is not None:
                    self.sync_attachment_responses(record,service)
                for i, event in enumerate(record.get("events", [])):
                    # Attachment-derived replies/diffs can quote private originals.
                    # Preserve earlier ordinary turns, but never index attachment context.
                    if record.get('attachment_context_start') is not None and i>=record['attachment_context_start']:
                        continue
                    role, status, text = "assistant", "complete", ""
                    if event["kind"] in ("user", "assistant"):
                        role = event["kind"]
                        text = event.get("text", "")
                        if role == "assistant":
                            status = event.get("message_status", "complete" if record["status"] == "succeeded" else "failed")
                    elif event["kind"] == "change":
                        text = "[로컬 파일 변경 관측] " + event["change"]["path"] + "\n" + event["change"].get("diff", "")[:5000]
                    if text.strip():
                        messages.append({"id":uid(source_id,str(i)), "role":role, "content":text, "status":status, "sources":event.get("sources", [])[:5]})
                title = record.get("title", "대화")
            else:
                service = "modal"
                title = record["spec"].get("entry") or record["spec"]["program"]
                observed = ""
                log = self.store.root / "gpu-jobs" / record["id"] / "artifacts" / "run.log"
                if log.is_file() and log.stat().st_size < 2_000_000:
                    observed = log.read_text(encoding="utf-8",errors="replace")[-1000:]
                text = f"[Modal 실제 실행 관측]\n작업 ID: {record['id']}\n프로그램: {record['spec']['program']}\n진입 파일: {title}\n선택 GPU: {record['spec']['gpu']}\n결과: {record['status']}\n실제 출력(run.log):\n{observed}\n" + json.dumps({"job_id":record["id"],"status":record["status"], "program":record["spec"]["program"], "gpu":record["spec"]["gpu"], "result":record.get("result"), "error":record.get("error"), "artifacts":record.get("artifacts",[])}, ensure_ascii=False, indent=2)
                messages.append({"id":uid(source_id,"job"), "role":"assistant", "content":text[:40000], "status":"complete"})
            fingerprint = hashlib.sha256(json.dumps([messages,record.get("share_memory",True)],sort_keys=True).encode()).hexdigest()
            try:
                meta = self.store.get("memory_source", source_id)
                if meta.get("fingerprint") == fingerprint:
                    return
                old = self.vault.get(source_id)
            except ValueError:
                meta, old = {}, None
            except VaultError:
                meta, old = {}, None
            shared = record.get("share_memory", meta.get("shared",True))
            labels = [service, *(word for word,_ in Counter(words(title)).most_common(4))]
            source = {"id":source_id, "title":f"[{service.capitalize()}] {title}"[:200], "revision":old["revision"] if old else 0,
                "shared":bool(shared), "concepts":list(dict.fromkeys(labels)), "messages":clean_data(messages)}
            saved, warning = self.vault.save(source)
            self.store.save("memory_source", {"id":source_id,"kind":kind,"source_id":record["id"],"service":service,"workspace":record.get("workspace",""),"fingerprint":fingerprint,"shared":bool(shared)})

    def sync_attachment_responses(self, record, service):
        """Index generated responses separately, excluded by default. Never copy inputs."""
        for index,event in enumerate(record.get('events',[])):
            if index<record['attachment_context_start'] or event['kind']!='assistant' or not event.get('text','').strip():continue
            ident=uid('task_response',record['id']+':'+str(index))
            status=event.get('message_status','complete' if record['status']=='succeeded' else 'failed')
            message={'id':uid(ident,'answer'),'role':'assistant','content':event['text'],'status':status,'sources':event.get('sources',[])[:5]}
            shared=event.get('shared',False) is True
            fingerprint=hashlib.sha256(json.dumps([message,shared],sort_keys=True).encode()).hexdigest()
            try:
                meta=self.store.get('memory_source',ident)
                if meta.get('fingerprint')==fingerprint:continue
                revision=self.vault.get(ident)['revision']
            except (ValueError,VaultError):revision=0
            self.vault.save({'id':ident,'title':f"[{service.capitalize()}] {record.get('title','대화')} · 응답 {index+1}"[:200],
                'revision':revision,'shared':shared,'concepts':[service],'messages':[message]})
            self.store.save('memory_source',{'id':ident,'kind':'task_response','source_id':record['id'],'event_index':index,
                'service':service,'fingerprint':fingerprint,'shared':shared,'attachment_related':True})

    def context(self, prompt, task_id):
        self.pending.join()
        sources = self.vault.search(prompt, exclude=uid("task",task_id), limit=5, budget=6000)
        # Keep observed execution headers + logs in the cited window, not just deep JSON metrics.
        for source in sources:
            try:
                meta = self.store.get("memory_source",source["chat_id"])
                if meta.get("service") == "modal":
                    chat = self.vault.get(source["chat_id"])
                    message = next(m for m in chat["messages"] if m["id"]==source["message_id"])
                    source["excerpt"] = message["content"][:1400]
            except (ValueError,VaultError,StopIteration):
                pass
        def priority(source):
            try:
                return 0 if self.store.get("memory_source",source["chat_id"]).get("service")=="modal" else 1
            except ValueError:
                return 1
        sources.sort(key=priority)
        output, left = [],6000
        for source in sources:
            if left<200:
                break
            source["excerpt"] = source["excerpt"][:min(1400,left)]
            left-=len(source["excerpt"])
            output.append(source)
        return output

    def graph(self):
        self.pending.join()
        graph = self.vault.graph()
        graph.pop("folder", None)
        mapping = {item["id"]:item for item in self.store.items("memory_source",10000)}
        graph["sources"] = [{**chat, **{k:v for k,v in mapping.get(chat["id"],{}).items() if k in ("kind","source_id","service","workspace","attachment_related")}} for chat in graph.pop("chats")]
        graph["services"] = {key:sum(s.get("service")==key and s["shared"] for s in graph["sources"]) for key in ("meta","baseten","modal")}
        return graph

    def source(self, source_id):
        self.pending.join()
        result=self.vault.get(source_id)
        try:result['attachment_related']=self.store.get('memory_source',source_id).get('attachment_related',False)
        except ValueError:pass
        return result

    def search(self, query):
        self.pending.join()
        return self.vault.search(query)

    def definitions(self, value):
        self.vault.set_definitions(value)
        return self.graph()

    def share(self, source_id, shared, attachment_confirmed=False):
        self.pending.join()
        with self.lock:
            try:metadata=self.store.get('memory_source',source_id)
            except ValueError:metadata={}
            if shared and metadata.get('attachment_related') and attachment_confirmed is not True:
                raise ValueError('첨부 내용이 포함될 수 있음. 확인 후 이 응답을 공유하세요.')
            source = self.vault.get(source_id)
            source["shared"] = bool(shared)
            self.vault.save(source)
            try:
                meta = self.store.get("memory_source",source_id)
                if meta['kind']=='task_response':self.store.update_event(meta['source_id'],meta['event_index'],shared=bool(shared))
                else:self.store.update(meta["kind"],meta["source_id"],share_memory=bool(shared))
                self.store.update("memory_source",source_id,shared=bool(shared))
            except ValueError:
                pass
        return self.graph()

    def goal_sources(self, goal_id, tasks, jobs):
        linked={uid('task',row['id']) for row in tasks}|{uid('gpu',row['id']) for row in jobs}
        linked.update(row['id'] for row in self.store.items('memory_source',None) if row.get('goal_id')==goal_id)
        return [source for source in self.vault.all() if source['id'] in linked or goal_id in source.get('concepts',[])]

    def exclude_goal(self, goal_id, tasks, jobs):
        # Caller has drained queued syncs and holds memory.lock; no source is removed.
        sources=self.goal_sources(goal_id,tasks,jobs)
        for source in sources:
            if source['shared']:
                source['shared']=False;self.vault.save(source)
            try:self.store.update('memory_source',source['id'],shared=False)
            except ValueError:pass
        for kind,records in (('task',tasks),('gpu',jobs)):
            for record in records:self.store.update(kind,record['id'],share_memory=False)
        return len(sources)

    def add_note(self, title, text, concepts):
        if not text.strip() or len(text)>50000 or contains_key(text):
            raise ValueError("메모 길이 또는 비밀값 포함 여부를 확인하세요.")
        record={"id":str(uuid4()),"title":title[:200] or "메모","revision":0,"shared":True,"concepts":concepts[:30],
                "messages":[{"id":str(uuid4()),"role":"user","content":text,"status":"complete"}]}
        saved,_=self.vault.save(record)
        self.store.save("memory_source",{"id":saved["id"],"kind":"note","service":"user","source_id":saved["id"],"shared":True})
        return self.graph()

    def import_folder(self, root):
        from pathlib import Path
        from .workspace import Workspace
        path = Workspace(root,self.store.root).root
        incoming = KnowledgeVault(path)
        chats = incoming.all()
        if len(chats)>1000:
            raise ValueError("한 번에 1000개 대화까지 가져올 수 있습니다.")
        imported, skipped = 0,0
        with self.lock:
            existing = {c["id"] for c in self.vault.all()}
            for chat in chats:
                if chat["id"] in existing:
                    skipped+=1
                    continue
                if contains_key(json.dumps(chat,ensure_ascii=False)):
                    raise ValueError("등록된 키가 포함된 대화는 가져오지 않습니다.")
                chat["revision"]=0
                saved,_=self.vault.save(clean_data(chat))
                self.store.save("memory_source",{"id":saved["id"],"kind":"imported","source_id":saved["id"],"service":"baseten","shared":saved["shared"]})
                imported+=1
            definitions=self.vault.definitions()
            used={item["label"].casefold() for item in definitions}
            for definition in incoming.definitions():
                if definition["label"].casefold() not in used:
                    try:
                        self.vault.check_definitions(definitions+[definition])
                    except VaultError:
                        continue
                    definitions.append(definition)
                    used.add(definition["label"].casefold())
            self.vault.set_definitions(definitions)
        return {"imported":imported,"skipped":skipped,"graph":self.graph()}
