import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .security import clean_data
from .ui_metrics import span, locked

_view_cache=ContextVar('store_ui_read_cache',default=None)
_display_read=ContextVar('store_pure_display_read',default=False)


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.on_save = None
        self.db = sqlite3.connect(self.root / "workbench.sqlite3", check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT, updated REAL, PRIMARY KEY(kind,id))")
        self.db.execute("CREATE INDEX IF NOT EXISTS records_kind_updated ON records(kind,updated DESC)")
        self.db.commit()

    @contextmanager
    def read_cache(self):
        """Request-local copies only; never used by the agent/execution loop."""
        token=_view_cache.set({})
        try:yield
        finally:_view_cache.reset(token)

    def version(self):
        with locked(self.lock):
            return self.db.total_changes,self.db.execute('PRAGMA data_version').fetchone()[0]

    @contextmanager
    def display_read(self):
        """Borrow private request-local values only inside the pure detail formatter."""
        token=_display_read.set(_view_cache.get() is not None)
        try:yield
        finally:_display_read.reset(token)

    @staticmethod
    def _copy(value):return value if _display_read.get() else deepcopy(value)

    def _cache(self):
        cache=_view_cache.get()
        if cache is None:return None
        version=self.version();key=id(self)
        if key not in cache or cache[key][0]!=version:cache[key]=(version,{})
        return cache[key][1]

    def save(self, kind, data, *, preserve_updated=False):
        data = clean_data(data)
        data.setdefault("id", uuid.uuid4().hex[:16])
        if not preserve_updated or 'updated' not in data:data["updated"] = time.time()
        with locked(self.lock):
            self.db.execute("INSERT OR REPLACE INTO records VALUES(?,?,?,?)", (kind, data["id"], json.dumps(data, ensure_ascii=False), data["updated"]))
            self.db.commit()
        if self.on_save:
            self.on_save(kind, data)
        return data

    def update_event(self, task_id, index, **values):
        with locked(self.lock):
            task = self.get("task",task_id)
            task["events"][index].update(values)
            return self.save("task",task)

    def get(self, kind, record_id):
        with locked(self.lock):
            cache=self._cache();key=('get',kind,record_id)
            if cache is not None and key in cache:return self._copy(cache[key])
            if cache is not None and ('items',kind,None) in cache:
                record=next((r for r in cache[('items',kind,None)] if r['id']==record_id),None)
                if record is None:raise ValueError("항목을 찾을 수 없습니다.")
                return self._copy(record)
            with span("db"):
                row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, record_id)).fetchone()
        if not row:
            raise ValueError("항목을 찾을 수 없습니다.")
        record=json.loads(row[0])
        if cache is not None:cache[key]=self._copy(record)
        return record

    def items(self, kind, limit=60):
        with locked(self.lock):
            cache=self._cache();key=('items',kind,limit)
            if cache is not None and key in cache:return self._copy(cache[key])
            if cache is not None and ('items',kind,None) in cache:
                rows=cache[('items',kind,None)]
                return self._copy(rows if limit is None or limit<0 else rows[:limit])
            with span("db"):
                rows = self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY updated DESC LIMIT ?", (kind, -1 if limit is None else limit)).fetchall()
        result=[json.loads(row[0]) for row in rows]
        if cache is not None:cache[key]=self._copy(result)
        return result

    def delete(self, kind, record_id):
        with locked(self.lock):
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, record_id))
            self.db.commit()

    def event(self, task_id, kind, **payload):
        with locked(self.lock):
            task = self.get("task", task_id)
            task.setdefault("events", []).append({"kind": kind, "time": time.time(), **payload})
            return self.save("task", task)

    def update(self, kind, record_id, **values):
        with locked(self.lock):
            record = self.get(kind, record_id)
            record.update(values)
            return self.save(kind, record)
