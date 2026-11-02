"""Local fake-time attention events; no real Windows, browser, model or GPU delivery."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace as NS

from workbench.goal_costs import Ledger
from workbench.notifications import Notifications, DEFAULTS
from workbench.security import KEY_NAMES, register_secret
from workbench.store import Store
from workbench.windows_alerts import WindowsAlerts
from test_goals import GoalHTTPTests, until


class FakeNative:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def send(self,payload,**options):
        self.calls.append({'payload':payload,**options})
        if self.fail:raise RuntimeError('Synthetic Windows failure')
        return {'status':'submitted','toast_submitted':options['toast'],'sound_requested':options['sound']}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'state')
        self.now=1000;self.native=FakeNative();self.service=Notifications(self.store,native=self.native,clock=lambda:self.now)
        self.store.on_save=self.service.observe
        self.goal=self.store.save('goal',{'id':'goal-test','title':'Sample Goal','status':'running','generation':1,'reason':'','timezone':'America/New_York',
            'budgets':{'meta':{'total':10,'daily':None},'baseten':{'total':100,'daily':None,'reserve':45},'modal':{'total':100,'daily':None,'llm':30}}})
        self.service.tick()
    def tearDown(self):
        self.service.stop();self.store.on_save=None;self.store.db.close();self.temp.cleanup()
    def message(self,key='message',**values):
        return self.store.save('message',{'id':key,'goal_id':self.goal['id'],'plan_item_id':None,'from':'Planner','to':'Human','type':'ESCALATE','body':'Decision needed','created':self.now,'evidence_refs':[],**values})
    def rows(self):return self.store.items('notification',None)
    def test_goal_approval_types_coalesce_correlated_escalation_and_resolve_after_decision(self):
        for kind in ('command','gpu','question'):
            self.store.save('goal_approval',{'id':kind,'goal_id':self.goal['id'],'task_id':'builder','plan_item_id':'item','kind':kind,'state':'pending'})
            self.message('escalate-'+kind,evidence_refs=[kind])
        self.service.tick();self.assertEqual(len(self.rows()),3);self.assertEqual(len(self.service.inbox()['urgent']),3)
        for row in self.rows():
            self.service.read(row['id'])
            with self.assertRaises(ValueError):self.service.resolve(row['id'])
        self.assertEqual(len(self.service.inbox()['urgent']),3)
        for key in ('command','gpu','question'):self.store.update('goal_approval',key,state='approved')
        self.service.tick();self.assertEqual(self.service.inbox()['urgent'],[])
        self.assertEqual(self.service.inbox()['unread_count'],0)
    def test_meta_work_approval_once_and_actual_processing_clears_banner(self):
        task={'id':'work','title':'Meta task','status':'waiting_approval','approval':{'id':'approval-1','kind':'command','payload':{'argv':['secret command']}}}
        self.store.save('task',task);self.service.tick();self.store.save('task',task);self.service.tick()
        self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['task_id'],'work')
        self.store.update('task','work',status='running',approval=None);self.service.tick()
        self.assertEqual(self.service.inbox()['urgent'],[])
    def test_escalation_is_once_even_if_same_message_is_saved_again(self):
        self.message();self.service.tick();self.message();self.service.tick()
        self.assertEqual(len(self.rows()),1);self.assertEqual(len(self.native.calls),1)
        self.assertEqual(self.rows()[0]['event'],'escalate')
    def test_coordinator_state_and_message_form_one_urgent_event(self):
        self.store.update('goal',self.goal['id'],status='blocked',platform_error={'id':'diagnostic','file':'C:\\private\\code.py'})
        self.message(body='Coordinator platform exception: AttributeError at module.function:22.')
        self.service.tick()
        self.assertEqual([r['event'] for r in self.rows()],['coordinator_error'])
        self.assertNotIn('private',json.dumps(self.rows()))
    def test_human_checkpoint_from_accepted_item_and_message_is_one_event(self):
        self.store.save('plan_item',{'id':'checkpoint','goal_id':self.goal['id'],'title':'M0: Human checkpoint','status':'done'})
        self.message(plan_item_id='checkpoint',body='Human checkpoint reached.')
        self.service.tick();self.assertEqual([r['event'] for r in self.rows() if r['severity']=='urgent'],['human_checkpoint'])
        checkpoint=next(r for r in self.rows() if r['event']=='human_checkpoint')
        self.service.resolve(checkpoint['id']);self.assertEqual(self.service.inbox()['urgent'],[])
        self.assertEqual(self.store.get('goal',self.goal['id'])['status'],'running')
    def test_completed_goal_does_not_silently_resolve_an_unreviewed_human_checkpoint(self):
        self.message(body='Human checkpoint reached.');self.service.tick()
        self.store.update('goal',self.goal['id'],status='done');self.service.tick()
        self.assertEqual([r['event'] for r in self.service.inbox()['urgent']],['human_checkpoint'])
    def test_budget_deadline_and_no_progress_pauses_are_warning_and_silent(self):
        for reason,progress,event in [('meta budget exceeded',0,'budget_pause'),('Deadline reached (UTC).',0,'deadline_pause'),('Three consecutive cycles without new observed evidence.',3,'no_progress_pause')]:
            self.store.update('goal',self.goal['id'],status='running');self.service.tick()
            self.store.update('goal',self.goal['id'],status='paused',reason=reason,no_progress=progress);self.service.tick()
            row=next(r for r in self.rows() if r['event']==event)
            self.assertEqual(row['severity'],'warning')
        self.assertEqual(len(self.rows()),3);self.assertTrue(all(not c['sound'] for c in self.native.calls))
    def test_user_pause_does_not_create_a_false_budget_or_failure_alert(self):
        self.store.update('goal',self.goal['id'],status='paused',reason='Paused by user.');self.service.tick()
        self.assertEqual(self.rows(),[])
    def test_item_blocked_warning_resolves_when_item_can_progress(self):
        self.store.save('plan_item',{'id':'blocked','goal_id':self.goal['id'],'title':'Inspect','status':'blocked'})
        self.service.tick();self.assertEqual(self.rows()[0]['event'],'item_blocked')
        self.store.update('plan_item','blocked',status='rework');self.service.tick()
        self.assertTrue(self.rows()[0]['resolved'])
    def test_stopped_goal_resolves_existing_blocked_alert_and_never_replays_old_items(self):
        self.store.save('plan_item',{'id':'old-block','goal_id':self.goal['id'],'title':'Old item','status':'blocked'})
        self.service.tick();self.assertEqual(len(self.rows()),1)
        self.store.update('goal',self.goal['id'],status='stopped');self.service.tick()
        self.assertTrue(self.rows()[0]['resolved']);self.assertEqual(self.service.inbox()['unread_count'],0)
        self.service=Notifications(self.store,native=self.native,clock=lambda:self.now);self.store.on_save=self.service.observe
        self.store.save('plan_item',{'id':'another-old-block','goal_id':self.goal['id'],'title':'Another item','status':'blocked'})
        self.store.save('plan_item',{'id':'old-milestone','goal_id':self.goal['id'],'title':'M9: Old milestone','status':'done'})
        self.store.update('goal',self.goal['id'],title='Stopped title metadata')
        self.service.tick();self.assertEqual(len(self.rows()),1)
    def test_done_is_information_app_only_and_milestone_requires_the_whole_group(self):
        for key in ('one','two'):self.store.save('plan_item',{'id':key,'goal_id':self.goal['id'],'title':'M0: '+key,'status':'todo'})
        self.store.update('plan_item','one',status='done');self.service.tick();self.assertEqual(self.rows(),[])
        self.store.update('plan_item','two',status='done');self.service.tick()
        self.store.update('goal',self.goal['id'],status='done');self.service.tick()
        self.assertCountEqual([r['event'] for r in self.rows()],['milestone_done','goal_done'])
        self.assertEqual(self.native.calls,[]);self.assertEqual(self.service.inbox()['browser'],[])
        self.assertEqual(self.service.inbox()['unread_count'],2)
    def test_explicit_milestone_event_is_one_information_event(self):
        self.message('milestone',type='NOTE',event='milestone_complete');self.service.tick()
        self.message('milestone',type='NOTE',event='milestone_complete');self.service.tick()
        self.assertEqual([r['event'] for r in self.rows()],['milestone_done'])
    def test_tree_milestone_waits_for_parent_and_all_descendants(self):
        for key,parent in [('root',None),('child','root'),('leaf','child')]:
            self.store.save('plan_item',{'id':key,'parent':parent,'goal_id':self.goal['id'],'title':key,'status':'todo'})
        self.store.update('plan_item','root',status='done');self.store.update('plan_item','child',status='done');self.service.tick()
        self.assertEqual(self.rows(),[])
        self.store.update('plan_item','leaf',status='done');self.service.tick()
        self.assertTrue(all(r['event']=='milestone_done' for r in self.rows()))
        self.assertTrue(self.rows())
    def test_budget_eighty_percent_is_once_and_has_no_raw_ledger_payload(self):
        self.service.ledger=Ledger(self.store)
        row={'id':'usage','goal_id':self.goal['id'],'provider':'meta','purpose':'development','state':'settled','estimated_usd':8,'created':self.now}
        self.store.save('ledger',row);self.service.tick();self.store.save('ledger',row);self.service.tick()
        self.assertEqual([r['event'] for r in self.rows()],['budget_80'])
    def test_reading_does_not_resolve_and_reminders_stop_only_when_handled(self):
        self.message();self.service.tick();row=self.rows()[0]
        self.now+=899;self.service.tick();self.assertEqual(len(self.native.calls),1)
        self.service.read(row['id']);self.assertEqual(self.service.inbox()['unread_count'],0)
        self.assertEqual(len(self.service.inbox()['urgent']),1)
        self.now+=1;self.service.tick();self.assertEqual(len(self.native.calls),2)
        self.service.resolve(row['id']);self.now+=900;self.service.tick()
        self.assertEqual(len(self.native.calls),2);self.assertEqual(self.service.inbox()['urgent'],[])
    def test_repeat_can_be_changed_or_disabled(self):
        self.service.configure({'repeat_minutes':1});self.message();self.service.tick()
        self.now+=60;self.service.tick();self.assertEqual(len(self.native.calls),2)
        self.service.configure({'repeat_minutes':0});self.now+=99999;self.service.tick();self.assertEqual(len(self.native.calls),2)
    def test_restart_and_title_metadata_change_do_not_reemit_a_handled_block(self):
        self.store.update('goal',self.goal['id'],status='blocked');self.service.tick()
        row=self.rows()[0];self.service.resolve(row['id'])
        self.store.update('goal',self.goal['id'],title='New metadata title');self.service.tick()
        self.service=Notifications(self.store,native=self.native,clock=lambda:self.now);self.store.on_save=self.service.observe;self.service.tick()
        self.assertEqual(len(self.rows()),1);self.assertTrue(self.rows()[0]['resolved'])
    def test_registered_keys_code_logs_and_full_paths_never_enter_notification_payload(self):
        secrets={name:'synthetic-credential-'+str(i)+'-000191' for i,name in enumerate(KEY_NAMES)}
        ephemeral='synthetic-ephemeral-"quoted"-\\token-000191';register_secret(ephemeral)
        with patch.dict(os.environ,secrets):
            title='Goal '+secrets['MODEL_API_KEY']+' '+ephemeral+' C:\\private\\full\\workspace \\n `print(123)`'
            self.store.update('goal',self.goal['id'],title=title)
            self.message(body='Full log '+secrets['BASETEN_API_KEY']+' /private/log/output.txt');self.service.tick()
            serialized=json.dumps([self.rows(),self.native.calls,self.service.inbox()],ensure_ascii=False)
            for secret in [*secrets.values(),ephemeral]:self.assertNotIn(secret,serialized)
            self.assertNotIn('C:\\private',serialized);self.assertNotIn('print(123)',serialized)
            self.assertNotIn('Full log',serialized)
    def test_windows_failure_keeps_inbox_and_cannot_modify_goal_progress(self):
        self.native.fail=True;before=self.store.get('goal',self.goal['id'])
        self.message();self.service.tick()
        self.assertEqual(self.store.get('goal',self.goal['id']),before)
        self.assertEqual(self.service.inbox()['unread_count'],1)
        self.assertEqual(self.rows()[0]['native']['status'],'failed')
        self.store.update('goal',self.goal['id'],status='done');self.service.tick()
        self.assertEqual(self.store.get('goal',self.goal['id'])['status'],'done')
    def test_sound_only_for_urgent_and_can_be_disabled(self):
        self.service.configure({'sound':False});self.message();self.service.tick()
        self.assertFalse(self.native.calls[0]['sound'])
        self.service.configure({'sound':True});self.message('second');self.service.tick()
        self.assertTrue(self.native.calls[-1]['sound'])
        self.store.update('goal',self.goal['id'],status='paused',reason='Budget cap');self.service.tick()
        self.assertFalse(self.native.calls[-1]['sound'])
    def test_type_channel_and_route_switches_are_persistent(self):
        self.service.configure({'channels':{'windows':False,'app':False},'sound':False,'routes':{'urgent':{'browser':True}}})
        self.message();self.service.tick();inbox=self.service.inbox()
        self.assertEqual(inbox['items'],[]);self.assertEqual(inbox['unread_count'],0)
        self.assertEqual(len(inbox['browser']),1);self.assertEqual(self.native.calls,[])
        other=Notifications(self.store,native=self.native);self.assertFalse(other.config()['channels']['app'])
        for data in ({'repeat_minutes':-1},{'repeat_minutes':True},{'repeat_minutes':1.2},{'channels':{'email':True}},{'enabled':{'urgent':'true'}}):
            with self.assertRaises(ValueError):self.service.configure(data)
    def test_multiple_browser_tabs_can_claim_each_delivery_only_once(self):
        self.message();self.service.tick();row=self.rows()[0];results=[]
        def claim():results.append(self.service.claim(row['id'],row['delivery_seq'],'browser')['claimed'])
        workers=[threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertCountEqual(results,[True,False])
        self.now+=900;self.service.tick()
        self.assertTrue(self.service.claim(row['id'],2,'browser')['claimed'])
    def test_server_delivers_without_any_browser_poll(self):
        self.service.start('http://127.0.0.1:8765');self.message()
        until(lambda:len(self.native.calls)==1)
        self.assertEqual(self.native.calls[0]['payload']['url'].split('?')[0],'http://127.0.0.1:8765/')
    def test_windows_bridge_uses_stdin_and_strips_keys_from_child_environment(self):
        token='synthetic-native-key-000191'
        result=NS(returncode=0,stdout=b'{"toast_submitted":true,"sound_requested":false,"status":"submitted"}')
        with patch.dict(os.environ,{'MODEL_API_KEY':token}),patch('workbench.windows_alerts.subprocess.run',return_value=result) as run:
            output=WindowsAlerts().send({'title':'Safe','body':'Summary','url':'http://127.0.0.1:8765/','tag':'offline'})
        self.assertTrue(output['toast_submitted']);self.assertNotIn('MODEL_API_KEY',run.call_args.kwargs['env'])
        self.assertNotIn(token,run.call_args.kwargs['input'].decode());self.assertNotIn('Safe',' '.join(run.call_args.args[0]))


class NotificationHTTPTests(GoalHTTPTests):
    def test_native_failure_cannot_interrupt_a_real_fake_provider_goal_flow(self):
        self.app.notifications.native=FakeNative(fail=True)
        self.app.goals.launch(self.goal['id'])
        request=until(lambda:(rows[0] if (rows:=self.app.goals.approvals(self.goal['id'])) else None))
        self.app.notifications.tick()
        self.assertEqual(len(self.app.notifications.inbox()['urgent']),1)
        self.assertTrue(self.app.notifications.native.calls)
        self.assertNotIn(self.app.store.get('goal',self.goal['id'])['status'],('blocked','done','paused'))
        self.app.goals.decide(self.goal['id'],request['id'],True)
        until(lambda:self.app.store.get('goal',self.goal['id'])['status']=='done')
        self.app.notifications.tick();self.assertEqual(self.app.notifications.inbox()['urgent'],[])
        self.assertFalse(any('platform exception' in m['body'] for m in self.app.goals.messages(self.goal['id'])))
    def test_notification_mutations_require_session_token(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        for path in ('settings','read','resolve','claim'):
            request=Request(self.url+'/api/notifications/'+path,data=b'{}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,403);caught.exception.close()
    def test_notification_api_has_redacted_inbox_and_persistent_read_state(self):
        from urllib.request import Request,urlopen
        self.app.notifications.native=FakeNative()
        self.app.goals.message(self.goal,'Planner','Human','ESCALATE','Original logs must not appear.')
        with urlopen(self.url+'/api/notifications') as response:data=json.load(response)
        self.assertEqual(data['unread_count'],1);self.assertNotIn('Original logs',json.dumps(data))
        row=data['items'][0]
        with urlopen(Request(self.url+'/api/notifications/read',data=json.dumps({'id':row['id']}).encode(),headers={'Content-Type':'application/json','X-Local-Token':self.app.token})) as response:self.assertTrue(json.load(response)['ok'])
        with urlopen(self.url+'/api/notifications') as response:self.assertEqual(json.load(response)['unread_count'],0)


for name in dir(GoalHTTPTests):
    if name.startswith('test_') and name not in NotificationHTTPTests.__dict__:setattr(NotificationHTTPTests,name,None)
del GoalHTTPTests
if __name__=='__main__':unittest.main()
