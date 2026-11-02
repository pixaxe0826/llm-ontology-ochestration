"""Local-only exception diagnostics. Never capture frame locals or provider payloads."""
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import traceback
import uuid

from .security import contains_key, is_link, redact

_lock = threading.RLock()


def _redacted(value):
    if isinstance(value,str):return redact(value)
    if isinstance(value,list):return [_redacted(v) for v in value]
    if isinstance(value,dict):return {k:_redacted(v) for k,v in value.items()}
    return value


def log_exception(state_root, error, *, component, goal_id=None, task_id=None, plan_item_id=None):
    frames = []
    current = error.__traceback__
    while current:
        frame = current.tb_frame
        frames.append({'file':frame.f_code.co_filename, 'line':current.tb_lineno,
                       'module':str(frame.f_globals.get('__name__', 'unknown')),
                       'function':frame.f_code.co_qualname})
        current = current.tb_next
    location = frames[-1] if frames else {'file':'unknown', 'line':0, 'module':'unknown', 'function':'unknown'}
    public = {'id':uuid.uuid4().hex, 'exception_type':type(error).__name__, **location, 'logged':False}
    record = {**public, 'logged':True, 'time_utc':datetime.now(timezone.utc).isoformat(), 'component':component,
              'goal_id':goal_id, 'task_id':task_id, 'plan_item_id':plan_item_id,
              'attribute':getattr(error, 'name', None) if isinstance(error, AttributeError) else None,
              'frames':frames, 'traceback':''.join(traceback.format_exception(error))}
    # Redact before opening a log file; exception messages can contain credentials.
    serialized = redact(json.dumps(_redacted(record), ensure_ascii=False))
    public = _redacted(public)
    if contains_key(serialized):return public
    try:
        root = Path(state_root).resolve()
        directory = root/'diagnostics'
        path = directory/'exceptions.jsonl'
        with _lock:
            if is_link(directory) or is_link(path):return public
            directory.mkdir(exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:stream.write(serialized+'\n')
        public['logged'] = True
    except OSError:
        # Diagnostic failure must never replace the original platform exception.
        pass
    return public


def exception_summary(diagnostic, component='Coordinator'):
    return (f"{component} platform exception: {diagnostic['exception_type']} at "
            f"{diagnostic['module']}.{diagnostic['function']}:{diagnostic['line']}. "
            'Original exception text is hidden. '+
            (f"Local diagnostic: {diagnostic['id']}." if diagnostic['logged'] else 'Local diagnostic could not be written.'))
