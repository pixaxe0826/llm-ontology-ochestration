"""Fake suppliers, temporary workspaces and event-driven human decisions; no paid calls."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench.goals import GoalManager,GoalWorkspace
from workbench.goal_costs import GoalLimit
from workbench.gpu import GPUManager
from workbench.memory import Memory
from workbench.notifications import Notifications
from workbench.plan_updates import prepare_plan
from workbench.security import register_secret
from workbench.store import Store
from test_goals import FakeProviders,response,until


class DecisionProviders(FakeProviders):
    def complete(self,provider,messages,model,*args,**kwargs):
        instruction=messages[-1].get('content','')
        if provider=='baseten' and 'Update dependencies and the impact' in instruction:
            self.calls.append({'provider':provider,'messages':messages,'replan':True})
            plan=json.loads(next(m['content'] for m in messages if m['content'].startswith('CURRENT PLAN')).split('\n',1)[1])
            return response(json.dumps({'items':[{'key':i['key'],'depends_on':i.get('depends_on') or [],
                'blocked_by_decision':i.get('blocked_by_decision') or [],'provisional_scope':i.get('provisional_scope') or 'none'} for i in plan],
                'summary':'Decision impact and dependencies recorded.'}))
        return super().complete(provider,messages,model,*args,**kwargs)


class GoalDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.folder=self.root/'workspace with spaces';self.folder.mkdir()
        self.store=Store(self.root/'state');self.memory=Memory(self.store);self.providers=DecisionProviders()
        self.manager=GoalManager(self.store,self.providers,GPUManager(self.store),self.memory)
        self.goal=self.manager.create({'title':'Decision test','objective':'Implement and verify bounded items.','workspace':str(self.folder),
            'deadline':time.time()+3600,'policy':{'mode':'autonomous','gpus':[]}},start=False)['goal']
    def tearDown(self):
        self.manager.shutdown()
        for t in self.manager.threads.values():t.join(3)
        until(lambda:not self.manager.builders.active)
        self.memory.pending.join();self.store.on_save=None;self.store.db.close();self.temp.cleanup()
    def items(self,*keys,scope='none'):
        self.manager.add_items(self.goal,[{'key':key,'title':key,'parent':None,'acceptance':['A real passing test is recorded.'],
            'depends_on':[],'blocked_by_decision':[],'provisional_scope':scope} for key in keys])
        self.store.update('goal',self.goal['id'],status='running')
        return self.manager.items(self.goal['id'])
    def decision(self,item,**values):
        return self.manager.create_decision(self.goal,{'question':'Which implementation?','options':['A','B'],'recommendation':'A',
            'reason':'A is a reversible local default.','reversible':False,**values},item_id=item['id'])
    def status(self):return self.store.get('goal',self.goal['id'])['status']
    def launch(self):self.manager.launch(self.goal['id'])
    def answer(self,d,**data):return self.manager.answer_decision(self.goal['id'],{'goal_id':self.goal['id'],'decision_id':d['id'],**data})
    def test_pending_decision_does_not_block_unrelated_item_then_event_wakes(self):
        blocked,free=self.items('blocked','independent');d=self.decision(blocked)
        self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.store.get('plan_item',free['id'])['status'],'done')
        self.assertEqual(self.store.get('plan_item',blocked['id'])['status'],'waiting')
        calls=len(self.providers.calls);time.sleep(.4);self.assertEqual(len(self.providers.calls),calls)
        # Even an accidental direct call cannot invoke a model in the wait state.
        with self.assertRaises(GoalLimit):self.manager.call(self.store.get('goal',self.goal['id']),'baseten','development','Planner',[{'role':'user','content':'status'}])
        self.assertEqual(len(self.providers.calls),calls)
        self.answer(d,option='A');until(lambda:self.status()=='done')
        self.assertGreater(len(self.providers.calls),calls)
    def test_dependency_prevents_early_assignment_and_cycle_is_rejected(self):
        first,second=self.items('first','second')
        self.manager.update_dependencies(self.goal['id'],{'item_id':second['id'],'depends_on':[first['id']],'blocked_by_decision':[]})
        self.assertFalse(self.manager.dependency_view(self.goal,self.store.get('plan_item',second['id']))['ready'])
        with self.assertRaisesRegex(ValueError,'Cyclic'):self.manager.update_dependencies(self.goal['id'],{'item_id':first['id'],'depends_on':[second['id']]})
    def test_only_reversible_local_scope_and_enabled_setting_is_provisional(self):
        item=self.items('local',scope='local_files_and_tests')[0];d=self.decision(item,reversible=True)
        item=self.store.get('plan_item',item['id'])
        self.assertTrue(self.manager.dependency_view(self.goal,item)['provisional'])
        self.assertFalse(self.manager.dependency_view({**self.goal,'allow_provisional':False},item)['ready'])
        self.store.update('decision',d['id'],reversible=False)
        self.assertFalse(self.manager.dependency_view(self.goal,item)['ready'])
        self.store.update('decision',d['id'],reversible=True)
        self.store.update('plan_item',item['id'],provisional_scope='none')
        self.assertFalse(self.manager.dependency_view(self.goal,self.store.get('plan_item',item['id']))['ready'])
    def provisional_task(self):
        item=self.items('local',scope='local_files_and_tests')[0];d=self.decision(item,reversible=True)
        task=self.store.save('task',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'generation':1,'decision_revision':0,'workspace':str(self.folder)})
        return item,d,task
    def test_provisional_tool_guards_gpu_paid_calls_spec_and_commands(self):
        _,_,task=self.provisional_task()
        for name,args in [('submit_gpu_job',{}),('get_gpu_job',{}),('memory_note',{}),('write_file',{'path':'spec/core.py'}),
                          ('write_file',{'path':'spec.yaml'}),('write_file',{'path':'values.json'}),
                          ('run_command',{'argv':['python','render.py']}),('run_command',{'argv':['git','commit']})]:
            with self.subTest(name=name,args=args),self.assertRaises(PermissionError):self.manager.provisional_guard(task,name,args)
        self.manager.provisional_guard(task,'write_file',{'path':'tests/test_math.py'})
        self.manager.provisional_guard(task,'run_command',{'argv':['python','-m','unittest','tests.test_math']})
        self.assertEqual(self.manager.ledger.rows(self.goal['id']),[])
    def test_provisional_write_marks_file_manifest_and_no_proxy_env_for_test(self):
        item,d,task=self.provisional_task();ws=GoalWorkspace(self.manager,task)
        result=ws.write('draft.py','# local draft\n')
        self.assertTrue(result['provisional']);self.assertEqual(result['decision_ids'],[d['id']])
        manifest=json.loads((self.folder/('.muse-provisional-'+self.goal['id']+'.json')).read_text())
        self.assertEqual(manifest['files'][0]['status'],'PROVISIONAL')
        self.manager.proxy_tokens[self.goal['id']]='synthetic-proxy-secret-not-exported'
        with patch('workbench.workspace.Workspace.run',return_value={'exit_code':0}) as run:
            ws.run(['python','-m','unittest'],'.',5)
            self.assertIsNone(run.call_args.kwargs['extra_env'])
    def test_same_answer_clears_only_provisional_marks(self):
        item,d,task=self.provisional_task();GoalWorkspace(self.manager,task).write('draft.py','# draft\n')
        self.store.update('plan_item',item['id'],status='done',accepted_criteria={'observed':['evidence']},bundles=2)
        before=self.store.get('plan_item',item['id']);calls=len(self.providers.calls)
        self.answer(d,option='A');after=self.store.get('plan_item',item['id'])
        for key in ('status','accepted_criteria','bundles','reworks','attempts'):self.assertEqual(after[key],before[key])
        self.assertFalse(after['provisional']);self.assertEqual(len(self.providers.calls),calls)
        self.assertEqual(self.store.items('goal_provisional_file',None)[0]['status'],'CONFIRMED')
    def test_different_answer_reworks_only_provisional_items_then_replans(self):
        item,d,task=self.provisional_task();GoalWorkspace(self.manager,task).write('draft.py','# draft\n')
        self.store.update('plan_item',item['id'],status='done',accepted_criteria={'observed':['evidence']})
        before=(self.folder/'draft.py').read_bytes();self.answer(d,option='B')
        current=self.store.get('plan_item',item['id'])
        self.assertEqual(current['status'],'rework');self.assertEqual(current['reworks'],0);self.assertTrue(current['needs_decision_replan'])
        self.assertTrue(self.store.get('goal',self.goal['id'])['decision_replan_pending'])
        self.assertEqual((self.folder/'draft.py').read_bytes(),before)
        with self.assertRaises(GoalLimit):self.manager.builder_item_guard(task)
        self.manager.refresh_dependencies(self.store.get('goal',self.goal['id']))
        self.assertTrue(any(c.get('replan') for c in self.providers.calls))
        self.assertFalse(self.store.get('plan_item',item['id'])['needs_decision_replan'])
    def test_end_to_end_provisional_completion_waits_then_same_answer_finishes_without_call(self):
        item=self.items('local',scope='local_files_and_tests')[0];d=self.decision(item,reversible=True)
        self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.store.get('plan_item',item['id'])['status'],'done')
        calls=len(self.providers.calls);self.answer(d,option='A');until(lambda:self.status()=='done')
        self.assertEqual(len(self.providers.calls),calls)
    def test_legacy_unknown_dependencies_not_inferred_and_refresh_preserves_plan(self):
        item=self.items('legacy')[0]
        old={k:v for k,v in item.items() if k not in ('depends_on','blocked_by_decision')}
        self.store.save('plan_item',old,preserve_updated=True)
        d=self.manager.detail(self.goal['id'])['items'][0]
        self.assertEqual(d['dependency_state'],'unconfirmed');self.assertFalse(d['ready'])
        self.assertNotIn('depends_on',self.store.get('plan_item',item['id']))
        self.manager.refresh_dependencies(self.store.get('goal',self.goal['id']))
        current=self.store.get('plan_item',item['id'])
        self.assertEqual(current['depends_on'],[]);self.assertEqual(current['blocked_by_decision'],[])
        for k in ('id','title','acceptance','status','attempts','reworks','bundles','evidence'):self.assertEqual(current[k],old[k])
    def test_unknown_rows_never_execute_after_failed_dependency_refresh(self):
        item=self.items('legacy')[0];old={k:v for k,v in item.items() if k not in ('depends_on','blocked_by_decision')};self.store.save('plan_item',old)
        self.providers.complete=lambda *args,**kwargs:response('invalid JSON')
        self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.store.items('task',None),[])
    def test_dependency_format_warning_needs_no_new_decision_and_explicit_edit_wakes(self):
        item=self.items('legacy')[0];self.store.save('plan_item',{k:v for k,v in item.items() if k not in ('depends_on','blocked_by_decision')})
        original=self.providers.complete;self.providers.complete=lambda *args,**kwargs:response('invalid JSON')
        self.launch();until(lambda:self.status()=='waiting_decision')
        self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
        self.providers.complete=original
        self.manager.update_dependencies(self.goal['id'],{'item_id':item['id'],'depends_on':[],'blocked_by_decision':[]})
        until(lambda:self.status()=='done')
        self.assertFalse(self.store.get('goal',self.goal['id'])['dependency_refresh_error'])
    def test_decision_answers_do_not_resume_user_paused_goal(self):
        item=self.items('paused')[0];d=self.decision(item);self.manager.control(self.goal['id'],'pause')
        self.answer(d,answer='B');self.assertEqual(self.status(),'paused');self.assertEqual(self.providers.calls,[])
    def test_decision_and_dependencies_validate_scope_secrets_and_pending_state(self):
        item=self.items('item')[0];d=self.decision(item)
        self.assertEqual(d['id'],self.decision(item)['id'])
        secret='synthetic-decision-secret-123456';register_secret(secret)
        with self.assertRaises(ValueError):self.answer(d,answer=secret)
        with self.assertRaises(ValueError):self.answer(d,option='unknown')
        with self.assertRaises(ValueError):self.manager.update_dependencies(self.goal['id'],{'item_id':item['id'],'blocked_by_decision':['foreign']})
        self.answer(d,answer='Direct answer');self.assertEqual(self.store.get('decision',d['id'])['state'],'answered')
        with self.assertRaises(ValueError):self.answer(d,answer='again')
    def test_user_dependency_edit_can_detach_and_attach_decision(self):
        item=self.items('item')[0];d=self.decision(item)
        self.manager.update_dependencies(self.goal['id'],{'item_id':item['id'],'blocked_by_decision':[]})
        self.assertTrue(self.manager.dependency_view(self.goal,self.store.get('plan_item',item['id']))['ready'])
        self.assertEqual(self.store.get('decision',d['id'])['affected_items'],[])
        self.manager.update_dependencies(self.goal['id'],{'item_id':item['id'],'blocked_by_decision':[d['id']]})
        self.assertFalse(self.manager.dependency_view(self.goal,self.store.get('plan_item',item['id']))['ready'])
    def test_pending_provisional_predecessor_does_not_unblock_gpu_scope(self):
        first,second=self.items('local','gpu')
        self.store.update('plan_item',first['id'],provisional_scope='local_files_and_tests')
        d=self.decision(first,reversible=True)
        self.store.update('plan_item',first['id'],status='done',provisional=True,provisional_decisions=[d['id']])
        self.manager.update_dependencies(self.goal['id'],{'item_id':second['id'],'depends_on':[first['id']]})
        view=self.manager.dependency_view(self.goal,self.store.get('plan_item',second['id']))
        self.assertFalse(view['ready']);self.assertEqual(view['pending_decisions'],[d['id']])
    def test_checkpoint_has_persisted_decision_and_planner_dependency_refresh(self):
        item=self.items('checkpoint')[0];item['title']='M0: Human checkpoint'
        self.manager.checkpoint_decision(self.goal,item)
        decisions=self.manager.pending_decisions(self.goal['id'])
        self.assertEqual(len(decisions),1);self.assertFalse(decisions[0]['reversible'])
        self.assertTrue(self.store.get('goal',self.goal['id'])['decision_replan_pending'])
        self.manager.checkpoint_decision(self.goal,item);self.assertEqual(len(self.manager.decisions(self.goal['id'])),1)
    def test_completed_checkpoint_blocks_only_its_explicit_descendants(self):
        checkpoint,geometry,physics=self.items('checkpoint','geometry','physics')
        checkpoint=self.store.update('plan_item',checkpoint['id'],title='M0: Human checkpoint',status='done')
        self.manager.checkpoint_decision(self.goal,checkpoint)
        self.manager.update_dependencies(self.goal['id'],{'item_id':geometry['id'],'depends_on':[checkpoint['id']]})
        self.assertFalse(self.manager.dependency_view(self.goal,self.store.get('plan_item',geometry['id']))['ready'])
        self.assertTrue(self.manager.dependency_view(self.goal,physics)['ready'])
    def test_completed_legacy_predecessor_with_unknown_dependencies_is_not_independence(self):
        first,second=self.items('legacy','dependent')
        first={k:v for k,v in first.items() if k not in ('depends_on','blocked_by_decision')};first['status']='done';self.store.save('plan_item',first)
        self.manager.update_dependencies(self.goal['id'],{'item_id':second['id'],'depends_on':[first['id']]})
        self.assertFalse(self.manager.dependency_view(self.goal,self.store.get('plan_item',second['id']))['ready'])
        self.manager.refresh_dependencies(self.store.get('goal',self.goal['id']))
        self.assertTrue(self.manager.dependency_view(self.goal,self.store.get('plan_item',second['id']))['ready'])
    def test_answer_after_deadline_wakes_but_never_calls_model(self):
        item=self.items('blocked')[0];d=self.decision(item);self.launch();until(lambda:self.status()=='waiting_decision')
        self.store.update('goal',self.goal['id'],deadline_epoch=time.time()-1)
        calls=len(self.providers.calls);self.answer(d,option='A');until(lambda:self.status()=='paused')
        self.assertEqual(len(self.providers.calls),calls)
    def test_wait_entry_rechecks_newly_available_work_to_avoid_lost_wakeup(self):
        blocked,free=self.items('blocked','just-unblocked');self.decision(blocked)
        event=threading.Event();self.manager.decision_events[self.goal['id']]=event
        with patch.object(event,'wait') as waiting:self.manager.wait_for_decisions(self.goal)
        waiting.assert_not_called();self.assertEqual(self.status(),'running')
    def test_finished_unreported_chunk_cannot_sleep_on_unrelated_decision(self):
        blocked,ready=self.items('P5','P3');self.decision(blocked)
        task=self.store.save('task',{'goal_id':self.goal['id'],'plan_item_id':ready['id'],'status':'succeeded'})
        self.store.update('plan_item',ready['id'],status='in_progress',task_id=task['id'],bundles=2,bundle_reported=1)
        event=threading.Event();self.manager.decision_events[self.goal['id']]=event
        with patch.object(event,'wait') as waiting:self.manager.wait_for_decisions(self.goal)
        waiting.assert_not_called();self.assertEqual(self.status(),'running')
    def test_approval_decision_blocks_item_but_other_items_continue(self):
        self.providers.command=['python','-m','compileall','-q','.'];self.items('first','second');self.launch()
        until(lambda:len(self.manager.approvals(self.goal['id']))==2)
        until(lambda:self.status()=='waiting_decision')
        ds=self.manager.pending_decisions(self.goal['id']);self.assertEqual(len(ds),2)
        calls=len(self.providers.calls);time.sleep(.3);self.assertEqual(len(self.providers.calls),calls)
        for d in ds:self.answer(d,option='Approve')
        until(lambda:self.status()=='done')
        self.assertEqual(self.providers.peak,1)
    def test_notifications_dedupe_and_resolve_after_answer(self):
        class Native:
            def send(self,*a,**k):raise RuntimeError('offline toast failure')
        service=Notifications(self.store,native=Native());self.store.on_save=service.observe
        item=self.items('item')[0];d=self.decision(item)
        self.manager.message(self.goal,'Planner','Human','ESCALATE','Choose an implementation.',item['id'],[d['id']])
        service.tick();service.observe('decision',d);service.tick()
        rows=self.store.items('notification',None);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source']['kind'],'decision')
        self.assertEqual(service.inbox()['urgent'][0]['decision_id'],d['id'])
        with self.assertRaises(ValueError):service.resolve(rows[0]['id'])
        self.answer(d,option='A');service.tick();self.assertEqual(service.inbox()['urgent'],[])
    def test_restart_preserves_decision_and_legacy_plan_and_marks_wait_interrupted(self):
        item=self.items('blocked')[0];d=self.decision(item);self.launch();until(lambda:self.status()=='waiting_decision')
        self.manager.shutdown()
        for t in self.manager.threads.values():t.join(3)
        self.assertEqual(self.status(),'interrupted')
        before=self.manager.items(self.goal['id'])
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        self.assertEqual(self.manager.items(self.goal['id']),before);self.assertEqual(self.store.get('decision',d['id'])['state'],'pending')


if __name__=='__main__':unittest.main()
