"""Minute-cadence, read-only Goal health observations. Never changes Goal/item state."""
from collections import Counter
import hashlib,json,re,threading,time
from .security import redact


class GoalWatchdog:
    def __init__(self,manager,notifications,clock=time.time):
        self.manager,self.store,self.notifications,self.clock=manager,manager.store,notifications,clock
        self.last_tick=float('-inf');self.lock=threading.RLock()

    def active(self,goal):
        m=self.manager
        if m.long_commands.active(goal['id']):return True
        with m.runtime_lock:
            if m.model_active.get(goal['id'],0):return True
        if any(m.store.get('task',tid).get('goal_id')==goal['id'] and m.store.get('task',tid)['status']=='running' for tid in list(m.builders.active)):return True
        for job in self.store.items('gpu',None):
            if job.get('id') not in m.gpu.active and job.get('status') not in ('running','queued'):continue
            if job.get('goal_id')==goal['id']:return True
            try:
                if self.store.get('task',job.get('task_id')).get('goal_id')==goal['id']:return True
            except ValueError:pass
        return False

    def tick(self):
        with self.lock:
            now=self.clock()
            if now-self.last_tick<60:return
            self.last_tick=now;present=set()
            for goal in self.store.items('goal',None):
                if goal.get('archived') or goal['status'] not in ('planning','running','waiting','waiting_approval','waiting_decision','blocked','paused','interrupted'):continue
                gid=goal['id'];generation=goal['generation'];busy=self.active(goal)
                try:state=self.store.get('goal_watchdog_state',gid)
                except ValueError:state={}
                if state.get('generation')!=generation or state.get('status')!=goal['status']:
                    state={'last_active':goal.get('updated',now),'generation':generation,'status_since':goal.get('status_since',goal.get('updated',now))}
                last=state.get('last_active',now)
                recent=[row.get('finished',row.get('created',0)) for row in self.store.items('goal_model_attempt',None) if row['goal_id']==gid and row.get('generation')==generation]
                recent.extend(row.get('finished',0) for row in self.store.items('task',None) if row.get('goal_id')==gid and row.get('generation')==generation)
                last=now if busy else max(last,*recent) if recent else last
                self.store.save('goal_watchdog_state',{'id':gid,'goal_id':gid,'generation':generation,'status':goal['status'],'status_since':state.get('status_since',goal.get('status_since',goal.get('updated',now))),'last_active':last,'checked_at':now})
                user_pause=goal.get('pause_origin')=='user' or bool(re.search(r'Paused by user|user requested|사용자.*일시정지',goal.get('reason',''),re.I))
                since=state.get('status_since',goal.get('status_since',goal.get('updated',now)))
                if goal['status'] in ('paused','interrupted') and not user_pause and now-since>=600:
                    present.add(self.warn(goal,'inactive_10m:'+goal['status'],'Goal has remained automatically paused/interrupted for at least 10 minutes.',now))
                if goal['status']=='running' and not busy and now-last>=600:
                    present.add(self.warn(goal,'no_active_stage','Goal is running with no active stage for at least 10 minutes.',now))
                if goal['status'] in ('waiting','waiting_approval','waiting_decision') and not self.manager.pending_decisions(gid) and not self.manager.approvals(gid):
                    present.add(self.warn(goal,'empty_wait','Goal is waiting without a pending decision or approval.',now))
                for item in self.manager.items(gid):
                    errors=[]
                    for row in self.store.items('goal_model_attempt',None):
                        if row['goal_id']==gid and row.get('plan_item_id')==item['id'] and row.get('generation')==generation and row['state']=='failed':errors.append(json.dumps(row.get('error',{}),sort_keys=True))
                    for task in self.store.items('task',None):
                        if task.get('goal_id')!=gid or task.get('plan_item_id')!=item['id'] or task.get('generation')!=generation:continue
                        if task.get('execution_cycle',0)!=item.get('execution_cycle',0):continue
                        for event in task.get('events',[]):
                            value=event.get('result')
                            if event.get('kind')=='tool_result' and isinstance(value,dict) and value.get('error'):
                                errors.append(str(event.get('name'))+':'+redact(str(value['error'])))
                            elif event.get('kind')=='error':errors.append(redact(str(event.get('diagnostic',event.get('text','')))))
                    review=item.get('review_error') or {}
                    if item.get('review_error_count',0)>=3 and review.get('message'):errors.extend([redact(review['message'])]*3)
                    for error,count in Counter(errors).items():
                        if count<3:continue
                        fingerprint=hashlib.sha256(error.encode()).hexdigest()[:20]
                        present.add(self.warn(goal,'repeated_error:'+item['id']+':'+fingerprint,'The same item error has occurred at least three times. Inspect its task/model-attempt records.',now,item['id'],count))
            for row in self.store.items('goal_watchdog',None):
                if row.get('active') and row['id'] not in present:self.store.update('goal_watchdog',row['id'],active=False,recovered_at=now)

    def warn(self,goal,condition,summary,now,item_id=None,count=None):
        identifier=hashlib.sha256((goal['id']+':'+str(goal['generation'])+':'+condition).encode()).hexdigest()[:24]
        try:old=self.store.get('goal_watchdog',identifier)
        except ValueError:old={}
        if not old.get('active'):
            episode=old.get('episode',0)+1
            self.store.save('goal_watchdog',{'id':identifier,'goal_id':goal['id'],'plan_item_id':item_id,'condition':condition,
                'active':True,'episode':episode,'created':now,'summary':summary,'count':count})
            self.notifications.emit('watchdog','watchdog:'+identifier+':'+str(episode),goal_id=goal['id'],item_id=item_id,
                source={'kind':'goal_watchdog','id':identifier},summary=summary)
        return identifier
