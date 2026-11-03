"""Keep failed observations for review without treating them as successful execution."""
import hashlib
import json
import re
from .security import redact
from .workspace import Workspace
from .command_files import discover,file_hash
from .item_states import dependency_target

EXECUTION_KINDS=('run_command','submit_gpu_job','get_gpu_job')


def execution_outcome(kind,result):
    if kind not in EXECUTION_KINDS:return None
    if kind=='run_command':
        interrupted=bool(result.get('interrupted') or result.get('cancelled'))
        timeout=result.get('timed_out',False if not interrupted else None)
        code=result.get('exit_code')
        success=code==0 and not interrupted and not timeout and not result.get('error') and not result.get('denied') and not result.get('budget_refused')
        return {'success':bool(success),'exit_code':code,'timeout':timeout,'interrupted':interrupted,
            'cancelled':bool(result.get('cancelled')),'timeout_s':result.get('timeout_s'),'duration_s':result.get('duration_s'),
            'output_excerpt':redact(str(result.get('output') or result.get('error') or ''))[:1500],
            **({key:result.get(key) for key in ('long_command_id','proxy_cost','budget_refused','proxy_rejections')} if result.get('long_command_id') else {})}
    inner=result.get('result') or {};code=inner.get('exit_code');status=result.get('status')
    return {'success':status=='succeeded' and code==0,'exit_code':code,'status':status,
        'gpu_job_id':result.get('id') or result.get('job_id'),'call_id':result.get('call_id'),
        'duration_s':(inner.get('summary') or {}).get('duration_s'),'failure':result.get('failure') or inner.get('failure')}


def gpu_record(manager,task,result):
    identifier=result.get('id') or result.get('job_id')
    if not identifier:return result
    try:job=manager.store.get('gpu',identifier)
    except ValueError:return result
    # An unrelated maintenance smoke must never prove a Goal item's acceptance.
    if job.get('task_id')!=task['id'] or job.get('workspace')!=task.get('workspace'):return None
    return {**{k:v for k,v in job.items() if k not in ('manifest','logs')},'input_manifest':job.get('manifest',[])}


def gpu_files(manager,result):
    entries=[]
    for name in result.get('artifacts',[]):
        try:
            path=manager.gpu.artifact(result['id'],name);data=path.read_bytes()
            entries.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'location':'local'})
        except (ValueError,OSError,KeyError):continue
    return entries


def collect(manager,item,task):
    existing=list(item.get('evidence',[]));fingerprints={e['sha256'] for e in existing};observed_files={};new=[]
    stale=set((item.get('rerun_requirement') or {}).get('stale_task_ids',[]))
    for entry in existing:
        if entry.get('path') and entry.get('task_id') not in stale:observed_files[entry['path']]=entry.get('file_sha256')
    folder=task.get('workspace')
    if not folder:
        try:folder=manager.store.get('goal',item['goal_id'])['workspace']
        except (ValueError,KeyError):pass
    ws=Workspace(folder,manager.store.root) if folder else None;command={};changed_sources={}
    for index,event in enumerate(task.get('events',[])):
        if event['kind']=='tool' and event.get('name')=='run_command':command=event.get('arguments',{});continue
        kind=event['kind'];payload=None;path=None;file_sha=None;artifacts=[]
        if kind=='change':
            change=event['change'];path=change['path'];file_sha=change['sha256'];observed_files[path]=file_sha
            if path.endswith('.py'):changed_sources[path]=file_sha
            payload={k:v for k,v in change.items() if k!='backup_id'}
        elif kind=='gpu_artifact_copy':
            kind='copy_gpu_artifact';payload=event.get('result') or {};path=payload.get('path');file_sha=payload.get('destination_sha256')
            if path:observed_files[path]=file_sha
        elif kind=='gpu':
            kind='submit_gpu_job';payload=gpu_record(manager,task,{'id':event['job_id']})
        elif kind=='tool_result':
            kind=event.get('name')
            if kind not in ('read_file','read_structured','list_files','search_files','run_command','submit_gpu_job','get_gpu_job','read_gpu_artifact'):continue
            payload=event.get('result')
            if isinstance(payload,list) and kind=='search_files':payload={'matches':payload}
            if not isinstance(payload,dict):continue
            if kind in ('submit_gpu_job','get_gpu_job'):payload=gpu_record(manager,task,payload)
            elif kind not in EXECUTION_KINDS and (payload.get('error') or payload.get('denied') or payload.get('cancelled')):continue
            if kind=='run_command' and item.get('rerun_requirement'):observed_files.pop(item['rerun_requirement']['output'],None)
            if kind in ('read_file','read_structured'):
                path=payload.get('path');file_sha=payload.get('sha256')
                if path:observed_files[path]=file_sha
        if payload is None:continue
        if kind in ('submit_gpu_job','get_gpu_job'):artifacts=gpu_files(manager,payload)
        outcome=execution_outcome(kind,payload)
        comparable={k:v for k,v in payload.items() if k not in ('duration_s','started','finished','updated')}
        inputs=dict(observed_files);tracking=None
        if kind=='run_command':
            tracking=payload.get('command_files')
            if not isinstance(tracking,dict):
                from .command_cwd import normalize_cwd,MISSING
                try:
                    if ws is None:raise ValueError('Workspace unavailable.')
                    hints=discover(ws,command.get('argv',[]),normalize_cwd(command.get('cwd',MISSING))[0])
                except (ValueError,OSError):hints={'inputs':[],'outputs':[],'method':'Historical command paths unavailable.'}
                recorded=next((e.get('files',{}) for e in existing if e['id']==task['id']+':'+str(index)),observed_files)
                inferred={p:recorded[p] for p in hints['inputs'] if recorded.get(p)}
                if not command.get('argv'):inferred=dict(changed_sources)
                tracking={'inputs':inferred,'outputs':{},
                    'method':hints['method'],'unverified_inputs':[p for p in hints['inputs'] if not recorded.get(p)],
                    'output_candidates':hints['outputs'],'note':'Legacy recovery uses recorded hashes only; no historical output hash is invented.'}
            inputs=dict(tracking.get('inputs',{}))
            if isinstance(comparable.get('output'),str):comparable['output']=re.sub(r'\b\d+(?:\.\d+)?s\b','<duration>',comparable['output'])
            comparable['files']=inputs
        if artifacts:comparable['artifact_hashes']=artifacts
        fingerprint=hashlib.sha256(json.dumps([kind,comparable],ensure_ascii=False).encode()).hexdigest()
        identifier=task['id']+':'+str(index)
        ref={'id':identifier,'task_id':task['id'],'event_index':index,'kind':kind,'sha256':fingerprint,'path':path,
            'file_sha256':file_sha,'files':inputs,'observed_at':event.get('time',task.get('updated',0)),
            'goal_id':item['goal_id'],'plan_item_id':item['id'],
            'gpu_job_id':(payload.get('id') or payload.get('job_id')) if kind in ('submit_gpu_job','get_gpu_job') else None}
        # A file observation proves that file, not every other file ever read in this item.
        if kind not in EXECUTION_KINDS:ref['files']={}
        if kind=='copy_gpu_artifact':
            ref.update(gpu_job_id=payload.get('source_job_id'),source_job_id=payload.get('source_job_id'),
                submitting_item_id=payload.get('submitting_item_id'),copying_item_id=payload.get('copying_item_id'),
                source_sha256=payload.get('source_sha256'),destination_sha256=payload.get('destination_sha256'),
                source_artifact=payload.get('source_artifact'),operation=payload.get('operation'))
            ref['gpu_input_hashes']=payload.get('gpu_input_hashes')
        if tracking is not None:ref['command_files']=tracking
        if kind in ('submit_gpu_job','get_gpu_job'):
            # A later report/notes edit is not a change to the GPU's approved inputs.
            ref['files']={f['path']:f['sha256'] for f in payload.get('input_manifest',[]) if isinstance(f,dict) and f.get('path') and f.get('sha256')}
        if outcome is not None:ref.update(execution=outcome,supports_acceptance=outcome['success'])
        if artifacts:ref['gpu_artifacts']=artifacts
        prior=next((n for n,e in enumerate(existing) if e['id']==identifier),None)
        if prior is not None:
            # Upgrade metadata under the same stable evidence ID, never duplicate it.
            if existing[prior]!=ref:existing[prior]=ref
        elif fingerprint not in fingerprints or kind in EXECUTION_KINDS:
            existing.append(ref)
            if fingerprint not in fingerprints:new.append(ref)
            fingerprints.add(fingerprint)
        if kind=='run_command':
            for name,sha in (tracking or {}).get('outputs',{}).items():
                output_ref={'id':identifier+'/output:'+name,'task_id':task['id'],'event_index':index,'kind':'command_output',
                    'sha256':hashlib.sha256((identifier+name+sha).encode()).hexdigest(),'path':name,'file_sha256':sha,
                    'files':{},'producer_evidence_id':identifier,'observed_at':ref['observed_at']}
                prior=next((n for n,e in enumerate(existing) if e['id']==output_ref['id']),None)
                if prior is None:existing.append(output_ref);new.append(output_ref)
                else:existing[prior]=output_ref
    manager.store.update('plan_item',item['id'],evidence=existing)
    return existing,new


def review_scope_items(manager,goal,item):
    rows={i['id']:i for i in manager.items(goal['id'])};selected={item['id']}
    while True:
        extra={i['id'] for i in rows.values() if i.get('parent') in selected}-selected
        for identifier in list(selected):
            if rows[identifier]['status']=='superseded':
                replacement,done=dependency_target(identifier,rows)
                if done and replacement not in selected:extra.add(replacement)
        if not extra:break
        selected.update(extra)
    # A bounded split may reuse its original parent's execution; no unrelated siblings/Goals.
    if item.get('split_from')==item.get('parent') and item.get('parent') in rows:selected.add(item['parent'])
    return [rows[i] for i in rows if i in selected]


def observations(manager,goal,item,owners=None):
    result={};ws=Workspace(goal['workspace'],manager.store.root)
    sources=[(owner,ref) for owner in (review_scope_items(manager,goal,item) if owners is None else owners) for ref in owner.get('evidence',[])]
    for owner,original in sources:
        ref=dict(original);reasons=[];payload={};outcome=None;task=None
        if original.get('task_id') in (owner.get('rerun_requirement') or {}).get('stale_task_ids',[]):reasons.append('Superseded by explicit rerun requirement.')
        try:
            if ref.get('observation_id'):
                observation=manager.store.get('goal_evidence',ref['observation_id'])
                if observation['goal_id']!=goal['id'] or observation.get('plan_item_id')!=owner['id']:continue
                payload=observation.get('result',{})
            else:
                task=manager.store.get('task',ref['task_id']);event=task['events'][ref['event_index']]
                if task.get('goal_id') not in (None,goal['id']) or task.get('plan_item_id') not in (None,owner['id']):continue
                if task.get('plan_item_id') is None and (ref.get('goal_id')!=goal['id'] or ref.get('plan_item_id')!=owner['id']):continue
                payload=event.get('result',{})
                if event['kind']=='change' and event.get('change',{}).get('gpu_job_id'):payload=event['change']
                if ref['kind'] in ('submit_gpu_job','get_gpu_job'):
                    payload=gpu_record(manager,task,{'id':ref.get('gpu_job_id')} if event['kind']=='gpu' else payload)
                    if payload is None:continue
            for path,sha in ref.get('files',{}).items():
                if sha:
                    try:
                        if file_hash(ws,path)!=sha:reasons.append('Input changed: '+path)
                    except (ValueError,OSError):reasons.append('Input missing or unreadable: '+path)
            if ref.get('path') and ref.get('file_sha256'):
                try:
                    if file_hash(ws,ref['path'])!=ref['file_sha256']:
                        reasons.append(('Output changed: ' if ref['kind']=='command_output' else 'Observed file changed: ')+ref['path'])
                except (ValueError,OSError):reasons.append('Observed file missing or unreadable: '+ref['path'])
            for artifact in ref.get('gpu_artifacts',[]):
                if hashlib.sha256(manager.gpu.artifact(ref['gpu_job_id'],artifact['path']).read_bytes()).hexdigest()!=artifact['sha256']:reasons.append('GPU output changed: '+artifact['path'])
            if ref['kind'] in ('submit_gpu_job','get_gpu_job') and payload.get('artifacts'):
                if set(payload['artifacts'])!={a['path'] for a in ref.get('gpu_artifacts',[])}:
                    reasons.append('Some GPU downloads lack local hash verification.')
            outcome=execution_outcome(ref['kind'],payload)
            if ref['kind']=='copy_gpu_artifact' or ref['kind']=='change' and payload.get('gpu_job_id'):
                from .goal_gpu import verify_copy_provenance,related_job
                # Historical copy change events predate the richer provenance fields.
                identifier=payload.get('source_job_id') or payload.get('gpu_job_id')
                _,_,job,submitter=related_job(manager,task,identifier,guard=False)
                proof={**payload,'source_job_id':identifier,'gpu_job_id':identifier,
                    'submitting_item_id':payload.get('submitting_item_id',submitter['plan_item_id']),
                    'copying_item_id':payload.get('copying_item_id',task['plan_item_id']),
                    'destination_sha256':payload.get('destination_sha256',payload.get('sha256')),'operation':payload.get('operation','copied')}
                verify_copy_provenance(manager,task,proof)
                ref.update(source_job_id=identifier,submitting_item_id=proof['submitting_item_id'],copying_item_id=proof['copying_item_id'],
                    source_artifact=proof.get('source_artifact'),source_sha256=proof.get('source_sha256'),destination_sha256=proof.get('destination_sha256'))
            if ref['kind']=='read_gpu_artifact' and payload.get('source_job_id'):
                from .goal_gpu import related_job,input_snapshot
                _,_,job,_=related_job(manager,task,payload['source_job_id'],guard=False)
                inputs=input_snapshot(manager,task,job)
                if inputs['gpu_stale_inputs']:raise ValueError('; '.join(inputs['gpu_stale_inputs']))
            if outcome is not None:ref['execution']=outcome
            if ref['kind']=='command_output' and not execution_outcome('run_command',payload)['success']:reasons.append('Producer command did not succeed.')
        except (ValueError,PermissionError,OSError,IndexError,KeyError,TypeError) as error:
            reasons.append(redact(str(error))[:800] if isinstance(error,ValueError) else 'Stored observation or artifact unavailable or GPU provenance no longer valid.')
        ref.update(source_plan_item_id=owner['id'],stale=bool(reasons),stale_reasons=reasons,
            stale_reason='; '.join(reasons) if reasons else 'Current: recorded inputs/file hashes unchanged.',
            supports_acceptance=ref['kind']!='git_commit' and not reasons and (outcome is None or outcome['success']))
        result[ref['id']]=ref
    from .human_approval import observations as human_observations
    result.update(human_observations(manager,goal,{owner['id'] for owner in (review_scope_items(manager,goal,item) if owners is None else owners)}))
    return result


def packet(observed,limit=10000,preferred=()):
    rows=[]
    rank={identifier:n for n,identifier in enumerate(preferred)}
    # Fresh successful executions must not be crowded out by old failures/stale history.
    ordered=sorted(reversed(list(observed.values())),key=lambda e:(rank.get(e['id'],len(rank)),not e.get('supports_acceptance',False),e['kind'] not in EXECUTION_KINDS,e.get('stale',False),-e.get('observed_at',0)))
    for ref in ordered:
        row={k:ref[k] for k in ('id','kind','path','file_sha256','spec_key_path','spec_value','supports_acceptance','stale','stale_reason','source_plan_item_id','producer_evidence_id','execution','gpu_job_id','gpu_artifacts','commit_hash') if k in ref}
        if ref['kind']=='copy_gpu_artifact':row['artifact_recovery']={k:ref.get(k) for k in ('operation','source_job_id','submitting_item_id','copying_item_id','source_artifact','source_sha256','destination_sha256')}
        if ref['kind']=='human_approval':row['human_approval']={'decision_id':ref['decision_id'],'approved_files':ref['files']}
        if 'execution' in row:row['execution']={**row['execution'],'output_excerpt':row['execution'].get('output_excerpt','')[:600]}
        if ref.get('command_files'):
            row['input_tracking']={k:ref['command_files'][k] for k in ('method','unverified_inputs','note') if k in ref['command_files']}
        if ref.get('_ledger') is not None:row['accounting']={k:ref['_ledger'].get(k) for k in ('provider','estimated_usd','usage_estimated','runtime_s','state')}
        if len(row.get('gpu_artifacts',[]))>12:
            row['gpu_artifact_count']=len(row['gpu_artifacts']);row['gpu_artifacts']=row['gpu_artifacts'][:12]
        if len(json.dumps(rows+[row],ensure_ascii=False))>limit:continue
        rows.append(row)
    return {'observed':rows,'omitted_count':len(observed)-len(rows),'instruction':'Only supports_acceptance=true IDs may prove acceptance. Failed/timeout IDs support concrete rework defects, never success.'}
