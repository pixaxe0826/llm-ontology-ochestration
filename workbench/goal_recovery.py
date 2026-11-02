"""Explicit, idempotent recovery of the recorded pre-fix P2 platform failures."""
import json
import time
from .workspace import Workspace

ERROR='Review was not supported by valid evidence: Unique plan keys required.'


def recover_proxy_item(store,goal_id,item_id):
    goal=store.get('goal',goal_id);item=store.get('plan_item',item_id)
    if item.get('platform_recovery_id'):return store.get('goal_recovery',item['platform_recovery_id'])
    if goal['status']!='paused' or item['goal_id']!=goal_id:raise ValueError('Recovery requires the same user-paused Goal.')
    if ERROR not in item.get('defects',[]):raise ValueError('The recorded Planner-format defect is not present.')
    reviews=[]
    for message in store.items('message',None):
        if message.get('goal_id')==goal_id and message.get('plan_item_id')==item_id and message['type']=='REVIEW':
            value=json.loads(message['body'])
            if value.get('decision')=='rework':
                if value.get('defects')!=[ERROR]:raise ValueError('A real Builder defect is present; do not reset its counters.')
                reviews.append(message['id'])
    tasks=[store.get('task',tid) for tid in item['task_ids']]
    if not reviews or item['reworks']!=len(reviews) or item['attempts']!=len(tasks) or item['bundles']!=len(tasks):
        raise ValueError('Counter history differs from the inspected platform-failure sequence.')
    if any(t.get('goal_id')!=goal_id or t.get('plan_item_id')!=item_id or t['status'] in ('running','queued','waiting_approval') for t in tasks):
        raise ValueError('A task is active or belongs to another item.')
    ws=Workspace(goal['workspace'],store.root)
    hashes={p:ws.read(p)['sha256'] for p in ('tools/proxy_check.py','review/PROXY_EVIDENCE.md')}
    requirement={'script':'tools/proxy_check.py','script_sha256':hashes['tools/proxy_check.py'],
                 'output':'review/PROXY_EVIDENCE.md','argv':['python','tools/proxy_check.py','review/PROXY_EVIDENCE.md'],
                 'stale_task_ids':item['task_ids'][:]}
    changes={'status':'todo','reworks':0,'attempts':0,'bundles':0,'bundle_reported':0,'task_id':None,'continue_pending':False,
             'defects':[d for d in item['defects'] if d!=ERROR],'review_error':None,'review_error_count':0,
             'rerun_requirement':requirement,
             'rerun_instruction':'Platform recovery authorized a fresh execution after the proxy model/catalog and legacy output-budget fixes. Run the existing tools/proxy_check.py unchanged with review/PROXY_EVIDENCE.md as its output, using the injected Goal proxy environment. The prior two-attempt limit concerned a different, broken platform revision. Preserve all historical records; obtain new command evidence and report the new result. Historical failures and a local marker-only check cannot satisfy this item.'}
    recovery=store.save('goal_recovery',{'goal_id':goal_id,'plan_item_id':item_id,'created':time.time(),'kind':'platform_proxy_rerun',
        'reason':'Pre-fix proxy failures, their blocked-report batch and the malformed Planner review consumed the item retry budget.',
        'waived_task_ids':item['task_ids'][:],'invalid_review_message_ids':reviews,'before':item,'changes':changes,
        'preserved_file_sha256':hashes,'state':'prepared'})
    store.update('plan_item',item_id,**changes,platform_recovery_id=recovery['id'])
    for path,sha in hashes.items():
        if ws.read(path)['sha256']!=sha:raise ValueError('Workspace evidence changed during recovery; inspect before proceeding.')
    store.update('goal_recovery',recovery['id'],state='applied')
    return store.get('goal_recovery',recovery['id'])
