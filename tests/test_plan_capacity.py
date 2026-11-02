import json,unittest
from unittest.mock import patch
import test_builder_productivity as fixtures
from workbench.plan_capacity import plan_limit,PlanCapacityError
from workbench.plan_updates import prepare_plan,initial_items
from workbench.goal_prompts import system


class PlanCapacityTests(unittest.TestCase):
    setUp=fixtures.ProductivityTests.setUp
    tearDown=fixtures.ProductivityTests.tearDown
    create=fixtures.ProductivityTests.create
    fixture=fixtures.ProductivityTests.fixture
    def test_default_40_custom_limit_and_update_at_capacity(self):
        rows=[{'key':'s'+str(n),'title':'Item '+str(n),'acceptance':['Observable result']} for n in range(40)]
        self.assertEqual(len(initial_items(rows)),40)
        with self.assertRaises(PlanCapacityError):initial_items(rows+[{'key':'last','title':'Last','acceptance':['Result']}])
        goal,item,task=self.fixture(max_plan_items=1);self.assertEqual(goal['max_plan_items'],1);self.assertEqual(self.manager.defaults()['max_plan_items'],40)
        self.assertTrue(prepare_plan([item],[{'key':item['key'],'title':'Updated'}],max_plan_items=1)['updates'])
        with self.assertRaises(PlanCapacityError):prepare_plan([item],[{'key':'new','title':'New','acceptance':['Result']}],max_plan_items=1)
    def test_settings_validation_and_saving_do_not_change_plan_body(self):
        goal,item,_=self.fixture();before=self.manager.items(goal['id']);objective=goal['objective']
        for value in (0,201,True,'40',1.5):
            with self.assertRaises(ValueError):self.manager.update(goal['id'],{'max_plan_items':value})
        self.manager.update(goal['id'],{'max_plan_items':60});self.assertEqual(self.store.get('goal',goal['id'])['max_plan_items'],60);self.assertEqual(self.manager.items(goal['id']),before);self.assertEqual(self.store.get('goal',goal['id'])['objective'],objective)
    def test_stall_capacity_notifies_planner_once_without_call_or_human(self):
        goal,item,_=self.fixture(max_plan_items=1);item=self.store.update('plan_item',item['id'],stall_replan_pending=True)
        with patch.object(self.manager,'phase',side_effect=AssertionError('No paid call at known cap')):self.assertFalse(self.manager.shrink_stalled_item(goal,item))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['wait_reason'],'plan_capacity');self.assertEqual(current['reworks'],0);self.assertEqual(self.manager.pending_decisions(goal['id']),[])
        self.assertEqual(len(self.store.items('goal_plan_capacity',None)),1);self.assertTrue(any(m['to']=='Planner' and 'consolidation' in m['body'] for m in self.manager.messages(goal['id'])))
        self.manager.update(goal['id'],{'max_plan_items':3});current=self.store.get('plan_item',item['id']);self.assertEqual(current['wait_reason'],'stall_replan');self.assertTrue(current['stall_replan_pending'])
    def test_review_split_capacity_never_spends_rework_or_creates_decision(self):
        goal,item,_=self.fixture(max_plan_items=1);item=self.store.update('plan_item',item['id'],status='review',review_trigger='batch_limit')
        reply={'decision':'rework','defects':['Split the bounded implementation.'],'criteria':[],'summary':'Split','goal_complete':False,'batch_action':'split','next_items':[{'key':'child','title':'Child','acceptance':['Result']}]}
        with patch.object(self.manager,'phase',return_value=json.dumps(reply)):self.assertFalse(self.manager.review(goal,item))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['reworks'],0);self.assertEqual(current['wait_reason'],'plan_capacity');self.assertEqual(self.manager.pending_decisions(goal['id']),[])
        self.assertEqual(self.store.items('goal_review_attempt',None)[0]['state'],'capacity_wait')
    def test_plan_limit_and_results_dependencies_are_in_planner_prompt(self):
        goal,_,_=self.fixture(max_plan_items=60);text=system(goal,'Planner');self.assertIn('capacity is 60',text);self.assertIn('dependent on the actual execution/evaluation',text)
    def test_initial_plan_overflow_pauses_without_human_decision(self):
        goal=self.create(start=False,max_plan_items=1)
        value={'items':[{'key':k,'title':k,'acceptance':['Result']} for k in ('one','two')],'summary':'Oversized'}
        with patch.object(self.manager,'phase',return_value=json.dumps(value)) as calls:self.manager.plan(goal)
        self.assertEqual(calls.call_count,2);self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused');self.assertEqual(self.manager.pending_decisions(goal['id']),[])
