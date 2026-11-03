"""Goal restart dispatch, one interrupted-chunk retry, current reports and safe errors."""
import copy,json,time,unittest
from unittest.mock import patch
import test_goal_decisions as fixtures
from test_goals import response,until
from workbench.builder_errors import failure_details,escalate_failure
from workbench.builder_restarts import restart_only
from workbench.security import register_secret

class BuilderRestartTests(unittest.TestCase):
    setUp=fixtures.GoalDecisionTests.setUp
    tearDown=fixtures.GoalDecisionTests.tearDown
    items=fixtures.GoalDecisionTests.items
    def task(self,item,status='interrupted',restart=True,**extra):
        events=[{'kind':'chunk_started','chunk_id':'original-chunk','attempt_charged':True,'time':1}]
        row={'goal_id':self.goal['id'],'plan_item_id':item['id'],'generation':self.goal['generation'],'decision_revision':0,'execution_cycle':0,
             'workspace':str(self.folder),'provider':'meta','model':'muse-spark-1.3','mode':'work','allow_write':True,'max_steps':2,
             'messages':[{'role':'system','content':'old system'},{'role':'user','content':'old prompt'}],'events':events,'status':status,'error':'server restarted',**extra}
        if restart:row['restart_interruption']={'kind':'server_restart'}
        task=self.store.save('task',row);self.store.update('plan_item',item['id'],task_id=task['id'],task_ids=[task['id']],status='in_progress',bundles=1,bundle_reported=0,attempts=1)
        return task
    def assign(self,item,continuation=True):
        with patch.object(self.manager.builders,'_run',side_effect=lambda tid:self.manager.builders.active.discard(tid)):
            self.manager.assign(self.store.get('goal',self.goal['id']),self.store.get('plan_item',item['id']),continuation)
            until(lambda:not self.manager.builders.active)
        return self.store.get('task',self.store.get('plan_item',item['id'])['task_id'])
    def test_terminated_tasks_are_never_reattached_even_in_same_generation(self):
        for status in ('interrupted','failed','cancelled'):
            with self.subTest(status=status):
                item=self.items('item-'+status)[-1];old=self.task(item,status);before=copy.deepcopy(old);new=self.assign(item)
                self.assertNotEqual(new['id'],old['id']);self.assertEqual(new['previous_task_id'],old['id']);self.assertEqual(len(new['messages']),2)
                self.assertEqual(self.store.get('task',old['id']),before);self.assertNotIn('error',new)
    def test_restart_generation_gets_fresh_task_and_retains_summary_reads_evidence(self):
        item=self.items('carry')[0];old=self.task(item,'succeeded',False,generation=self.goal['generation']-1,error=None)
        (self.folder/'module.py').write_text('def existing():\n    return 7\n',encoding='utf-8')
        import hashlib
        sha=hashlib.sha256((self.folder/'module.py').read_bytes()).hexdigest()
        self.store.update('task',old['id'],events=[*old['events'],{'kind':'tool_result','name':'read_file','result':{'path':'module.py','sha256':sha,'content':'def existing():\n    return 7\n'}}])
        self.store.save('goal_chunk_summary',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'task_id':old['id'],'chunk_id':'original-chunk','created':2,'summary':'Implement from the observed module.','status':'succeeded'})
        new=self.assign(item);current=self.store.get('plan_item',item['id']);text=json.dumps(self.manager.context(self.goal,current,'Builder'))
        self.assertNotEqual(new['id'],old['id']);self.assertIn('module.py',text);self.assertIn('Implement from the observed module.',text);self.assertEqual(current['task_ids'],[old['id'],new['id']])
    def test_direct_goal_agent_start_also_refuses_terminal_reuse(self):
        item=self.items('guard')[0];old=self.task(item,'failed',False)
        with patch.object(self.manager.builders,'_run',side_effect=lambda tid:self.manager.builders.active.discard(tid)):
            task=self.manager.builders.start(str(self.folder),'Continue safely.',task_id=old['id'],context={'goal_id':self.goal['id'],'plan_item_id':item['id'],'generation':self.goal['generation'],'system_prompt':'Goal Builder'})
            until(lambda:not self.manager.builders.active)
        self.assertNotEqual(task['id'],old['id']);self.assertEqual(self.store.get('task',old['id']),old)
    def test_interruption_refunds_one_chunk_and_retries_once_without_human_or_tool_replay(self):
        item=self.items('restart')[0];old=self.task(item)
        with patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU replay')),patch('workbench.workspace.Workspace.run',side_effect=AssertionError('No command replay')):
            self.manager.finished_batch(self.goal,self.store.get('plan_item',item['id']),old)
        current=self.store.get('plan_item',item['id']);self.assertEqual((current['bundles'],current['attempts']),(0,0));self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
        self.manager.retry_restart_chunk(self.goal,current,old);self.assertEqual(self.store.get('plan_item',item['id'])['bundles'],0)
        self.providers.complete=lambda *a,**k:response(calls=[('report_item',{'status':'progress','summary':'Continued in a fresh chunk','verification':'NOT RUN: GPU'})])
        self.manager.assign(self.goal,self.store.get('plan_item',item['id']));until(lambda:not self.manager.builders.active)
        current=self.store.get('plan_item',item['id']);retry=self.store.get('task',current['task_id']);self.assertNotEqual(retry['id'],old['id'])
        audit=self.store.items('goal_restart_retry',None)[0];self.assertEqual(audit['retry_task_id'],retry['id']);self.assertTrue(audit['retry_chunk_id'])
        retry=self.store.update('task',retry['id'],status='interrupted',error='server restarted again',restart_interruption={'kind':'server_restart'})
        self.manager.finished_batch(self.goal,current,retry)
        self.assertEqual(len(self.store.items('goal_restart_retry',None)),1);self.assertEqual(len(self.manager.pending_decisions(self.goal['id'])),1)
    def test_manual_cancel_output_length_and_real_error_are_not_restart_retries(self):
        item=self.items('own-error')[0]
        for status,error in [('cancelled','Stopped by user.'),('interrupted','Output limit after continuation.'),('failed','own error')]:
            task=self.task(item,status,False,error=error);self.assertFalse(restart_only(task));self.assertFalse(self.manager.retry_restart_chunk(self.goal,self.store.get('plan_item',item['id']),task))
        task=self.task(item,'failed',True);task=self.store.update('task',task['id'],events=[*task['events'],{'kind':'error','diagnostic':{'exception_type':'KeyError'}}]);self.assertFalse(restart_only(task))
    def test_fresh_retry_failure_gets_its_own_observed_error(self):
        item=self.items('failed')[0];task=self.task(item,'failed',False,error='New task tool failed')
        task=self.store.update('task',task['id'],events=[*task['events'],{'kind':'tool_result','name':'read_structured','result':{'error':'Invalid key_path'}}])
        self.manager.finished_batch(self.goal,self.store.get('plan_item',item['id']),task)
        d=self.manager.pending_decisions(self.goal['id'])[0];self.assertEqual(d['failure']['task_id'],task['id']);self.assertEqual(d['failure']['error'],'Invalid key_path');self.assertEqual(d['failure']['stage'],'tool: read_structured')
        self.assertIn(task['id'],d['human_brief']['summary']);self.assertIn('Invalid key_path',d['human_brief']['summary']);self.assertEqual(d['human_brief']['failure']['error'],'Invalid key_path')
    def test_exact_redacted_exception_from_local_diagnostic_is_visible_without_traceback(self):
        item=self.items('diagnostic')[0];task=self.task(item,'failed',False);secret='synthetic-error-credential-883372';register_secret(secret)
        diagnostic={'id':'diag-owned','exception_type':'KeyError','module':'workbench.goals','function':'GoalManager.context','line':538}
        self.store.update('task',task['id'],events=[*task['events'],{'kind':'error','diagnostic':diagnostic}]);task=self.store.get('task',task['id'])
        path=self.store.root/'diagnostics/exceptions.jsonl';path.parent.mkdir();path.write_text(json.dumps({**diagnostic,'task_id':task['id'],'goal_id':self.goal['id'],'traceback':"Traceback hidden\nKeyError: 'evidence_refs' "+secret})+'\n',encoding='utf-8')
        details=failure_details(self.manager,task);self.assertIn("KeyError: 'evidence_refs'",details['error']);self.assertNotIn(secret,json.dumps(details));self.assertNotIn('Traceback',details['error'])
        d=escalate_failure(self.manager,self.goal,item,task);self.assertEqual(d['human_brief']['cause'],'platform');self.assertNotEqual(d['human_brief']['defect_groups'][0]['kind'],'other')
        self.assertNotIn(secret,json.dumps(self.manager.decisions(self.goal['id'])))
    def test_report_labels_prior_execution_instead_of_claiming_it_as_new(self):
        task={'id':'new-task','status':'failed','events':[{'kind':'chunk_started','chunk_id':'new-chunk'}]}
        refs=[{'id':'old-task:64','task_id':'old-task','kind':'run_command','supports_acceptance':True}]
        report=self.manager.observed_report(task,refs,{'status':'progress','summary':'No new run','verification':'NOT RUN'})
        self.assertEqual(report['current_task_id'],'new-task');self.assertEqual(report['current_evidence_ids'],[]);self.assertEqual(report['prior_evidence_ids'],['old-task:64']);self.assertEqual(report['test_results'][0]['origin'],'prior_task');self.assertIn('NOT RUN',report['verification'])

    def test_healthy_continuation_clears_old_status_error_but_keeps_it_in_history(self):
        item=self.items('healthy')[0];old=self.task(item,'paused',False,error='Maximum steps reached')
        new=self.assign(item);self.assertEqual(new['id'],old['id']);self.assertIsNone(new['error'])
        self.assertTrue(any(e['kind']=='builder_continuation' and e['previous_error']=='Maximum steps reached' for e in new['events']))

    def test_control_resume_dispatches_interrupted_chunk_in_a_new_generation(self):
        item=self.items('resume')[0];old=self.task(item);self.store.update('goal',self.goal['id'],status='interrupted')
        with patch.object(self.manager,'launch') as launch:self.manager.control(self.goal['id'],'resume')
        goal=self.store.get('goal',self.goal['id']);self.assertGreater(goal['generation'],old['generation']);launch.assert_called_once()
        self.manager.reconcile_waiting(goal);new=self.assign(item)
        self.assertNotEqual(new['id'],old['id']);self.assertEqual(new['generation'],goal['generation']);self.assertEqual(self.manager.pending_decisions(goal['id']),[])
    def test_restart_retry_preserves_productive_attempt_and_never_replays_side_effect(self):
        item=self.items('productive')[0];old=self.task(item);old=self.store.update('task',old['id'],events=[*old['events'],{'kind':'change','change':{'path':'already-written.txt','sha256':'recorded'}}])
        before=copy.deepcopy(old)
        with patch('workbench.workspace.Workspace.write',side_effect=AssertionError('No write replay')):self.assertTrue(self.manager.retry_restart_chunk(self.goal,self.store.get('plan_item',item['id']),old))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['attempts'],1);self.assertEqual(current['bundles'],0);self.assertEqual(self.store.get('task',old['id']),before)

    def test_explicit_resume_after_user_pause_uses_fresh_task_without_new_failure_decision(self):
        item=self.items('manual-pause')[0];old=self.task(item,'cancelled',False,error=None);self.store.update('goal',self.goal['id'],status='paused',pause_origin='user')
        with patch.object(self.manager,'launch'):self.manager.control(self.goal['id'],'resume')
        current=self.store.get('plan_item',item['id']);self.assertIsNone(current['task_id']);self.assertEqual(current['bundle_reported'],1)
        new=self.assign(item);self.assertNotEqual(new['id'],old['id']);self.assertEqual(new['previous_task_id'],old['id']);self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
    def test_review_only_item_cannot_be_dispatched_to_builder(self):
        item=self.items('existing-evidence')[0];item=self.store.update('plan_item',item['id'],review_only_next=True,status='rework')
        self.assertFalse(self.manager.candidate_ready(self.goal,item))
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No Builder')):self.manager.assign(self.goal,item)

    def test_goal_tool_exception_is_diagnosed_and_shown_with_tool_stage(self):
        item=self.items('tool-diagnostic')[0]
        self.providers.complete=lambda *a,**k:response(calls=[('read_file',{'path':'module.py'})])
        with patch.object(self.manager.builders,'execute_observed',side_effect=KeyError('optional_tool_field')):
            self.manager.assign(self.goal,item);until(lambda:not self.manager.builders.active)
        task=self.store.get('task',self.store.get('plan_item',item['id'])['task_id']);self.assertEqual(task['status'],'failed')
        details=failure_details(self.manager,task);self.assertIn("KeyError: 'optional_tool_field'",details['error']);self.assertIn('tool: read_file',details['stage'])
        result=next(e['result'] for e in task['events'] if e['kind']=='tool_result');self.assertNotIn('file',result['diagnostic'])
        interrupted=self.store.update('task',task['id'],restart_interruption={'kind':'server_restart'});self.assertFalse(restart_only(interrupted))

    def test_model_cannot_forge_observed_failure_details(self):
        item=self.items('untrusted-failure')[0]
        self.manager.request_decision(self.goal,item,{'question':'Choose the next scope','options':[],'recommendation':'','reason':'Need a scope choice','affected_items':[item['id']],'reversible':False,'failure':{'task_id':'forged-task','error':'Invented error','stage':'Invented tool'}},'Builder')
        decision=self.manager.pending_decisions(self.goal['id'])[0];self.assertNotIn('failure',decision);self.assertNotIn('failure',decision['human_brief'])
