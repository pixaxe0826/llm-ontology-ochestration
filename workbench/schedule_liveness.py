"""Waits need observable triggers; finished/queued work is never an empty plan."""
import hashlib
import json
import time
from .goal_costs import GoalLimit
from .plan_updates import prepare_plan
from .security import redact
from .item_states import terminal,dependency_target

JOB_LIVE={'queued','running','cancelling','waiting_approval'}


def dependency_cycles(items):
    """Strong components of unfinished dependency + parent/child scheduling edges."""
    all_items={i['id']:i for i in items};nodes={key:i for key,i in all_items.items() if not terminal(i)}
    graph={key:{dependency_target(ref,all_items)[0] for ref in i.get('depends_on') or []}&nodes.keys() for key,i in nodes.items()}
    for key,item in nodes.items():
        graph[key].update(c['id'] for c in nodes.values() if c['id']!=key and (c.get('parent')==key or c['id'] in (item.get('split_item_ids') or [])))
    reverse={key:set() for key in nodes}
    for key,edges in graph.items():
        for edge in edges:reverse[edge].add(key)
    seen=set();order=[]
    for root in graph:
        if root in seen:continue
        stack=[(root,False)]
        while stack:
            key,visited=stack.pop()
            if visited:order.append(key);continue
            if key in seen:continue
            seen.add(key);stack.append((key,True));stack.extend((edge,False) for edge in graph[key] if edge not in seen)
    seen=set();cycles=[]
    for root in reversed(order):
        if root in seen:continue
        group=[];stack=[root];seen.add(root)
        while stack:
            key=stack.pop();group.append(key)
            for edge in reverse[key]:
                if edge not in seen:seen.add(edge);stack.append(edge)
        if len(group)>1 or root in graph[root]:cycles.append(sorted(group))
    return sorted(cycles)


class GoalLiveness:
    def live_gpu_jobs(self,goal,item=None):
        jobs=[]
        for row in self.store.items('gpu',None):
            if row.get('status') not in JOB_LIVE:continue
            gid,pid=row.get('goal_id'),row.get('plan_item_id')
            if not gid or not pid:
                try:task=self.store.get('task',row.get('task_id'));gid=gid or task.get('goal_id');pid=pid or task.get('plan_item_id')
                except ValueError:pass
            if gid==goal['id'] and (item is None or pid==item['id']):jobs.append(row)
        return jobs

    def wait_triggers(self,goal,item):
        view=self.dependency_view(goal,item)
        return {'dependencies':view['unresolved_dependencies'],'decisions':view['pending_decisions'],
            'children':[c['id'] for c in self.child_items(goal,item) if not terminal(c)],
            'gpu':[r['id'] for r in self.live_gpu_jobs(goal,item)],
            'long_commands':[r['id'] for r in self.long_commands.active(goal['id'],item['id'])]}

    def pending_builder_result(self,item):
        return bool(self.task_for_item(item) and item.get('task_id') not in self.builders.active and item.get('bundle_reported',0)<item['bundles'])

    def live_scheduler_work(self,goal,items=None,ignore_approvals=False):
        items=self.items(goal['id']) if items is None else items
        if self.long_commands.active(goal['id']) or self.live_gpu_jobs(goal):return True
        if any(i['status']=='in_progress' or self.pending_builder_result(i) for i in items if not terminal(i)):return True
        return any(self.store.get('task',tid).get('goal_id')==goal['id'] and (not ignore_approvals or self.store.get('task',tid)['status']!='waiting_approval') for tid in list(self.builders.active))

    def schedule_issue(self,goal,kind,signature,**values):
        identifier=hashlib.sha256((goal['id']+'\0'+kind+'\0'+json.dumps(signature,sort_keys=True)).encode()).hexdigest()[:24]
        try:return self.store.get('goal_schedule_issue',identifier)
        except ValueError:
            return self.store.save('goal_schedule_issue',{'id':identifier,'goal_id':goal['id'],'kind':kind,'state':'detected',
                'created':time.time(),'signature':signature,**values,**self.times(goal)})

    def wake_wait(self,goal,item):
        """Data repair only. Execution is exclusively the ordinary dispatch path."""
        if terminal(item):return item
        before={k:item.get(k) for k in ('id','status','wait_reason','waiting_resume','bundles','bundle_reported','execution_cycle','task_id','updated')}
        issue=self.schedule_issue(goal,'orphan_wait',before,plan_item_id=item['id'],before=before,triggers=self.wait_triggers(goal,item))
        values={'wait_reason':None,'needs_decision_replan':False}
        if item.get('waiting_resume')=='review' or item['bundles']>=self.bundle_limit(item) and item.get('wait_reason')!='deferred':
            values.update(status='review',waiting_resume=None,continue_pending=False,
                review_trigger='completed' if (item.get('report') or {}).get('report_status')=='completed' else 'batch_limit')
        else:
            if item.get('wait_reason')=='deferred' and item['bundles']>=self.bundle_limit(item):
                values.update(bundles=0,bundle_reported=0,task_id=None,execution_cycle=(item.get('execution_cycle') or 0)+1,
                    cycle_started_at=time.time(),
                    historical_bundles=item.get('historical_bundles',0)+item['bundles'],review_error=None,review_error_count=0,
                    review_trigger=None,document_cycle_active=False)
            continuing=bool(values.get('bundles',item['bundles']))
            values.update(status='todo',waiting_resume='continue' if continuing else 'start',continue_pending=continuing)
        current=self.store.update('plan_item',item['id'],**values)
        self.store.update('goal_schedule_issue',issue['id'],state='repaired',repair='fresh_cycle' if 'execution_cycle' in values else 'wake',
            after={k:current.get(k) for k in before},finished=time.time())
        return current

    def inspect_schedule(self,goal):
        cycles=dependency_cycles(self.items(goal['id']))
        if not cycles:return None
        affected={v for c in cycles for v in c}
        graph=[{'id':i['id'],'parent':i.get('parent') if i.get('parent') in affected else None,
            'depends_on':sorted(set(i.get('depends_on') or [])&affected),
            'split_item_ids':sorted(set(i.get('split_item_ids') or [])&affected)} for i in self.items(goal['id']) if i['id'] in affected]
        graph.sort(key=lambda i:i['id'])
        return self.schedule_issue(goal,'dependency_cycle',{'cycles':cycles,'graph':graph},affected_items=sorted({v for c in cycles for v in c}))

    def repair_schedule_cycles(self,goal):
        issue=self.inspect_schedule(goal)
        if not issue or issue['state'] in ('repaired','escalated') or self.builder_busy(goal):return
        # Persist the claim before calling: restart never repeats this paid repair.
        claimed=issue['state']=='attempted'
        self.store.update('goal_schedule_issue',issue['id'],state='attempted',attempted=time.time())
        error='A previously claimed cycle repair was interrupted; no automatic replay.'
        if not claimed:
            try:
                instruction=('Repair the recorded scheduling dependency cycle once. Return JSON {"items":[existing-item updates],"summary":"..."}. '
                    'Use existing keys/IDs only; change parent/depends_on only as needed to preserve the intended deliverables. '
                    'A parent waits for unfinished children, so a child must not depend on its parent. Never execute Builder work or claim results. '
                    'Cycle record: '+json.dumps(issue['signature']))
                raw,human_queue=self.inbox_phase(goal,None,instruction)
                self.store.update('goal_schedule_issue',issue['id'],response=redact(raw))
                value=self.parse(raw);patch=prepare_plan(self.items(goal['id']),value.get('items',[]),max_plan_items=goal.get('max_plan_items',40))
                if patch['creates']:raise ValueError('Cycle repair must update existing items only.')
                projected={i['id']:i for i in self.items(goal['id'])}
                for row in patch['updates']:projected[row['id']]={**projected[row['id']],**row,'parent':row['parent_id']}
                if dependency_cycles(list(projected.values())):raise ValueError('Planner repair still contains a scheduling cycle.')
                self.apply_plan_patch(goal,patch)
                self.finish_inbox(goal,human_queue,value)
                self.store.update('goal_schedule_issue',issue['id'],state='repaired',finished=time.time())
                self.message(goal,'System','Human','NOTE','Planner repaired a scheduling dependency cycle; preserved work and evidence.',evidence=[issue['id']])
                return
            except GoalLimit:raise
            except Exception as failure:error=redact(str(failure)) if isinstance(failure,(ValueError,TypeError,KeyError,AttributeError)) else type(failure).__name__+': Planner cycle repair failed.'
        self.store.update('goal_schedule_issue',issue['id'],state='escalated',error=error,finished=time.time())
        self.create_decision(goal,{'question':'The plan has a scheduling dependency cycle. The single Planner repair did not produce a valid acyclic plan. Review the recorded dependencies.',
            'options':[],'recommendation':'','reason':error,'affected_items':issue['affected_items'],'reversible':True},source='scheduler-cycle:'+issue['id'])
