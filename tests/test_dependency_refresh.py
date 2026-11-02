"""Regression of human retry + malformed dependency JSON, entirely offline."""
import json
import unittest
from unittest.mock import patch
import test_goal_decisions as fixtures
from test_goals import response
from workbench.dependency_contract import dependency_instruction
from workbench.plan_updates import prepare_plan


class DependencyRefreshTests(unittest.TestCase):
    setUp=fixtures.GoalDecisionTests.setUp
    tearDown=fixtures.GoalDecisionTests.tearDown
    items=fixtures.GoalDecisionTests.items
    decision=fixtures.GoalDecisionTests.decision
    answer=fixtures.GoalDecisionTests.answer

    def refresh(self,value):
        goal=self.store.update('goal',self.goal['id'],decision_replan_pending=True)
        with patch.object(self.manager,'phase',return_value=json.dumps(value)) as calls:
            self.manager.refresh_dependencies(goal)
        return calls

    def test_empty_missing_and_null_updates_preserve_graph(self):
        self.items('P3')
        before=self.manager.items(self.goal['id'])
        for value in ({},{'items':[]},{'items':None}):
            calls=self.refresh(value);self.assertEqual(calls.call_count,1)
            self.assertEqual(self.manager.items(self.goal['id']),before)
        self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])

    def test_recorded_four_batch_p3_human_retry_keeps_history(self):
        item=self.items('P3')[0];self.store.update('plan_item',item['id'],title='P3 Component physics',status='blocked',bundles=4,attempts=4,evidence=[{'preserved':True}])
        d=self.decision(item)
        self.answer(d,answer='Do not restructure the plan; no dependency edits. Reopen P3 as a single rework. Complete existing physics modules and run pytest after each.')
        result=self.store.get('plan_item',item['id'])
        self.assertEqual(result['status'],'rework');self.assertEqual(result['bundles'],0)
        self.assertEqual(result['attempts'],4);self.assertEqual(result['historical_bundles'],4)
        self.assertEqual(result['evidence'],[{'preserved':True}]);self.assertEqual(result['depends_on'],item['depends_on'])
        self.assertIn('existing physics',result['instruction'])
        self.manager.apply_decision_action(self.goal,result,'rework',result['instruction'],d['id'])
        self.assertEqual(len(self.store.items('goal_item_action',None)),1)

    def test_invalid_graph_twice_applies_valid_action_without_new_decision(self):
        item=self.items('P3')[0];self.store.update('plan_item',item['id'],status='blocked',bundles=4,needs_decision_replan=True)
        d=self.decision(item);self.answer(d,answer='Retry this item and complete the existing files, with pytest.')
        original=self.store.get('plan_item',item['id'])
        value={'items':[{'key':'P3','depends_on':['does-not-exist']}],
            'actions':[{'key':'P3','resolution':'rework','instruction':'Finish existing modules and run pytest.'}]}
        calls=self.refresh(value);self.assertEqual(calls.call_count,2)
        result=self.store.get('plan_item',item['id'])
        self.assertEqual(result['status'],'rework');self.assertEqual(result['depends_on'],original['depends_on'])
        self.assertEqual(result['blocked_by_decision'],original['blocked_by_decision']);self.assertEqual(result['attempts'],original['attempts'])
        self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
        attempts=self.store.items('goal_dependency_attempt',None)
        self.assertEqual(len(attempts),2);self.assertTrue(all('Unknown or self dependency' in a['error'] for a in attempts))
        self.assertIn('Specific validation error:',calls.call_args_list[1].args[2])

    def test_resolution_with_empty_graph_update_passes(self):
        item=self.items('P3')[0];self.store.update('plan_item',item['id'],status='blocked')
        d=self.decision(item);self.answer(d,answer='Retry the existing item.')
        calls=self.refresh({'items':[],'actions':[{'key':'P3','resolution':'rework','instruction':'Implement and verify.'}]})
        self.assertEqual(calls.call_count,1);self.assertEqual(self.store.get('plan_item',item['id'])['status'],'rework')

    def test_prompt_explains_actual_reference_and_priority_rules(self):
        item=self.items('P3')[0];text=dependency_instruction([item],[])
        for expected in ('items":[]','blocked_by_decision','INTEGER','10000','UNKNOWN','actions','four-chunk'):
            self.assertIn(expected,text)
        with self.assertRaisesRegex(ValueError,'Priority'):
            prepare_plan([item],[{'key':'P3','priority':'100'}])

    def test_raw_response_and_error_are_redacted(self):
        from workbench.security import register_secret
        secret='synthetic-dependency-credential-111987';register_secret(secret)
        self.items('P3');self.refresh({'items':[{'key':'P3','priority':secret}]})
        self.assertNotIn(secret,json.dumps(self.store.items('goal_dependency_attempt',None)))


if __name__=='__main__':unittest.main()
