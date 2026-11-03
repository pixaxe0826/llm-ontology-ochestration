"""Small credential-safe failure details for Goal cards; never expose traceback bodies."""
import json,re
from .security import redact,contains_key,is_link
from .builder_failures import failure_kind

def safe_error(value,limit=1800):
    text=redact(str(value or 'Unknown Builder failure.'))
    text=re.sub(r'https?://\S+','[URL]',text)
    return text[:limit] if not contains_key(text) else 'Protected error details redacted.'

def failure_details(manager,task):
    events=task.get('events',[]);start=max((n for n,e in enumerate(events) if e.get('kind')=='chunk_started'),default=0);current=events[start:]
    diagnostic=next((e['diagnostic'] for e in reversed(current) if e.get('kind')=='error' and e.get('diagnostic')),{})
    failed_tool=next((e for e in reversed(current) if e.get('kind')=='tool_result' and isinstance(e.get('result'),dict) and e['result'].get('error')),None)
    tool_diagnostic=bool(not diagnostic and failed_tool and failed_tool['result'].get('diagnostic'))
    if tool_diagnostic:diagnostic=failed_tool['result']['diagnostic']
    name=diagnostic.get('exception_type') or ('Interrupted' if task['status'] in ('interrupted','cancelled') else 'BuilderFailure')
    stage=(diagnostic.get('module','')+'.'+diagnostic.get('function','')).strip('.') or 'Builder/model response'
    if tool_diagnostic:stage='tool: '+failed_tool.get('name','unknown')+' / '+stage
    message=safe_error(task.get('error'))
    path=manager.store.root/'diagnostics/exceptions.jsonl'
    if diagnostic.get('id') and path.is_file() and not is_link(path.parent) and not is_link(path):
        try:
            with path.open('rb') as stream:
                stream.seek(max(0,path.stat().st_size-2_000_000));lines=stream.read().decode('utf-8',errors='replace').splitlines()
            for line in reversed(lines):
                try:record=json.loads(line)
                except ValueError:continue
                if record.get('id')!=diagnostic['id'] or record.get('task_id')!=task['id'] or record.get('goal_id')!=task.get('goal_id'):continue
                tail=str(record.get('traceback','')).strip().splitlines()
                if tail:message=safe_error(tail[-1])
                break
        except OSError:pass
    if failed_tool and not diagnostic:
        stage='tool: '+failed_tool.get('name','unknown');message=safe_error(failed_tool['result']['error'])
    return {'task_id':task['id'],'chunk_id':next((e.get('chunk_id') for e in reversed(current) if e.get('kind')=='chunk_started'),None),
        'error':message,'stage':safe_error(stage,240),'exception_type':name,'category':failure_kind(name),'diagnostic_id':diagnostic.get('id')}

def escalate_failure(manager,goal,item,task):
    failure=failure_details(manager,task)
    question='Builder execution failed.\nTask: '+task['id']+'\nStage: '+failure['stage']+'\nError: '+failure['error']
    decision=manager.create_decision(goal,{'question':question,'options':[],'recommendation':'','reason':'Inspect the observed failure before retrying.','affected_items':[item['id']],'reversible':False,'failure':failure},
        item_id=item['id'],source='builder-failure:'+task['id']+':'+str(failure['chunk_id']),role='System')
    manager.message(goal,'System','Human','ESCALATE',question,item['id'],[decision['id'],task['id']])
    return decision
