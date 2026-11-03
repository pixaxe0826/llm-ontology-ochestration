"""Fresh Goal tasks and one retry for explicitly recorded server interruption only."""
import time
from .item_states import terminal

TERMINATED={'interrupted','failed','cancelled'}

def last_chunk(task):
    return next((e.get('chunk_id') for e in reversed(task.get('events',[])) if e.get('kind')=='chunk_started'),None)

def restart_only(task):
    marked=(task.get('restart_interruption') or {}).get('kind')=='server_restart'
    legacy=task.get('status')=='interrupted' and task.get('error')=='서버가 재시작됐습니다. 실행 상태를 확인한 뒤 계속하거나 중지하세요.'
    if task.get('status') not in TERMINATED or not (marked or legacy):return False
    events=task.get('events',[]);start=max((n for n,e in enumerate(events) if e.get('kind')=='chunk_started'),default=0)
    if task.get('status')=='failed' and any(e.get('kind')=='tool_result' and isinstance(e.get('result'),dict) and e['result'].get('error') for e in events[start:]):return False
    return not any(e.get('kind')=='error' for e in events[start:])

def paused_cancellation(task):
    if task.get('status')!='cancelled' or restart_only(task):return False
    error=task.get('error') or ''
    return (not error or error.startswith(('Goal is not running:','Builder was stopped.','Human interrupt'))) and not any(e.get('kind')=='error' for e in task.get('events',[])[max((n for n,e in enumerate(task.get('events',[])) if e.get('kind')=='chunk_started'),default=0):])

class GoalBuilderRestarts:
    def reusable_builder(self,goal,item,task):
        return bool(task and task.get('goal_id')==goal['id'] and task.get('plan_item_id')==item['id'] and
            task.get('generation')==goal['generation'] and task.get('status') in ('succeeded','paused') and
            not (self.builders.cancels.get(task['id']) and self.builders.cancels[task['id']].is_set()))

    def retry_restart_chunk(self,goal,item,task):
        if terminal(item) or not restart_only(task) or task['id'] in self.builders.active:return False
        chunk=last_chunk(task)
        # If the one replacement chunk is interrupted again, the normal failure decision wins.
        if any(r.get('retry_task_id')==task['id'] and r.get('retry_chunk_id')==chunk for r in self.store.items('goal_restart_retry',None)):return False
        identifier=task['id']+':'+str(chunk or 'queued')
        try:audit=self.store.get('goal_restart_retry',identifier)
        except ValueError:
            events=task.get('events',[]);start=max((n for n,e in enumerate(events) if e.get('kind')=='chunk_started'),default=0)
            productive=any(e.get('kind') in ('change','gpu') or e.get('kind')=='tool_result' and e.get('name') in ('run_command','start_long_command') for e in events[start:])
            charged=next((e.get('attempt_charged',False) for e in reversed(events) if e.get('kind')=='chunk_started'),task.get('attempt_charged',False))
            audit=self.store.save('goal_restart_retry',{'id':identifier,'goal_id':goal['id'],'plan_item_id':item['id'],'source_task_id':task['id'],'source_chunk_id':chunk,
                'state':'claimed','before':{k:item.get(k) for k in ('status','bundles','bundle_reported','attempts','reworks','task_id')},'created':time.time(),**self.times(goal)})
            bundles=max(0,item['bundles']-1)
            self.store.update('plan_item',item['id'],status='todo',task_id=None,bundles=bundles,bundle_reported=min(item.get('bundle_reported',0),bundles),
                attempts=max(0,item['attempts']-(1 if charged and not productive else 0)),wait_reason=None,waiting_resume=None,continue_pending=bundles>0,
                restart_retry_pending=audit['id'],restart_retry_ids=list(dict.fromkeys([*item.get('restart_retry_ids',[]),audit['id']])))
            self.message(goal,'System','Builder','NOTE','Server interruption: one fresh Builder chunk will continue this item using preserved summaries, read history and evidence. Inspect recorded outcomes; never replay a command, write or GPU submission just because the server restarted.',item['id'],[audit['id'],task['id']])
            self.store.update('goal_restart_retry',audit['id'],state='pending_dispatch')
        return audit.get('state') in ('claimed','pending_dispatch')
