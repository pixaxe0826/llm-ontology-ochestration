"""Explicit report protocol and sticky chunks, using fake suppliers and local files."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench.goals import GoalManager
from workbench.gpu import GPUManager
from workbench.memory import Memory
from workbench.store import Store
from test_goals import FakeProviders,response,until


class SchedulingProviders(FakeProviders):
    def __init__(self,mode='progress'):
        super().__init__();self.mode=mode;self.phases={};self.trace=[];self.chunks=[];self.reviews=[]
    def complete(self,provider,messages,model,*args,**kwargs):
        current=json.loads(next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])['item']
        if provider=='baseten' and 'Review this REPORT' in messages[-1]['content']:
            self.trace.append(('review',current['title'],current.get('review_trigger')))
            self.reviews.append((current['title'],current.get('review_trigger'),current['bundles']))
            if current['title']=='A' and current.get('review_trigger')=='batch_limit' and self.mode in ('split','defer'):
                self.calls.append({'provider':provider,'messages':messages})
                return response(json.dumps({'decision':'rework','defects':['Remaining scope needs a bounded follow-up.'],'criteria':[],
                    'summary':'Split remaining work.' if self.mode=='split' else 'Defer until a scope decision.',
                    'batch_action':self.mode,'goal_complete':False,
                    'next_items':[{'key':'remaining-A','title':'Child of A','acceptance':['Observed local work.']}] if self.mode=='split' else []}))
            return super().complete(provider,messages,model,*args,**kwargs)
        if provider!='meta':return super().complete(provider,messages,model,*args,**kwargs)
        self.calls.append({'provider':provider,'messages':messages})
        title=current['title'];phase=self.phases.get(current['id'],0);self.phases[current['id']]=phase+1
        chunk=(title,current['bundles'])
        if not self.chunks or self.chunks[-1]!=chunk:self.chunks.append(chunk)
        self.trace.append(('builder',title,phase))
        if self.mode=='platform_error' and title=='A' and phase==1:raise AttributeError('Synthetic Builder platform failure')
        if self.mode=='completed_then_decision' and title=='A' and phase==1:
            return response(calls=[('report_item',{'status':'completed','summary':'A completed local work.','verification':'Observed file changes.'}),
                ('request_decision',{'question':'Accept this configuration?','options':['Yes','No'],'recommendation':'Yes','reason':'Review the observed configuration.','affected_items':[current['id']],'reversible':False})])
        if self.mode=='approval' and title=='A':
            if phase==0:return response(calls=[('run_command',{'argv':['python','-m','compileall','-q','.'],'cwd':'.','timeout':5})])
            return self.report('completed',title)
        if self.mode=='step_limit' and title=='A' and phase<3:
            return response('Working on the next file.',calls=[('write_file',{'path':f'{title}-{phase}.txt','content':str(phase),'expected_sha256':None})])
        if phase%2==0:return response('Working on the assigned scope.',calls=[('write_file',{'path':f'{title}-{phase}.txt','content':str(phase),'expected_sha256':None})])
        progress=title=='A' and phase==1 and self.mode in ('progress','plain','step_limit') or title=='B' and phase==1 and self.mode=='approval' or title=='A' and self.mode in ('split','defer')
        if self.mode=='plain' and title=='A':
            if progress:return response('Gathering the remaining sections; implementation is still in progress.')
            return response(json.dumps({'report_status':'completed','summary':'A complete.','verification':'Observed file changes.'}))
        return self.report('progress' if progress else 'completed',title)
    @staticmethod
    def report(status,title):
        summary=title+' '+status+'.'
        return response(calls=[('report_item',{'status':status,'summary':summary,'verification':'Observed local file changes; NOT RUN: GPU.'})])


class GoalSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.folder=self.root/'workspace';self.folder.mkdir()
        self.store=Store(self.root/'state');self.memory=Memory(self.store);self.providers=SchedulingProviders()
        self.manager=GoalManager(self.store,self.providers,GPUManager(self.store),self.memory)
        self.goal=self.manager.create({'title':'Sticky chunks','objective':'Finish bounded local items.','workspace':str(self.folder),
            'policy':{'mode':'autonomous','gpus':[]},'max_steps':2},start=False)['goal']
    def tearDown(self):
        self.manager.shutdown()
        for thread in list(self.manager.threads.values()):thread.join(3)
        until(lambda:not self.manager.builders.active)
        self.store.on_save=None;self.memory.pending.join();self.store.db.close();self.temp.cleanup()
    def seed(self,*titles):
        self.manager.add_items(self.goal,[{'key':title,'title':title,'acceptance':['Observed local work.']} for title in titles])
        return self.manager.items(self.goal['id'])
    def launch(self):self.manager.launch(self.goal['id'])
    def status(self):return self.store.get('goal',self.goal['id'])['status']
    def done(self):return until(lambda:self.status()=='done')
    def test_progress_continues_same_item_without_intermediate_planner_review(self):
        a,b=self.seed('A','B');self.launch();self.done()
        self.assertEqual(self.providers.chunks,[('A',1),('A',2),('B',1)])
        self.assertEqual(self.providers.reviews,[('A','completed',2),('B','completed',1)])
        current=self.store.get('plan_item',a['id']);self.assertEqual((current['attempts'],current['bundles'],current['reworks']),(1,2,0))
        self.assertEqual(len(current['task_ids']),1)
        progress=[json.loads(m['body']) for m in self.manager.messages(self.goal['id']) if m['type']=='REPORT' and m['plan_item_id']==a['id']]
        self.assertEqual([p['report_status'] for p in progress],['progress','completed'])
        self.assertEqual(len([c for c in self.providers.calls if c['provider']=='baseten']),2)
    def test_plain_prose_success_is_progress_and_json_completion_is_supported(self):
        self.providers.mode='plain';self.seed('A','B');self.launch();self.done()
        self.assertEqual(self.providers.chunks,[('A',1),('A',2),('B',1)])
        self.assertEqual(len(self.providers.reviews),2)
    def test_step_limit_without_completion_continues_before_other_todo(self):
        self.providers.mode='step_limit';a,b=self.seed('A','B');self.launch();self.done()
        self.assertEqual(self.providers.chunks,[('A',1),('A',2),('B',1)])
        self.assertEqual(self.store.get('plan_item',a['id'])['attempts'],1)
        self.assertEqual(self.providers.reviews[0],('A','completed',2))
    def test_approval_parks_item_and_answer_does_not_preempt_another_items_continuation(self):
        self.providers.mode='approval';a,b=self.seed('A','B')
        original=self.manager.finished_batch;observed=[]
        def finished(goal,item,task):
            if item['id']==b['id'] and self.manager.batch_report(task)['status']=='progress':
                request=self.manager.approvals(goal['id'])[0]
                observed.append(self.store.get('plan_item',a['id'])['status'])
                self.manager.decide(goal['id'],request['id'],True)
                until(lambda:self.store.get('task',request['task_id'])['status']=='queued')
            return original(goal,item,task)
        self.manager.finished_batch=finished;self.launch();self.done()
        self.assertEqual(observed,['waiting'])
        self.assertEqual(self.providers.chunks,[('A',1),('B',1),('B',2),('A',1)])
        self.assertEqual(self.store.get('plan_item',a['id'])['bundles'],1)
        b_last=max(i for i,e in enumerate(self.providers.trace) if e[0]=='builder' and e[1]=='B')
        a_resume=next(i for i,e in enumerate(self.providers.trace) if e[0]=='builder' and e[1]=='A' and e[2]==1)
        self.assertGreater(a_resume,b_last)
    def test_human_decision_parks_only_affected_item_and_waits_without_models(self):
        a,b=self.seed('A','B');decision=self.manager.create_decision(self.goal,{'question':'Choose A?','options':['Yes','No'],'recommendation':'Yes','reversible':False},item_id=a['id'])
        self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.store.get('plan_item',a['id'])['status'],'waiting')
        self.assertEqual(self.store.get('plan_item',b['id'])['status'],'done')
        count=len(self.providers.calls);time.sleep(.3);self.assertEqual(len(self.providers.calls),count)
        self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Yes'});self.done()
        self.assertEqual(self.providers.chunks,[('B',1),('A',1),('A',2)])
    def test_at_most_one_in_progress_during_running_and_approval_handoffs(self):
        self.providers.mode='approval';self.seed('A','B');counts=[]
        def saved(kind,row):
            if kind=='plan_item':counts.append(sum(i['status']=='in_progress' for i in self.manager.items(self.goal['id'])))
        self.store.on_save=saved;self.launch()
        request=until(lambda:next(iter(self.manager.approvals(self.goal['id'])),None))
        self.manager.decide(self.goal['id'],request['id'],True);self.done()
        self.assertLessEqual(max(counts),1)
    def test_answer_waits_until_approval_and_linked_decision_are_fully_published(self):
        self.providers.mode='approval';self.seed('A');published=threading.Event();release=threading.Event();answered=threading.Event()
        original=self.store.save;result=[]
        def save(kind,row,**kwargs):
            value=original(kind,row,**kwargs)
            if kind=='goal_approval' and value['state']=='pending' and not value.get('decision_id'):
                published.set();release.wait(3)
            return value
        self.store.save=save;self.launch();self.assertTrue(published.wait(5))
        request=self.manager.approvals(self.goal['id'])[0]
        def answer():
            try:result.append(self.manager.decide(self.goal['id'],request['id'],True))
            finally:answered.set()
        worker=threading.Thread(target=answer);worker.start()
        self.assertFalse(answered.wait(.08));release.set();worker.join(3);self.assertTrue(result);self.done()
        decisions=self.manager.decisions(self.goal['id']);self.assertEqual(len(decisions),1);self.assertEqual(decisions[0]['state'],'answered')
        # A late correlated ESCALATE must not reopen an already handled approval.
        before=len(decisions);self.manager.message(self.goal,'Builder','Human','ESCALATE','Approval required for command.',request['plan_item_id'],[request['id']])
        self.assertEqual(len(self.manager.decisions(self.goal['id'])),before)
    def test_platform_failure_parks_item_without_progress_review_and_continues_other_work(self):
        self.providers.mode='platform_error';a,b=self.seed('A','B');self.launch();until(lambda:self.status()=='waiting_decision')
        item=self.store.get('plan_item',a['id']);self.assertEqual(item['status'],'waiting');self.assertEqual(item['wait_reason'],'platform_error')
        self.assertEqual(self.store.get('plan_item',b['id'])['status'],'done')
        self.assertEqual(self.providers.reviews,[('B','completed',1)])
    def test_completed_chunk_waiting_for_decision_is_reviewed_after_answer_without_new_builder(self):
        self.providers.mode='completed_then_decision';a=self.seed('A')[0];self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.providers.reviews,[])
        item=self.store.get('plan_item',a['id']);self.assertEqual(item['waiting_resume'],'review')
        before=self.providers.phases[a['id']];decision=self.manager.pending_decisions(self.goal['id'])[0]
        self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Yes'});self.done()
        self.assertEqual(self.providers.phases[a['id']],before);self.assertEqual(self.providers.reviews,[('A','completed',1)])
    def test_four_chunks_review_once_then_defer_without_fifth_builder_chunk(self):
        self.providers.mode='defer';a,b=self.seed('A','B');self.launch();until(lambda:self.status()=='waiting_decision')
        item=self.store.get('plan_item',a['id']);self.assertEqual((item['status'],item['wait_reason'],item['bundles']),('waiting','deferred',4))
        self.assertEqual(self.providers.chunks[:4],[('A',1),('A',2),('A',3),('A',4)])
        self.assertEqual(self.providers.reviews,[('A','batch_limit',4),('B','completed',1)])
        self.assertEqual(self.store.get('plan_item',b['id'])['status'],'done')
        self.assertTrue(self.manager.pending_decisions(self.goal['id']))
    def test_four_chunks_can_split_remaining_scope_and_review_parent_from_child_evidence(self):
        self.providers.mode='split';a,b=self.seed('A','B');self.launch();self.done()
        parent=self.store.get('plan_item',a['id']);self.assertEqual((parent['status'],parent['bundles'],parent['attempts']),('done',4,1))
        child=next(i for i in self.manager.items(self.goal['id']) if i['title']=='Child of A')
        self.assertEqual(child['parent'],a['id']);self.assertEqual(child['split_from'],a['id']);self.assertEqual(child['status'],'done')
        self.assertEqual([r for r in self.providers.reviews if r[0]=='A'],[('A','batch_limit',4),('A','children_completed',4)])
        self.assertTrue(parent['accepted_criteria']);self.assertTrue(set(e['id'] for e in child['evidence'])<=set(e['id'] for e in parent['evidence']))
    def test_limit_split_cannot_depend_on_exhausted_parent(self):
        a=self.seed('A')[0];self.store.update('plan_item',a['id'],bundles=4,review_trigger='batch_limit')
        verdict={'decision':'rework','defects':['Split remaining work.'],'criteria':[],'next_items':[{'key':'bad-child','title':'Bad child','acceptance':['Observed.'],'depends_on':['A']}],
            'batch_action':'split','goal_complete':False}
        with self.assertRaisesRegex(ValueError,'exhausted parent'):self.manager.validate_review(self.goal,self.store.get('plan_item',a['id']),verdict)
        self.assertEqual(len(self.manager.items(self.goal['id'])),1)
    def test_legacy_multiple_in_progress_become_ready_without_counter_or_evidence_loss(self):
        items=self.seed('A','B','C')
        for item in items:self.store.update('plan_item',item['id'],status='in_progress',attempts=2,bundles=2,continue_pending=True)
        before=copy.deepcopy(self.manager.items(self.goal['id']));self.manager.reconcile_waiting(self.goal)
        for old,new in zip(before,self.manager.items(self.goal['id'])):
            self.assertEqual(new['status'],'todo')
            for key in ('id','title','acceptance','attempts','bundles','reworks','evidence','task_ids'):self.assertEqual(new[key],old[key])
    def test_legacy_detail_display_is_waiting_without_writing_history(self):
        a,b=self.seed('A','B')
        for item in (a,b):self.store.update('plan_item',item['id'],status='in_progress')
        before=self.manager.items(self.goal['id']);d=self.manager.detail(self.goal['id'])
        self.assertTrue(all(i['execution_status']=='waiting' for i in d['items']))
        self.assertEqual(before,self.manager.items(self.goal['id']))
    def test_pause_resume_between_chunks_keeps_attempt_and_starts_fresh_safe_history(self):
        a,b=self.seed('A','B');selected=threading.Event();release=threading.Event();original=self.manager.assign;held=[False]
        def assign(goal,item,continuation=False):
            if continuation and not held[0]:held[0]=True;selected.set();release.wait(3)
            return original(goal,item,continuation)
        self.manager.assign=assign;self.launch();self.assertTrue(selected.wait(5))
        current=self.store.get('plan_item',a['id']);old_id=current['task_id'];old=copy.deepcopy(self.store.get('task',old_id))
        self.manager.control(self.goal['id'],'pause');release.set();self.manager.threads[self.goal['id']].join(3)
        self.assertEqual(self.store.get('plan_item',a['id'])['bundles'],1)
        self.manager.control(self.goal['id'],'resume');self.done()
        item=self.store.get('plan_item',a['id']);self.assertEqual((item['attempts'],item['bundles']),(1,2))
        self.assertNotEqual(item['task_id'],old_id);self.assertEqual(self.store.get('task',old_id),old)


if __name__=='__main__':unittest.main()
