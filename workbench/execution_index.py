"""Read-only display joins. Never supply these inferred links to execution or evidence policy."""
from collections import defaultdict
from functools import wraps
import time
from .goal_titles import suggested_title


KINDS = ('goal', 'plan_item', 'task', 'gpu', 'goal_long_command', 'ledger', 'goal_evidence')
FIELDS = ('goal_id', 'plan_item_id', 'task_id')


def display_read(fn):
    @wraps(fn)
    def read(self,*args,**kwargs):
        with self.store.lock:return fn(self,*args,**kwargs)
    return read


def elapsed(row):
    start = row.get('started') or row.get('created')
    if start is None:return None
    finish = row.get('finished')
    if finish is None and row.get('status') not in ('queued', 'running', 'cancelling'):return None
    return max(0, (finish or time.time()) - start)


class ExecutionIndex:
    def __init__(self, store):
        self.store = store
        self.version = None

    def snapshot(self):
        # total_changes covers this connection; data_version covers other SQLite connections.
        with self.store.lock:
            version = (self.store.db.total_changes, self.store.db.execute('PRAGMA data_version').fetchone()[0])
            if version == self.version:return self
            self.rows = {kind:self.store.items(kind, None) for kind in KINDS}
            self.goals = {r['id']:r for r in self.rows['goal']}
            self.items = {r['id']:r for r in self.rows['plan_item']}
            self.tasks = {r['id']:r for r in self.rows['task']}
            self.links = {}
            self._build()
            self.version = version
        return self

    def _resolve(self, kind, row, candidates):
        choices = {field:set() for field in FIELDS}
        # Existing ownership wins over reused evidence in a parent/review item.
        authoritative = row.get('goal_id') and row.get('plan_item_id')
        for candidate in [row, *([] if authoritative else candidates)]:
            for field in FIELDS:
                if candidate.get(field):choices[field].add(candidate[field])
        if kind == 'task':choices['task_id'].add(row['id'])
        for identifier in tuple(choices['plan_item_id']):
            item = self.items.get(identifier)
            if item:choices['goal_id'].add(item['goal_id'])
        conflict = any(len(values)>1 for values in choices.values())
        result = {key:next(iter(value)) if len(value)==1 else None for key,value in choices.items()}
        goal, item = self.goals.get(result['goal_id']), self.items.get(result['plan_item_id'])
        if conflict:status = 'conflict'
        elif goal and item and result['task_id']:status = 'linked'
        elif result['goal_id']:status = 'partial'
        elif candidates or row.get('goal_id') or row.get('plan_item_id'):status = 'unlinked'
        else:status = 'direct'
        link = {**result, 'status':status, 'goal_title':(goal.get('title') or suggested_title(goal.get('objective'))) if goal else '연결된 Goal 없음',
                'item_title':item.get('title','') if item else '항목 연결 미확인', 'item_key':item.get('key','') if item else '',
                'goal_available':bool(goal), 'source':'record' if all(row.get(k)==result[k] for k in FIELDS) else 'display_index'}
        self.links[(kind,row['id'])] = link
        return link

    def _build(self):
        task_hints, gpu_hints, long_hints = defaultdict(list), defaultdict(list), defaultdict(list)
        for item in self.rows['plan_item']:
            owner = {'goal_id':item['goal_id'], 'plan_item_id':item['id']}
            for task_id in set([*item.get('task_ids',[]), *([item['task_id']] if item.get('task_id') else [])]):
                task_hints[task_id].append(owner)
            for ref in item.get('evidence',[]):
                claim = {**owner, 'task_id':ref.get('task_id')}
                if ref.get('task_id'):task_hints[ref['task_id']].append(owner)
                if ref.get('gpu_job_id'):gpu_hints[ref['gpu_job_id']].append(claim)
            # Some older cycle records preserve submission IDs separately.
            for job_id in (item.get('gpu_submission_status') or {}).get('job_ids',[]):gpu_hints[job_id].append(owner)
        for row in self.rows['ledger'] + self.rows['goal_evidence']:
            owner = {k:row.get(k) for k in FIELDS}
            if not owner.get('goal_id') and not owner.get('plan_item_id'):continue
            if owner.get('task_id'):task_hints[owner['task_id']].append(owner)
            if row.get('gpu_job_id'):gpu_hints[row['gpu_job_id']].append(owner)
            if row.get('long_command_id'):long_hints[row['long_command_id']].append(owner)
        for row in self.rows['goal_long_command']:
            if row.get('task_id'):task_hints[row['task_id']].append({k:row.get(k) for k in FIELDS})
        for task in self.rows['task']:
            link = self._resolve('task', task, task_hints[task['id']])
            if link['status'] == 'conflict':continue
            for event in task.get('events',[]):
                job_id = event.get('job_id') if event.get('kind')=='gpu' else None
                if event.get('kind')=='tool_result' and event.get('name')=='submit_gpu_job' and isinstance(event.get('result'),dict):
                    job_id = event['result'].get('id') or event['result'].get('job_id')
                if job_id and link.get('goal_id'):gpu_hints[job_id].append(link)
        for kind, hints in (('gpu',gpu_hints), ('goal_long_command',long_hints)):
            for row in self.rows[kind]:
                candidates = list(hints[row['id']])
                task_link = self.links.get(('task',row.get('task_id')))
                if task_link and task_link['status']!='conflict' and task_link.get('goal_id'):candidates = [task_link]
                self._resolve(kind,row,candidates)

    @display_read
    def context(self, kind, identifier):
        self.snapshot()
        return dict(self.links.get((kind,identifier), {}))

    @display_read
    def report(self):
        self.snapshot()
        result = {}
        for kind in ('gpu','task','goal_long_command'):
            links = [self.links[(kind,row['id'])] for row in self.rows[kind]]
            result[kind] = {'total':len(links), 'linked':sum(l['status']=='linked' for l in links),
                'recovered':sum(l['status']=='linked' and l['source']=='display_index' for l in links),
                'direct':sum(l['status']=='direct' for l in links),
                'unlinked':sum(l['status'] in ('partial','unlinked','conflict') for l in links)}
        return result

    def gpu_view(self, job):
        link = self.links[('gpu',job['id'])]
        rows = [r for r in self.rows['ledger'] if r.get('gpu_job_id')==job['id'] and r.get('workload','gpu')=='gpu' and r.get('state')!='released']
        result = job.get('result') or {}
        runtime = (result.get('summary') or {}).get('duration_s')
        if runtime is None:runtime = next((r.get('runtime_s') for r in rows if r.get('runtime_s') is not None),None)
        files = {path:{'path':path,'location':'local'} for path in (job.get('artifacts') or [])}
        for artifact in (result.get('retained_outputs') or []):
            if isinstance(artifact,dict) and artifact.get('path') and artifact['path'] not in files:files[artifact['path']] = {**artifact,'location':'volume'}
        copies = []
        task = self.tasks.get(link.get('task_id'),{})
        for event in task.get('events',[]):
            change = event.get('change') or {}
            if event.get('kind')=='change' and change.get('gpu_job_id')==job['id']:
                copies.append({k:change.get(k) for k in ('path','sha256','source_artifact')})
        return {'kind':'gpu', 'id':job['id'], **{k:link[k] for k in FIELDS}, 'context':link,
            'status':job['status'], 'gpu':job.get('spec',{}).get('gpu'), 'program':job.get('spec',{}).get('program'),
            'label':job.get('spec',{}).get('entry') or job.get('spec',{}).get('program'),
            'time':job.get('started') or job.get('created') or job.get('updated'), 'runtime_s':runtime,
            'elapsed_s':elapsed(job), 'cost_known':bool(rows),
            'estimated_usd':sum(r.get('estimated_usd',0) for r in rows),
            'reserved_usd':sum(r.get('reserved_usd',0) for r in rows if r.get('state')=='reserved'),
            'ledger_ids':[r['id'] for r in rows], 'files':list(files.values()), 'copies':copies}

    @display_read
    def goal(self, goal_id):
        self.snapshot()
        entries, consumed_long = [], set()
        linked = lambda kind,row:self.links[(kind,row['id'])].get('goal_id')==goal_id and self.links[(kind,row['id'])]['status']!='conflict'
        jobs = [self.gpu_view(row) for row in self.rows['gpu'] if linked('gpu',row)]
        entries.extend(jobs)
        for row in self.rows['goal_long_command']:
            if not linked('goal_long_command',row):continue
            consumed_long.add(row['id'])
            result = row.get('result') or {}
            entries.append({'kind':'long_command','id':row['id'],**{k:self.links[('goal_long_command',row['id'])][k] for k in FIELDS},
                'label':row.get('label') or '장시간 명령', 'status':row['status'], 'time':row.get('started') or row.get('created') or row.get('updated'),
                'elapsed_s':elapsed(row), 'exit_code':result.get('exit_code'), 'timed_out':result.get('timed_out',False),
                'argv':row.get('argv',[]), 'cwd':row.get('cwd','.'), 'evidence_id':row.get('evidence_id'),
                'output_tail':str(row.get('output_tail') or result.get('output') or '')[-1500:], 'proxy_cost':result.get('proxy_cost')})
        for task in self.rows['task']:
            if not linked('task',task):continue
            owner = {k:self.links[('task',task['id'])][k] for k in FIELDS}
            entries.append({'kind':'builder','id':task['id'],**owner,'label':task.get('title') or 'Builder',
                'status':task.get('status','unknown'),'time':task.get('created') or next((e.get('time') for e in task.get('events',[]) if e.get('time')),task.get('updated'))})
            args = {}
            for index,event in enumerate(task.get('events',[])):
                if event.get('kind')=='tool' and event.get('name')=='run_command':args = event.get('arguments') or {}
                if event.get('kind')!='tool_result' or event.get('name')!='run_command':continue
                result = event.get('result')
                if not isinstance(result,dict):continue
                if (event.get('long_command_id') or result.get('long_command_id')) in consumed_long:continue
                status = 'cancelled' if result.get('cancelled') else 'interrupted' if result.get('interrupted') else 'succeeded' if result.get('exit_code')==0 and not result.get('timed_out') and not result.get('error') else 'failed'
                entries.append({'kind':'command','id':task['id']+':'+str(index),**owner,'evidence_id':task['id']+':'+str(index),
                    'label':' '.join(str(a) for a in args.get('argv',[])) or '로컬 명령', 'argv':args.get('argv',[]),'cwd':args.get('cwd','.'),
                    'status':status,'time':event.get('time',task.get('updated')), 'elapsed_s':result.get('duration_s'),
                    'exit_code':result.get('exit_code'),'timed_out':result.get('timed_out',False),
                    'output_tail':str(result.get('output') or result.get('error') or '')[-1500:]})
        entries.sort(key=lambda e:(e.get('time') or 0,e['id']))
        gpu_ledger = [r for r in self.rows['ledger'] if r.get('goal_id')==goal_id and r.get('provider')=='modal' and r.get('workload','gpu')=='gpu' and r.get('state')!='released']
        linked_ledger = {identifier for job in jobs for identifier in job['ledger_ids']}
        unattributed = sum(r.get('estimated_usd',0) for r in gpu_ledger if r['id'] not in linked_ledger)
        return {'entries':entries, 'gpu_summary':{'total':len(jobs),
            **{state:sum(job['status']==state for job in jobs) for state in ('succeeded','failed','cancelled','running','queued','interrupted')},
            'estimated_usd':sum(job['estimated_usd'] for job in jobs)+unattributed,
            'reserved_usd':sum(r.get('reserved_usd',0) for r in gpu_ledger if r.get('state')=='reserved'),
            'unattributed_usd':unattributed,'unknown_cost_jobs':sum(not job['cost_known'] for job in jobs)}}
