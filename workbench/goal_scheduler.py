"""Sticky Builder batches, explicit completion reports and observable parked states."""
import json
import re
import time

from .goal_costs import GoalLimit
from .security import contains_key
from .schedule_liveness import GoalLiveness
from .dependency_contract import explicit_reopen
from .item_states import terminal


class GoalScheduler(GoalLiveness):
    def release_answered_waits(self,goal):
        with self.lock:return self._release_answered_waits_locked(goal)

    def _release_answered_waits_locked(self,goal):
        """Execute only explicit, linked human resume instructions; never guess an answer."""
        for item in self.items(goal['id']):
            if terminal(item) or self.child_items(goal,item):continue
            source=item.get('decision_action_source')
            if not source or item.get('task_id') in self.builders.active:continue
            try:decision=self.store.get('decision',source)
            except ValueError:continue
            if decision['goal_id']!=goal['id'] or decision['state']!='answered' or item['id'] not in decision['affected_items']:continue
            if source+':rework' in (item.get('applied_decision_actions') or {}):continue
            answered_at=decision.get('answered_at') or 0
            if item.get('cycle_started_at',0)>answered_at or any(r.get('plan_item_id')==item['id'] and r.get('action')=='rework' and r.get('state')=='applied' and r.get('created',0)>answered_at for r in self.store.items('goal_item_action',None)):continue
            task=self.task_for_item(item)
            failed_at=item.get('wait_started_at') or ((task or {}).get('finished') or (task or {}).get('created') or 0)
            if failed_at>(decision.get('answered_at') or 0):continue  # A later failure needs a later decision.
            lines=[s for s in decision.get('answer','').splitlines() if not re.search(r'\b(?:do not|don\x27t|never|defer|pause|stop)\b|금지|중지|보류',s,re.I)]
            if not (explicit_reopen(decision.get('answer',''),item,allow_unscoped_retry=len(decision['affected_items'])==1) or
                item.get('wait_reason') in ('platform_error','decision') and any(re.search(r'copy_gpu_artifact|\b(?:resume|continue|retry|rework|reopen)\b|재개|이어|재시도|회수',s,re.I) for s in lines)):continue
            if any(self.wait_triggers(goal,item).values()):continue
            self.apply_decision_action(goal,item,'rework',decision['answer'],source)
            self.message(goal,'System','Human','NOTE','Applied the already-answered explicit recovery instruction; released the platform/decision wait without running a Builder.',item['id'],[source])

    def scheduling_blockers(self,goal,items=None):
        items=self.items(goal['id']) if items is None else items;by_id={i['id']:i for i in items};rows=[]
        for item in items:
            if terminal(item):continue
            view=self.dependency_view(goal,item);children=self.child_items(goal,item)
            reason=item.get('wait_reason') or item['status']
            if children and any(not terminal(c) for c in children):reason='children'
            rows.append({'id':item['id'],'title':item['title'],'status':item['status'],'reason':reason,
                'dependencies':[by_id[i]['title'] if i in by_id else 'Unconfirmed dependency' for i in view['unresolved_dependencies']],
                'pending_decisions':view['pending_decisions'],'needs_decision_replan':bool(item.get('needs_decision_replan'))})
        return rows

    def block_if_no_runnable(self,goal,items):
        # Re-read after dispatch: the Builder can finish between the tick's first
        # result-consumption pass and this check. That is pending work, not a stop.
        self.reconcile_waiting(goal)
        self.refresh_split_reviews(goal)
        items=self.items(goal['id'])
        if not any(not terminal(i) for i in items) or self.live_scheduler_work(goal,items) or self.pending_decisions(goal['id']):return False
        issue=self.inspect_schedule(goal)
        if issue and issue['state'] in ('detected','attempted'):return False  # next tick's single Planner repair
        if any(self.candidate_ready(goal,i) or (i['status']=='review' and not (i.get('review_error') or {}).get('needs_human') and self.dependency_view(goal,i)['ready'] and not i.get('needs_decision_replan')) for i in items):return False
        for item in items:
            children=self.child_items(goal,item)
            if not terminal(item) and children and all(terminal(c) for c in children) and item.get('children_review_revision')!=self.child_review_revision(children):
                return False  # The next scheduler tick will open the aggregate review.
        rows=self.scheduling_blockers(goal,items)
        from .human_briefs import ko
        names={'children':'하위 항목 완료 대기','dependency':'선행 항목 대기','decision':'판단 결과 적용 대기','platform_error':'플랫폼 오류 대기','deferred':'보류','plan_capacity':'계획 상한'}
        why='; '.join(r['title']+' — '+(names.get(r['reason'],r['reason']) if ko(goal) else r['reason']) for r in rows[:5])
        summary=(f'진행 중단: 미완료 {len(rows)}개가 있지만 실행 가능한 항목이 없습니다. '+why if ko(goal) else f'Progress stopped: {len(rows)} unfinished items remain but none can run. '+why)
        self.save(goal,status='blocked',reason='No runnable items; inspect the recorded dependency and waiting reasons.',scheduling_blocked=True,execution_blockers=rows,scheduling_human_summary=summary)
        note=self.message(goal,'System','Human','NOTE','No runnable work remains. '+json.dumps(rows,ensure_ascii=False))
        self.store.update('message',note['id'],human_summary=summary,human_display_language=goal.get('human_display_language','Korean'))
        return True

    def child_items(self,goal,item):
        identifiers=set(item.get('split_item_ids') or [])
        return [i for i in self.items(goal['id']) if i['id']!=item['id'] and (i.get('parent')==item['id'] or i['id'] in identifiers)]

    def task_for_item(self,item):
        if not item.get('task_id'):return None
        try:return self.store.get('task',item['task_id'])
        except ValueError:return None

    def execution_view(self,goal,item):
        task=self.task_for_item(item);view=self.dependency_view(goal,item)
        running=bool(task and task['id'] in self.builders.active and task['status']=='running')
        status=item['status'];reason=item.get('wait_reason')
        children=self.child_items(goal,item)
        if item['status']!='superseded' and children and any(not terminal(c) for c in children):status='waiting_children';reason='children'
        if status in ('todo','rework','in_progress','waiting'):
            if running:status='in_progress';reason=None
            elif task and task['id'] in self.builders.active:
                status='waiting';reason='approval' if task['status']=='waiting_approval' else 'builder_queue'
            elif not view['ready'] and status!='rework' and reason not in ('split_children','deferred','platform_error','plan_capacity'):
                status='waiting';reason='decision' if view['pending_decisions'] else 'dependency'
            elif status=='in_progress':status='waiting';reason='continuation' if item.get('continue_pending') else 'interrupted'
        return {'execution_status':status,'wait_reason':reason,'children_total':len(children),'children_done':sum(terminal(c) for c in children),'bundle_limit':self.bundle_limit(item)}

    def park_item(self,item,reason,*,resume='continue',**values):
        if reason=='platform_error':values.setdefault('wait_started_at',time.time())
        return self.store.update('plan_item',item['id'],status='waiting',wait_reason=reason,waiting_resume=resume,**values)

    def focus_pinned(self,goal,identifier):
        if not identifier:return False
        item=next((i for i in self.items(goal['id']) if i['id']==identifier),None)
        if not item or terminal(item) or not self.dependency_view(goal,item)['ready'] or item.get('needs_decision_replan'):return False
        if self.child_items(goal,item):return False
        task=self.task_for_item(item)
        if self.long_commands.active(goal['id'],item['id']):return False
        if task and task['id'] in self.builders.active:
            return task['status']!='waiting_approval'
        return bool(item['status']=='in_progress' and item.get('bundle_reported',0)<item['bundles'] or
            item.get('continue_pending') and item['status'] in ('in_progress','waiting','todo') and item['bundles']<self.bundle_limit(item) or
            item['status']=='rework' and item['bundles']<self.bundle_limit(item))

    def builder_turn_allowed(self,task):
        goal=self.guard(task['goal_id'],task['generation'])
        focus=goal.get('builder_focus_item_id')
        return not focus or focus==task['plan_item_id'] or not self.focus_pinned(goal,focus)

    def mark_builder_running(self,task):
        # Called while the global Builder gate and manager lock are held.
        goal,item=self.builder_item_guard(task)
        if self.child_items(goal,item):raise GoalLimit('Parent items are reviewed from child evidence; no parent Builder is allowed.')
        for other in self.items(goal['id']):
            if other['id']==item['id'] or other['status']!='in_progress':continue
            active=self.task_for_item(other)
            if active and active['id'] in self.builders.active and active['status']=='running':
                raise GoalLimit('Another Builder owns the execution slot.')
            self.park_item(other,'continuation',resume='continue',continue_pending=True)
        self.store.update('plan_item',item['id'],status='in_progress',wait_reason=None,waiting_resume=None)
        self.save(goal,builder_focus_item_id=item['id'])

    def record_builder_report(self,task_id,args,chunk_id):
        if not isinstance(args,dict) or set(args)-{'status','summary','verification'} or contains_key(json.dumps(args,ensure_ascii=False)):
            raise ValueError('Builder reports must contain only non-secret report fields.')
        if args.get('status') not in ('progress','completed'):raise ValueError('Report status must be progress or completed.')
        if not isinstance(args.get('summary'),str) or not 1<=len(args['summary'])<=2000:raise ValueError('Report summary must contain 1–2000 characters.')
        if not isinstance(args.get('verification',''),str) or len(args.get('verification',''))>2000:raise ValueError('Invalid verification summary.')
        report={**args,'chunk_id':chunk_id,'created':time.time()}
        self.store.update('task',task_id,builder_report=report)
        return {'recorded':True,'status':report['status'],'note':'This is a Builder report, not Reviewer acceptance. Observed evidence is collected by the server.'}

    def batch_report(self,task):
        starts=[e for e in task.get('events',[]) if e['kind']=='chunk_started']
        chunk=starts[-1].get('chunk_id') if starts else None
        explicit=task.get('builder_report') or {}
        if explicit.get('chunk_id')==chunk and explicit.get('status') in ('progress','completed'):return explicit
        # A text-only response is not proof of completion. JSON is the no-tool fallback.
        texts=[e.get('text','') for e in task.get('events',[]) if e['kind']=='assistant' and (not starts or e.get('time',0)>=starts[-1].get('time',0))]
        text=texts[-1] if texts else ''
        try:
            value=self.parse(text)
            if value.get('report_status') in ('progress','completed'):
                return {'status':value['report_status'],'summary':str(value.get('summary',''))[:2000],'verification':str(value.get('verification',''))[:2000]}
        except (ValueError,TypeError,AttributeError):pass
        return {'status':'progress','summary':text[:2000] or 'No explicit completion report.','verification':'NOT VERIFIED: Builder completion was not explicitly reported.'}

    @staticmethod
    def observed_report(task,evidence,declared):
        return {**({'chunk_status':task['chunk_outcome']['status']} if task.get('chunk_outcome') else {}),'summary':declared['summary'][:1200],'report_status':declared['status'],'builder_verification':declared.get('verification','')[:2000],
            'changed_files':sorted({e['path'] for e in evidence if e['kind']=='change' and e.get('path')}),
            'test_results':[{'kind':e['kind'],'result':'PASS: observed exit code 0.' if e.get('supports_acceptance',True) else 'FAIL/NOT VERIFIED: observed execution did not succeed.',
                'execution':e.get('execution'),'evidence_path':e['id']} for e in evidence if e['kind'] in ('run_command','submit_gpu_job','get_gpu_job')],
            'evidence_paths':[e['id'] for e in evidence],
            'verification':'Observed command/GPU results supplied.' if any(e['kind'] in ('run_command','submit_gpu_job','get_gpu_job') for e in evidence) else 'NOT RUN: command/GPU verification. NOT VERIFIED: execution outcome.'}

    def park_finished_batch(self,goal,item,task):
        evidence,_=self.collect_evidence(item,task);declared=self.batch_report(task);report=self.observed_report(task,evidence,declared)
        self.message(goal,'Builder','Planner','REPORT',json.dumps(report,ensure_ascii=False),item['id'],report['evidence_paths'])
        review=declared['status']=='completed' or item['bundles']>=self.bundle_limit(item)
        self.store.update('task',task['id'],status='paused',error='Waiting for a human decision/dependency.')
        view=self.dependency_view(goal,item)
        self.park_item(item,'decision' if view['pending_decisions'] else 'dependency',resume='review' if review else 'continue',
            bundle_reported=item['bundles'],continue_pending=not review,report=report,
            review_trigger='completed' if declared['status']=='completed' else 'batch_limit' if review else None)

    def reconcile_waiting(self,goal):
        """Lazy adoption on an active coordinator tick; never rewrite plans on startup/read."""
        self.release_answered_waits(goal)
        for item in self.items(goal['id']):
            if self.child_items(goal,item):continue
            if item['status'] not in ('in_progress','waiting','waiting_children','todo','rework'):continue
            task=self.task_for_item(item);view=self.dependency_view(goal,item)
            if task and task['id'] in self.builders.active:
                if task['status']=='waiting_approval' and item['status']!='waiting':self.park_item(item,'approval',resume='active_task')
                elif task['status']=='queued' and item['status']!='waiting':self.park_item(item,'builder_queue',resume='active_task')
                continue
            if item['status'] in ('in_progress','waiting') and self.pending_builder_result(item):
                continue  # finished_batch must consume this result once before parking it.
            triggers=self.wait_triggers(goal,item)
            if triggers['gpu'] or triggers['long_commands']:
                if item['status']!='waiting' or item.get('wait_reason') not in ('gpu','long_command'):
                    self.park_item(item,'long_command' if triggers['long_commands'] else 'gpu',resume=item.get('waiting_resume') or 'continue')
            elif not view['ready']:
                reason='decision' if view['pending_decisions'] else 'dependency'
                if item['status']=='waiting' and item.get('wait_reason') in ('deferred','platform_error','split_children','plan_capacity'):reason=item['wait_reason']
                resume='continue' if item.get('continue_pending') or item['status']=='in_progress' or item['status']=='todo' and item['bundles']>0 else item.get('waiting_resume','start')
                if any(triggers.values()):
                    if item['status']!='waiting' or item.get('wait_reason')!=reason:self.park_item(item,reason,resume=resume)
                elif item['status'] in ('waiting','waiting_children','in_progress'):
                    # Unknown legacy metadata must be classified by Planner, never assumed safe.
                    issue=self.schedule_issue(goal,'unconfirmed_wait',{'id':item['id'],'dependency_state':view['dependency_state']},plan_item_id=item['id'])
                    self.store.update('plan_item',item['id'],status='blocked',wait_reason=None,waiting_resume=None)
            elif item['status'] in ('waiting','waiting_children','in_progress'):
                self.wake_wait(goal,item)
        for item in self.items(goal['id']):
            if self.child_items(goal,item):continue
            if item['status']=='review' and item.get('review_trigger') is None and (item.get('report') or {}).get('report_status')!='completed' and not item.get('review_error'):
                if item['bundles']>=self.bundle_limit(item):self.prepare_limit_review(goal,item)
                else:self.park_item(item,'continuation',resume='continue',continue_pending=True)

    def builder_busy(self,goal):
        focus=goal.get('builder_focus_item_id')
        for identifier in list(self.builders.active):
            task=self.store.get('task',identifier)
            if task.get('goal_id')!=goal['id']:continue
            if task['status']=='running' or task['status']=='queued' and (task['plan_item_id']==focus or not self.focus_pinned(goal,focus)):
                return True
        return False

    def candidate_ready(self,goal,item):
        if terminal(item):return False
        if self.child_items(goal,item):return False
        if self.long_commands.active(goal['id'],item['id']) or self.live_gpu_jobs(goal,item):return False
        if item.get('needs_decision_replan') or not self.dependency_view(goal,item)['ready']:return False
        if item.get('task_id') in self.builders.active:return False
        if item['status']=='in_progress':return not self.pending_builder_result(item)
        if item['status'] in ('todo','rework'):return True
        return item['status']=='waiting' and item.get('wait_reason') not in ('split_children','deferred','platform_error','plan_capacity') and item.get('waiting_resume') in ('start','continue','active_task')

    def next_builder(self,goal):
        items=sorted(self.items(goal['id']),key=lambda i:(i.get('priority',100),i['created']))
        ready=[i for i in items if self.candidate_ready(goal,i)]
        focus=next((i for i in ready if i['id']==goal.get('builder_focus_item_id')),None)
        # A just-ended progress chunk stays pinned ahead of every other todo/rework row.
        continuations=[i for i in ready if i.get('continue_pending') or i.get('waiting_resume')=='continue']
        return focus or (continuations[0] if continuations else ready[0] if ready else None)

    def prepare_limit_review(self,goal,item):
        if terminal(item):return
        if self.child_items(goal,item):return
        if self.long_commands.active(goal['id'],item['id']):return
        report=item.get('report') or {'summary':'The existing Builder batch allowance is exhausted; use preserved evidence.','report_status':'progress'}
        self.store.update('plan_item',item['id'],status='review',wait_reason=None,waiting_resume=None,continue_pending=False,
            report=report,review_trigger='batch_limit')

    def refresh_split_reviews(self,goal):
        for item in self.items(goal['id']):
            if item['status']=='superseded':continue
            children=self.child_items(goal,item)
            if not children:continue
            # Do not discard a running tool's result. Reconcile immediately after its boundary.
            if item.get('task_id') in self.builders.active:continue
            if any(not terminal(c) for c in children):
                if item['status']!='waiting_children':
                    self.store.update('plan_item',item['id'],status='waiting_children',wait_reason='children',waiting_resume='review',continue_pending=False,review_trigger=None)
                continue
            if terminal(item):continue
            # Evidence reindexing updates timestamps even when observations are identical.
            # Only substantive child results may trigger another paid parent review.
            revision=self.child_review_revision(children)
            if item.get('children_review_revision')==revision:continue
            refs={r['id']:r for r in item.get('evidence',[])}
            for child in children:
                for ref in child.get('evidence',[]):refs[ref['id']]=ref
            self.store.update('plan_item',item['id'],status='review',wait_reason=None,waiting_resume=None,evidence=list(refs.values()),
                review_trigger='children_completed',split_children_completed=True,children_review_revision=revision)

    @staticmethod
    def child_review_revision(children):
        return [[c['id'],c['status'],c.get('superseded_by'),c.get('accepted_criteria'),c.get('evidence')] for c in children]
