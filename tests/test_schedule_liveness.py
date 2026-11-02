"""Fake-provider regressions for waiting triggers and the Builder handoff gap."""
import json,time,unittest
from unittest.mock import patch
import test_goal_decisions as fixtures
from test_goals import response,until
from workbench.schedule_liveness import dependency_cycles
from workbench.plan_updates import prepare_plan


class ScheduleLivenessTests(unittest.TestCase):
    setUp=fixtures.GoalDecisionTests.setUp
    tearDown=fixtures.GoalDecisionTests.tearDown
    items=fixtures.GoalDecisionTests.items
    decision=fixtures.GoalDecisionTests.decision
    def wait(self,item,reason,**values):
        return self.store.update('plan_item',item['id'],status='waiting',wait_reason=reason,waiting_resume=None,**values)
    def current(self,item):return self.store.get('plan_item',item['id'])

    def test_all_wait_reasons_without_live_triggers_wake_without_execution(self):
        reasons=('dependency','decision','deferred','platform_error','children','split_children','gpu','long_command','approval','builder_queue','continuation','interrupted','plan_capacity','future_reason',None)
        for reason in reasons:self.wait(self.items('case-'+str(reason))[-1],reason)
        with patch.object(self.manager,'assign',side_effect=AssertionError('Repair cannot dispatch')),patch.object(self.manager,'call',side_effect=AssertionError('Repair cannot poll')):
            self.manager.reconcile_waiting(self.goal)
        for item in self.manager.items(self.goal['id']):
            self.assertEqual(item['status'],'todo');self.assertIsNone(item['wait_reason']);self.assertTrue(self.manager.candidate_ready(self.goal,item))
        self.assertEqual(len(self.store.items('goal_schedule_issue',None)),len(reasons))
        self.manager.reconcile_waiting(self.goal);self.assertEqual(len(self.store.items('goal_schedule_issue',None)),len(reasons))

    def test_dependency_holds_then_continues_same_cycle(self):
        blocker,item=self.items('blocker','preview');self.wait(item,'deferred',depends_on=[blocker['id']],bundles=2,bundle_reported=2,execution_cycle=3)
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(item)['status'],'waiting')
        self.store.update('plan_item',blocker['id'],status='done');self.manager.reconcile_waiting(self.goal)
        current=self.current(item);self.assertEqual((current['status'],current['bundles'],current['execution_cycle']),('todo',2,3));self.assertEqual(current['waiting_resume'],'continue')

    def test_exhausted_deferred_blocker_done_opens_cycle_and_preserves_history(self):
        blocker,item=self.items('blocker','preview');evidence=[{'id':'preserved-evidence'}]
        self.wait(item,'deferred',depends_on=[blocker['id']],bundles=4,bundle_reported=4,execution_cycle=2,historical_bundles=6,evidence=evidence,task_ids=['old-task'],attempts=3,reworks=1)
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(item)['bundles'],4)
        self.store.update('plan_item',blocker['id'],status='done');self.manager.reconcile_waiting(self.goal)
        current=self.current(item);self.assertEqual((current['bundles'],current['execution_cycle'],current['historical_bundles']),(0,3,10))
        self.assertEqual(current['evidence'],evidence);self.assertEqual(current['task_ids'],['old-task']);self.assertEqual((current['attempts'],current['reworks']),(3,1))
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(item)['execution_cycle'],3)

    def test_answered_unscoped_retry_resets_chunk_counter_once(self):
        item=self.items('preview')[0];self.wait(item,'deferred',bundles=4,bundle_reported=4);d=self.decision(item)
        self.manager.answer_decision(self.goal['id'],{'decision_id':d['id'],'answer':"I'll approve the reset of 4-chunk limit. retry."})
        current=self.current(item);self.assertEqual((current['status'],current['bundles'],current['execution_cycle']),('rework',0,1))
        self.manager.release_answered_waits(self.goal);self.assertEqual(self.current(item)['execution_cycle'],1)

    def test_legacy_answered_retry_waits_for_blocker_then_resets(self):
        blocker,item=self.items('blocker','preview');d=self.decision(item)
        self.store.update('decision',d['id'],state='answered',answer='Approve rework; retry.',answered_at=time.time())
        self.wait(item,'deferred',bundles=4,bundle_reported=4,depends_on=[blocker['id']],decision_action_source=d['id'])
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(item)['bundles'],4)
        self.store.update('plan_item',blocker['id'],status='done');self.manager.reconcile_waiting(self.goal)
        self.assertEqual((self.current(item)['bundles'],self.current(item)['execution_cycle']),(0,1));self.assertEqual(len(self.store.items('goal_item_action',None)),1)

    def test_answer_during_active_builder_defers_reset_until_boundary(self):
        item=self.items('preview')[0];d=self.decision(item);tid='active-task'
        self.store.save('task',{'id':tid,'goal_id':self.goal['id'],'plan_item_id':item['id'],'status':'running','created':time.time(),'events':[]})
        self.wait(item,'decision',bundles=4,bundle_reported=4,task_id=tid);self.manager.builders.active.add(tid)
        try:
            self.manager.answer_decision(self.goal['id'],{'decision_id':d['id'],'answer':'Reset chunk limit and retry.'})
            self.assertEqual(self.current(item)['bundles'],4)
        finally:self.manager.builders.active.discard(tid)
        self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(item)['bundles'],0)

    def test_old_answer_never_replays_over_newer_directive_cycle(self):
        item=self.items('preview')[0];d=self.decision(item)
        self.store.update('decision',d['id'],state='answered',answer='Reset chunk limit and retry.',answered_at=time.time()-10)
        self.store.save('goal_item_action',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'action':'rework','state':'applied','created':time.time()-5,'directive_id':'later-interrupt'})
        self.wait(item,'long_command',bundles=1,bundle_reported=1,execution_cycle=2,decision_action_source=d['id']);self.manager.reconcile_waiting(self.goal)
        self.assertEqual((self.current(item)['bundles'],self.current(item)['execution_cycle']),(1,2))

    def test_pending_decision_holds_until_answer(self):
        item=self.items('preview')[0];d=self.decision(item);self.wait(item,'future_reason');self.manager.reconcile_waiting(self.goal)
        self.assertEqual(self.current(item)['status'],'waiting')
        self.manager.answer_decision(self.goal['id'],{'decision_id':d['id'],'option':'A'});self.manager.reconcile_waiting(self.goal)
        self.assertTrue(self.manager.candidate_ready(self.goal,self.current(item)))

    def test_gpu_and_long_commands_hold_then_wake_and_prevent_empty_stop(self):
        for kind in ('gpu','goal_long_command'):
            item=self.items('case-'+kind)[-1];self.wait(item,'future_reason')
            job=self.store.save(kind,{'goal_id':self.goal['id'],'plan_item_id':item['id'],'status':'running','created':time.time()})
            self.manager.reconcile_waiting(self.goal);self.assertFalse(self.manager.candidate_ready(self.goal,self.current(item)))
            self.assertFalse(self.manager.block_if_no_runnable(self.goal,[]))
            self.store.update(kind,job['id'],status='succeeded');self.manager.reconcile_waiting(self.goal)
            self.assertTrue(self.manager.candidate_ready(self.goal,self.current(item)))

    def test_children_hold_then_parent_uses_aggregate_review(self):
        parent,child=self.items('parent','child');self.store.update('plan_item',child['id'],parent=parent['id'])
        self.manager.refresh_split_reviews(self.goal);self.manager.reconcile_waiting(self.goal);self.assertEqual(self.current(parent)['status'],'waiting_children')
        self.store.update('plan_item',child['id'],status='done');self.manager.refresh_split_reviews(self.goal);self.assertEqual(self.current(parent)['status'],'review')

    def test_unconsumed_builder_result_in_handoff_gap_is_live_work(self):
        item=self.items('handoff')[0];task=self.store.save('task',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'status':'paused','events':[]})
        self.store.update('plan_item',item['id'],status='in_progress',task_id=task['id'],bundles=1,bundle_reported=0)
        self.assertFalse(self.manager.block_if_no_runnable(self.goal,[]));self.assertFalse(self.store.get('goal',self.goal['id']).get('scheduling_blocked'))
        self.store.update('plan_item',item['id'],bundle_reported=1);self.manager.reconcile_waiting(self.goal);self.assertTrue(self.manager.candidate_ready(self.goal,self.current(item)))

    def test_orphan_in_progress_is_dispatched_by_normal_scheduler(self):
        item=self.items('orphan')[0];self.store.update('plan_item',item['id'],status='in_progress')
        self.assertFalse(self.manager.block_if_no_runnable(self.goal,[]));candidate=self.manager.next_builder(self.goal);self.assertEqual(candidate['id'],item['id'])
        self.manager.assign(self.goal,candidate);until(lambda:not self.manager.builders.active)
        self.assertTrue(any(m['type']=='ASSIGN' and m['plan_item_id']==item['id'] for m in self.manager.messages(self.goal['id'])))

    def test_plan_update_repairs_wait_without_starting_builder(self):
        blocker,item=self.items('blocker','preview');self.wait(item,'deferred',depends_on=[blocker['id']])
        value=prepare_plan(self.manager.items(self.goal['id']),[{'key':item['key'],'depends_on':[]}])
        with patch.object(self.manager,'assign',side_effect=AssertionError('No repair dispatch')):self.manager.apply_plan_patch(self.goal,value)
        self.assertEqual(self.current(item)['status'],'todo')

    def cycle(self):
        a,b=self.items('a','b');self.wait(a,'dependency',depends_on=[b['id']]);self.wait(b,'dependency',depends_on=[a['id']]);return a,b
    def test_cycle_detection_includes_parent_child_edges(self):
        a,b=self.cycle();self.assertEqual(dependency_cycles(self.manager.items(self.goal['id'])),[sorted([a['id'],b['id']])])
        self.store.update('plan_item',a['id'],depends_on=[]);self.store.update('plan_item',b['id'],parent=a['id'],depends_on=[a['id']]);self.assertTrue(dependency_cycles(self.manager.items(self.goal['id'])))
    def test_cycle_has_one_planner_repair_before_human(self):
        a,b=self.cycle()
        with patch.object(self.manager,'call',return_value=response(json.dumps({'items':[{'key':a['key'],'depends_on':[]}]}))) as call:
            self.manager.repair_schedule_cycles(self.goal);self.manager.repair_schedule_cycles(self.goal)
        self.assertEqual(call.call_count,1);self.assertFalse(dependency_cycles(self.manager.items(self.goal['id'])));self.assertFalse(self.manager.pending_decisions(self.goal['id']))
    def test_failed_cycle_repair_escalates_once_across_status_changes(self):
        a,b=self.cycle()
        with patch.object(self.manager,'call',return_value=response('{"items":[]}')) as call:
            self.manager.repair_schedule_cycles(self.goal);self.store.update('plan_item',a['id'],status='todo');self.manager.repair_schedule_cycles(self.goal)
        self.assertEqual(call.call_count,1);self.assertEqual(len(self.manager.pending_decisions(self.goal['id'])),1)


if __name__=='__main__':unittest.main()
