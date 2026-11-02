"""Bounded model retries and claimed-once restart recovery; no tool replay."""
import threading,time,uuid
from .goal_costs import GoalLimit
from .model_retry import DELAYS,retryable,failure_label
from .security import redact


def unattended(value=False):
    if type(value) is not bool:raise ValueError('Unattended mode must be a boolean.')
    return value

def gpu_cycle_limit(value=3):
    if type(value) is not int or not 0<=value<=1000:raise ValueError('GPU submissions per cycle must be an integer from 0 to 1000.')
    return value


class GoalOperations:
    def model_retry(self,goal,provider,operation,*,role,task_id=None,item_id=None,purpose='development'):
        call_id=uuid.uuid4().hex[:16]
        def guard():
            cancelled=self.builders.cancels.get(task_id) if task_id else None
            if cancelled is not None and cancelled.is_set():raise GoalLimit('Builder cancelled before the next model attempt.')
            current=self.store.get('goal',goal['id'])
            if role in ('Planner','Builder') and current['status']=='waiting_decision':raise GoalLimit('No model calls while waiting for a human decision.')
            return self.product_guard(goal) if purpose=='product' else self.guard(goal['id'],goal['generation'])
        for index in range(3):
            guard()
            row=self.store.save('goal_model_attempt',{'goal_id':goal['id'],'plan_item_id':item_id,'task_id':task_id,
                'generation':goal['generation'],'call_id':call_id,'role':role,'provider':provider,'retry_index':index,
                'state':'running','created':time.time(),**self.times(goal)})
            with self.runtime_lock:self.model_active[goal['id']]=self.model_active.get(goal['id'],0)+1
            try:
                result=operation(index,call_id)
                self.store.update('goal_model_attempt',row['id'],state='succeeded',finished=time.time())
                return result
            except Exception as error:
                label=failure_label(error);again=provider in ('meta','baseten') and retryable(error) and index<len(DELAYS)
                self.store.update('goal_model_attempt',row['id'],state='failed',finished=time.time(),error=label,retry_scheduled=again)
                if not again:raise
                delay=DELAYS[index]
                if task_id:
                    task=self.store.get('task',task_id)
                    self.store.event(task_id,'model_retry',retry_index=index+1,delay_seconds=delay,attempt_id=row['id'],**label)
                    self.store.update('task',task_id,model_retry_count=task.get('model_retry_count',0)+1)
            finally:
                with self.runtime_lock:self.model_active[goal['id']]=max(0,self.model_active.get(goal['id'],1)-1)
            checked=guard()
            if purpose!='product' and time.time()+delay>=checked['deadline_epoch']:
                self.control(goal['id'],'pause',reason='Deadline reached before the next model retry (UTC).')
                raise GoalLimit('Deadline leaves no time for another retry.')
            signal=self.signals.get(goal['id'])
            if signal is not None:
                if signal.wait(delay):raise GoalLimit('Goal stopped during model retry backoff.')
            else:self.retry_sleep(delay)
            guard()

    def remember_restart(self,goal):
        if goal['status']!='running' or not goal.get('unattended',False):return None
        identifier=goal['id']+':'+str(goal['generation'])
        try:return self.store.get('goal_restart',identifier)
        except ValueError:return self.store.save('goal_restart',{'id':identifier,'goal_id':goal['id'],'generation':goal['generation'],
            'state':'pending','previous_status':'running','created':time.time()})

    def resume_unattended(self):
        for intent in self.store.items('goal_restart',None):
            with self.lock:
                if self.operations_stopping.is_set():return
                try:pending=self.store.get('goal_restart',intent['id'])
                except ValueError:continue
                if pending['state']!='pending':continue
                goal=self.store.get('goal',intent['goal_id'])
                if not goal.get('unattended',False) or goal.get('archived') or goal['status']!='interrupted' or goal['generation']!=intent['generation']:
                    self.store.update('goal_restart',intent['id'],state='cancelled');continue
                # Claim before calling control: a failed/partial resume is never attempted twice.
                self.store.update('goal_restart',intent['id'],state='attempted',attempted_at=time.time())
                try:
                    self.control(goal['id'],'resume')
                    self.store.update('goal_restart',intent['id'],state='resumed',finished=time.time())
                except Exception as error:
                    label=failure_label(error)
                    label['reason']=redact(str(error))[:500] if isinstance(error,(ValueError,PermissionError)) else 'Resume failed; raw exception omitted.'
                    self.store.update('goal_restart',intent['id'],state='failed',finished=time.time(),error=label)
                    current=self.store.get('goal',goal['id']);worker=self.threads.get(goal['id'])
                    if current['status'] in ('planning','running') and not (worker and worker.is_alive()):
                        self.save(current,status='interrupted',reason='Unattended resume failed; resume explicitly after inspection.')
                    notifier=getattr(self,'notifier',None)
                    if notifier:
                        try:notifier.emit('unattended_resume_failed','restart:'+intent['id'],goal_id=goal['id'],source={'kind':'goal_restart','id':intent['id']})
                        except Exception:pass
