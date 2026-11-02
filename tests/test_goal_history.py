"""Offline Goal archive/delete; financial accounting and source files survive."""
import copy
import json
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from test_goals import GoalHTTPTests
from workbench.goal_costs import GoalLimit
from workbench.goal_history import OWNED_KINDS
from workbench.memory import uid
from workbench.notifications import Notifications


class GoalHistoryTests(GoalHTTPTests):
    def manage(self,action,**extra):
        return self.app.goal_history.manage({'goal_id':self.goal['id'],'action':action,**extra})

    def inactive(self,status='paused'):
        return self.app.store.update('goal',self.goal['id'],status=status)

    def delete(self,exclude=False):
        self.manage('archive')
        return self.manage('delete',confirmation_title=self.goal['title'],confirmed=True,exclude_memory=exclude)

    def record_sources(self):
        gid=self.goal['id']
        task=self.app.store.save('task',{'id':'goal-history-task','goal_id':gid,'provider':'meta','title':'Retained Builder source',
            'workspace':str(self.folder),'status':'succeeded','share_memory':True,'events':[{'kind':'assistant','text':'GoalHistoryUniqueResult evidence.', 'message_status':'complete'}]})
        gpu=self.app.store.save('gpu',{'id':'goal-history-gpu','task_id':task['id'],'workspace':str(self.folder),'status':'succeeded','share_memory':True,
            'spec':{'program':'python','entry':'gpu.py','gpu':'T4'},'result':{'volume':'retained-fixture-volume','retained_outputs':[{'path':'output.txt','size':1}]}})
        self.app.memory.add_note('Goal decision','GoalHistoryUniqueNote retained source.',[gid,'Planner'])
        self.app.memory.add_note('Unrelated decision','GoalHistoryOtherNote remains shared.',['another-goal','Planner'])
        self.app.memory.pending.join()
        return task,gpu

    def test_archive_and_restore_preserve_status_and_records_for_all_allowed_states(self):
        self.app.goals.add_items(self.goal,[{'key':'P1','title':'Preserve plan','acceptance':['Observed evidence']}])
        self.app.store.save('message',{'goal_id':self.goal['id'],'body':'Retain history','type':'NOTE','from':'Planner','to':'Builder','created':1})
        preserved={kind:self.app.store.items(kind,None) for kind in ('plan_item','message','decision','ledger')}
        for status in ('paused','stopped','done'):
            before=self.inactive(status)
            self.manage('archive');archived=self.app.store.get('goal',self.goal['id'])
            self.assertTrue(archived['archived']);self.assertEqual(archived['status'],status)
            ignored={'updated','archived','archived_at'}
            self.assertEqual({k:v for k,v in before.items() if k not in ignored},{k:v for k,v in archived.items() if k not in ignored})
            for kind,rows in preserved.items():self.assertEqual(self.app.store.items(kind,None),rows)
            self.manage('restore');restored=self.app.store.get('goal',self.goal['id'])
            self.assertFalse(restored['archived']);self.assertEqual(restored['status'],status)

    def test_running_and_waiting_goals_cannot_archive_or_delete_even_with_forged_archive_flag(self):
        for status in ('planning','running','waiting_approval','waiting_decision','blocked','interrupted'):
            for archived in (False,True):
                self.app.store.update('goal',self.goal['id'],status=status,archived=archived)
                with self.subTest(status=status,archived=archived):
                    with self.assertRaises(ValueError):self.manage('archive')
                    with self.assertRaises(ValueError):self.manage('delete',confirmed=True,confirmation_title=self.goal['title'])
        self.assertIsNotNone(self.app.store.get('goal',self.goal['id']))

    def test_paused_goal_with_live_builder_gpu_or_reserved_call_cannot_archive(self):
        self.inactive();task,gpu=self.record_sources()
        self.app.goals.builders.active.add(task['id'])
        with self.assertRaisesRegex(ValueError,'종료 중'):self.manage('archive')
        self.app.goals.builders.active.remove(task['id']);self.app.gpu.active.add(gpu['id'])
        with self.assertRaisesRegex(ValueError,'종료 중'):self.manage('archive')
        self.app.gpu.active.remove(gpu['id'])
        row=self.app.goals.ledger.reserve(self.goal,'baseten','product',.01)
        with self.assertRaisesRegex(ValueError,'종료 중'):self.manage('archive')
        self.app.goals.ledger.settle(row,actual_cost=.01);self.manage('archive')

    def test_only_archived_goal_with_exact_title_and_confirmation_can_be_deleted(self):
        self.inactive()
        with self.assertRaisesRegex(ValueError,'아카이브'):self.manage('delete',confirmation_title=self.goal['title'],confirmed=True)
        self.manage('archive')
        for title,confirmed in ((None,True),('',True),('wrong',True),(self.goal['title']+' ',True),(self.goal['title'],False)):
            with self.subTest(title_type=type(title).__name__),self.assertRaisesRegex(ValueError,'제목'):
                self.manage('delete',confirmation_title=title,confirmed=confirmed)
        self.manage('delete',confirmation_title=self.goal['title'],confirmed=True)
        with self.assertRaises(ValueError):self.app.store.get('goal',self.goal['id'])

    def test_delete_removes_owned_records_and_notifications_but_retains_unrelated_goal(self):
        self.inactive();gid=self.goal['id']
        for kind in OWNED_KINDS:self.app.store.save(kind,{'goal_id':gid,'created':1,'state':'answered','status':'done'})
        self.app.store.save('notification',{'goal_id':gid,'title':'Remove this alert'})
        self.app.store.save('notification_state',{'id':'goal:'+gid,'status':'paused'})
        other=self.app.goals.create({'title':'Other goal','objective':'Preserve unrelated goal','workspace':str(self.folder)},start=False)['goal']
        self.app.store.save('message',{'goal_id':other['id'],'body':'Unrelated source','created':1})
        self.delete()
        for kind in OWNED_KINDS+('notification',):self.assertFalse([r for r in self.app.store.items(kind,None) if r.get('goal_id')==gid])
        self.assertEqual(self.app.store.get('goal',other['id']),other)
        self.assertTrue([r for r in self.app.store.items('message',None) if r.get('goal_id')==other['id']])
        with self.assertRaises(ValueError):self.app.store.get('notification_state','goal:'+gid)

    def test_deleted_goal_spend_still_enforces_platform_daily_and_product_reserve_limits(self):
        self.inactive();ledger=self.app.goals.ledger
        ledger.prices=copy.deepcopy(ledger.prices);ledger.prices['limits']['baseten'].update(total=10,daily=7,reserve=4)
        for purpose,amount in (('development',5),('product',2)):
            row=ledger.reserve(self.goal,'baseten',purpose,amount);ledger.settle(row,actual_cost=amount)
        before=ledger.rows();self.delete();self.assertEqual(ledger.rows(),before)
        other=self.app.goals.create({'title':'New Goal','objective':'Must respect previous spend','workspace':str(self.folder)},start=False)['goal']
        with self.assertRaisesRegex(GoalLimit,'일일'):ledger.check_reservation(other,'baseten','development',.01)
        ledger.prices['limits']['baseten']['daily']=None
        with self.assertRaisesRegex(GoalLimit,'개발'):ledger.check_reservation(other,'baseten','development',2)
        with self.assertRaisesRegex(GoalLimit,'제품 예비분'):ledger.check_reservation(other,'baseten','product',3)
        self.assertEqual(sum(r['estimated_usd'] for r in ledger.rows()),7)

    def test_delete_preserves_files_git_task_gpu_and_shared_memory_by_default(self):
        self.inactive();task,gpu=self.record_sources();(self.folder/'.git').mkdir();sentinel=self.folder/'.git'/'HEAD';sentinel.write_text('ref: refs/heads/test\n')
        artifact=self.folder/'keep.txt';artifact.write_text('Do not remove workspace files.')
        sources=self.app.memory.vault.all();original_tasks=self.app.store.items('task',None);original_gpu=self.app.store.items('gpu',None)
        preview=self.app.goal_history.preview(self.goal['id']);self.assertEqual(preview['memory_records'],3);self.assertEqual(preview['gpu_results'],1)
        self.delete();self.assertEqual(self.app.memory.vault.all(),sources)
        self.assertEqual(self.app.store.items('task',None),original_tasks);self.assertEqual(self.app.store.items('gpu',None),original_gpu)
        self.assertEqual(sentinel.read_text(),'ref: refs/heads/test\n');self.assertEqual(artifact.read_text(),'Do not remove workspace files.')
        self.assertTrue(self.app.memory.search('GoalHistoryUniqueResult'))

    def test_optional_exclusion_retains_sources_and_search_excludes_only_target_goal(self):
        self.inactive();task,gpu=self.record_sources();before={s['id']:copy.deepcopy(s) for s in self.app.memory.vault.all()}
        result=self.delete(exclude=True);self.app.memory.pending.join();self.assertEqual(result['memory_excluded'],3)
        after={s['id']:s for s in self.app.memory.vault.all()};self.assertEqual(before.keys(),after.keys())
        for identifier,source in before.items():self.assertEqual(source['messages'],after[identifier]['messages'])
        self.assertFalse(self.app.memory.search('GoalHistoryUniqueResult'));self.assertFalse(self.app.memory.search('GoalHistoryUniqueNote'))
        self.assertTrue(self.app.memory.search('GoalHistoryOtherNote'))
        self.assertFalse(self.app.store.get('task',task['id'])['share_memory']);self.assertFalse(self.app.store.get('gpu',gpu['id'])['share_memory'])
        self.app.memory.sync('task',self.app.store.get('task',task['id']));self.assertFalse(self.app.memory.source(uid('task',task['id']))['shared'])

    def test_archive_blocks_resume_proxy_and_http_mutations_until_restore(self):
        self.inactive();self.manage('archive')
        with self.assertRaisesRegex(ValueError,'복원'):self.app.goals.control(self.goal['id'],'resume')
        with self.assertRaises(PermissionError):self.app.goals.proxy_goal(self.token)
        for route,data in (('title',{'title':'Changed'}),('settings',{'policy':{'mode':'autonomous'}}),('control',{'action':'resume'})):
            with self.assertRaisesRegex(ValueError,'복원'):self.app.handle('POST','/api/goals/'+route,{'goal_id':self.goal['id'],**data},{})
        self.manage('restore');self.assertEqual(self.app.store.get('goal',self.goal['id'])['status'],'paused')

    def test_stale_notification_events_cannot_recreate_deleted_goal_alerts(self):
        self.inactive();gid=self.goal['id']
        stale={'id':'stale-escalation','goal_id':gid,'type':'ESCALATE','body':'Old event','created':1}
        self.app.notifications.observe('message',stale);self.delete();self.app.notifications.tick()
        self.assertFalse([r for r in self.app.store.items('notification',None) if r.get('goal_id')==gid])
        self.assertIsNone(self.app.notifications.emit('escalate','stale-key',goal_id=gid))
        self.app.notifications.deliver_native('missing-deleted-alert',1,True,True)
        restarted=Notifications(self.app.store,self.app.goals.ledger);restarted.tick()
        self.assertFalse([r for r in self.app.store.items('notification',None) if r.get('goal_id')==gid])

    def test_archive_hides_goal_notifications_without_deleting_them(self):
        self.inactive();notice=self.app.notifications.emit('escalate','archive-notice',goal_id=self.goal['id'])
        self.assertTrue(self.app.notifications.inbox()['items']);self.manage('archive')
        self.assertFalse(self.app.notifications.inbox()['items']);self.assertEqual(self.app.store.get('notification',notice['id']),notice)
        self.manage('restore');self.assertTrue(self.app.notifications.inbox()['items'])

    def test_archived_flag_and_original_state_survive_manager_restart(self):
        from workbench.goals import GoalManager
        self.inactive();self.manage('archive')
        restarted=GoalManager(self.app.store,self.app.goals.providers,self.app.gpu,self.app.memory)
        goal=restarted.detail(self.goal['id'])['goal']
        self.assertTrue(goal['archived']);self.assertEqual(goal['status'],'paused')
        with self.assertRaisesRegex(ValueError,'복원'):restarted.control(goal['id'],'resume')
        self.manage('restore');self.assertFalse(restarted.detail(goal['id'])['goal']['archived'])

    def test_all_provider_ledgers_are_unchanged_after_delete(self):
        self.inactive();ledger=self.app.goals.ledger
        for provider in ('meta','modal','baseten'):
            row=ledger.reserve(self.goal,provider,'development',.2);ledger.settle(row,actual_cost=.2)
        before=copy.deepcopy(ledger.rows());self.delete();self.assertEqual(ledger.rows(),before)
        self.assertEqual({r['provider'] for r in ledger.rows()},{'meta','modal','baseten'})

    def test_history_http_requires_local_session_token_and_bad_title_error_is_visible(self):
        self.inactive()
        def request(payload,token=None):
            headers={'Content-Type':'application/json'}
            if token:headers['X-Local-Token']=token
            return Request(self.url+'/api/goals/history',data=json.dumps(payload).encode(),headers=headers)
        with self.assertRaises(HTTPError) as caught:urlopen(request({'goal_id':self.goal['id'],'action':'archive'}))
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        with urlopen(request({'goal_id':self.goal['id'],'action':'archive'},self.app.token)) as result:self.assertTrue(json.load(result)['ok'])
        with self.assertRaises(HTTPError) as caught:urlopen(request({'goal_id':self.goal['id'],'action':'delete','confirmed':True,'confirmation_title':'wrong'},self.app.token))
        self.assertEqual(caught.exception.code,400);self.assertIn('제목',json.load(caught.exception)['error']);caught.exception.close()
        with urlopen(self.url+'/api/goals/history?goal_id='+self.goal['id']) as result:self.assertTrue(json.load(result)['can_delete'])


for name in dir(GoalHTTPTests):
    if name.startswith('test_') and name not in GoalHistoryTests.__dict__:setattr(GoalHistoryTests,name,None)
del GoalHTTPTests,name
if __name__=='__main__':unittest.main()
