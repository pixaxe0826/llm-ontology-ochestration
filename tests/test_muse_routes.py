"""Offline route selection, shared budgets and credential boundaries."""
import contextlib
import io
import json
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

from test_goals import GoalTests, GoalHTTPTests, response
from workbench.goal_costs import GoalLimit
from workbench.providers import Providers, BASETEN_MODEL
from workbench.security import register_secret

ENDPOINT = 'muse-spark-open-test.us-west.modal.direct'


class MuseRouteTests(GoalTests):
    def modal(self, cap=30):
        self.providers.modal_models=lambda:[{'id':ENDPOINT}]
        self.manager.muse_routes.discover()
        return self.manager.muse_routes.save({'route':'modal','endpoint_id':ENDPOINT,'modal_llm_cap':cap})

    def test_default_baseten_and_explicit_switch_routes_planner_and_product(self):
        goal=self.create(start=False)
        self.assertEqual(self.manager.muse_routes.config()['route'],'baseten')
        self.manager.phase(goal,None,'Initial review.')
        self.assertEqual(self.providers.calls[-1]['provider'],'baseten')
        self.modal()
        captured=[]
        def complete(*args,**kwargs):captured.append((args,kwargs));return response('{"summary":"reviewed"}')
        self.providers.complete=complete
        self.manager.phase(goal,None,'Initial review.')
        self.assertEqual(captured[-1][0][0],'modal_llm');self.assertEqual(captured[-1][0][2],ENDPOINT)
        self.assertIsNone(captured[-1][0][3])  # Unverified native tools use validated JSON fallback.
        self.assertIsNone(captured[-1][1]['reasoning_effort'])
        token='synthetic-route-product-token-000149';register_secret(token)
        self.manager.proxy_tokens[goal['id']]=token
        result=self.manager.proxy(token,{'model':BASETEN_MODEL,'messages':[{'role':'user','content':'product'}]})
        self.assertEqual(captured[-1][0][0],'modal_llm');self.assertEqual(result['muse_route'],'modal')
        rows=self.manager.ledger.rows(goal['id'])
        self.assertEqual({r['route'] for r in rows},{'baseten','modal'})
        self.assertEqual(rows[0]['provider'],'modal');self.assertEqual(rows[0]['workload'],'llm')
        self.assertEqual(rows[0]['purpose'],'product')
        self.assertEqual(rows[0]['logical_model'],BASETEN_MODEL)

    def test_meta_and_baseten_chat_do_not_follow_goal_route(self):
        self.modal();goal=self.create(start=False)
        self.providers.complete=lambda *a,**k:response('Meta builder')
        self.manager.call(goal,'meta','development','Builder',[{'role':'user','content':'build'}],model='muse-spark-1.3')
        self.assertEqual(self.manager.ledger.rows(goal['id'])[0]['provider'],'meta')
        provider=Providers(self.store);client=MagicMock();client.__enter__.return_value=client
        client.chat.completions.create.return_value.__enter__.return_value=[]
        with patch.object(provider,'client',return_value=client) as factory:
            list(provider.stream_chat('baseten',[{'role':'user','content':'chat'}]))
        factory.assert_called_once_with('baseten')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'],BASETEN_MODEL)

    def test_modal_sdk_fixed_address_auth_and_unsupported_effort_omitted(self):
        provider=Providers(self.store);client=MagicMock();client.__enter__.return_value=client
        token='wk-synthetic-id.ws-synthetic-secret'
        with patch('workbench.providers.refresh_keys'),patch.dict(os.environ,{'MODAL_PROXY_TOKEN':token}),patch('openai.OpenAI',return_value=client) as factory:
            provider.complete('modal_llm',[{'role':'user','content':'review'}],ENDPOINT,reasoning_effort='high')
            self.assertEqual(factory.call_args.kwargs['base_url'],'https://inference.us-west.modal.direct/v1')
            self.assertEqual(factory.call_args.kwargs['api_key'],token)
        options=client.chat.completions.create.call_args.kwargs
        self.assertNotIn('reasoning_effort',options);self.assertEqual(options['max_tokens'],4096)

    def test_endpoint_requires_authenticated_catalog_and_explicit_valid_route(self):
        for data in ({'route':'automatic'},{'route':'modal'},{'route':'modal','endpoint_id':ENDPOINT},
                     {'route':'modal','endpoint_id':'https://evil.example/v1'},{'modal_llm_cap':float('nan')}):
            with self.assertRaises(ValueError):self.manager.muse_routes.save(data)
        self.modal()
        self.providers.modal_models=lambda:[]
        self.manager.muse_routes.discover()
        with self.assertRaises(ValueError):self.manager.muse_routes.target()
        self.assertEqual(self.manager.muse_routes.config()['route'],'modal')  # No fallback.

    def test_modal_llm_platform_cap_pauses_without_call_or_fallback(self):
        self.modal(cap=0);goal=self.create(start=False)
        with self.assertRaises(GoalLimit):self.manager.phase(goal,None,'Review.')
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')
        self.assertEqual(self.providers.calls,[])
        self.assertEqual(self.manager.muse_routes.config()['route'],'modal')

    def test_modal_llm_goal_cap_and_modal_total_are_both_enforced(self):
        self.modal();goal=self.create(start=False,budgets={'modal':{'total':100,'llm':0}})
        with self.assertRaises(GoalLimit):self.manager.phase(goal,None,'Review.')
        other=self.create(start=False,budgets={'modal':{'total':0,'llm':30}})
        with self.assertRaises(GoalLimit):self.manager.phase(other,None,'Review.')
        self.assertEqual(self.providers.calls,[])

    def test_unexpected_reported_usage_over_cap_pauses_immediately(self):
        self.modal(cap=.1);goal=self.create(start=False)
        self.providers.complete=lambda *a,**k:response('Reported usage exceeds reservation',tokens=30000)
        with self.assertRaises(GoalLimit):self.manager.phase(goal,None,'Review.')
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')
        self.assertGreater(self.manager.ledger.rows(goal['id'])[0]['estimated_usd'],.1)

    def test_gpu_has_separate_headroom_and_llm_spend_is_combined(self):
        self.modal();goal=self.create(start=False)
        row=self.manager.ledger.reserve(goal,'modal','development',30,model=ENDPOINT,route='modal',workload='llm')
        self.manager.ledger.settle(row,actual_cost=30)
        with self.assertRaises(GoalLimit):self.manager.ledger.reserve(goal,'modal','product',.01,model=ENDPOINT,workload='llm')
        gpu=self.manager.ledger.reserve(goal,'modal','development',70,gpu='T4')
        self.manager.ledger.settle(gpu,actual_cost=70)
        with self.assertRaises(GoalLimit):self.manager.ledger.reserve(goal,'modal','development',.01,gpu='T4')
        costs=self.manager.ledger.summary(goal)['modal']
        self.assertEqual(costs['llm_spent'],30);self.assertEqual(costs['gpu_spent'],70);self.assertEqual(costs['spent'],100)
        self.assertEqual(self.manager.muse_routes.status()['modal_llm_committed'],30)

    def test_modal_daily_cap_applies_to_llm_and_gpu_together(self):
        self.modal();goal=self.create(start=False,budgets={'modal':{'total':100,'llm':30,'daily':1}})
        row=self.manager.ledger.reserve(goal,'modal','development',.9,gpu='T4')
        self.manager.ledger.settle(row,actual_cost=.9)
        with self.assertRaises(GoalLimit):self.manager.ledger.reserve(goal,'modal','product',.11,model=ENDPOINT,workload='llm')

    def test_baseten_eighty_percent_only_suggests_and_never_changes_route(self):
        goal=self.create(start=False)
        self.store.save('ledger',{'goal_id':goal['id'],'provider':'baseten','purpose':'chat','state':'settled','estimated_usd':80,'created':0})
        status=self.manager.muse_routes.status()
        self.assertTrue(status['suggest_modal']);self.assertFalse(status['automatic_fallback'])
        self.assertEqual(self.manager.muse_routes.target()['provider'],'baseten')
        self.assertEqual(self.providers.calls,[])

    def test_continuations_snapshot_route_and_meter_all_tokens(self):
        self.modal();goal=self.create(start=False);calls=[]
        def complete(provider,*args,**kwargs):
            calls.append(provider)
            if len(calls)==1:
                self.manager.muse_routes.save({'route':'baseten'})
                return response('A',finish='length',tokens=3)
            return response('B',tokens=5)
        self.providers.complete=complete
        result=self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'review'}],model=BASETEN_MODEL)
        self.assertEqual(result.choices[0].message.content,'AB');self.assertEqual(calls,['modal_llm','modal_llm'])
        rows=self.manager.ledger.rows(goal['id']);self.assertEqual(sum(r['output_tokens'] for r in rows),8)
        self.assertTrue(all(r['provider']=='modal' and r['route']=='modal' for r in rows))
        self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'review'}],model=BASETEN_MODEL)
        self.assertEqual(calls[-1],'baseten')

    def test_concurrent_llm_reservations_cannot_exceed_subcap(self):
        self.modal(cap=1);goal=self.create(start=False);outcomes=[]
        def reserve():
            try:self.manager.ledger.reserve(goal,'modal','development',.6,model=ENDPOINT,workload='llm');outcomes.append('ok')
            except GoalLimit:outcomes.append('limited')
        workers=[threading.Thread(target=reserve) for _ in range(2)]
        for worker in workers:worker.start()
        for worker in workers:worker.join()
        self.assertCountEqual(outcomes,['ok','limited'])

    def test_modal_error_never_retries_on_baseten_and_retains_estimate(self):
        self.modal();goal=self.create(start=False);calls=[]
        def fail(provider,*args,**kwargs):calls.append(provider);raise RuntimeError('Offline network failure')
        self.providers.complete=fail
        with self.assertRaises(RuntimeError):self.manager.phase(goal,None,'Review.')
        self.assertEqual(calls,['modal_llm'])
        row=self.manager.ledger.rows(goal['id'])[0]
        self.assertEqual(row['route'],'modal');self.assertEqual(row['state'],'settled');self.assertTrue(row['usage_estimated'])
        self.assertGreater(row['estimated_usd'],0)


class MuseRouteHTTPTests(GoalHTTPTests):
    def test_modal_proxy_and_error_do_not_expose_key_in_response_or_logs(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        self.app.goals.muse_routes.providers.modal_models=lambda:[{'id':ENDPOINT}]
        self.app.goals.muse_routes.discover()
        self.app.goals.muse_routes.save({'route':'modal','endpoint_id':ENDPOINT})
        secret='wk-synthetic-http.ws-synthetic-route-secret';logs=io.StringIO()
        self.app.goals.providers.complete=lambda *a,**k:response(secret)
        with patch.dict(os.environ,{'MODAL_PROXY_TOKEN':secret}),contextlib.redirect_stderr(logs),contextlib.redirect_stdout(logs):
            with urlopen(self.request(self.token)) as result:body=result.read().decode()
            self.assertNotIn(secret,body)
            def failure():raise ValueError(secret)
            self.app.goals.muse_routes.providers.modal_models=failure
            request=Request(self.url+'/api/muse-open/models',data=b'{}',headers={'Content-Type':'application/json','X-Local-Token':self.app.token})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertNotIn(secret,caught.exception.read().decode());caught.exception.close()
        self.assertNotIn(secret,logs.getvalue())
        self.assertNotIn(secret,json.dumps(self.app.store.items('ledger',None)))
        row=self.app.goals.ledger.rows(self.goal['id'])[0];self.assertEqual(row['provider'],'modal');self.assertEqual(row['purpose'],'product')

    def test_route_mutation_keeps_session_protection(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        for path in ('/api/muse-open','/api/muse-open/models'):
            request=Request(self.url+path,data=b'{}',headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,403);caught.exception.close()


# Reuse fixtures without duplicating the already-running legacy tests.
for target, base in ((MuseRouteTests,GoalTests),(MuseRouteHTTPTests,GoalHTTPTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in target.__dict__:setattr(target,name,None)
del GoalTests, GoalHTTPTests, target, base, name

if __name__=='__main__':unittest.main()
