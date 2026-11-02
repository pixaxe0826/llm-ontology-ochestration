"""Separate provider interruptions from platform exceptions; preserve all paid usage."""
import time

PROVIDER_ERRORS={'APIConnectionError','APITimeoutError','RateLimitError','AuthenticationError','PermissionDeniedError','BadRequestError','NotFoundError','InternalServerError','APIStatusError'}
PROVIDER_ERRORS.update({'ConnectionResetError','ConnectionAbortedError','ConnectionError','BrokenPipeError','ConnectError','ReadError','RemoteProtocolError'})


def failure_kind(name):return 'provider' if name in PROVIDER_ERRORS else 'platform'


def recover_failed_chunk(manager,goal,item,task):
    """One bounded failed chunk waiver; no verdict, decision or ledger is changed."""
    if task.get('status')!='failed':return None
    errors=[(n,e) for n,e in enumerate(task.get('events',[])) if e.get('kind')=='error' and e.get('diagnostic')]
    if not errors:return None
    error_index,error=errors[-1];diagnostic=error['diagnostic'];name=diagnostic.get('exception_type')
    if name not in PROVIDER_ERRORS and name not in ('AttributeError','KeyError','TypeError','NameError','UnboundLocalError'):return None
    starts=[(n,e) for n,e in enumerate(task['events']) if e.get('kind')=='chunk_started']
    if not starts:return None
    index,chunk=starts[-1];token=task['id']+':'+chunk['chunk_id']
    if error_index<index:return None
    prior=item.get('failure_recovery_ids',{})
    if token in prior:return manager.store.get('goal_recovery',prior[token])
    current=task['events'][index:]
    substantive=any(e.get('kind') in ('change','gpu') or e.get('kind')=='tool_result' and e.get('name')=='run_command' and isinstance(e.get('result'),dict) and e['result'].get('exit_code') is not None for e in current)
    charged=chunk.get('attempt_charged',len(starts)==1)
    values={'bundles':max(0,item['bundles']-1),'attempts':max(0,item['attempts']-(1 if charged and not substantive else 0))}
    values['bundle_reported']=min(item.get('bundle_reported',0),values['bundles'])
    audit=manager.store.save('goal_recovery',{'kind':'builder_failed_chunk','goal_id':goal['id'],'plan_item_id':item['id'],'task_id':task['id'],
        'chunk_id':chunk['chunk_id'],'exception_type':name,'failure_kind':failure_kind(name),'diagnostic_id':diagnostic.get('id'),
        'before':{k:item.get(k) for k in ('attempts','bundles','bundle_reported','reworks')},'after':{**values,'reworks':item['reworks']},
        'reason':'Provider/platform interruption is not a Builder rework. Productive attempts and all work, messages, GPU jobs and ledger usage remain.','created':time.time(),'state':'prepared'})
    manager.store.update('plan_item',item['id'],**values,failure_recovery_ids={**prior,token:audit['id']})
    manager.store.update('goal_recovery',audit['id'],state='applied');return manager.store.get('goal_recovery',audit['id'])
