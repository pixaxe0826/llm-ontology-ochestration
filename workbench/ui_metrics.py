"""Local-only request measurements. Never log bodies, queries, headers or credentials."""
from contextlib import contextmanager
from contextvars import ContextVar
import json,os,threading,time
from pathlib import Path
from urllib.parse import urlsplit
from .security import redact

current=ContextVar('ui_request_metrics',default=None)

def accepts_gzip(header):
    for part in header.lower().split(','):
        pieces=[p.strip() for p in part.split(';')]
        if pieces[0]!='gzip':continue
        try:quality=next((float(p.split('=',1)[1]) for p in pieces[1:] if p.startswith('q=')),1)
        except (ValueError,IndexError):continue
        if 0<quality<=1:return True
    return False

@contextmanager
def span(name):
    metric=current.get();start=time.perf_counter() if metric else 0
    try:yield
    finally:
        if metric:metric[name]=metric.get(name,0)+(time.perf_counter()-start)*1000

@contextmanager
def locked(lock):
    with span('lock'):lock.acquire()
    try:yield
    finally:lock.release()

class RequestMetrics:
    def __init__(self,root):
        self.root=Path(root);self.lock=threading.Lock()
        try:config=json.loads(Path(__file__).with_name('ui_performance.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):config={}
        self.config=config
        self.enabled=config.get('timing',True) and os.environ.get('MUSE_UI_TIMING','1')!='0'

    @contextmanager
    def request(self,handler):
        if not self.enabled:yield;return
        metric={'db':0,'file':0,'lock':0,'serialize':0,'started':time.perf_counter()};token=current.set(metric)
        try:yield
        finally:
            current.reset(token)
            if 'bytes' in metric:
                row={'time':time.time(),'method':getattr(handler,'command',''),'path':redact(urlsplit(getattr(handler,'path','')).path)[:180],
                     'status':metric.get('status'),'duration_ms':round((time.perf_counter()-metric['started'])*1000,3),
                     **{k:round(metric.get(k,0),3) for k in ('db','file','lock','serialize')},
                     'response_bytes':metric['bytes'],'wire_bytes':metric.get('wire_bytes',metric['bytes'])}
                try:
                    with self.lock:
                        folder=self.root/'diagnostics';folder.mkdir(exist_ok=True)
                        path=folder/'requests.jsonl'
                        if path.exists() and path.stat().st_size>5*1024*1024:
                            path.replace(folder/'requests.previous.jsonl')
                        with path.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
                except OSError:pass

def response_metrics(status,size,wire_size=None):
    metric=current.get()
    if metric is None:return None
    metric.update(status=status,bytes=size,wire_bytes=size if wire_size is None else wire_size)
    total=(time.perf_counter()-metric['started'])*1000
    return ', '.join([f'total;dur={total:.2f}',*[f'{k};dur={metric.get(k,0):.2f}' for k in ('db','file','lock','serialize')]])
