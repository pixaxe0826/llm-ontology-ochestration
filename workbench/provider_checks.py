"""Explicit, priced connection tests. A text probe never certifies tools or vision."""
import time
from .goal_costs import GoalLimit
from .model_retry import failure_usage
from .response_data import usage_dict


class ProviderChecks:
    def __init__(self,manager):self.manager=manager

    def status(self):
        m=self.manager;public=m.model_registry.public();rows=m.ledger.rows()
        for provider in public['providers']:
            selected=[r for r in rows if r.get('provider_id',('modal_llm' if r.get('provider')=='modal' and r.get('workload')=='llm' else r.get('provider')))==provider['id'] and r['state']!='released']
            provider['spend']={'estimated_usd':sum(r.get('estimated_usd',0) for r in selected),'reserved_usd':sum(r.get('reserved_usd',0) for r in selected if r['state']=='reserved')}
        return public

    def test(self,data):
        if not isinstance(data,dict) or set(data)-{'provider','model'}:raise ValueError('Connection test requires provider/model only.')
        m=self.manager;provider,model=m.model_registry.model(data.get('provider'),data.get('model'))
        route={'provider':provider['id'],'model':model['id'],'reasoning_effort':None}
        budget=provider.get('budget_provider',provider['id']);budgets=m.ledger.budgets()
        budgets[budget]={**budgets[budget],'total':.05,'daily':None,**({'reserve':0} if budget=='baseten' else {}),**({'llm':.05} if budget=='modal' else {})}
        goal={'id':'provider-check:'+provider['id']+':'+str(time.time_ns()),'budgets':budgets,'timezone':'UTC'}
        messages=[{'role':'user','content':'Reply with OK. This is a short connection test.'}]
        limit=min(128,model['capabilities']['max_output']);estimate=m.ledger.estimate_call(budget,model['id'],messages,max_tokens=limit)
        reservation=m.ledger.reserve(goal,budget,'development',estimate,model=model['id'],role='ConnectionTest',provider_id=provider['id'],route=provider['id'],workload='llm',price_estimated=model.get('prices') is None)
        try:
            response=(m.providers.goal_complete(provider['id'],messages,model['id'],None,limit,timeout_seconds=30) if callable(getattr(m.providers,'goal_complete',None)) else m.providers.complete(provider['id'],messages,model['id'],None,limit))
            row=m.ledger.settle(reservation,usage_dict(getattr(response,'usage',None)))
            return {'connected':True,'provider':provider['id'],'model':model['id'],'ledger_id':row['id'],'estimated_usd':row['estimated_usd'],
                'finish_reason':response.choices[0].finish_reason,'capability_verification':'NOT VERIFIED: text connectivity only; declared capabilities unchanged.'}
        except Exception as error:
            m.ledger.settle(reservation,failure_usage(error),error=type(error).__name__)
            raise
