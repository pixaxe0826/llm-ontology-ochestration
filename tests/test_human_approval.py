import unittest
import test_goal_decisions as fixtures
from workbench.acceptance_rules import normalize_criteria,needs_execution
from workbench.review_evidence import packet


class HumanApprovalTests(unittest.TestCase):
    setUp=fixtures.GoalDecisionTests.setUp;tearDown=fixtures.GoalDecisionTests.tearDown;items=fixtures.GoalDecisionTests.items

    def setup_item(self):
        item=self.items('visual')[0];(self.folder/'render.png').write_bytes(b'new image')
        text='Human approves this render.';criteria,rules=normalize_criteria([{'text':text,'kind':'human_approval','approval_paths':['render.png']}])
        item=self.store.update('plan_item',item['id'],acceptance=criteria,acceptance_rules=rules)
        decision=self.manager.create_decision(self.goal,{'question':'Approve this render?','options':['Approve','Reject'],'recommendation':'','reason':'Visual checkpoint.',
            'approval_paths':['render.png'],'approval_option':'Approve','reversible':False},item_id=item['id'])
        return item,decision

    def verdict(self,item,decision):
        return {'decision':'accept','defects':[],'criteria':[{'criterion':item['acceptance'][0],'evidence_ids':['approval:'+decision['id']]}],'goal_complete':False,'next_items':[]}

    def test_approving_human_decision_accepts_unchanged_files_without_command(self):
        item,decision=self.setup_item();self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        accepted,valid,_=self.manager.validate_review(self.goal,item,self.verdict(item,decision))
        self.assertIn(item['acceptance'][0],accepted);self.assertFalse(needs_execution(item,item['acceptance'][0]))
        ref=valid['approval:'+decision['id']];self.assertEqual(ref['kind'],'human_approval');self.assertIn('render.png',ref['files'])
        self.assertEqual(packet(valid)['observed'][0]['human_approval']['decision_id'],decision['id'])

    def test_pending_rejected_or_automatic_decision_never_accepts(self):
        item,decision=self.setup_item()
        with self.assertRaises(ValueError):self.manager.validate_review(self.goal,item,self.verdict(item,decision))
        self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Reject'})
        with self.assertRaises(ValueError):self.manager.validate_review(self.goal,item,self.verdict(item,decision))
        self.store.update('decision',decision['id'],files_approved=True,answered_by='Human',automatic=True)
        with self.assertRaises(ValueError):self.manager.validate_review(self.goal,item,self.verdict(item,decision))

    def test_changed_files_stale_the_approval_and_require_a_new_request(self):
        item,decision=self.setup_item();self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        (self.folder/'render.png').write_bytes(b'different image')
        observed=self.manager.observed_evidence(self.goal,item);self.assertTrue(observed['approval:'+decision['id']]['stale'])
        with self.assertRaises(ValueError):self.manager.validate_review(self.goal,item,self.verdict(item,decision))
        new=self.manager.create_decision(self.goal,{'question':'Approve this render?','options':['Approve','Reject'],'approval_paths':['render.png'],'reversible':False},item_id=item['id'])
        self.assertNotEqual(new['id'],decision['id'])

    def test_cannot_approve_changed_request_or_unrelated_files(self):
        item,decision=self.setup_item();(self.folder/'render.png').write_bytes(b'changed before answer')
        with self.assertRaisesRegex(ValueError,'changed'):self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        self.assertEqual(self.store.get('decision',decision['id'])['state'],'pending')
        with self.assertRaises(ValueError):normalize_criteria([{'text':'Approval','kind':'human_approval','approval_paths':['../outside']}])
        with self.assertRaises(ValueError):normalize_criteria([{'text':'Approval','kind':'human_approval'}])

    def test_execution_criteria_still_cannot_use_human_approval_as_execution(self):
        item,decision=self.setup_item();self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        text=item['acceptance'][0];item=self.store.update('plan_item',item['id'],acceptance_rules={text:{'kind':'execution'}})
        with self.assertRaisesRegex(ValueError,'successful command'):self.manager.validate_review(self.goal,item,self.verdict(item,decision))

    def test_approval_of_different_file_does_not_satisfy_the_criterion(self):
        item,decision=self.setup_item();self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        (self.folder/'other.png').write_bytes(b'other image')
        text=item['acceptance'][0];item=self.store.update('plan_item',item['id'],acceptance_rules={text:{'kind':'human_approval','approval_paths':['other.png']}})
        with self.assertRaisesRegex(ValueError,'other.png'):self.manager.validate_review(self.goal,item,self.verdict(item,decision))

    def test_decision_from_another_goal_cannot_satisfy_approval(self):
        item,decision=self.setup_item();self.manager.answer_decision(self.goal['id'],{'decision_id':decision['id'],'option':'Approve'})
        self.store.update('decision',decision['id'],goal_id='another-goal')
        with self.assertRaises(ValueError):self.manager.validate_review(self.goal,item,self.verdict(item,decision))

if __name__=='__main__':unittest.main()
