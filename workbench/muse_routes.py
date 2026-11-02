"""Explicit Muse Spark Open routing. Never fail over on errors or budget limits."""
import re
import time

from .generation import settings as generation_settings
from .goal_costs import number
from .providers import BASETEN_MODEL
from .security import clean_data, refresh_keys

MODAL_BASE_URL = 'https://inference.us-west.modal.direct/v1'
ENDPOINT = re.compile(r'[a-z0-9][a-z0-9-]*\.us-west\.modal\.direct')


class MuseRoutes:
    def __init__(self, store, providers, ledger):
        self.store, self.providers, self.ledger = store, providers, ledger

    def config(self):
        try: value = self.store.get('settings', 'muse_open')
        except ValueError: value = {}
        return {'route': value.get('route', 'baseten'), 'endpoint_id': value.get('endpoint_id', ''),
                'modal_llm_cap': number(value.get('modal_llm_cap', self.ledger.prices['limits']['modal'].get('llm', 30)), 'Modal LLM 상한')}

    def catalog(self):
        try: return self.store.get('settings', 'muse_open_catalog')
        except ValueError: return {'endpoints': [], 'checked_at': None}

    def discover(self):
        # IDs are chosen only from this authenticated discovery, never arbitrary URLs.
        models = self.providers.modal_models()
        endpoints = []
        for model in models:
            item = model if isinstance(model, dict) else {'id': str(model)}
            identifier = item.get('id', '')
            if isinstance(identifier, str) and ENDPOINT.fullmatch(identifier):
                endpoints.append({'id': identifier})
        return self.store.save('settings', {'id': 'muse_open_catalog', 'endpoints': sorted(endpoints, key=lambda e:e['id']),
                                           'model_ids': [str(m.get('id', '')) if isinstance(m, dict) else str(m) for m in models], 'checked_at': time.time()})

    def save(self, data):
        if not isinstance(data, dict) or set(data)-{'route', 'endpoint_id', 'modal_llm_cap'}:
            raise ValueError('Muse Spark Open 경로 설정을 확인하세요.')
        value = {**self.config(), **data}
        if value['route'] not in ('baseten', 'modal'):
            raise ValueError('경로는 baseten 또는 modal이어야 합니다.')
        value['modal_llm_cap'] = number(value['modal_llm_cap'], 'Modal LLM 상한')
        endpoint = value['endpoint_id']
        if not isinstance(endpoint, str) or (endpoint and not ENDPOINT.fullmatch(endpoint)):
            raise ValueError('조회된 Modal 엔드포인트 호스트 이름을 선택하세요.')
        if endpoint and endpoint not in {e['id'] for e in self.catalog()['endpoints']}:
            raise ValueError('현재 토큰으로 엔드포인트 목록을 먼저 조회하세요.')
        if value['route'] == 'modal' and not endpoint:
            raise ValueError('Muse Spark Open 엔드포인트 없음. Modal에서 등록하고 목록을 다시 조회하세요.')
        # The user identifies which discovered hostname serves Muse Spark Open.
        self.store.save('settings', {'id':'muse_open', **value})
        return self.status()

    def target(self, capabilities=None):
        value = self.config()
        if capabilities is None: capabilities = generation_settings().get('capabilities', {})
        if value['route'] == 'baseten':
            return {'provider':'baseten', 'budget_provider':'baseten', 'route':'baseten', 'model':BASETEN_MODEL,
                    'tools_supported':bool(capabilities.get('baseten_tools', False)),
                    'reasoning_supported':bool(capabilities.get('baseten_reasoning', False))}
        endpoint = value['endpoint_id']
        if not endpoint or not ENDPOINT.fullmatch(endpoint) or endpoint not in {e['id'] for e in self.catalog()['endpoints']}:
            raise ValueError('Muse Spark Open 엔드포인트 없음. 경로 설정에서 목록을 다시 조회하세요.')
        return {'provider':'modal_llm', 'budget_provider':'modal', 'route':'modal', 'model':endpoint,
                'tools_supported':bool(capabilities.get('modal_llm_tools', False)),
                'reasoning_supported':bool(capabilities.get('modal_llm_reasoning', False))}

    def status(self):
        import os
        refresh_keys()
        value = self.config()
        rows = [r for r in self.ledger.rows() if r['state'] != 'released']
        baseten = sum(self.ledger.amount(r) for r in rows if r['provider'] == 'baseten')
        cap = self.ledger.prices['limits']['baseten']['total']
        llm = sum(self.ledger.amount(r) for r in rows if r['provider'] == 'modal' and r.get('workload') == 'llm')
        gpu = sum(self.ledger.amount(r) for r in rows if r['provider'] == 'modal' and r.get('workload') != 'llm')
        capabilities = generation_settings().get('capabilities', {})
        return clean_data({**value, 'base_url': MODAL_BASE_URL if value['route']=='modal' else 'https://inference.baseten.co/v1',
                'modal_ready': bool(os.environ.get('MODAL_PROXY_TOKEN')), 'catalog': self.catalog(),
                'reasoning_supported':bool(capabilities.get('modal_llm_reasoning' if value['route']=='modal' else 'baseten_reasoning',False)),
                'modal_llm_committed':llm, 'modal_gpu_committed':gpu, 'baseten_committed':baseten, 'baseten_total':cap,
                'suggest_modal':value['route']=='baseten' and (baseten >= cap*.8 if cap else True),
                'automatic_fallback':False})
