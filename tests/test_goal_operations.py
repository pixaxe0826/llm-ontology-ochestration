"""Offline fault injection for retries, unattended restart, watchdog and GPU defaults."""
import copy,json,os,threading,time,unittest
from unittest.mock import MagicMock,patch
import test_goals as fixtures
from workbench.goals import GoalManager
from workbench.goal_costs import GoalLimit
from workbench.goal_gpu import submission_status,check_submission
from workbench.goal_operations import unattended,gpu_cycle_limit
from workbench.goal_watchdog import GoalWatchdog
from workbench.goal_prompts import system
from workbench.model_retry import retryable
from workbench.notifications import Notifications
from workbench.providers import BASETEN_MODEL

class APIConnectionError(RuntimeError):pass
class StatusError(RuntimeError):
    def __init__(self,status,body=None):self.status_code=status;self.body=body;super().__init__('opaque upstream')


class GoalOperationsTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create

    def fixture(self,**values):
        goal=self.create(start=False,**values)
        self.manager.add_items(goal,[{'key':'work','title':'Implement geometry','acceptance':['Observed artifact exists.'],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0]
        task=self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'workspace':str(self.folder),
            'status':'succeeded','events':[],'messages':[],'execution_cycle':0})
        self.manager.retry_sleep=MagicMock()
        return goal,item,task

    def call(self,goal,task,provider='meta'):
        return self.manager.call(goal,provider,'development','Builder' if provider=='meta' else 'Planner',
            [{'role':'user','content':'Verify this bounded item.'}],model=goal['builder_model'] if provider=='meta' else BASETEN_MODEL,task_id=task['id'])

    def test_connection_then_429_then_success_backoff_and_every_attempt_ledgered(self):
        goal,item,task=self.fixture();known={'usage':{'input_tokens':12,'output_tokens':4}}
        self.providers.complete=MagicMock(side_effect=[APIConnectionError('opaque'),StatusError(429,known),fixtures.response('OK')])
        self.assertEqual(self.call(goal,task).choices[0].message.content,'OK')
        self.assertEqual(self.providers.complete.call_count,3);self.assertEqual([c.args[0] for c in self.manager.retry_sleep.call_args_list],[5,20])
        rows=self.manager.ledger.rows(goal['id']);self.assertEqual(len(rows),3)
        self.assertEqual(sorted(r['retry_index'] for r in rows),[0,1,2]);self.assertEqual(len({r['model_call_id'] for r in rows}),1)
        unknown=next(r for r in rows if r['retry_index']==0);self.assertTrue(unknown['usage_estimated']);self.assertEqual(unknown['estimated_usd'],unknown['reserved_usd'])
        reported=next(r for r in rows if r['retry_index']==1);self.assertEqual(reported['input_tokens'],12);self.assertEqual(reported['output_tokens'],4)
        self.assertEqual(self.store.get('task',task['id'])['model_retry_count'],2)
        attempts=self.store.items('goal_model_attempt',None);self.assertEqual([r['state'] for r in sorted(attempts,key=lambda r:r['retry_index'])],['failed','failed','succeeded'])

    def test_baseten_5xx_retries_keep_fixed_model_and_two_retry_limit(self):
        goal,item,task=self.fixture();self.providers.complete=MagicMock(side_effect=StatusError(503))
        with self.assertRaises(StatusError):self.call(goal,task,'baseten')
        self.assertEqual(self.providers.complete.call_count,3);self.assertEqual(len(self.manager.ledger.rows(goal['id'])),3)
        self.assertTrue(all(c.args[2]==BASETEN_MODEL for c in self.providers.complete.call_args_list))
        self.assertEqual(self.store.get('task',task['id'])['model_retry_count'],2)

    def test_other_4xx_and_format_errors_are_not_retried(self):
        goal,item,task=self.fixture()
        for error in (StatusError(400),StatusError(401),StatusError(403),StatusError(404),StatusError(422),ValueError('invalid schema'),TypeError('invalid fields')):
            with self.subTest(kind=type(error).__name__,status=getattr(error,'status_code',None)):
                self.providers.complete=MagicMock(side_effect=error)
                with self.assertRaises(type(error)):self.call(goal,task)
                self.assertEqual(self.providers.complete.call_count,1)
        self.manager.retry_sleep.assert_not_called()

    def test_reset_wrapped_connection_and_non_retryable_classification(self):
        self.assertTrue(retryable(ConnectionResetError('reset')))
        wrapped=RuntimeError('transport');wrapped.__cause__=ConnectionResetError('reset');self.assertTrue(retryable(wrapped))
        self.assertFalse(retryable(ValueError('format')));self.assertFalse(retryable(StatusError(401)))
        self.assertTrue(retryable(StatusError(500)));self.assertFalse(retryable(StatusError(600)))

    def test_pause_during_backoff_and_budget_cap_prevent_another_call(self):
        goal,item,task=self.fixture();self.providers.complete=MagicMock(side_effect=APIConnectionError('connection'))
        self.manager.retry_sleep.side_effect=lambda delay:self.manager.control(goal['id'],'pause')
        with self.assertRaises(GoalLimit):self.call(goal,task)
        self.assertEqual(self.providers.complete.call_count,1)
        self.manager.save(goal,status='running',budgets=self.manager.ledger.budgets({'meta':{'total':.00001}}))
        self.providers.complete.reset_mock()
        with self.assertRaises(GoalLimit):self.call(goal,task)
        self.providers.complete.assert_not_called()

    def test_deadline_before_retry_pauses_without_sleeping(self):
        goal,item,task=self.fixture();goal=self.store.update('goal',goal['id'],deadline_epoch=time.time()+2)
        self.providers.complete=MagicMock(side_effect=StatusError(500))
        with self.assertRaises(GoalLimit):self.call(goal,task)
        self.assertEqual(self.providers.complete.call_count,1);self.manager.retry_sleep.assert_not_called()
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')

    def test_no_secret_in_retry_audit_or_ledger(self):
        goal,item,task=self.fixture();secret='synthetic-retry-secret-42098'
        self.providers.complete=MagicMock(side_effect=[APIConnectionError(secret),fixtures.response('OK')])
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):self.call(goal,task)
        persisted='\n'.join(raw for raw, in self.store.db.execute('SELECT data FROM records'))
        self.assertNotIn(secret,persisted)

    def test_goal_flow_retries_model_but_writes_and_command_execute_once(self):
        original=self.providers.complete;failures=[]
        def complete(provider,*args,**kwargs):
            if provider=='meta' and len(failures)<2:failures.append(1);raise APIConnectionError('transient')
            return original(provider,*args,**kwargs)
        self.providers.complete=complete
        with patch('workbench.goal_operations.DELAYS',(0,0)):
            goal=self.create();fixtures.until(lambda:self.store.get('goal',goal['id'])['status']=='done')
        tasks=[t for t in self.store.items('task',None) if t.get('goal_id')==goal['id']]
        events=[e for t in tasks for e in t['events']]
        self.assertEqual(sum(e['kind']=='change' for e in events),1)
        self.assertEqual(sum(e['kind']=='tool_result' and e.get('name')=='run_command' for e in events),1)
        self.assertEqual(sum(t.get('model_retry_count',0) for t in tasks),2)
        self.assertFalse(self.store.items('gpu',None))

    def test_three_builder_failures_wait_for_provider_decision(self):
        original=self.providers.complete
        self.providers.complete=lambda provider,*a,**k: (_ for _ in ()).throw(APIConnectionError('offline')) if provider=='meta' else original(provider,*a,**k)
        with patch('workbench.goal_operations.DELAYS',(0,0)):
            goal=self.create();fixtures.until(lambda:bool(self.manager.pending_decisions(goal['id'])))
        decisions=self.manager.pending_decisions(goal['id']);self.assertTrue(decisions)
        task=next(t for t in self.store.items('task',None) if t.get('goal_id')==goal['id'])
        self.assertEqual(task['model_retry_count'],2);self.assertEqual(task.get('failure_kind'),'provider')
        self.assertEqual(len([r for r in self.store.items('goal_model_attempt',None) if r['role']=='Builder']),3)

    def restart(self):
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        return self.manager

    def test_unattended_default_false_validation_and_settings(self):
        goal,item,task=self.fixture();self.assertFalse(goal['unattended']);self.assertEqual(goal['gpu_jobs_per_cycle'],3)
        self.manager.save(goal,status='paused');self.manager.update(goal['id'],{'unattended':True,'gpu_jobs_per_cycle':5})
        saved=self.store.get('goal',goal['id']);self.assertTrue(saved['unattended']);self.assertEqual(saved['gpu_jobs_per_cycle'],5)
        self.assertEqual(saved['objective'],goal['objective'])
        for value in ('true',1,None):
            with self.assertRaises(ValueError):unattended(value)
        for value in (-1,True,1.5,1001):
            with self.assertRaises(ValueError):gpu_cycle_limit(value)

    def test_clean_restart_running_unattended_resumes_exactly_once(self):
        goal,item,task=self.fixture(unattended=True);self.manager.save(goal,status='running')
        self.manager.shutdown();self.assertEqual(self.store.get('goal',goal['id'])['status'],'interrupted')
        manager=self.restart()
        with patch.object(manager,'control',return_value={}) as control:
            manager.resume_unattended();manager.resume_unattended()
        control.assert_called_once_with(goal['id'],'resume')
        self.assertEqual(self.store.items('goal_restart',None)[0]['state'],'resumed')

    def test_crash_running_resumes_but_paused_waiting_or_disabled_never_do(self):
        goal,item,task=self.fixture(unattended=True);self.manager.save(goal,status='running');manager=self.restart()
        with patch.object(manager,'control') as control:manager.resume_unattended();control.assert_called_once()
        for state,enabled in (('paused',True),('waiting_decision',True),('waiting_approval',True),('planning',True),('running',False)):
            other=self.create(start=False,unattended=enabled);self.manager.save(other,status=state)
        manager=self.restart()
        with patch.object(manager,'control') as control:manager.resume_unattended();control.assert_not_called()

    def test_resume_failure_recorded_notified_once_and_never_retried(self):
        goal,item,task=self.fixture(unattended=True);self.manager.save(goal,status='running');manager=self.restart();manager.notifier=MagicMock()
        with patch.object(manager,'control',side_effect=ValueError('expired deadline')) as control:
            manager.resume_unattended();manager.resume_unattended()
        self.assertEqual(control.call_count,1);manager.notifier.emit.assert_called_once()
        self.assertEqual(self.store.items('goal_restart',None)[0]['state'],'failed');self.assertEqual(self.store.get('goal',goal['id'])['status'],'interrupted')

    def test_manual_pause_cancels_saved_resume_intent(self):
        goal,item,task=self.fixture(unattended=True);goal=self.manager.save(goal,status='running');self.manager.remember_restart(goal)
        self.manager.control(goal['id'],'pause');self.restart()
        with patch.object(self.manager,'control') as control:self.manager.resume_unattended();control.assert_not_called()

    def test_gpu_default_three_counts_failures_and_keeps_item_override_one(self):
        goal,item,task=self.fixture();self.assertEqual(submission_status(self.manager,goal,item)['remaining'],3)
        for n,state in enumerate(('failed','cancelled','succeeded')):self.store.save('gpu',{'id':str(n),'task_id':task['id'],'status':state})
        self.assertEqual(submission_status(self.manager,goal,item)['remaining'],0)
        with self.assertRaises(PermissionError):check_submission(self.manager,goal,item)
        override={**item,'max_gpu_jobs_per_cycle':1,'execution_cycle':1}
        self.assertEqual(submission_status(self.manager,goal,override)['remaining'],1)
        self.assertEqual(submission_status(self.manager,{**goal,'gpu_jobs_per_cycle':8},override)['limit'],1)

    def test_legacy_untagged_gpu_counts_in_first_cycle_and_planner_rule(self):
        goal,item,task=self.fixture();task.pop('execution_cycle');self.store.save('task',task);self.store.save('gpu',{'task_id':task['id'],'status':'interrupted'})
        self.assertEqual(submission_status(self.manager,goal,item)['remaining'],2)
        self.assertIn('Never create or assign review-only',system(goal,'Planner'));self.assertIn('copy_gpu_artifact first',system(goal,'Builder'))

    def test_gpu_submit_failure_is_not_a_model_retry(self):
        goal,item,task=self.fixture();self.manager.save(goal,policy={**goal['policy'],'gpus':['T4'],'gpu_per_job':1})
        (self.folder/'render.py').write_text('pass',encoding='utf-8')
        task=self.store.update('task',task['id'],allow_write=True,status='running');self.manager.builders.cancels[task['id']]=threading.Event()
        args={'program':'blender','entry':'render.py','files':['render.py'],'gpu':'T4','timeout':600,'args':[],'packages':[],'volume':''}
        with patch.object(self.manager.gpu,'submit',side_effect=ConnectionResetError('offline submit')) as submit:
            with self.assertRaises(ConnectionResetError):self.manager.builders.execute(task['id'],'submit_gpu_job',args)
        submit.assert_called_once();self.manager.retry_sleep.assert_not_called();self.assertEqual(self.store.items('goal_model_attempt',None),[])
        self.assertEqual(len(self.manager.ledger.rows(goal['id'])),1)

    def test_unattended_real_control_continues_same_goal_with_fake_provider(self):
        goal,item,task=self.fixture(unattended=True);self.manager.save(goal,status='running');self.manager.shutdown();manager=self.restart()
        manager.resume_unattended();fixtures.until(lambda:self.store.get('goal',goal['id'])['status']=='done')
        self.assertEqual(len(self.store.items('goal',None)),1);self.assertEqual(self.manager.items(goal['id'])[0]['id'],item['id'])
        self.assertEqual(self.store.items('goal_restart',None)[0]['state'],'resumed')

    def test_error_usage_malformed_does_not_hide_retryable_upstream_status(self):
        goal,item,task=self.fixture();self.providers.complete=MagicMock(side_effect=[StatusError(503,{'usage':{'input_tokens':'invalid','output_tokens':1}}),fixtures.response('OK')])
        self.call(goal,task);self.assertEqual(self.providers.complete.call_count,2)
        self.assertTrue(next(row for row in self.manager.ledger.rows(goal['id']) if row['retry_index']==0)['usage_estimated'])


class WatchdogTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create

    def fixture(self):
        goal=self.create(start=False);self.manager.add_items(goal,[{'key':'x','title':'Bounded task','acceptance':['File exists.'],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0];self.manager.save(goal,status='running')
        self.now=time.time();self.native=MagicMock();self.native.send.return_value={'status':'ok'}
        self.notifications=Notifications(self.store,native=self.native,clock=lambda:self.now)
        self.notifications.configure({'channels':{'windows':False,'browser':False},'sound':False})
        self.watchdog=GoalWatchdog(self.manager,self.notifications,clock=lambda:self.now)
        return self.store.get('goal',goal['id']),item

    def test_running_inactive_ten_minutes_warns_once_without_goal_mutation(self):
        goal,item=self.fixture();before=self.store.get('goal',goal['id']);self.watchdog.tick();self.now+=601;self.watchdog.tick()
        self.assertEqual(self.store.get('goal',goal['id']),before);self.assertEqual(len(self.store.items('notification',None)),1)
        self.now+=60;self.watchdog.tick();self.assertEqual(len(self.store.items('notification',None)),1)
        self.assertEqual(self.store.items('notification',None)[0]['severity'],'warning');self.assertEqual(len(self.providers.calls),0)

    def test_active_model_or_builder_prevents_inactivity_and_recovery_resolves(self):
        goal,item=self.fixture();self.watchdog.tick();self.now+=601;self.watchdog.tick()
        self.manager.model_active[goal['id']]=1;self.now+=60;self.watchdog.tick();self.notifications.tick()
        self.assertTrue(self.store.items('notification',None)[0]['resolved'])
        self.assertFalse(any(row.get('active') for row in self.store.items('goal_watchdog',None)))

    def test_waiting_without_queue_warns_but_pending_decision_suppresses_it(self):
        goal,item=self.fixture();self.manager.save(goal,status='waiting_decision');before=self.store.get('goal',goal['id']);self.watchdog.tick()
        self.assertEqual(len(self.store.items('notification',None)),1);self.assertEqual(self.store.get('goal',goal['id']),before)
        self.manager.create_decision(goal,{'question':'Choose?','options':['A','B'],'recommendation':'A','reason':'Inspect.','reversible':False},item_id=item['id'])
        self.now+=60;self.watchdog.tick();self.notifications.tick()
        self.assertTrue(next(row for row in self.store.items('notification',None) if row['event']=='watchdog')['resolved'])

    def test_same_item_same_error_three_times_warns_without_raw_secret(self):
        goal,item=self.fixture();secret='synthetic-watchdog-secret-28649'
        task={'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'status':'failed',
            'events':[{'kind':'tool_result','name':'run_command','result':{'error':secret+' bad input'}}]*3}
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):
            self.store.save('task',task);self.watchdog.tick()
            rows=self.store.items('notification',None);self.assertEqual(len(rows),1);self.assertNotIn(secret,json.dumps(rows))
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'running')

    def test_minute_throttle_and_different_errors_do_not_trigger_three(self):
        goal,item=self.fixture();self.watchdog.tick();first=self.store.get('goal_watchdog_state',goal['id'])
        self.now+=59;self.watchdog.tick();self.assertEqual(self.store.get('goal_watchdog_state',goal['id']),first)
        self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'status':'failed',
            'events':[{'kind':'tool_result','name':'run_command','result':{'error':value}} for value in ('one','two','three')]})
        self.now+=1;self.watchdog.tick();self.assertEqual(self.store.items('notification',None),[])

    def test_three_model_failures_warn_even_when_goal_is_blocked(self):
        goal,item=self.fixture();self.manager.save(goal,status='blocked')
        for index in range(3):self.store.save('goal_model_attempt',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
            'state':'failed','created':self.now,'finished':self.now,'error':{'exception_type':'APIConnectionError','category':'provider'}})
        self.watchdog.tick();self.assertEqual(len(self.store.items('notification',None)),1)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'blocked')


if __name__=='__main__':unittest.main()
