"""Durable, validated human updates wait for an inactive Builder chunk boundary."""
import time


def active_items(manager,goal_id):
    return {manager.store.get('task',tid).get('plan_item_id') for tid in list(manager.builders.active)
            if manager.store.get('task',tid).get('goal_id')==goal_id}


def queue(manager,goal,row,prepared):
    identifier=row['id']
    manager.store.save('goal_inbox_deferred',{'id':identifier,'goal_id':goal['id'],'directive_id':identifier,'state':'pending',
        'action':prepared['normalized_action'],'target_items':prepared['deferred_targets'],
        'active_items_at_queue':prepared['defer_active'],'created':time.time(),**manager.times(goal)})
    manager.store.save('goal_inbox_attempt',{'goal_id':goal['id'],'directive_id':identifier,'state':'queued_boundary',
        'normalized_action':prepared['normalized_action'],'created':time.time(),**manager.times(goal)})
    manager.ack_directive(goal,row,'queued',[],[],[],evidence=[identifier],server_only=True)


def apply_pending(manager,goal_id):
    with manager.lock:
        goal=manager.store.get('goal',goal_id)
        if goal['status'] not in ('planning','running','waiting_approval','waiting_decision'):return False
        applied=False
        rows=sorted((r for r in manager.store.items('goal_inbox_deferred',None) if r['goal_id']==goal_id and r['state']=='pending'),key=lambda r:r['created'])
        for pending in rows:
            if set(pending['target_items']) & active_items(manager,goal_id):continue
            row=manager.store.get('goal_directive',pending['directive_id'])
            if row['state']=='queued':
                # Revalidate against the latest plan. Never call a provider just to drain a queue.
                manager.finish_inbox(goal,[row],{'human_actions':[pending['action']]},allow_correction=False)
                applied=True
            state=manager.store.get('goal_directive',pending['directive_id'])['state']
            if state!='queued':manager.store.update('goal_inbox_deferred',pending['id'],state=state,applied_at=time.time())
        return applied
