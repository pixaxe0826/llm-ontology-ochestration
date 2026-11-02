"""Offline product-proxy identities, authenticated catalog and safe failures."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from test_goals import GoalHTTPTests, response
from workbench.providers import BASETEN_MODEL
from workbench.product_proxy import MUSE_ALIASES, proxy_error_message
from workbench.product_proxy import product_output
from workbench.providers import Providers
from unittest.mock import MagicMock

ENDPOINT = 'muse-spark-open-proxy-test.us-west.modal.direct'


class ProductProxyTests(GoalHTTPTests):
    def test_legacy_floor_header_and_ledger_preserve_larger_requests_up_to_model_cap(self):
        for requested,effective in ((32,1024),(1024,1024),(2048,2048),(20000,20000),(99999,24000)):
            with urlopen(self.complete(max_tokens=requested)) as result:
                body=json.load(result)
                self.assertEqual(result.headers.get('X-Muse-Proxy-Adjusted'),f'max_tokens {requested}->{effective}' if requested!=effective else None)
            self.assertEqual(self.app.goals.providers.calls[-1]['limit'],effective)
            row=self.app.goals.ledger.rows(self.goal['id'])[0]
            self.assertEqual(row['proxy_adjusted'],requested!=effective)
            self.assertEqual(row['proxy_requested_tokens'],requested);self.assertEqual(row['proxy_effective_tokens'],effective)

    def test_explicit_completion_limit_stays_strict_and_takes_precedence(self):
        with urlopen(self.complete(max_tokens=32,max_completion_tokens=32)) as result:
            self.assertIsNone(result.headers.get('X-Muse-Proxy-Adjusted'));self.assertNotIn('muse_proxy_adjustment',json.load(result))
        self.assertEqual(self.app.goals.providers.calls[-1]['limit'],32)
        self.assertFalse(self.app.goals.ledger.rows(self.goal['id'])[0]['proxy_adjusted'])

    def test_product_token_validation_and_floor_respect_small_model_cap(self):
        for value in (0,-1,True,None,'32',1.2):
            with self.assertRaises(ValueError):product_output({'max_tokens':value},BASETEN_MODEL)
        with patch('workbench.product_proxy.generation_settings',return_value={'model_caps':{BASETEN_MODEL:512},'unknown_model_cap':4096}):
            self.assertEqual(product_output({'max_tokens':32},BASETEN_MODEL)[0],512)

    def test_sdk_receives_large_explicit_product_request_without_work_role_clamp(self):
        provider=Providers(self.app.store);client=MagicMock();client.__enter__.return_value=client
        with patch.object(provider,'client',return_value=client):
            provider.complete('baseten',[{'role':'user','content':'OK'}],BASETEN_MODEL,max_tokens=20000,reasoning_effort='high',role='Product')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['max_tokens'],20000)

    def test_adjusted_stream_has_header_and_every_continuation_is_metered(self):
        count=[]
        def complete(*args,**kwargs):
            count.append(args[4]);return response('A' if len(count)==1 else 'B',finish='length' if len(count)==1 else 'stop')
        self.app.goals.providers.complete=complete
        with urlopen(self.complete(max_tokens=32,stream=True)) as result:
            self.assertEqual(result.headers['X-Muse-Proxy-Adjusted'],'max_tokens 32->1024')
            self.assertIn('[DONE]',result.read().decode())
        self.assertEqual(count,[1024,1024])
        self.assertTrue(all(r['proxy_adjusted'] for r in self.app.goals.ledger.rows(self.goal['id'])))

    def models(self, token=None, **headers):
        if token is not None:
            headers['Authorization'] = 'Bearer ' + token
        return Request(self.url+'/proxy/v1/models', headers=headers)

    def complete(self, model=..., **extra):
        body = {'messages': [{'role': 'user', 'content': 'Reply OK.'}], **extra}
        if model is not ...:
            body['model'] = model
        return Request(self.url+'/proxy/v1/chat/completions', data=json.dumps(body).encode(),
                       headers={'Authorization': 'Bearer '+self.token, 'Content-Type': 'application/json'})

    def modal(self):
        self.app.goals.muse_routes.providers.modal_models = lambda: [{'id': ENDPOINT}]
        self.app.goals.muse_routes.discover()
        self.app.goals.muse_routes.save({'route': 'modal', 'endpoint_id': ENDPOINT})

    def test_all_baseten_aliases_empty_and_omitted_use_fixed_provider_and_response_id(self):
        for requested in (*MUSE_ALIASES, '', None, '   ', ...):
            with self.subTest(model=requested), urlopen(self.complete(requested)) as result:
                body = json.load(result)
            call = self.app.goals.providers.calls[-1]
            self.assertEqual((call['provider'], call['model']), ('baseten', BASETEN_MODEL))
            self.assertEqual((body['model'], body['muse_route']), (BASETEN_MODEL, 'baseten'))
        rows = self.app.goals.ledger.rows(self.goal['id'])
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(r['purpose'] == 'product' and r['model'] == BASETEN_MODEL for r in rows))

    def test_unsupported_models_are_rejected_with_allowed_ids_and_no_echo(self):
        secret = 'synthetic-proxy-model-secret-69420'
        for model in ('muse-spark-1.3', 'other-model', 1026, True, {}, [], secret):
            with self.subTest(model=model), patch.dict(os.environ, {'BASETEN_API_KEY': secret}):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(self.complete(model))
                self.assertEqual(caught.exception.code, 400)
                body = json.load(caught.exception); caught.exception.close()
                self.assertIn('허용 모델: '+BASETEN_MODEL, body['error']['message'])
                self.assertNotIn(secret, json.dumps(body))
        self.assertEqual(self.app.goals.providers.calls, [])
        self.assertEqual(self.app.goals.ledger.rows(self.goal['id']), [])

    def test_models_requires_goal_bearer_token_not_the_local_session_token(self):
        for token in (None, '', 'wrong-token-000000000000', self.app.token):
            with self.subTest(token_present=bool(token)), self.assertRaises(HTTPError) as caught:
                urlopen(self.models(token))
            self.assertEqual(caught.exception.code, 401); caught.exception.close()
        self.assertEqual(self.app.goals.providers.calls, [])

    def test_catalog_is_openai_format_and_does_not_call_upstream_or_charge(self):
        with urlopen(self.models(self.token)) as result:
            body = json.load(result)
        self.assertEqual(body, {'object': 'list', 'data': [
            {'id': BASETEN_MODEL, 'object': 'model', 'created': 0, 'owned_by': 'baseten'}]})
        self.assertNotIn(self.token, json.dumps(body))
        self.assertEqual(self.app.goals.providers.calls, [])
        self.assertEqual(self.app.goals.ledger.rows(self.goal['id']), [])

    def test_modal_catalog_and_aliases_use_endpoint_identity_including_response(self):
        self.modal()
        def complete(provider, messages, model, *args, **kwargs):
            self.app.goals.providers.calls.append({'provider': provider, 'model': model})
            return response('OK')
        self.app.goals.providers.complete = complete
        with urlopen(self.models(self.token)) as result:
            self.assertEqual(json.load(result)['data'][0]['id'], ENDPOINT)
        for model in (ENDPOINT, 'muse-spark-1026', BASETEN_MODEL, None):
            with urlopen(self.complete(model)) as result:
                body = json.load(result)
            call = self.app.goals.providers.calls[-1]
            self.assertEqual((call['provider'], call['model']), ('modal_llm', ENDPOINT))
            self.assertEqual((body['model'], body['muse_route']), (ENDPOINT, 'modal'))
        self.assertTrue(all(r['provider'] == 'modal' and r['purpose'] == 'product'
                            for r in self.app.goals.ledger.rows(self.goal['id'])))

    def test_route_snapshot_and_actual_response_model_survive_mid_call_config_change(self):
        self.modal(); calls = []
        def complete(provider, messages, model, *args, **kwargs):
            calls.append((provider, model))
            self.app.goals.muse_routes.save({'route': 'baseten'})
            return response('A' if len(calls) == 1 else 'B', finish='length' if len(calls) == 1 else 'stop')
        self.app.goals.providers.complete = complete
        with urlopen(self.complete('muse-spark-1026')) as result:
            body = json.load(result)
        self.assertEqual(calls, [('modal_llm', ENDPOINT)]*2)
        self.assertEqual(body['model'], ENDPOINT)
        self.assertEqual(body['choices'][0]['message']['content'], 'AB')

    def test_buffered_stream_uses_actual_fixed_model_id(self):
        with urlopen(self.complete('muse-spark-open', stream=True)) as result:
            wire = result.read().decode()
        chunk = json.loads(wire.splitlines()[0][6:])
        self.assertEqual(chunk['model'], BASETEN_MODEL)
        self.assertIn('[DONE]', wire)

    def test_upstream_404_is_classified_without_raw_key_url_or_logs(self):
        secret = 'synthetic-proxy-upstream-secret-002516'
        def fail(*args, **kwargs):
            error = RuntimeError('raw '+secret+' https://private.example/'+secret)
            error.status_code = 404
            error.body = {'error': {'message': 'model not found '+secret,
                          'url': 'https://private.example/'+secret, 'code': 'model_not_found'}}
            raise error
        self.app.goals.providers.complete = fail
        logs = io.StringIO()
        with patch.dict(os.environ, {'BASETEN_API_KEY': secret}), contextlib.redirect_stderr(logs), contextlib.redirect_stdout(logs):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.complete('muse-spark-1026'))
            self.assertEqual(caught.exception.code, 400)
            body = json.load(caught.exception); caught.exception.close()
        self.assertEqual(body['error']['message'], 'upstream 404: model not found')
        combined = json.dumps(body)+logs.getvalue()+json.dumps(self.app.goals.ledger.rows(self.goal['id']))
        for value in (secret, self.token, 'https://private.example'):
            self.assertNotIn(value, combined)

    def test_upstream_categories_never_reflect_arbitrary_response_content(self):
        for status, category in ((400, 'invalid request'), (401, 'authentication failed'),
                                 (403, 'permission denied'), (429, 'quota exceeded'),
                                 (500, 'provider service error'), (504, 'request timed out')):
            error = RuntimeError('raw error must not be shown')
            error.status_code = status
            error.body = {'message': 'quota exhausted https://secret.example/raw-key'}
            self.assertEqual(proxy_error_message(error), f'upstream {status}: {category}')

    def test_models_keeps_origin_and_paused_goal_protection(self):
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.models(self.token, Origin='https://external.example'))
        self.assertEqual(caught.exception.code, 403); caught.exception.close()
        self.app.goals.control(self.goal['id'], 'pause')
        with self.assertRaises(HTTPError) as caught:
            urlopen(self.models(self.token))
        self.assertEqual(caught.exception.code, 401); caught.exception.close()
        self.assertEqual(self.app.store.get('goal', self.goal['id'])['status'], 'paused')
        self.assertEqual(self.app.goals.providers.calls, [])


# Use existing fixtures without running inherited legacy tests a second time.
for name in dir(GoalHTTPTests):
    if name.startswith('test_') and name not in ProductProxyTests.__dict__:
        setattr(ProductProxyTests, name, None)
del GoalHTTPTests, name

if __name__ == '__main__':
    unittest.main()
