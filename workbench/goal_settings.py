"""Atomic Goal configuration changes, boundary application and approval reassessment."""
import re
import time
import json

from .goal_policy import policy, command_assessment
from .goal_time import deadline, stamp, zone
from .security import contains_key, redact
from .goal_models import validate_builder_model
from .goal_references import reference_limits
from .goal_operations import unattended,gpu_cycle_limit
from .reference_pages import page_limits
from .git_checkpoint import enabled as auto_commit_enabled
from .builder_productivity import read_limits
from .plan_capacity import plan_limit

from .human_briefs import display_language

FIELDS={'human_display_language','output_language','timezone','builder_model','reasoning','budgets','policy','deadline','allow_provisional','reference_limits','unattended','gpu_jobs_per_cycle','reference_page_limits','auto_commit','read_loop_limits','max_plan_items'}


FIELDS.add('role_routes')


class GoalSettings:
    @staticmethod
    def provisional_setting(value):
        if type(value) is not bool:raise ValueError('Allow provisional must be a boolean.')
        return value

    def settings_history(self,goal_id):
        return sorted([row for row in self.store.items('goal_setting_change',None) if row['goal_id']==goal_id],key=lambda r:r['created'])

    def validated_settings(self,goal,data):
        if not isinstance(data,dict) or set(data)-FIELDS-{'goal_id'}:
            raise ValueError('Only Goal configuration fields may be changed; objective and workspace are immutable.')
        if contains_key(json.dumps(data,ensure_ascii=False)):
            raise ValueError('Goal settings must not contain secrets.')
        values={}
        if 'human_display_language' in data:values['human_display_language']=display_language(data['human_display_language'])
        if 'max_plan_items' in data:values['max_plan_items']=plan_limit(data['max_plan_items'])
        if 'read_loop_limits' in data:values['read_loop_limits']=read_limits(data['read_loop_limits'])
        if 'auto_commit' in data:values['auto_commit']=auto_commit_enabled(data['auto_commit'])
        if 'reference_page_limits' in data:values['reference_page_limits']=page_limits(data['reference_page_limits'])
        if 'unattended' in data:values['unattended']=unattended(data['unattended'])
        if 'gpu_jobs_per_cycle' in data:values['gpu_jobs_per_cycle']=gpu_cycle_limit(data['gpu_jobs_per_cycle'])
        if 'reference_limits' in data:values['reference_limits']=reference_limits(data['reference_limits'])
        if 'allow_provisional' in data:values['allow_provisional']=self.provisional_setting(data['allow_provisional'])
        if 'output_language' in data:
            language=data['output_language']
            if not isinstance(language,str) or not language.strip() or len(language.strip())>60 or any(c in language for c in '\n\r{}'):
                raise ValueError('Use a short output language name.')
            values['output_language']=language.strip()
        if 'timezone' in data:
            zone(data['timezone']);values['timezone']=data['timezone']
            values['deadline_local']=stamp(goal['deadline_epoch'],data['timezone'])['local']
        if 'role_routes' in data:
            values['role_routes']=self.validate_routes(data['role_routes'],self.effective_routes(goal))
            values['budgets']=self.ledger.budgets(goal['budgets'])
        if 'builder_model' in data:
            values['builder_model']=validate_builder_model(data['builder_model'])
        if 'reasoning' in data:
            selected=data['reasoning']
            if goal.get('role_routes') and isinstance(selected,dict):
                if set(selected)-{'Planner','Builder'}:raise ValueError('Unknown reasoning role.')
                values['reasoning']=self.reasoning_values({role:value for role,value in selected.items() if value in ('minimal','low','medium','high','xhigh')})
            else:values['reasoning']=self.reasoning_values(selected)
            if goal.get('role_routes') or 'role_routes' in values:
                routes=values.get('role_routes',self.effective_routes(goal))
                for role in ('Planner','Builder'):
                    _,model=self.model_registry.model(routes[role]['provider'],routes[role]['model'])
                    routes[role]={**routes[role],'reasoning_effort':selected.get(role,routes[role].get('reasoning_effort')) if model['capabilities']['reasoning_efforts'] else None}
                values['role_routes']=self.validate_routes(routes)
        if 'role_routes' in values:
            values['reasoning']={**goal.get('reasoning',{}),**{role:route['reasoning_effort'] for role,route in values['role_routes'].items() if role in ('Planner','Builder') and route and route.get('reasoning_effort') in ('minimal','low','medium','high','xhigh')}}
        if 'budgets' in data:
            supplied=data['budgets']
            defaults=self.ledger.budgets(goal['budgets'])
            if not isinstance(supplied,dict) or set(supplied)-set(defaults):raise ValueError('Choose valid budget providers.')
            merged={key:{**value,**supplied.get(key,{})} if isinstance(supplied.get(key,{}),dict) else None for key,value in defaults.items()}
            values['budgets']=self.ledger.budgets(merged)
        if 'policy' in data:
            if not isinstance(data['policy'],dict):raise ValueError('Policy must be an object.')
            values['policy']=policy({**goal['policy'],**data['policy']})
        if 'deadline' in data:values.update(deadline(data['deadline'],values.get('timezone',goal['timezone']),time.time()))
        return {key:value for key,value in values.items() if goal.get(key)!=value}

    def update(self,goal_id,data):
        if 'unblock_item' in data or 'rereview_item' in data or 'retry_review_item' in data:
            return self.update_item(goal_id,data)
        with self.lock:
            goal=self.store.get('goal',goal_id)
            pending=goal.get('pending_settings')
            effective={**goal,**(pending or {}).get('values',{})}
            values=self.validated_settings(effective,data)
            if not values:
                if pending and goal['status'] not in ('planning','running'):
                    self.apply_settings(goal,pending['values'],pending['change_id'])
                return self.detail(goal_id)
            combined={**(pending or {}).get('values',{}),**values}
            busy=bool(self.long_commands.active(goal_id)) or any(self.store.get('task',tid).get('goal_id')==goal_id for tid in list(self.builders.active))
            worker=self.threads.get(goal_id)
            defer=goal['status'] in ('planning','running') and (busy or worker is not None and worker.is_alive())
            change=self.store.save('goal_setting_change',{'goal_id':goal_id,'created':time.time(),**self.times(goal),
                'state':'pending' if defer else 'applied','before':{k:goal.get(k) for k in combined},'after':combined})
            if pending:self.store.update('goal_setting_change',pending['change_id'],state='superseded',superseded_by=change['id'])
            if defer:self.save(goal,pending_settings={'change_id':change['id'],'values':combined})
            else:self.apply_settings(goal,combined,change['id'])
            return self.detail(goal_id)

    def apply_settings(self,goal,values,change_id):
        self.save(goal,**values,pending_settings=None)
        if 'reasoning' in values:self.store.save('settings',{'id':'goal_defaults','reasoning':values['reasoning']})
        goal=self.store.get('goal',goal['id'])
        self.store.update('goal_setting_change',change_id,state='applied',applied=time.time(),**self.times(goal))
        if 'human_display_language' in values:self.backfill_human_briefs(goal['id'],language_changed=True)
        self.reassess_approvals(goal,change_id)
        if 'max_plan_items' in values:self.release_capacity_waiters(goal)
        if goal['status']=='waiting_decision':self.wake_decisions(goal['id'])

    def apply_pending_settings(self,goal_id):
        with self.lock:
            goal=self.store.get('goal',goal_id);pending=goal.get('pending_settings')
            if not pending:return False
            if self.long_commands.active(goal_id):return False
            for tid in list(self.builders.active):
                task=self.store.get('task',tid)
                if task.get('goal_id')==goal_id and task['status']!='waiting_approval':return False
            self.apply_settings(goal,pending['values'],pending['change_id'])
            return True

    def approval_assessment(self,goal,kind,payload):
        if kind=='command':
            return command_assessment(goal['workspace'],self.store.root,payload['argv'],payload.get('cwd','.'),goal['policy'])
        if kind!='gpu':return {'auto':False,'reason':'A human answer is required.'}
        if goal['policy']['mode']!='autonomous':return {'auto':False,'reason':'Manual policy requires individual approval.'}
        spec=payload['spec'];cost=self.gpu_estimate(spec)
        if spec['gpu'] not in goal['policy']['gpus']:return {'auto':False,'reason':'The GPU is not in this Goal\'s automatic approval list.'}
        if cost>goal['policy']['gpu_per_job']:return {'auto':False,'reason':'Estimated GPU cost exceeds the automatic per-job limit.'}
        try:self.ledger.check_reservation(goal,'modal','development',cost,gpu=spec['gpu'])
        except ValueError:return {'auto':False,'reason':'Estimated GPU cost exceeds a Goal or platform total/daily budget.'}
        return {'auto':True,'reason':'GPU and estimated cost match the autonomous policy.'}

    def reassess_approvals(self,goal,change_id):
        for request in self.approvals(goal['id']):
            try:verdict=self.approval_assessment(goal,request['kind'],request['payload'])
            except (ValueError,PermissionError,OSError,KeyError,TypeError):verdict={'auto':False,'reason':'The command is prohibited or no longer a valid workspace command.'}
            slot=self.builders.approvals.get(request['id'])
            active=goal['status'] in ('planning','running','waiting_approval','waiting_decision') and time.time()<goal['deadline_epoch']
            if verdict['auto'] and slot and active:
                self.store.update('goal_approval',request['id'],state='approved',automatic=True,settings_change_id=change_id,
                                  auto_approval_reason=verdict['reason'],**self.times(goal))
                if request.get('decision_id'):
                    self.store.update('decision',request['decision_id'],state='answered',answer='Approve',answered_at=time.time(),automatic=True,**self.times(goal))
                self.wake_decisions(goal['id'])
                slot['approved']=True;slot['event'].set()
            else:
                reason=verdict['reason'] if not verdict['auto'] else 'Goal or approval worker is inactive; resume explicitly to request it again.'
                self.store.update('goal_approval',request['id'],auto_approval_reason=redact(reason),settings_change_id=change_id)
