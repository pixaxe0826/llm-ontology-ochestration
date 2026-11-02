"""Terminal retirement, replacement dependencies, audit and active-only capacity."""
import copy,json,unittest
from unittest.mock import patch
import test_goal_inbox as inbox
from test_goals import response
from workbench.plan_updates import prepare_plan
from workbench.plan_capacity import active_count,PlanCapacityError
from workbench.goal_prompts import system

class SupersededTests(unittest.TestCase):
    setUp=inbox.GoalInboxTests.setUp
    tearDown=inbox.GoalInboxTests.tearDown
    items=inbox.GoalInboxTests.items
    submit=inbox.GoalInboxTests.submit
    action=inbox.GoalInboxTests.action
    def close(self,item,replacement=None):return self.manager.supersede_item(self.goal,item,'Obsolete variant replaced by current work.',replacement,actor='Human',source='approved-repair')
    def test_terminal_not_scheduled_woken_reviewed_or_batch_limited(self):
        a,b=self.items('old','new');a=self.close(a,b['id'])
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No Builder')):
            self.manager.assign(self.goal,a);self.manager.wake_wait(self.goal,a);self.manager.prepare_limit_review(self.goal,a)
        self.assertFalse(self.manager.candidate_ready(self.goal,a));self.assertFalse(self.manager.focus_pinned(self.goal,a['id']));self.assertFalse(self.manager.review(self.goal,a))
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.store.get('plan_item',a['id'])['status'],'superseded');self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
    def test_dependency_waits_for_replacement_done_and_none_stays_unresolved(self):
        a,b,c=self.items('old','new','dependent');self.store.update('plan_item',c['id'],depends_on=[a['id']]);self.close(a,b['id'])
        c=self.store.get('plan_item',c['id']);self.assertEqual(self.manager.dependency_view(self.goal,c)['unresolved_dependencies'],[b['id']])
        self.store.update('plan_item',b['id'],status='done');self.assertTrue(self.manager.dependency_view(self.goal,c)['ready'])
        self.close(self.store.get('plan_item',a['id']));self.assertFalse(self.manager.dependency_view(self.goal,c)['ready'])
    def test_parent_counts_retired_children_finished_and_packet_lists_them(self):
        parent,a=self.items('parent','old');self.store.update('plan_item',a['id'],parent=parent['id']);self.close(a)
        self.manager.refresh_split_reviews(self.goal);parent=self.store.get('plan_item',parent['id'])
        self.assertEqual(parent['status'],'review');self.assertEqual(self.manager.execution_view(self.goal,parent)['children_done'],1)
        verdict={'decision':'rework','defects':['An execution is still required.'],'criteria':[],'summary':'No command yet','next_items':[],'goal_complete':False}
        with patch.object(self.manager,'inbox_phase',return_value=(json.dumps(verdict),[])):self.manager.review(self.goal,parent)
        attempt=self.store.items('goal_review_attempt',None)[0];self.assertIn(a['id'],attempt['evidence_packet']['superseded_items'])
    def test_parent_waits_only_on_live_children_not_retired_children_batch(self):
        parent,a,b=self.items('parent','old','new');self.store.update('plan_item',a['id'],parent=parent['id']);self.store.update('plan_item',b['id'],parent=parent['id']);self.close(a,b['id'])
        self.manager.refresh_split_reviews(self.goal);self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'waiting_children');self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
    def test_audit_preserves_history_evidence_counters_commits_and_is_idempotent(self):
        a,b=self.items('old','new');a=self.store.update('plan_item',a['id'],bundles=4,reworks=2,commit_hash='abc123',evidence=[{'id':'retained'}],task_ids=['retained-task'])
        before=copy.deepcopy(a);saved=self.close(a,b['id']);again=self.close(saved,b['id'])
        for k in ('bundles','reworks','commit_hash','evidence','task_ids'):self.assertEqual(saved[k],before[k])
        audits=self.store.items('goal_item_supersession',None);self.assertEqual(len(audits),1);self.assertEqual(audits[0]['before'],before);self.assertEqual(audits[0]['source'],'approved-repair');self.assertEqual(again['id'],a['id']);self.assertTrue(saved['superseded_utc'])
    def test_invalid_foreign_replacement_cycles_and_active_step_rejected(self):
        a,b=self.items('old','new')
        for replacement in ('foreign',a['id']):
            with self.assertRaises(ValueError):self.close(a,replacement)
        self.close(a,b['id'])
        with self.assertRaises(ValueError):self.close(b,a['id'])
        self.store.update('plan_item',b['id'],task_id='active');self.manager.builders.active.add('active')
        try:
            with self.assertRaises(ValueError):self.close(self.store.get('plan_item',b['id']))
        finally:self.manager.builders.active.discard('active')
    def test_human_actions_applies_actual_closure_and_cannot_reopen_retired(self):
        a,b=self.items('old','new');row=self.submit('instruction','This item is superseded. Close it now.',item=a)
        action=self.action(row,item_actions=[{'item_id':a['id'],'resolution':'superseded','reason':'New item covers this work.','superseded_by':b['id']}])
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]});self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'applied');self.assertEqual(self.store.get('plan_item',a['id'])['status'],'superseded')
        with self.assertRaises(ValueError):self.manager.validate_item_action(self.goal,self.store.get('plan_item',a['id']),'rework','retry')
    def test_prose_only_applied_closure_ack_is_corrected_once_not_claimed_applied(self):
        a=self.items('old')[0];row=self.submit('instruction','Close this item as superseded.',item=a)
        with patch.object(self.manager,'call',return_value=response(json.dumps({'human_actions':[self.action(row)]}))) as phase:
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[self.action(row)]})
        self.assertEqual(phase.call_count,1);self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'partial');self.assertNotEqual(self.store.get('plan_item',a['id'])['status'],'superseded')
    def test_validated_plan_update_and_human_status_form(self):
        a,b=self.items('old','new');patch_data=prepare_plan([a,b],[{'key':a['key'],'status':'superseded','superseded_reason':'Clear replacement','superseded_by':b['key']}])
        self.manager.apply_plan_patch(self.goal,patch_data);self.assertEqual(self.store.get('plan_item',a['id'])['superseded_by'],b['id'])
        row=self.submit('instruction','Close this item as superseded.',item=b)
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[self.action(row,items=[{'key':b['key'],'status':'superseded','superseded_reason':'Human cancels this item.'}])]})
        self.assertEqual(self.store.get('plan_item',b['id'])['status'],'superseded')
    def test_plan_requires_reason_and_replacement_and_checks_replacement_cycles(self):
        a,b=self.items('a','b')
        for fields in ({'superseded_by':b['id']},{'superseded_reason':'obsolete'},{'superseded_reason':'obsolete','superseded_by':{}}):
            with self.assertRaises(ValueError):prepare_plan([a,b],[{'key':'a','status':'superseded',**fields}])
        with self.assertRaises(ValueError):prepare_plan([a,b],[{'key':'a','status':'superseded','superseded_reason':'old','superseded_by':'b'},{'key':'b','depends_on':['a']}])
        patch_data=prepare_plan([a],[{'key':'a','status':'superseded','superseded_reason':'old','superseded_by':'new'},{'key':'new','title':'New','acceptance':['New result']}]);self.manager.apply_plan_patch(self.goal,patch_data);self.assertEqual(self.store.get('plan_item',a['id'])['status'],'superseded')
    def test_active_capacity_ignores_history_preserves_records_and_releases_slots(self):
        a,b,c=self.items('a','b','c');self.store.update('plan_item',a['id'],status='done');self.close(b,a['id']);rows=self.manager.items(self.goal['id'])
        self.assertEqual(active_count(rows),1)
        prepared=prepare_plan(rows,[{'key':'new','title':'New','acceptance':['New result']}],max_plan_items=2);self.manager.apply_plan_patch(self.goal,prepared);self.assertEqual(len(self.manager.items(self.goal['id'])),4)
        with self.assertRaises(PlanCapacityError):prepare_plan(self.manager.items(self.goal['id']),[{'key':'over','title':'Overflow','acceptance':['New result']}],max_plan_items=2)
        # One active retirement and one new deliverable in the same plan need one slot, not two.
        rows=self.manager.items(self.goal['id']);self.assertTrue(prepare_plan(rows,[{'key':'c','status':'superseded','superseded_reason':'replacement','superseded_by':'next'},{'key':'next','title':'Next','acceptance':['Next result']}],max_plan_items=2)['creates'])
    def test_history_metadata_updates_beyond_capacity_but_reopening_criteria_counts(self):
        a,b=self.items('a','b');a=self.store.update('plan_item',a['id'],status='done');rows=[a,b]
        self.assertTrue(prepare_plan(rows,[{'key':'a','title':'Historical title'}],max_plan_items=1)['updates'])
        with self.assertRaises(PlanCapacityError):prepare_plan(rows,[{'key':'a','acceptance':['A new execution']}],max_plan_items=1)
        self.assertIn('superseded',system(self.goal,'Planner'));self.assertIn('non-terminal',system(self.goal,'Planner'))

    def test_capacity_wait_releases_when_active_items_finish_without_deleting_history(self):
        a,b=self.items('a','b');goal=self.store.update('goal',self.goal['id'],max_plan_items=2)
        error=PlanCapacityError(2,3);self.manager.capacity_notice(goal,a,error,'review_split')
        self.store.update('plan_item',b['id'],status='done');self.manager.release_capacity_waiters(goal)
        self.assertEqual(self.store.get('plan_item',a['id'])['status'],'review');self.assertEqual(len(self.manager.items(goal['id'])),2)
    def test_retirement_withdraws_only_pending_decisions_all_of_whose_items_are_retired(self):
        a,b=self.items('a','b');pending=self.manager.create_decision(self.goal,{'question':'Old item question','options':[],'affected_items':[a['id']]})
        answered=self.store.save('decision',{**pending,'id':'answered-history','state':'answered','answer':'Keep the history'})
        mixed=self.manager.create_decision(self.goal,{'question':'Shared scope','options':[],'affected_items':[a['id'],b['id']]})
        self.close(self.store.get('plan_item',a['id']),b['id']);self.assertEqual(self.store.get('decision',pending['id'])['state'],'withdrawn');self.assertEqual(self.store.get('decision',mixed['id'])['state'],'pending');self.assertEqual(self.store.get('decision',answered['id']),answered)

    def test_retired_item_never_enters_automatic_stall_replanning(self):
        a,b=self.items('a','b');a=self.store.update('plan_item',a['id'],stall_replan_pending=True,stall_replan_attempted=True)
        a=self.close(a,b['id'])
        with patch.object(self.manager,'inbox_phase',side_effect=AssertionError('No Planner on retired work')):
            self.assertFalse(self.manager.shrink_stalled_item(self.goal,a));self.manager.recover_stall_request(self.goal,a)
        self.assertFalse(self.store.get('plan_item',a['id'])['stall_replan_pending'])
    def test_stall_split_capacity_counts_active_not_historical_items(self):
        a,b,c=self.items('a','b','c');self.store.update('plan_item',b['id'],status='done');self.close(c,b['id']);a=self.store.update('plan_item',a['id'],stall_replan_pending=True)
        goal=self.store.update('goal',self.goal['id'],max_plan_items=2)
        value={'items':[{'key':'small-child','title':'Small implementation','acceptance':['Result file exists'],'files_to_read':[],'files_to_write':['result.py'],'commands_to_run':[],'depends_on':[],'blocked_by_decision':[]}],'summary':'One small implementation child'}
        with patch.object(self.manager,'inbox_phase',return_value=(json.dumps(value),[])) as planner:self.assertTrue(self.manager.shrink_stalled_item(goal,a))
        self.assertEqual(planner.call_count,1);self.assertEqual(len(self.manager.items(goal['id'])),4);self.assertEqual(active_count(self.manager.items(goal['id'])),2)
