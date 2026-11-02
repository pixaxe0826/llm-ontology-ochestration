"""Bounded display-response cache; no execution, policy or evidence consumers."""
from collections import OrderedDict
import hashlib,json,threading,time
from .security import clean_data,redact
from .ui_metrics import span

class ViewResponses:
    def __init__(self,maximum=16*1024*1024):
        self.lock=threading.Lock();self.values=OrderedDict();self.maximum=maximum;self.size=0

    def get(self,key):
        with self.lock:
            value=self.values.get(key)
            if value is not None:self.values.move_to_end(key)
            return value

    def put(self,key,value):
        if len(value)>self.maximum:return
        with self.lock:
            if key in self.values:self.size-=len(self.values.pop(key))
            self.values[key]=value;self.size+=len(value)
            while self.size>self.maximum or len(self.values)>64:
                _,removed=self.values.popitem(last=False);self.size-=len(removed)

    @staticmethod
    def eligible(path):
        import re
        return path in ('/api/ui/history','/api/tasks','/api/jobs','/api/goals','/api/notifications','/api/execution-index') or bool(re.fullmatch(r'/api/(?:goals|tasks)/[a-f0-9]{16}',path))

    def response(self,app,path,query):
        version=app.store.version()
        runtime=(tuple(app.agents.active),tuple(app.goals.builders.active),tuple(app.gpu.active),tuple(app.goals.long_commands.runtime))
        clock=int(time.time()) if any(runtime) else None
        key=(path,json.dumps(query,sort_keys=True),version,runtime,clock)
        enabled=app.ui_metrics.config.get('response_cache',True)
        body=self.get(key) if enabled else None
        if body is None:
            value=app.handle('GET',path,{},query)
            with span('serialize'):body=json.dumps(clean_data(value),ensure_ascii=False).encode('utf-8')
            if enabled and app.store.version()==version:self.put(key,body)
        else:
            # Newly registered credentials must also disappear from an existing cached view.
            with span('serialize'):body=redact(body.decode('utf-8')).encode('utf-8')
        tag='W/"'+hashlib.sha256(body).hexdigest()+'"'
        return body,tag
