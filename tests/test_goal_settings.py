"""Atomic settings, inactive-state preservation and autonomous command boundaries."""
import copy
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from test_goals import GoalTests, GoalHTTPTests, until, response
from workbench.goals import GoalManager
from workbench.goal_policy import policy, command_decision, command_assessment


class GoalSettingsTests(GoalTests):
    def test_full_paused_ui_payload_no_duplicate_timezone_and_preserves_work(self):
        goal=self.create(start=False,policy={'mode':'manual'});self.manager.save(goal,status='paused')
        self.manager.add_items(goal,[{'key':'P2','title':'Product check','acceptance':['Observed response.']}])
        self.manager.message(goal,'Planner','Builder','NOTE','Keep the original message.')
        self.manager.ledger.settle(self.manager.ledger.reserve(goal,'baseten','product',.01),actual_cost=.01)
        before=self.manager.detail(goal['id']);goal=before['goal']
        data={key:copy.deepcopy(goal[key]) for key in ('builder_model','reasoning','output_language','timezone','budgets','policy')}
        data.update(goal_id=goal['id'],deadline=goal['deadline_local']);data['policy']['mode']='autonomous'
        after=self.manager.update(goal['id'],data)
        self.assertEqual(after['goal']['status'],'paused');self.assertEqual(after['goal']['policy']['mode'],'autonomous')
        for key in ('items','messages','ledger'):self.assertEqual(before[key],after[key])
        self.assertEqual(goal['objective'],after['goal']['objective'])
        self.assertEqual(len(after['settings_history']),1)
        self.assertEqual(after['settings_history'][0]['state'],'applied')
        self.assertEqual(self.providers.calls,[])

    def test_all_requested_states_allow_settings_and_keep_status(self):
        for status in ('paused','waiting_approval','interrupted'):
            goal=self.create(start=False);self.manager.save(goal,status=status)
            result=self.manager.update(goal['id'],{'policy':{'mode':'autonomous','commands':['python <workspace.py>'],'gpus':['T4'],'gpu_per_job':.5},
                'budgets':{'meta':{'total':42}},'deadline':time.time()+7200,'reasoning':{'Planner':'medium','Builder':'high'},'builder_model':'muse-spark-1.3-contributor'})
            saved=result['goal']
            self.assertEqual(saved['status'],status);self.assertEqual(saved['builder_model'],'muse-spark-1.3-contributor')
            self.assertEqual(saved['policy']['gpu_per_job'],.5);self.assertEqual(saved['budgets']['meta']['total'],42)
            self.assertEqual(saved['budgets']['baseten'],goal['budgets']['baseten'])
            self.assertEqual(saved['reasoning']['Planner'],'medium')

    def test_timezone_change_updates_local_stamp_without_changing_utc_deadline(self):
        goal=self.create(start=False);self.manager.save(goal,status='paused')
        saved=self.manager.update(goal['id'],{'timezone':'Asia/Seoul'})['goal']
        self.assertEqual(saved['timezone'],'Asia/Seoul');self.assertTrue(saved['time_local'].endswith('+09:00'))
        self.assertEqual(saved['deadline_epoch'],goal['deadline_epoch'])
        self.assertTrue(saved['deadline_local'].endswith('+09:00'))

    def test_validation_is_atomic_including_reasoning_defaults_and_audit(self):
        goal=self.create(start=False);before=self.store.get('settings','goal_defaults')
        for data in ({'reasoning':{'Planner':'low'},'budgets':{'baseten':{'total':1,'reserve':45}}},
                     {'policy':{'mode':'invalid'}},{'timezone':'not-a-timezone'},
                     {'objective':'replace original'},{'workspace':str(self.folder)}, {'builder_model':None}):
            with self.assertRaises(ValueError):self.manager.update(goal['id'],data)
            self.assertEqual(self.store.get('goal',goal['id']),goal)
            self.assertEqual(self.store.get('settings','goal_defaults'),before)
            self.assertEqual(self.manager.settings_history(goal['id']),[])

    def test_running_change_waits_for_builder_boundary_and_merges_newer_changes(self):
        goal=self.create(start=False);self.manager.save(goal,status='running')
        self.store.save('task',{'id':'busy','goal_id':goal['id'],'status':'running'})
        self.manager.builders.active.add('busy')
        try:
            self.manager.update(goal['id'],{'policy':{'mode':'manual'},'builder_model':'muse-spark-1.3-contributor'})
            self.manager.update(goal['id'],{'reasoning':{'Planner':'low','Builder':'medium'}})
            queued=self.store.get('goal',goal['id'])
            self.assertEqual(queued['builder_model'],goal['builder_model'])
            self.assertFalse(self.manager.apply_pending_settings(goal['id']))
            self.assertEqual([r['state'] for r in self.manager.settings_history(goal['id'])],['superseded','pending'])
        finally:self.manager.builders.active.discard('busy')
        self.assertTrue(self.manager.apply_pending_settings(goal['id']))
        saved=self.store.get('goal',goal['id'])
        self.assertEqual(saved['status'],'running');self.assertIsNone(saved['pending_settings'])
        self.assertEqual(saved['builder_model'],'muse-spark-1.3-contributor');self.assertEqual(saved['policy']['mode'],'manual')
        self.assertEqual(saved['reasoning']['Builder'],'medium')

    def test_pending_settings_survive_restart_and_apply_on_explicit_resume(self):
        goal=self.create(start=False);self.manager.save(goal,status='running')
        self.store.save('task',{'id':'busy','goal_id':goal['id'],'status':'running'});self.manager.builders.active.add('busy')
        try:self.manager.update(goal['id'],{'builder_model':'muse-spark-1.3-contributor'})
        finally:self.manager.builders.active.discard('busy')
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'interrupted')
        self.assertEqual(self.store.get('goal',goal['id'])['builder_model'],goal['builder_model'])
        with patch.object(self.manager,'launch') as launch:self.manager.control(goal['id'],'resume')
        launch.assert_called_once_with(goal['id'])
        self.assertEqual(self.store.get('goal',goal['id'])['builder_model'],'muse-spark-1.3-contributor')

    def test_switch_to_autonomous_reassesses_pending_requests_without_messages_or_spend(self):
        (self.folder/'script.py').write_text('print("OK")')
        goal=self.create(start=False,policy={'mode':'manual'});self.manager.save(goal,status='waiting_approval')
        requests=(('allowed',['python','script.py','review/out.md']),('outside',['python','-m','compileall']),('forbidden',['git','push']))
        slots={}
        for identifier,argv in requests:
            slots[identifier]={'event':threading.Event(),'approved':False};self.manager.builders.approvals[identifier]=slots[identifier]
            self.store.save('goal_approval',{'id':identifier,'goal_id':goal['id'],'kind':'command','state':'pending','payload':{'argv':argv,'cwd':'.'}})
        before=self.manager.detail(goal['id'])
        result=self.manager.update(goal['id'],{'policy':{'mode':'autonomous'}})
        self.assertTrue(slots['allowed']['event'].is_set());self.assertTrue(slots['allowed']['approved'])
        self.assertFalse(slots['outside']['event'].is_set());self.assertFalse(slots['forbidden']['event'].is_set())
        self.assertEqual(self.store.get('goal_approval','allowed')['state'],'approved')
        self.assertIn('does not match',self.store.get('goal_approval','outside')['auto_approval_reason'])
        self.assertIn('prohibited',self.store.get('goal_approval','forbidden')['auto_approval_reason'])
        for key in ('messages','items','ledger'):self.assertEqual(before[key],result[key])

    def test_real_manual_approval_worker_continues_after_policy_save(self):
        goal=self.create(policy={'mode':'manual'})
        request=until(lambda:next(iter(self.manager.approvals(goal['id'])),None))
        until(lambda:self.store.get('goal',goal['id'])['status']=='waiting_decision')
        self.assertIn('Manual',request['auto_approval_reason'])
        self.manager.update(goal['id'],{'policy':{'mode':'autonomous'}})
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
        self.assertTrue(self.store.get('goal_approval',request['id'])['automatic'])

    def test_paused_goal_never_wakes_workers_or_revives_cancelled_approvals(self):
        (self.folder/'script.py').write_text('print(1)')
        goal=self.create(start=False,policy={'mode':'manual'});self.manager.save(goal,status='paused')
        event=threading.Event();self.manager.builders.approvals['inactive']={'event':event,'approved':False}
        for identifier,state in (('inactive','pending'),('old','cancelled')):
            self.store.save('goal_approval',{'id':identifier,'goal_id':goal['id'],'kind':'command','state':state,
                'payload':{'argv':['python','script.py'],'cwd':'.'}})
        self.manager.update(goal['id'],{'policy':{'mode':'autonomous'}})
        self.assertFalse(event.is_set());self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')
        self.assertEqual(self.store.get('goal_approval','old')['state'],'cancelled')
        self.assertEqual(self.providers.calls,[])

    def test_pending_gpu_requests_are_reassessed_against_new_policy_and_daily_cap(self):
        goal=self.create(start=False,policy={'mode':'manual'},budgets={'modal':{'total':1,'daily':.001}})
        self.manager.save(goal,status='waiting_approval');slots={}
        for identifier,gpu,timeout in (('fits','T4',1),('budget','T4',600),('unlisted','L4',1)):
            slots[identifier]={'event':threading.Event(),'approved':False};self.manager.builders.approvals[identifier]=slots[identifier]
            self.store.save('goal_approval',{'id':identifier,'goal_id':goal['id'],'kind':'gpu','state':'pending',
                'payload':{'spec':{'gpu':gpu,'timeout':timeout}}})
        self.manager.update(goal['id'],{'policy':{'mode':'autonomous','gpus':['T4'],'gpu_per_job':1}})
        self.assertTrue(slots['fits']['event'].is_set())
        self.assertFalse(slots['budget']['event'].is_set());self.assertFalse(slots['unlisted']['event'].is_set())
        self.assertEqual(self.manager.ledger.rows(goal['id']),[])

    def test_gpu_reassessment_checks_allowlist_per_job_and_total_daily_without_spend(self):
        goal=self.create(start=False,policy={'mode':'autonomous','gpus':['T4'],'gpu_per_job':1},budgets={'modal':{'total':1,'daily':.001}})
        spec={'gpu':'T4','timeout':600}
        result=self.manager.approval_assessment(goal,'gpu',{'spec':spec})
        self.assertFalse(result['auto']);self.assertIn('budget',result['reason'])
        goal['budgets']['modal']['daily']=None
        self.assertTrue(self.manager.approval_assessment(goal,'gpu',{'spec':spec})['auto'])
        self.assertFalse(self.manager.approval_assessment(goal,'gpu',{'spec':{'gpu':'L4','timeout':1}})['auto'])
        goal['policy']['gpu_per_job']=.00001
        self.assertFalse(self.manager.approval_assessment(goal,'gpu',{'spec':spec})['auto'])
        self.assertEqual(self.manager.ledger.rows(goal['id']),[])

    def test_workspace_script_args_and_test_options_are_safe_and_auto_with_goal_proxy_env(self):
        (self.folder/'tools').mkdir();(self.folder/'review').mkdir()
        script=self.folder/'tools/proxy_check.py'
        script.write_text('import os\nassert os.environ.get("MUSE_PROXY_TOKEN")\nassert "MODEL_API_KEY" not in os.environ\nassert "BASETEN_API_KEY" not in os.environ\nprint("PROXY_ENV_OK")\n')
        self.providers.command=['python','tools/proxy_check.py','review/PROXY_EVIDENCE.md']
        goal=self.create()
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
        self.assertEqual(self.manager.approvals(goal['id']),[])
        task=self.store.get('task',self.manager.items(goal['id'])[0]['task_id'])
        self.assertIn('PROXY_ENV_OK',json.dumps(task['events']))
        for argv in (['python','tools/proxy_check.py','review/out.md','--dry-run','--output=review/a.json','hello world'],
                     ['python','-m','pytest','tests/test_one.py','-q','--junitxml=review/result.xml'],
                     ['python','-m','unittest','discover','-s','tests','-p','test_*.py']):
            self.assertTrue(command_decision(self.folder,self.store.root,argv,settings=policy({'mode':'autonomous'})))

    def test_unsafe_python_arguments_are_never_automatically_approved(self):
        script=self.folder/'script.py';script.write_text('print(1)')
        settings=policy({'mode':'autonomous'})
        for arg in ('a;b','x|y','a&b','>out','<in','`cmd`','$HOME','line\nbreak','a..b',str(self.folder/'out.md')):
            self.assertFalse(command_decision(self.folder,self.store.root,['python','script.py',arg],settings=settings),arg)
        with self.assertRaises((ValueError,PermissionError)):
            command_decision(self.folder,self.store.root,['python',str(script)],settings=settings)
        for argv in (['python','script.py','../outside'],['python','script.py',str(self.folder.parent/'out.md')],['git','push']):
            with self.assertRaises((ValueError,PermissionError)):command_decision(self.folder,self.store.root,argv,settings=settings)
        self.assertIn('not selected',command_assessment(self.folder,self.store.root,['python','script.py'],settings=policy({'mode':'autonomous','commands':[]}))['reason'])

    def test_next_builder_call_uses_saved_model_instead_of_stale_task_model(self):
        goal=self.create(start=False);self.manager.add_items(goal,[{'key':'one','title':'One','acceptance':['Observed file.']}])
        item=self.manager.items(goal['id'])[0]
        self.store.save('task',{'id':'model-check','goal_id':goal['id'],'generation':goal['generation'],'plan_item_id':item['id']})
        self.manager.update(goal['id'],{'builder_model':'muse-spark-1.3-contributor'})
        captured=[]
        def complete(*args,**kwargs):captured.append(args[2]);return response('OK')
        self.providers.complete=complete;self.manager.builders.providers.local.task_id='model-check'
        self.manager.builders.providers.completion('meta',[{'role':'system','content':''},{'role':'user','content':'go'}],[],model=goal['builder_model'])
        self.assertEqual(captured,['muse-spark-1.3-contributor'])


class GoalSettingsHTTPTests(GoalHTTPTests):
    def test_settings_auth_errors_and_saved_audit_are_returned(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        url=self.url+'/api/goals/settings'
        def request(data,token=True):return Request(url,data=json.dumps({'goal_id':self.goal['id'],**data}).encode(),headers={'Content-Type':'application/json',**({'X-Local-Token':self.app.token} if token else {})})
        with self.assertRaises(HTTPError) as caught:urlopen(request({'policy':{'mode':'autonomous'}},False))
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        with self.assertRaises(HTTPError) as caught:urlopen(request({'timezone':'invalid-zone'}))
        self.assertEqual(caught.exception.code,400);self.assertIn('timezone',json.load(caught.exception)['error']);caught.exception.close()
        self.app.goals.control(self.goal['id'],'pause')
        with urlopen(request({'policy':{'mode':'autonomous'},'timezone':'America/New_York'})) as result:body=json.load(result)
        self.assertEqual(body['goal']['status'],'paused');self.assertEqual(body['goal']['policy']['mode'],'autonomous')
        self.assertEqual(body['settings_history'][0]['state'],'applied')


for target,base in ((GoalSettingsTests,GoalTests),(GoalSettingsHTTPTests,GoalHTTPTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in target.__dict__:setattr(target,name,None)
del target,base,name,GoalTests,GoalHTTPTests
if __name__=='__main__':unittest.main()
