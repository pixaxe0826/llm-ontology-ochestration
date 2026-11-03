"""Persistent, fail-closed cost reservations; all dollar figures are estimates."""
from datetime import datetime, timezone, timedelta
import json
import math
from pathlib import Path
from zoneinfo import ZoneInfo
import threading
import time
from .response_data import usage_dict


class GoalLimit(ValueError):
    pass


def number(value, label):
    if isinstance(value, bool):
        raise ValueError(label + "는 0 이상의 숫자여야 합니다.")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(label + "는 0 이상의 숫자여야 합니다.")
    return result


class Ledger:
    def __init__(self, store, prices=None, registry=None):
        self.store = store
        self.prices = prices or json.loads(Path(__file__).with_name('goal_prices.json').read_text(encoding='utf-8'))
        self.lock = threading.RLock()
        self.registry=registry

    def provider_limits(self):
        result={**(self.registry.budget_defaults() if self.registry else {}),**self.prices['limits']}
        if self.registry:
            try:saved=self.store.get('settings','provider_registry').get('providers',{})
            except ValueError:saved={}
            for row in saved.values():result[row.get('budget_provider',row['id'])]=row['budget']
        return result

    def limits_for(self,goal,provider):
        defaults=self.provider_limits()[provider]
        return goal.get('budgets',{}).get(provider,defaults)

    def budgets(self, supplied=None):
        supplied = supplied or {}
        result = {}
        for provider, defaults in self.provider_limits().items():
            value = supplied.get(provider, {})
            if not isinstance(value, dict):
                raise ValueError('제공자별 예산을 확인하세요.')
            result[provider] = {'total':number(value.get('total', defaults['total']), '전체 상한'),
                                'daily':None if value.get('daily', defaults['daily']) in (None, '') else number(value.get('daily',defaults['daily']), '일일 상한')}
            if provider == 'baseten':
                result[provider]['reserve'] = number(value.get('reserve', defaults['reserve']), '제품 예비분')
                if result[provider]['reserve'] > result[provider]['total']:
                    raise ValueError('Baseten 제품 예비분은 전체 상한 이하여야 합니다.')
            if provider == 'modal':
                result[provider]['llm'] = number(value.get('llm', defaults.get('llm', 30)), 'Modal LLM 상한')
        return result

    def rate(self, provider, model):
        if self.registry:
            # Registered model prices override provider fallbacks; existing custom test price tables remain authoritative for built-ins.
            try:saved=self.store.get('settings','provider_registry').get('providers',{})
            except ValueError:saved={}
            if provider not in self.prices['tokens_per_million'] and provider!='modal' or ('modal_llm' if provider=='modal' else provider) in saved:
                if (registered:=self.registry.rate(provider,model)) is not None:return registered
        if provider == 'modal': provider = 'modal_llm'
        name = 'meta_contributor' if provider == 'meta' and 'contributor' in model else provider
        if name not in self.prices['tokens_per_million']:
            raise ValueError('가격표에 없는 공급자입니다.')
        return self.prices['tokens_per_million'][name]

    def token_cost(self, provider, model, usage):
        usage=usage_dict(usage)
        if usage is None:raise ValueError('Provider usage totals are unavailable.')
        prompt = max(0, int(usage.get('prompt_tokens', usage.get('input_tokens', 0)) or 0))
        details = usage.get('prompt_tokens_details') or usage.get('input_tokens_details') or {}
        cached = min(prompt, max(0, int(details.get('cached_tokens', usage.get('cached_input_tokens', 0)) or 0)))
        output = max(0, int(usage.get('completion_tokens', usage.get('output_tokens', 0)) or 0))
        output_details=usage.get('completion_tokens_details') or usage.get('output_tokens_details') or {}
        reasoning_known=output_details.get('reasoning_tokens',usage.get('reasoning_tokens')) is not None
        reasoning_tokens=min(output,max(0,int(output_details.get('reasoning_tokens',usage.get('reasoning_tokens',0)) or 0)))
        rate = self.rate(provider, model)
        cost = ((prompt-cached)*rate['input'] + cached*rate['cached_input'] + output*rate['output']) / 1_000_000
        return {'input_tokens':prompt, 'cached_input_tokens':cached, 'output_tokens':output, 'reasoning_tokens':reasoning_tokens,'reasoning_tokens_known':reasoning_known,'estimated_usd':cost}

    def estimate_call(self, provider, model, messages, tools=None, max_tokens=4096, input_token_bound=None):
        # UTF-8 bytes + overhead is a conservative input bound, not chars/4.
        size = input_token_bound if input_token_bound is not None else len(json.dumps([messages,tools or []], ensure_ascii=False).encode('utf-8')) + 1024
        return self.token_cost(provider, model, {'prompt_tokens':size, 'completion_tokens':max_tokens})['estimated_usd']

    @staticmethod
    def day_start(name='America/New_York'):
        now = datetime.now(ZoneInfo(name))
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    @staticmethod
    def amount(row):
        return row.get('reserved_usd',0) if row['state'] == 'reserved' else row.get('estimated_usd',0)

    def rows(self, goal_id=None):
        return [r for r in self.store.items('ledger', None) if goal_id is None or r['goal_id'] == goal_id]

    def check_reservation(self, goal, provider, purpose, amount, **metadata):
        amount = number(amount, '추정 비용')
        with self.lock:
            rows = [r for r in self.rows() if r['provider'] == provider and r['state'] != 'released']
            warnings = []
            for scope, limits in [('platform', self.provider_limits()[provider]), ('goal', self.limits_for(goal,provider))]:
                scoped = rows if scope == 'platform' else [r for r in rows if r['goal_id'] == goal['id']]
                total = sum(self.amount(r) for r in scoped)
                caps = [('전체', total, limits['total'])]
                if limits.get('daily') is not None:
                    day_zone = self.prices.get('timezone','America/New_York') if scope=='platform' else goal['timezone']
                    caps.append(('일일', sum(self.amount(r) for r in scoped if r['created'] >= self.day_start(day_zone)), limits['daily']))
                if provider == 'baseten':
                    product = purpose == 'product'
                    used = sum(self.amount(r) for r in scoped if (r['purpose'] == 'product') == product)
                    cap = limits.get('reserve',0) if product else max(0, limits['total']-limits.get('reserve',0))
                    caps.append(('제품 예비분' if product else '개발 예산(제품 예비분 제외)', used, cap))
                if provider == 'modal' and metadata.get('workload') == 'llm':
                    llm_cap = limits.get('llm', 30)
                    if scope == 'platform':
                        try: llm_cap = self.store.get('settings','muse_open').get('modal_llm_cap',llm_cap)
                        except ValueError: pass
                    used = sum(self.amount(r) for r in scoped if r.get('workload') == 'llm')
                    caps.append(('LLM 사용', used, number(llm_cap, 'Modal LLM 상한')))
                for label, used, cap in caps:
                    if used >= cap or used+amount > cap+1e-12:
                        raise GoalLimit(f'{provider} {label} 상한 도달 또는 다음 호출 비용 초과: Goal을 일시정지합니다.')
                    if cap and (used+amount)/cap >= .8:
                        warnings.append(f'{provider} estimated budget warning: at least 80% of the {scope} allowance is committed.')
            return warnings

    def reserve(self, goal, provider, purpose, amount, model='', role='', **metadata):
        amount=number(amount,'추정 비용')
        with self.lock:
            warnings=self.check_reservation(goal,provider,purpose,amount,**metadata)
            if metadata.get('price_estimated'):warnings.append(provider+' price not registered; conservative estimated pricing is used.')
            return self.store.save('ledger', {'goal_id':goal['id'], 'provider':provider, 'purpose':purpose, 'role':role,
                'route':metadata.pop('route',provider), 'workload':metadata.pop('workload','gpu' if provider=='modal' else 'llm'),
                'model':model, 'created':time.time(), 'state':'reserved', 'reserved_usd':amount, 'estimated_usd':0,
                'input_tokens':0, 'cached_input_tokens':0, 'output_tokens':0,'reasoning_tokens':0, 'warnings':warnings, **metadata})

    def settle(self, reservation, usage=None, actual_cost=None, estimated_cost=None, **metadata):
        with self.lock:
            row = self.store.get('ledger',reservation['id'])
            if row['state'] != 'reserved':
                return row
            if actual_cost is not None:
                values = {'estimated_usd':number(actual_cost,'실행 비용'), 'usage_estimated':False}
            elif (normalized:=usage_dict(usage)) is not None:
                values = {**self.token_cost(row['provider'],row['model'],normalized), 'usage_estimated':normalized['usage_estimated']}
            else:
                # Missing usage or an ambiguous network failure must not create a free call.
                values = {'estimated_usd':row['reserved_usd'], 'usage_estimated':True}
            if estimated_cost is not None:values['estimated_usd']=number(estimated_cost,'토큰과 도구의 추정 비용')
            return self.store.update('ledger',row['id'],state='settled',**values,**metadata)

    def check_modal_llm(self, goal):
        """Stop immediately if settled usage reaches a cap (even if usage exceeded its reservation)."""
        with self.lock:
            rows=[r for r in self.rows() if r['provider']=='modal' and r['state']!='released']
            for scope,limits in [('platform',self.prices['limits']['modal']),('goal',goal['budgets']['modal'])]:
                selected=rows if scope=='platform' else [r for r in rows if r['goal_id']==goal['id']]
                cap=limits.get('llm',30)
                if scope=='platform':
                    try:cap=self.store.get('settings','muse_open').get('modal_llm_cap',cap)
                    except ValueError:pass
                llm=sum(self.amount(r) for r in selected if r.get('workload')=='llm')
                total=sum(self.amount(r) for r in selected)
                if llm>=cap or total>=limits['total']:
                    raise GoalLimit('modal LLM/전체 상한 도달: Goal을 일시정지합니다.')

    def summary(self, goal):
        rows = self.rows(goal['id'])
        result = {}
        for provider, limits in goal['budgets'].items():
            selected = [r for r in rows if r['provider'] == provider and r['state'] != 'released']
            spent = sum(r.get('estimated_usd',0) for r in selected)
            reserved = sum(r.get('reserved_usd',0) for r in selected if r['state'] == 'reserved')
            result[provider] = {**limits,'spent':spent,'reserved':reserved,'ratio':(spent+reserved)/limits['total'] if limits['total'] else 1,
                'reasoning_tokens':sum(r.get('reasoning_tokens',0) for r in selected),
                'reasoning_tokens_known':any(r.get('reasoning_tokens_known',False) for r in selected),
                'product_spent':sum(r.get('estimated_usd',0) for r in selected if r['purpose']=='product'),
                'reference_search_count':sum(r.get('search_count') or 0 for r in selected if r.get('reference_tool')=='reference_search'),
                'reference_search_estimated_usd':sum(r.get('search_estimated_usd',0) for r in selected),
                'warnings':sorted({w for r in selected for w in r.get('warnings',[])})}
            if provider == 'modal':
                llm = [r for r in selected if r.get('workload')=='llm']
                result[provider].update(llm=limits.get('llm',30),
                    llm_spent=sum(r.get('estimated_usd',0) for r in llm),
                    llm_reserved=sum(r.get('reserved_usd',0) for r in llm if r['state']=='reserved'),
                    gpu_spent=sum(r.get('estimated_usd',0) for r in selected if r.get('workload')!='llm'))
        return result
