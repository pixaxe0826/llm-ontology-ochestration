"""Goal-configured plan capacity. Capacity is not a Builder defect or a human decision."""
import time
from .item_states import terminal

DEFAULT=40

def active_count(items):return sum(not terminal(item) for item in items)


def plan_limit(value=DEFAULT):
    if type(value) is not int or not 1<=value<=200:raise ValueError('max_plan_items must be an integer from 1 to 200.')
    return value


class PlanCapacityError(ValueError):
    def __init__(self,limit,count):
        self.limit,self.count=limit,count
        super().__init__(f'Active plan item limit {limit} reached ({count} active requested). Completed-item consolidation or a configured limit increase is needed; preserve historical records. Done and superseded items use no capacity. Do not ask a human decision or charge Builder rework for this capacity condition.')


class GoalPlanCapacity:
    def capacity_notice(self,goal,item,error,stage):
        key=goal['id']+':'+(item['id'] if item else 'initial')+':'+stage+':'+str(error.limit)
        try:record=self.store.get('goal_plan_capacity',key)
        except ValueError:
            record=self.store.save('goal_plan_capacity',{'id':key,'goal_id':goal['id'],'plan_item_id':item['id'] if item else None,
                'limit':error.limit,'requested':error.count,'active_at_request':active_count(self.items(goal['id'])),'stage':stage,'state':'waiting','created':time.time(),**self.times(goal)})
            self.message(goal,'System','Planner','NOTE',str(error),item['id'] if item else None,[record['id']])
        if item:
            self.park_item(item,'plan_capacity',resume=None,continue_pending=False,
                capacity_stage=stage,capacity_resume_status=item['status'])
        return record

    def release_capacity_waiters(self,goal):
        current=active_count(self.items(goal['id']));limit=plan_limit(goal.get('max_plan_items',DEFAULT))
        if current>=limit:return
        released=set()
        for item in self.items(goal['id']):
            if item.get('wait_reason')!='plan_capacity':continue
            if terminal(item):continue
            records=[r for r in self.store.items('goal_plan_capacity',None) if r.get('plan_item_id')==item['id'] and r['state']=='waiting']
            requested=max((r['requested']-max(0,r['active_at_request']-current) if 'active_at_request' in r else current+1 for r in records),default=0)
            if requested>limit:continue
            released.add(item['id'])
            if item.get('capacity_stage')=='stall_split':
                self.park_item(item,'stall_replan',resume=None,stall_replan_pending=True,stall_replan_attempted=False)
            else:self.store.update('plan_item',item['id'],status='review',wait_reason=None,waiting_resume=None)
        for record in self.store.items('goal_plan_capacity',None):
            if record['goal_id']==goal['id'] and record['state']=='waiting' and (record.get('plan_item_id') in released or record.get('plan_item_id') is None and record['requested']<=plan_limit(goal.get('max_plan_items',DEFAULT))):self.store.update('goal_plan_capacity',record['id'],state='capacity_available')
