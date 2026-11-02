"""Explicit human reopen, one format correction, answered waits and attention events."""
import copy,json,time,unittest
from unittest.mock import patch
import test_goal_inbox as inbox
import test_goal_operations as operations
import test_notifications as alerts
from test_goals import response
from workbench.goal_watchdog import GoalWatchdog
from workbench.notifications import Notifications
from workbench.goal_inbox import inbox_contract
from workbench.goal_prompts import system
from workbench.security import register_secret


class DirectiveRecoveryTests(unittest.TestCase):
    setUp=inbox.GoalInboxTests.setUp;tearDown=inbox.GoalInboxTests.tearDown
    items=inbox.GoalInboxTests.items;submit=inbox.GoalInboxTests.submit;action=inbox.GoalInboxTests.action

    def test_completed_multi_item_reopen_replaces_criteria_preserves_history_and_original(self):
        a,b=self.items('agent','evaluation');saved=[]
        for item in (a,b):
            saved.append(self.store.update('plan_item',item['id'],status='done',bundles=4,execution_cycle=None,last_commit_hash='existing-commit',accepted_criteria={'old':['old-evidence']}))
        row=self.submit('interrupt','Use the real model for every tool choice. Rework both items.')
        action=self.action(row,items=[{'key':a['key'],'acceptance':[{'text':'Every tool is model-chosen.','kind':'execution'}]},
            {'key':b['key'],'acceptance':[{'text':'Report actual real-model results.','kind':'document','doc_role':'results','document_paths':['eval/RESULTS.md']}],'depends_on':[a['id']]}],
            item_actions=[{'item_id':i['id'],'resolution':'rework','instruction':'Collect fresh evidence.'} for i in (a,b)])
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No manual Builder launch')):
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]})
        for old in saved:
            current=self.store.get('plan_item',old['id']);self.assertEqual(current['status'],'rework');self.assertEqual(current['bundles'],0)
            self.assertEqual(current['historical_bundles'],4);self.assertEqual(current['execution_cycle'],1)
            for key in ('evidence','task_ids','last_commit_hash','reworks','attempts'):self.assertEqual(current.get(key),old.get(key))
            self.assertIn(row['text'],current['instruction']);self.assertEqual(current['accepted_criteria'],{})
        self.assertEqual(self.store.get('plan_item',b['id'])['depends_on'],[a['id']]);self.assertEqual(len(self.store.items('goal_acceptance_change',None)),2)

    def test_one_correction_gets_specific_error_and_applies_the_completed_item(self):
        item=self.items('agent')[0];self.store.update('plan_item',item['id'],status='done')
        row=self.submit('instruction','Reopen the completed agent.')
        bad=self.action(row,items=[{'key':item['key'],'status':'rework'}])
        good=self.action(row,item_actions=[{'item_id':item['id'],'resolution':'rework','instruction':'Use the real model.'}])
        with patch.object(self.manager,'call',return_value=response(json.dumps({'human_actions':[good]}))) as call:
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[bad]})
        self.assertEqual(call.call_count,1);self.assertIn('plan_update_fields_forbidden',call.call_args.args[4][-1]['content'])
        self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'applied');self.assertEqual(self.store.get('plan_item',item['id'])['status'],'rework')
        errors=[a for a in self.store.items('goal_inbox_attempt',None) if a['state']=='validation_error']
        self.assertEqual(len(errors),1);self.assertIn('"status": "rework"',errors[0]['response'])

    def test_two_invalid_criteria_shapes_leave_item_unchanged_and_explain_failure(self):
        item=self.items('agent')[0];item=self.store.update('plan_item',item['id'],status='done')
        row=self.submit('instruction','Replace the acceptance criteria.')
        bad={'human_actions':[self.action(row,items=[{'key':item['key'],'acceptance':[{'text':'Real run.','kind':'unknown'}]}])]}
        with patch.object(self.manager,'call',return_value=response(json.dumps(bad))) as call:self.manager.finish_inbox(self.goal,[row],bad)
        self.assertEqual(call.call_count,1);self.assertEqual(self.store.get('plan_item',item['id']),item)
        self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'partial')
        ack=self.manager.messages(self.goal['id'])[-1];self.assertIn('수용 기준',' '.join(ack['human_brief']['what_happened']))
        self.assertEqual(len([r for r in self.store.items('goal_inbox_attempt',None) if r['state']=='validation_error']),2)

    def test_note_cannot_reopen_even_when_text_starts_instruction_reproduces_incident(self):
        item=self.items('agent')[0];item=self.store.update('plan_item',item['id'],status='done')
        row=self.submit('note','Instruction: reopen the completed agent.')
        bad=self.action(row,item_actions=[{'item_id':item['id'],'resolution':'rework'}])
        with patch.object(self.manager,'call',side_effect=AssertionError('No correction call for a note')):
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[bad]})
        self.assertEqual(self.store.get('plan_item',item['id']),item)
        ack=self.manager.messages(self.goal['id'])[-1];self.assertIn('참고(note)',' '.join(ack['human_brief']['what_happened']))
        self.assertEqual(self.store.items('goal_inbox_attempt',None)[0]['validation_error'],'note_is_reference_only')

    def test_malformed_interrupt_json_has_one_correction_without_builder(self):
        item=self.items('agent')[0];row=self.submit('interrupt','Use this implementation direction.')
        good=self.action(row,instructions=[{'item_id':item['id'],'text':'Implement the real model loop.'}])
        with patch.object(self.manager,'call',side_effect=[response('bad json'),response(json.dumps({'human_actions':[good]}))]) as call:
            self.assertTrue(self.manager.process_interrupts(self.goal))
        self.assertEqual(call.call_count,2);self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'applied')
        self.assertTrue(any(r.get('response')=='bad json' for r in self.store.items('goal_inbox_attempt',None)))

    def test_invalid_second_target_does_not_partially_mutate_first(self):
        a,b=self.items('a','b');before=self.manager.items(self.goal['id']);row=self.submit('instruction','Reopen both items.')
        bad={'human_actions':[self.action(row,items=[{'key':a['key'],'acceptance':['Changed criterion.']}],
            item_actions=[{'item_id':a['id'],'resolution':'rework'},{'item_id':'missing','resolution':'rework'}])]}
        with patch.object(self.manager,'call',return_value=response(json.dumps(bad))):self.manager.finish_inbox(self.goal,[row],bad)
        self.assertEqual(self.manager.items(self.goal['id']),before)

    def test_contract_and_prompt_use_directive_actions_not_decision_actions(self):
        text=inbox_contract([{'id':'human','kind':'interrupt','text':'Rework.','plan_item_id':None}])
        self.assertIn('Completed items may be reopened',text);self.assertIn('"resolution":"rework"',text)
        self.assertIn('human_actions',system(self.goal,'Planner'));self.assertIn('"resolution":"rework"',system(self.goal,'Planner'))

    def test_rejected_secret_response_is_redacted_in_attempts_and_ack(self):
        item=self.items('a')[0];row=self.submit('instruction','Update the criterion safely.')
        secret='inbox-private-credential-'+('K'*32);register_secret(secret)
        value={'human_actions':[self.action(row,unapplied=[secret])]}
        with patch.object(self.manager,'call',return_value=response(json.dumps(value))):self.manager.finish_inbox(self.goal,[row],value)
        stored=json.dumps(self.store.items('goal_inbox_attempt',None)+self.manager.messages(self.goal['id']))
        self.assertNotIn(secret,stored)

    def test_claimed_format_correction_is_not_replayed_after_restart(self):
        self.items('a');row=self.submit('instruction','Use the real model.')
        self.store.update('goal_directive',row['id'],state='processing',correction_attempted=True)
        with patch.object(self.manager,'call',side_effect=AssertionError('No repeated correction')):
            self.manager.recover_directives()
            self.manager.finish_inbox(self.goal,[row],{})
        self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'partial')


class AnsweredWaitTests(unittest.TestCase):
    setUp=inbox.GoalInboxTests.setUp;tearDown=inbox.GoalInboxTests.tearDown
    items=inbox.GoalInboxTests.items;decision=inbox.GoalInboxTests.decision

    def waiting(self):
        item=self.items('preview')[0];d=self.decision(item)
        self.store.update('decision',d['id'],state='answered',answer='Do not submit a new GPU job.\nUse copy_gpu_artifact for the already succeeded job.',answered_at=time.time())
        item=self.store.update('plan_item',item['id'],status='waiting',wait_reason='platform_error',waiting_resume=None,decision_action_source=d['id'],needs_decision_replan=False)
        return item,d

    def test_answered_explicit_copy_releases_stale_lock_once_without_model_or_gpu(self):
        item,d=self.waiting()
        with patch.object(self.manager,'call',side_effect=AssertionError('No model polling')),patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')):
            self.manager.refresh_dependencies(self.goal);self.manager.refresh_dependencies(self.goal)
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['status'],'rework');self.assertIsNone(current['wait_reason'])
        self.assertIn('copy_gpu_artifact',current['instruction']);self.assertEqual(len(self.store.items('goal_item_action',None)),1)
        self.assertEqual(self.manager.next_builder(self.goal)['id'],item['id'])

    def test_later_failure_and_ambiguous_answer_do_not_auto_retry(self):
        item,d=self.waiting();self.store.update('plan_item',item['id'],wait_started_at=time.time()+5)
        self.manager.release_answered_waits(self.goal);self.assertEqual(self.store.get('plan_item',item['id'])['status'],'waiting')
        self.store.update('plan_item',item['id'],wait_started_at=0);self.store.update('decision',d['id'],answer='Pause and inspect; do not retry.')
        self.manager.release_answered_waits(self.goal);self.assertEqual(self.store.get('plan_item',item['id'])['status'],'waiting')

    def test_dependency_ready_with_missing_resume_is_restored_without_new_cycle(self):
        a,b=self.items('a','b');self.store.update('plan_item',a['id'],status='done')
        item=self.store.update('plan_item',b['id'],status='waiting',wait_reason='dependency',waiting_resume=None,depends_on=[a['id']],execution_cycle=1)
        self.manager.reconcile_waiting(self.goal)
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['waiting_resume'],'start');self.assertEqual(current['execution_cycle'],1)
        self.assertEqual(self.manager.next_builder(self.goal)['id'],item['id'])

    def test_orphan_wait_is_repaired_without_manufacturing_a_decision(self):
        item=self.items('preview')[0];self.store.update('plan_item',item['id'],status='waiting',wait_reason='platform_error',waiting_resume=None)
        self.assertFalse(self.manager.block_if_no_runnable(self.goal,self.manager.items(self.goal['id'])))
        self.assertTrue(self.manager.candidate_ready(self.goal,self.store.get('plan_item',item['id'])))
        self.assertFalse(self.manager.pending_decisions(self.goal['id']))

    def test_just_completed_child_is_pending_parent_review_not_an_idle_dead_end(self):
        parent,child=self.items('parent','child');self.store.update('plan_item',child['id'],parent=parent['id'],status='done')
        self.store.update('plan_item',parent['id'],status='waiting_children',wait_reason='children')
        self.assertFalse(self.manager.block_if_no_runnable(self.goal,self.manager.items(self.goal['id'])))


class NewAttentionTests(unittest.TestCase):
    setUp=alerts.NotificationTests.setUp;tearDown=alerts.NotificationTests.tearDown;rows=alerts.NotificationTests.rows

    def test_ack_severity_localized_summary_once_and_resolved_by_new_instruction(self):
        for state,event,severity in [('partial','directive_partial','warning'),('rejected','directive_rejected','warning'),('applied','directive_applied','info')]:
            self.store.save('goal_directive',{'id':state,'goal_id':self.goal['id'],'state':state})
            row={'id':'ack-'+state,'goal_id':self.goal['id'],'type':'ACK','to':'Human','created':self.now,'ack_status':state,'directive_id':state,
                'human_brief':{'language':'Korean','summary':'지시 처리 결과 '+state,'unapplied':['수용 기준 형식을 확인하세요.']}}
            self.store.save('message',row);self.service.tick();self.store.save('message',row);self.service.tick()
            found=[n for n in self.rows() if n['event']==event];self.assertEqual(len(found),1);self.assertEqual(found[0]['severity'],severity)
            self.assertIn('지시 처리 결과',found[0]['summary'])
        self.store.update('goal_directive','partial',resolved_by='applied');self.service.tick()
        self.assertTrue(next(r for r in self.rows() if r['event']=='directive_partial')['resolved'])

    def test_waiting_decision_count_urgent_repeat_and_resolution(self):
        for i in range(2):self.store.save('decision',{'id':str(i),'goal_id':self.goal['id'],'state':'pending','source':'test','affected_items':[]})
        self.store.update('goal',self.goal['id'],status='waiting_decision');self.service.tick()
        row=next(r for r in self.rows() if r['event']=='waiting_decision')
        self.assertEqual(row['severity'],'urgent');self.assertEqual(row['event_label'],'진행 중단: 판단 2건 대기')
        self.now+=900;self.service.tick();self.assertEqual(self.store.get('notification',row['id'])['delivery_seq'],2)
        self.store.update('goal',self.goal['id'],status='running');self.service.tick();self.assertTrue(self.store.get('notification',row['id'])['resolved'])

    def test_no_runnable_is_urgent_once_and_clears_on_resume(self):
        self.store.update('goal',self.goal['id'],status='blocked',scheduling_blocked=True,scheduling_human_summary='미완료 3개: 플랫폼 오류 대기입니다.')
        self.service.tick();rows=[r for r in self.rows() if r['event']=='no_runnable'];self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['severity'],'urgent');self.assertIn('미완료 3개',rows[0]['summary'])
        self.store.update('goal',self.goal['id'],status='running');self.service.tick();self.assertTrue(self.store.get('notification',rows[0]['id'])['resolved'])


class InactiveWatchdogTests(unittest.TestCase):
    setUp=operations.GoalOperationsTests.setUp;tearDown=operations.GoalOperationsTests.tearDown
    create=operations.GoalOperationsTests.create;fixture=operations.GoalOperationsTests.fixture

    def test_system_pause_and_interruption_warn_after_ten_minutes_user_pause_exempt(self):
        goal,item,task=self.fixture();now=2_000_000_000
        service=Notifications(self.store,native=alerts.FakeNative(),clock=lambda:now);watch=GoalWatchdog(self.manager,service,clock=lambda:now)
        for status in ('paused','interrupted'):
            self.store.update('goal',goal['id'],status=status,status_since=now-599,pause_origin='system',reason='Automatic stop.')
            watch.tick();self.assertFalse([r for r in self.store.items('notification',None) if r['event']=='watchdog' and not r['resolved']])
            now+=61;watch.tick();warnings=[r for r in self.store.items('notification',None) if r['event']=='watchdog']
            self.assertTrue(warnings);self.assertEqual(warnings[0]['severity'],'warning')
            for r in warnings:self.store.update('notification',r['id'],resolved=True)
            self.store.update('goal',goal['id'],status='done');now+=61;watch.tick()
        before=len(self.store.items('notification',None));self.store.update('goal',goal['id'],status='paused',pause_origin='user',reason='Paused by user.',status_since=now-1000)
        now+=61;watch.tick();self.assertEqual(len(self.store.items('notification',None)),before)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused');service.stop()


if __name__=='__main__':unittest.main()
