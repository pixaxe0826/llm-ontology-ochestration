"""No paid calls: active requirements are durable and applied only between chunks."""
import copy,json,threading
from unittest.mock import patch
import unittest
from test_goal_inbox import GoalInboxTests as F
from workbench.goal_inbox import inbox_contract


class InboxBoundaryTests(unittest.TestCase):
    setUp=F.setUp;items=F.items;submit=F.submit;action=F.action

    def tearDown(self):
        self.manager.builders.active.clear();F.tearDown(self)

    def active(self,item):
        task=self.store.save('task',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'generation':self.goal['generation'],
            'workspace':str(self.folder),'status':'running','events':[],'messages':[]})
        self.store.update('plan_item',item['id'],task_id=task['id'],status='in_progress',bundles=2,execution_cycle=3)
        self.manager.builders.active.add(task['id']);return task

    def test_active_reopen_queues_requirements_without_reset_or_extra_planner_call(self):
        item=self.items('active')[0];task=self.active(item);before=self.store.get('plan_item',item['id'])
        row=self.submit('instruction','Keep current work and add new requirements.',item=item)
        criterion='New requirements verified.'
        action=self.action(row,items=[{'key':item['key'],'acceptance':[{'text':criterion,'kind':'execution'}]}],
            item_actions=[{'item_id':item['id'],'resolution':'rework','instruction':'Add the new requirement without discarding current work.'}])
        with patch.object(self.manager,'call',side_effect=AssertionError('No correction/model call')):
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]})
            self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'queued')
            self.assertEqual(self.store.get('plan_item',item['id']),before)
            self.assertFalse(self.manager.apply_deferred_directives(self.goal['id']))
            self.assertIn(row['text'],json.dumps(self.manager.builder_directions(self.goal,item)))
            self.manager.builders.active.remove(task['id']);self.store.update('task',task['id'],status='paused')
            self.assertTrue(self.manager.apply_deferred_directives(self.goal['id']))
            self.assertFalse(self.manager.apply_deferred_directives(self.goal['id']))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['acceptance'],[criterion]);self.assertIn(row['text'],current['instruction'])
        for key in ('bundles','execution_cycle','attempts','reworks','task_id'):self.assertEqual(current.get(key),before.get(key))
        self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'applied')
        self.assertEqual(self.store.get('goal_inbox_deferred',row['id'])['state'],'applied')
        self.assertEqual(self.store.items('goal_item_action',None),[])
        acks=[m for m in self.manager.messages(self.goal['id']) if m['type']=='ACK'];self.assertEqual([m['ack_status'] for m in acks],['queued','applied'])
        context=json.dumps(self.manager.context(self.goal,current,'Builder'));self.assertIn(criterion,context);self.assertIn(row['text'],context)

    def test_plain_instruction_and_multi_item_patch_wait_as_one_action(self):
        a,b=self.items('a','b');task=self.active(a);original=copy.deepcopy(b)
        row=self.submit('instruction','Keep both files consistent.',item=a)
        action=self.action(row,items=[{'key':b['key'],'title':'Updated after boundary'}],instructions=[{'item_id':a['id'],'text':'Preserve the current file and add the check.'}])
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]})
        self.assertEqual(self.store.get('plan_item',b['id']),original)
        self.manager.builders.active.remove(task['id']);self.manager.apply_deferred_directives(self.goal['id'])
        self.assertEqual(self.store.get('plan_item',b['id'])['title'],'Updated after boundary')
        self.assertIn(row['text'],self.store.get('plan_item',a['id'])['instruction'])

    def test_queued_update_survives_recovery_but_revalidates_the_current_graph(self):
        a,b=self.items('a','b');task=self.active(a)
        row=self.submit('instruction','Use the new dependency.',item=a)
        action=self.action(row,items=[{'key':a['key'],'depends_on':[b['id']]}],item_actions=[{'item_id':a['id'],'resolution':'rework'}])
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]})
        self.manager.recover_directives();self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'queued')
        self.store.update('plan_item',b['id'],depends_on=[a['id']]);self.manager.builders.active.remove(task['id'])
        with patch.object(self.manager,'call',side_effect=AssertionError('No extra call at the boundary')):self.manager.apply_deferred_directives(self.goal['id'])
        self.assertEqual(self.store.get('plan_item',a['id'])['depends_on'],[])
        self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'partial')
        self.assertEqual(self.store.get('goal_inbox_deferred',row['id'])['state'],'partial')

    def test_pause_preserves_queue_and_no_reopen_contract_is_explicit(self):
        item=self.items('one')[0];task=self.active(item);row=self.submit('instruction','Add a requirement.',item=item)
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[self.action(row,item_actions=[{'item_id':item['id'],'resolution':'rework'}])]})
        self.manager.builders.active.remove(task['id']);self.store.update('goal',self.goal['id'],status='paused',pause_origin='user')
        self.assertFalse(self.manager.apply_deferred_directives(self.goal['id']));self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'queued')
        self.assertIn('needs NO reopen',inbox_contract([row]))

    def test_active_unsafe_or_invalid_changes_are_not_queued(self):
        item=self.items('one')[0];self.active(item);row=self.submit('instruction','Add a safe requirement.',item=item)
        action=self.action(row,items=[{'key':item['key'],'depends_on':[item['id']]}],item_actions=[{'item_id':item['id'],'resolution':'rework'}])
        self.manager.finish_inbox(self.goal,[row],{'human_actions':[action]},allow_correction=False)
        self.assertEqual(self.store.items('goal_inbox_deferred',None),[]);self.assertEqual(self.store.get('goal_directive',row['id'])['state'],'partial')

    def test_active_chunk_cannot_see_new_direction_until_next_chunk(self):
        item=self.items('one')[0];task=self.active(item)
        self.store.event(task['id'],'chunk_started',chunk_id='old-chunk');self.manager.builders.local.chunk_id='old-chunk'
        try:
            row=self.submit('instruction','Use this in the NEXT chunk.',item=item)
            self.assertEqual(self.manager.builder_directions(self.goal,self.store.get('plan_item',item['id'])),[])
            self.manager.finish_inbox(self.goal,[row],{'human_actions':[self.action(row,item_actions=[{'item_id':item['id'],'resolution':'rework'}])]})
            self.assertEqual(self.manager.builder_directions(self.goal,self.store.get('plan_item',item['id'])),[])
            self.manager.builders.active.remove(task['id']);self.manager.apply_deferred_directives(self.goal['id'])
            self.store.event(task['id'],'chunk_started',chunk_id='next-chunk');self.manager.builders.local.chunk_id='next-chunk'
            self.assertEqual(self.manager.builder_directions(self.goal,self.store.get('plan_item',item['id']))[0]['text'],row['text'])
        finally:del self.manager.builders.local.chunk_id

if __name__=='__main__':unittest.main()
