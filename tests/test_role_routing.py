import copy,json,threading,unittest
from unittest.mock import patch
import test_goals as fixtures
from test_model_registry import registration
from workbench.goal_costs import GoalLimit
from workbench.goals import GoalManager
from workbench.gpu import GPUManager


def routes(builder='baseten',planner='baseten'):
    model=lambda p:'meta-models/muse-spark-open-1026' if p=='baseten' else 'muse-spark-1.3' if p=='meta' else 'test-model'
    return {'Planner':{'provider':planner,'model':model(planner),'reasoning_effort':'low'},
            'Builder':{'provider':builder,'model':model(builder),'reasoning_effort':'low'},
            'ImageAnalysis':{'provider':'meta','model':'muse-spark-1.3','reasoning_effort':'low'},'ReferenceSearch':None}


class RoutedFake(fixtures.FakeProviders):
    def complete(self,provider,messages,model,*args,**kwargs):
        fake='meta' if 'You are the Builder' in messages[0]['content'] else 'baseten'
        response=super().complete(fake,messages,model,*args,**kwargs)
        self.calls[-1]['provider']=provider
        return response


class RoleRoutingTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp;tearDown=fixtures.GoalTests.tearDown;create=fixtures.GoalTests.create;terminal=fixtures.GoalTests.terminal

    def test_baseten_builder_and_both_roles_baseten_complete_real_local_loop(self):
        self.manager.shutdown();self.providers=RoutedFake();self.manager=GoalManager(self.store,self.providers,GPUManager(self.store),self.memory)
        for planner in ('meta','baseten'):
            goal=self.create(role_routes=routes(planner=planner),auto_commit=False)
            final=self.terminal(goal['id']);self.assertEqual(final['status'],'done')
            tasks=[t for t in self.store.items('task',None) if t.get('goal_id')==goal['id']]
            self.assertEqual(tasks[0]['provider'],'baseten');self.assertEqual(tasks[0]['model'],'meta-models/muse-spark-open-1026')
            self.assertTrue(any(e['kind']=='tool_result' and e.get('name')=='run_command' and e['result']['exit_code']==0 for e in tasks[0]['events']))
            ledger=self.manager.ledger.rows(goal['id']);builder=[r for r in ledger if r['role']=='Builder'];self.assertTrue(builder)
            self.assertTrue(all(r['provider_id']=='baseten' and r['purpose']=='development' for r in builder))

    def test_registered_model_prices_and_missing_price_warning(self):
        self.manager.model_registry.save(registration(prices={'input':2,'cached_input':.5,'output':4}))
        goal=self.create(start=False,role_routes=routes(builder='test_provider'))
        self.providers.complete=lambda *a,**k:fixtures.response('done')
        self.manager.call(goal,'test_provider','development','Builder',[{'role':'user','content':'work'}])
        row=self.manager.ledger.rows(goal['id'])[0];self.assertEqual(row['provider'],'test_provider');self.assertEqual(row['model'],'test-model');self.assertAlmostEqual(row['estimated_usd'],(10*2+10*.5+5*4)/1e6)
        self.manager.model_registry.save(registration())
        self.manager.call(goal,'test_provider','development','Builder',[{'role':'user','content':'work'}])
        row=self.manager.ledger.rows(goal['id'])[0];self.assertTrue(row['price_estimated']);self.assertIn('conservative',' '.join(row['warnings']))

    def test_product_configuration_and_reserve_are_independent(self):
        goal=self.create(start=False,role_routes=routes(),budgets={'baseten':{'total':1,'reserve':.9}})
        with self.assertRaises(GoalLimit):self.manager.ledger.reserve(goal,'baseten','development',.11)
        product=self.manager.ledger.reserve(goal,'baseten','product',.2);self.manager.ledger.settle(product,actual_cost=.2)
        self.assertEqual(self.manager.muse_routes.config()['route'],'baseten')
        self.assertEqual(self.manager.effective_routes(goal)['Builder']['provider'],'baseten')

    def test_route_update_waits_for_chunk_and_dispatches_fresh_task(self):
        goal=self.create(start=False,role_routes=routes(builder='meta'));self.manager.add_items(goal,[{'key':'one','title':'one','acceptance':['File exists'],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0];old=self.store.save('task',{'provider':'meta','model':'muse-spark-1.3','goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'status':'paused','events':[]})
        self.store.update('plan_item',item['id'],task_id=old['id'],task_ids=[old['id']],status='in_progress',bundles=1);self.store.update('goal',goal['id'],status='running')
        self.manager.builders.active.add(old['id'])
        try:
            self.manager.update(goal['id'],{'role_routes':routes(builder='baseten')})
            self.assertEqual(self.store.get('goal',goal['id'])['role_routes']['Builder']['provider'],'meta')
            self.assertFalse(self.manager.apply_pending_settings(goal['id']))
        finally:self.manager.builders.active.remove(old['id'])
        self.assertTrue(self.manager.apply_pending_settings(goal['id']))
        def start(*a,**k):return self.store.save('task',{'provider':k['provider'],'model':k['model'],'status':'queued',**k['context']})
        with patch.object(self.manager.builders,'start',side_effect=start) as starter:self.manager.assign(self.store.get('goal',goal['id']),self.store.get('plan_item',item['id']),True)
        self.assertIsNone(starter.call_args.kwargs['task_id']);self.assertEqual(starter.call_args.kwargs['provider'],'baseten')

    def test_item_override_validated_and_pending_without_counter_reset(self):
        goal=self.create(start=False,role_routes=routes(builder='meta'));self.manager.add_items(goal,[{'key':'one','title':'one','acceptance':['File exists'],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0];task=self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'status':'running'});self.store.update('plan_item',item['id'],task_id=task['id'],bundles=2)
        self.manager.builders.active.add(task['id'])
        try:self.manager.update_item_route(goal['id'],{'plan_item_id':item['id'],'builder_route':routes()['Builder']});self.assertNotIn('builder_route',self.store.get('plan_item',item['id']))
        finally:self.manager.builders.active.remove(task['id'])
        self.manager.apply_item_routes(goal['id']);current=self.store.get('plan_item',item['id']);self.assertEqual(current['bundles'],2);self.assertEqual(self.manager.model_route(goal,'Builder',current)['provider'],'baseten')
        with self.assertRaises(ValueError):self.manager.update_item_route(goal['id'],{'plan_item_id':item['id'],'builder_route':{'provider':'unknown','model':'x'}})

    def test_registered_provider_errors_use_existing_bounded_retry_and_ledger(self):
        self.manager.model_registry.save(registration());goal=self.create(start=False,role_routes=routes(builder='test_provider'));calls=[]
        def operation(*args,**kw):
            calls.append(args[0])
            if len(calls)<3:
                error=RuntimeError('fake upstream');error.status_code=503;raise error
            return fixtures.response('done')
        self.manager.retry_sleep=lambda _:None;self.providers.complete=operation
        self.manager.call(goal,'test_provider','development','Builder',[{'role':'user','content':'work'}])
        self.assertEqual(calls,['test_provider']*3);self.assertEqual(len(self.manager.ledger.rows(goal['id'])),3)

    def test_auxiliary_roles_use_their_own_provider_and_ledger(self):
        from test_goal_references import search_response,PNG
        from unittest.mock import MagicMock
        self.manager.model_registry.save(registration(style='responses',prices={'input':1,'output':2,'web_search':.01}))
        value=routes(builder='meta');value['ImageAnalysis']=value['ReferenceSearch']={'provider':'test_provider','model':'test-model','reasoning_effort':'low'}
        goal=self.create(start=False,role_routes=value)
        self.manager.add_items(goal,[{'key':'one','title':'Reference check','acceptance':['Record observations.'],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0];task=self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'workspace':str(self.folder),'status':'succeeded','events':[]})
        self.providers.goal_search=MagicMock(return_value=search_response());self.providers.complete=MagicMock(return_value=fixtures.response('Two side rails.'))
        self.manager.reference_tool(task,'reference_search',{'query':'conveyor drawing'})
        (self.folder/'render.png').write_bytes(PNG)
        self.manager.reference_tool(task,'analyze_images',{'paths':['render.png'],'question':'Describe the layout.'})
        self.assertEqual(self.providers.goal_search.call_args.args[0],'test_provider');self.assertEqual(self.providers.complete.call_args.args[0],'test_provider')
        rows=self.manager.ledger.rows(goal['id']);self.assertEqual({r['role'] for r in rows},{'ReferenceSearch','ImageAnalysis'})
        self.assertTrue(all(r['provider_id']=='test_provider' and r['provider']=='test_provider' for r in rows))

    def test_loaded_legacy_goal_routes_are_anchored_without_rewriting_history(self):
        goal=self.create(start=False);before=copy.deepcopy(self.store.get('goal',goal['id']))
        self.manager.bind_legacy_routes();self.assertEqual(before,self.store.get('goal',goal['id']))
        with patch.object(self.manager.muse_routes,'target',return_value={'provider':'modal_llm','model':'future-endpoint'}):
            anchored=self.manager.effective_routes(goal)
        self.assertEqual(anchored['Planner']['provider'],'baseten');self.assertEqual(anchored['Builder']['provider'],'meta')

    def test_execution_feedback_names_the_criterion_and_available_execution_ids(self):
        goal=self.create(start=False,role_routes=routes());text='smoke.py exists and produces the expected output'
        self.manager.add_items(goal,[{'key':'one','title':'Smoke','acceptance':[{'text':text,'kind':'execution'}],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0];valid={'task:4':{'kind':'read_file'},'task:8':{'kind':'run_command'}}
        verdict={'decision':'accept','defects':[],'criteria':[{'criterion':text,'evidence_ids':['task:4']}],'next_items':[]}
        with patch.object(self.manager,'valid_evidence',return_value=valid),patch.object(self.manager,'observed_evidence',return_value=valid):
            with self.assertRaises(ValueError) as caught:self.manager.validate_review(goal,item,verdict)
        message=str(caught.exception);self.assertIn(text,message);self.assertIn('task:4',message);self.assertIn('task:8',message)
        self.assertIn('do not rerun',message)

if __name__=='__main__':unittest.main()
