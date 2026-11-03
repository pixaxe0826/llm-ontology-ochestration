"""Resolve role routes once per request/chunk; product routing remains independent."""
import copy,time
from .provider_registry import ROLES
from .generation import settings as generation_settings


class GoalRouting:
    def route_catalog(self,provider=None):
        return [{'provider':p['id'],'model':m['id'],'roles':m['roles'],'capabilities':m['capabilities'],'prices':m['effective_prices'],'price_estimated':m['price_estimated']}
            for p in self.model_registry.public()['providers'] if provider is None or p['id']==provider for m in p['models']]

    def bind_legacy_routes(self):
        try:saved=self.store.get('settings','goal_route_anchors')
        except ValueError:saved={'id':'goal_route_anchors','goals':{}}
        changed=False
        for goal in self.store.items('goal',None):
            if goal.get('role_routes') or goal['id'] in saved['goals']:continue
            saved['goals'][goal['id']]=self.legacy_routes(goal);changed=True
        if changed:self.store.save('settings',saved)
        self.legacy_routes_by_goal=saved['goals']

    def legacy_routes(self,goal):
        target=self.muse_routes.target()
        builder={'provider':'meta','model':goal['builder_model'],'reasoning_effort':goal.get('reasoning',{}).get('Builder','high')}
        from .providers import BASETEN_MODEL
        return {'Planner':{'provider':target.get('provider','baseten'),'model':target.get('model',BASETEN_MODEL),'reasoning_effort':goal.get('reasoning',{}).get('Planner','high')},
            'Builder':builder,'ImageAnalysis':copy.deepcopy(builder),'ReferenceSearch':copy.deepcopy(builder)}

    def effective_routes(self,goal):
        return copy.deepcopy(goal.get('role_routes') or getattr(self,'legacy_routes_by_goal',{}).get(goal['id']) or self.legacy_routes(goal))

    def validate_routes(self,value,current=None):
        if not isinstance(value,dict) or set(value)-set(ROLES):raise ValueError('Role routes must name Planner, Builder, ImageAnalysis or ReferenceSearch.')
        merged={**(current or {}),**value}
        if not all(role in merged for role in ('Planner','Builder')):raise ValueError('Planner and Builder routes are required.')
        return {role:self.model_registry.validate_route(merged.get(role),role,optional=role in ('ImageAnalysis','ReferenceSearch')) for role in ROLES}

    def model_route(self,goal,role,item=None,override=None):
        routes=self.effective_routes(goal);value=override or ((item or {}).get('builder_route') if role=='Builder' else None) or routes.get(role)
        if value is None:raise ValueError(role+' is disabled: select a model with the required capability in Goal settings.')
        provider,model=self.model_registry.model(value['provider'],value['model']);cap=model['capabilities']
        effort=value.get('reasoning_effort',goal.get('reasoning',{}).get(role,goal.get('reasoning',{}).get('Builder','high')))
        if not goal.get('role_routes') and override is None:effort=goal.get('reasoning',{}).get(role,goal.get('reasoning',{}).get('Builder','high'))
        if not cap['reasoning_efforts']:effort=None
        elif effort not in cap['reasoning_efforts']:raise ValueError('Selected reasoning effort is not registered for this model.')
        return {'provider':provider['id'],'model':model['id'],'reasoning_effort':effort,'capabilities':cap,
            'budget_provider':provider.get('budget_provider',provider['id']),'route':'modal' if provider['id']=='modal_llm' else provider['id'],
            'price_estimated':model.get('prices') is None,'provider_revision':provider.get('revision',0),'api_style':provider['api_style']}

    def route_limit(self,route,role,requested=None):
        config=generation_settings();effort=route['reasoning_effort'];cap=route['capabilities']['max_output']
        base=config['reasoning_output_limits'].get(effort,16000 if role=='Planner' else 8000)
        return max(1,min(cap,base,int(requested) if requested is not None else base))

    def routed_complete(self,route,messages,tools,limit,timeout):
        # Preserve the existing injectable transport seam used by offline tests/extensions.
        method=self.providers.complete if 'complete' in vars(self.providers) else getattr(self.providers,'goal_complete',None) or self.providers.complete
        return method(route['provider'],messages,route['model'],tools,limit,timeout_seconds=timeout,reasoning_effort=route['reasoning_effort'])

    def update_item_route(self,goal_id,data):
        if not isinstance(data,dict) or set(data)-{'goal_id','plan_item_id','builder_route'}:raise ValueError('Invalid item route fields.')
        with self.lock:
            goal=self.store.get('goal',goal_id);item=self.store.get('plan_item',data.get('plan_item_id'))
            if item['goal_id']!=goal_id or item['status']=='superseded':raise ValueError('Choose a current item in this Goal.')
            route=self.model_registry.validate_route(data.get('builder_route'),'Builder',optional=True)
            active=item.get('task_id') in self.builders.active
            audit=self.store.save('goal_item_route_change',{'goal_id':goal_id,'plan_item_id':item['id'],'before':item.get('builder_route'),'after':route,
                'state':'pending' if active else 'applied','created':time.time(),**self.times(goal)})
            self.store.update('plan_item',item['id'],**({'pending_builder_route':{'route':route,'change_id':audit['id']}} if active else {'builder_route':route,'pending_builder_route':None}))
            return self.detail(goal_id)

    def apply_item_routes(self,goal_id):
        with self.lock:
            for item in self.items(goal_id):
                pending=item.get('pending_builder_route')
                if not pending or item.get('task_id') in self.builders.active:continue
                route=self.model_registry.validate_route(pending['route'],'Builder',optional=True)
                self.store.update('plan_item',item['id'],builder_route=route,pending_builder_route=None)
                self.store.update('goal_item_route_change',pending['change_id'],state='applied',applied_at=time.time())
