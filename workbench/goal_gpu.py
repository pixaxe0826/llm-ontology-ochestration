"""Goal-local artifact recovery and optional human-cycle GPU submission caps."""
import hashlib,os,shutil,time,uuid
from .security import KEY_NAMES,redact,secret_values
from .workspace import Workspace
from .goal_operations import gpu_cycle_limit

class GPUArtifactEvidenceError(ValueError):
    def __init__(self,defects):self.defects=defects;super().__init__('; '.join(defects))

def require_recovery(manager,goal,item,valid,criteria):
    requirement=item.get('gpu_artifact_requirement')
    if not requirement:return
    selected={ref for criterion in criteria for ref in criterion.get('evidence_ids',[])};groups={}
    for identifier,ref in valid.items():
        if identifier not in selected or not ref.get('source_job_id'):continue
        if ref['kind'] not in ('copy_gpu_artifact','change'):continue
        if ref['source_job_id'] in requirement.get('excluded_job_ids',[]):continue
        if requirement.get('submitting_item_id') and ref.get('submitting_item_id')!=requirement['submitting_item_id']:continue
        groups.setdefault(ref['source_job_id'],set()).add((ref.get('source_artifact'),ref.get('path')))
    expected=set(requirement['paths'].items())
    if not any(expected<=paths for paths in groups.values()):
        raise GPUArtifactEvidenceError(['Missing current-input GPU recovery proofs for the full PNG/GLB set from one job; cite copied/verified-present evidence IDs for every required path.'])
    if requirement.get('analysis_paths'):
        analyzed={path for identifier,ref in valid.items() if identifier in selected and ref['kind']=='analyze_images' for path in ref.get('files',{})}
        if not set(requirement['analysis_paths'])<=analyzed:raise GPUArtifactEvidenceError(['Missing current-image analyze_images evidence for the required new render paths.'])
    if requirement.get('visual_request_after') is not None:
        paths=list(requirement['paths'].values())
        decisions=[d for d in manager.decisions(goal['id']) if item['id'] in d.get('affected_items',[]) and d['state'] in ('pending','answered') and d['created']>=requirement['visual_request_after']]
        if not any(all(path in d.get('question','')+'\n'+d.get('reason','') for path in paths) for d in decisions):raise GPUArtifactEvidenceError(['A human visual approval request must present all new render paths; no such recorded request exists.'])


def input_snapshot(manager,task,job):
    """Submission manifest is authoritative; copied bytes cannot refresh inputs."""
    manifest=job.get('manifest') or []
    expected={f['path']:f['sha256'] for f in manifest if isinstance(f,dict) and isinstance(f.get('path'),str) and isinstance(f.get('sha256'),str)}
    stale=[];ws=Workspace(task['workspace'],manager.store.root)
    if not expected:stale.append('GPU submission input hashes are unavailable.')
    entry=(job.get('spec') or {}).get('entry')
    if entry and entry not in expected:stale.append('GPU entry script is missing from submission hashes: '+entry)
    if job.get('input_hashes') is not None and job['input_hashes']!=expected:stale.append('GPU submission hash records disagree.')
    for path,submitted in expected.items():
        try:current=hashlib.sha256(ws.path(path,True).read_bytes()).hexdigest()
        except (ValueError,OSError):current=None
        if current!=submitted:stale.append('GPU input changed: '+path+' (submitted '+submitted[:12]+', current '+str(current)[:12]+').')
    return {'gpu_input_hashes':expected,'gpu_inputs_status':'stale' if stale else 'current','gpu_stale_inputs':stale}


def submission_status(manager,goal,item):
    limit=item.get('max_gpu_jobs_per_cycle')
    if limit is None:limit=gpu_cycle_limit(goal.get('gpu_jobs_per_cycle',3))
    if isinstance(limit,bool) or not isinstance(limit,int) or limit<0:raise ValueError('GPU cycle limit must be a nonnegative integer.')
    cycle=item.get('execution_cycle',0);legacy=item.get('gpu_legacy_cycle') or {};jobs=[]
    for job in manager.store.items('gpu',None):
        if not job.get('task_id'):continue
        try:task=manager.store.get('task',job['task_id'])
        except ValueError:continue
        if task.get('goal_id')!=goal['id'] or task.get('plan_item_id')!=item['id']:continue
        tagged=task.get('execution_cycle')
        current=tagged==cycle if tagged is not None else (legacy.get('cycle')==cycle and job.get('started',0)>=legacy.get('since',float('inf')) if legacy else cycle==0)
        if current:jobs.append(job['id'])
    return {'limit':limit,'remaining':max(0,limit-len(jobs)),'job_ids':jobs,'execution_cycle':cycle,
        'instruction':'Reuse succeeded jobs and copy_gpu_artifact. A failed/queued/cancelled submission also consumes this cap; only an answered human rework decision starts a new cycle.'}


def check_submission(manager,goal,item):
    preferred=(item.get('gpu_artifact_requirement') or {}).get('preferred_job_id')
    if preferred:
        old=manager.store.get('gpu',preferred)
        if old.get('status')=='succeeded' and not input_snapshot(manager,{'workspace':goal['workspace']},old)['gpu_stale_inputs']:
            raise PermissionError('The preferred GPU job still matches current inputs. Recover its artifacts; do not render again.')
    status=submission_status(manager,goal,item)
    if status['remaining']==0:raise PermissionError('GPU submission limit reached for this human-approved execution cycle. Reuse the existing job artifacts; do not resubmit. Job IDs: '+', '.join(status['job_ids']))
    return status


def related_job(manager,task,job_id,*,guard=True):
    """Execution authorization uses original records, never the display index."""
    if guard:goal,item=manager.builder_item_guard(task)
    else:
        goal=manager.store.get('goal',task['goal_id']);item=manager.store.get('plan_item',task['plan_item_id'])
        if item['goal_id']!=goal['id']:raise PermissionError('GPU artifact Goal mismatch.')
    job=manager.store.get('gpu',job_id)
    try:owner=manager.store.get('task',job.get('task_id'))
    except ValueError:raise PermissionError('GPU artifact requires a recorded submitting Goal task.') from None
    if owner.get('goal_id')!=goal['id'] or job.get('workspace')!=task['workspace'] or owner.get('workspace')!=task['workspace']:
        raise PermissionError('GPU artifacts must belong to the same Goal and workspace.')
    if job.get('goal_id') not in (None,goal['id']) or job.get('plan_item_id') not in (None,owner.get('plan_item_id')):
        raise PermissionError('GPU job ownership records disagree.')
    items={row['id']:row for row in manager.items(goal['id'])}
    source=items.get(owner.get('plan_item_id'))
    if source is None:raise PermissionError('Submitting item must belong to this Goal.')
    def parents(row):
        declared=row.get('parent') or row.get('split_from')
        if declared and declared not in items:raise PermissionError('GPU item has an invalid parent reference.')
        return ({declared} if declared else set())|{p['id'] for p in items.values() if row['id'] in (p.get('split_item_ids') or [])}
    own_parents=parents(item);source_parents=parents(source)
    related=(source['id']==item['id'] or source['id'] in own_parents or item['id'] in source_parents or
             bool(own_parents&source_parents) or not own_parents and not source_parents)
    if not related:raise PermissionError('GPU artifacts are limited to this item, its parent, children or siblings in the same Goal.')
    return goal,item,job,owner


def read_artifact(manager,task,args):
    goal,item,job,owner=related_job(manager,task,args['job_id'])
    if args['path'] not in job.get('artifacts',[]):raise ValueError('The requested file is not a downloaded artifact of this job.')
    source=manager.gpu.artifact(job['id'],args['path'])
    if not source.is_file() or source.stat().st_size>200_000:raise ValueError('Text artifact reads are limited to 200 KB.')
    data=source.read_bytes()
    return {'content':redact(data.decode('utf-8')),'gpu_job_id':job['id'],'source_job_id':job['id'],
        'submitting_item_id':owner['plan_item_id'],'reading_item_id':item['id'],'source_artifact':args['path'],
        'source_sha256':hashlib.sha256(data).hexdigest(),**input_snapshot(manager,task,job)}


def verify_copy_provenance(manager,task,proof):
    """Review rechecks current bytes and original ownership; never trust prose."""
    goal,item,job,owner=related_job(manager,task,proof['source_job_id'],guard=False)
    if proof.get('operation') not in ('copied','verified_present'):raise ValueError('Invalid GPU artifact recovery operation.')
    if job['id']!=proof.get('gpu_job_id') or owner['plan_item_id']!=proof.get('submitting_item_id') or item['id']!=proof.get('copying_item_id'):
        raise ValueError('GPU copy provenance does not match original ownership.')
    if job.get('status')!='succeeded' or (job.get('result') or {}).get('exit_code')!=0:raise ValueError('GPU copy source did not succeed.')
    inputs=input_snapshot(manager,task,job)
    if proof.get('gpu_input_hashes',inputs['gpu_input_hashes'])!=inputs['gpu_input_hashes']:raise ValueError('GPU copy input provenance disagrees with submission.')
    if inputs['gpu_stale_inputs']:raise ValueError('; '.join(inputs['gpu_stale_inputs']))
    if proof.get('source_artifact') not in job.get('artifacts',[]):raise ValueError('GPU copy source is not a downloaded artifact.')
    source=manager.gpu.artifact(job['id'],proof['source_artifact'])
    if source.stat().st_size>32_000_000 or hashlib.sha256(source.read_bytes()).hexdigest()!=proof.get('source_sha256'):
        raise ValueError('GPU copy source bytes changed.')
    destination=Workspace(task['workspace'],manager.store.root).path(proof['path'],True)
    if destination.stat().st_size>32_000_000 or hashlib.sha256(destination.read_bytes()).hexdigest()!=proof.get('destination_sha256') or proof.get('source_sha256')!=proof.get('destination_sha256'):
        raise ValueError('GPU copy destination bytes changed.')
    return True


def copy_artifact(manager,task,args):
    goal,item=manager.builder_item_guard(task)
    if not task.get('allow_write'):raise PermissionError('This Builder is read-only.')
    # Copying an existing result is local, but provisional assumptions cannot introduce binary changes.
    manager.provisional_guard(task,'copy_gpu_artifact',args)
    goal,item,job,owner=related_job(manager,task,args['job_id'])
    if job.get('status')!='succeeded' or (job.get('result') or {}).get('exit_code')!=0:raise ValueError('Copy requires an existing succeeded GPU job.')
    if args['path'] not in job.get('artifacts',[]):raise ValueError('The requested file is not a downloaded artifact of this job.')
    source=manager.gpu.artifact(job['id'],args['path'])
    if not source.is_file() or source.stat().st_size>32_000_000:raise ValueError('A local artifact copy is limited to 32 MB.')
    data=source.read_bytes();ws=Workspace(task['workspace'],manager.store.root);path=args['destination'];destination=ws.path(path)
    def secret(content):return any(value.encode() in content for value in secret_values() if value)
    if secret(data):raise ValueError('Artifact contains protected credential content.')
    backup_id=None;sha=hashlib.sha256(data).hexdigest();existed=destination.exists()
    provenance={'gpu_job_id':job['id'],'source_job_id':job['id'],'submitting_item_id':owner['plan_item_id'],
        'copying_item_id':item['id'],'source_artifact':args['path'],'source_sha256':sha,'destination_sha256':sha,**input_snapshot(manager,task,job)}
    if destination.exists():
        if not destination.is_file() or destination.stat().st_size>32_000_000:raise ValueError('Destination is not a supported file.')
        old=destination.read_bytes()
        if secret(old):raise ValueError('Destination contains protected credential content.')
        current_sha=hashlib.sha256(old).hexdigest()
        if current_sha==sha:
            proof={'path':destination.relative_to(ws.root).as_posix(),'sha256':sha,'binary':True,'created':False,
                'copied':False,'verified_present':True,'operation':'verified_present',**provenance}
            manager.store.event(task['id'],'gpu_artifact_copy',result=proof)
            return proof
        if current_sha!=args.get('expected_sha256'):
            raise ValueError('Existing destination requires its current expected_sha256; destination '+destination.relative_to(ws.root).as_posix()+
                ' current_sha256='+current_sha+'. Retry once with expected_sha256="'+current_sha+'"; no file was overwritten.')
        backup_id=uuid.uuid4().hex;backup=manager.store.root/'backups'/backup_id;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(destination,backup)
    destination.parent.mkdir(parents=True,exist_ok=True);ws.path(path)
    temporary=destination.with_name(destination.name+'.muse-tmp-'+uuid.uuid4().hex[:8])
    try:temporary.write_bytes(data);temporary.replace(destination)
    finally:temporary.unlink(missing_ok=True)
    if hashlib.sha256(destination.read_bytes()).hexdigest()!=sha:raise ValueError('GPU copy destination verification failed.')
    change={'path':destination.relative_to(ws.root).as_posix(),'sha256':sha,'backup_id':backup_id,'binary':True,'created':not existed,
        'diff':'Existing GPU artifact copied locally; no GPU execution.','copied':True,'verified_present':False,'operation':'copied',**provenance}
    manager.store.event(task['id'],'gpu_artifact_copy',result={k:v for k,v in change.items() if k not in ('diff','backup_id')})
    manager.store.event(task['id'],'change',change=change)
    return {k:v for k,v in change.items() if k not in ('diff','backup_id')}
