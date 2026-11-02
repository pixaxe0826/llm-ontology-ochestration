"""Goal MVP: fake providers, real temporary files/commands, no cloud calls."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from workbench.goal_costs import GoalLimit
from workbench.goal_policy import command_decision, policy
from workbench.goal_prompts import system
from workbench.goal_time import deadline
from workbench.generation import output_limit
from workbench.goals import GoalManager
from workbench.gpu import GPUManager
from workbench.memory import Memory
from workbench.providers import BASETEN_MODEL
from workbench.security import register_secret, redact, child_environment
from workbench.store import Store


def response(text='',calls=None,finish=None,tokens=5):
    tools=[NS(id=str(i),function=NS(name=name,arguments=json.dumps(args))) for i,(name,args) in enumerate(calls or [])]
    return NS(choices=[NS(message=NS(content=text,tool_calls=tools,model_extra={}),finish_reason=finish or ('tool_calls' if tools else 'stop'))],
              usage=NS(model_dump=lambda:{'prompt_tokens':20,'completion_tokens':tokens,'prompt_tokens_details':{'cached_tokens':10}}))


def until(predicate,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=predicate()
        if value:return value
        time.sleep(.025)
    raise AssertionError('Timed out waiting for offline Goal flow.')


class FakeProviders:
    def __init__(self,items=1,rework=False,invalid=False,command=None):
        self.count=items;self.rework=rework;self.invalid=invalid;self.command=command
        self.steps={};self.calls=[];self.live=0;self.peak=0;self.lock=threading.Lock()
    def settings(self):
        return {'meta':{'ready':True,'model':'muse-spark-1.3'},'baseten':{'ready':True,'model':BASETEN_MODEL}}
    def complete(self,provider,messages,model,tools=None,max_tokens=4096,**kwargs):
        self.calls.append({'provider':provider,'messages':messages,'model':model,'limit':max_tokens,'reasoning_effort':kwargs.get('reasoning_effort'),'tools':tools})
        last=messages[-1]['content'] or ''
        if provider=='baseten':
            if 'initial plan' in last or 'previous plan JSON' in last:
                if self.invalid:return response('not json')
                return response(json.dumps({'items':[{'key':'step-'+str(i),'parent':None,'title':'Verify item '+str(i),'acceptance':['A real passing test is recorded.'],'role':'Builder'} for i in range(self.count)],'summary':'Implement and verify the bounded items.'}))
            if 'Review this REPORT' in last:
                current=next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES'))
                item=json.loads(current.split('\n',1)[1])['item']
                refs=json.loads(last.split('VALID OBSERVED EVIDENCE IDS: ',1)[1])
                return response(json.dumps({'decision':'rework' if self.rework else 'accept','defects':['Change the implementation.'] if self.rework else [],
                    'criteria':[] if self.rework else [{'criterion':c,'evidence_ids':refs[-1:]} for c in item['acceptance']],
                    'summary':'Observed verification reviewed.','next_items':[],'goal_complete':True}))
            if 'Review this Builder question' in last:return response('{"answer":"Use the existing unit test structure.","needs_human":false}')
            return response('Product response.')
        with self.lock:self.live+=1;self.peak=max(self.peak,self.live)
        try:
            time.sleep(.005)
            current=next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES'))
            item=json.loads(current.split('\n',1)[1])['item'];key=item['id'];step=self.steps.get(key,0);self.steps[key]=step+1
            filename='test_'+key+'.py'
            if step%3==0:
                sha=None
                for message in reversed(messages):
                    if message.get('role')=='tool':
                        try:
                            val=json.loads(message['content'])
                            if val.get('path')==filename:sha=val.get('sha256')
                        except (ValueError,AttributeError):pass
                # Rework has a fresh task. Obtain the existing sha through a real read first.
                if self.rework and step>=3:
                    self.steps[key]=step+1
                    return response(calls=[('read_file',{'path':filename})])
                source='import unittest\nclass TestGoal(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(2 + 2, 4)\n# attempt '+str(step)+'\n'
                return response(calls=[('write_file',{'path':filename,'content':source,'expected_sha256':sha})])
            if self.rework and step>=4 and step%3==1:
                # Each failed review must get new observed evidence, independent of stagnation detection.
                return response(calls=[('run_command',{'argv':['python','-m','unittest',filename],'cwd':'.','timeout':5})])
            if step%3==1:
                return response(calls=[('run_command',{'argv':self.command or ['python','-m','unittest',filename],'cwd':'.','timeout':5})])
            summary='Implemented the file and observed the unit test result. See '+filename+'.'
            return response(summary,calls=[('report_item',{'status':'completed','summary':summary,'verification':'PASS: observed unit test result.'})])
        finally:
            with self.lock:self.live-=1


class GoalTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.folder=self.base/'workspace';self.folder.mkdir()
        self.store=Store(self.base/'state');self.memory=Memory(self.store);self.store.on_save=self.memory.enqueue
        self.providers=FakeProviders();self.manager=GoalManager(self.store,self.providers,GPUManager(self.store),self.memory)
    def tearDown(self):
        self.manager.shutdown()
        for thread in list(self.manager.threads.values()):thread.join(timeout=3)
        until(lambda:not self.manager.builders.active)
        self.memory.pending.join();self.store.on_save=None;self.store.db.close();self.temp.cleanup()
    def create(self,start=True,**values):
        data={'title':'Offline Goal','objective':'Create and verify a small unit test.','workspace':str(self.folder),'policy':{'mode':'autonomous','gpus':[]},'deadline':time.time()+3600,**values}
        return self.manager.create(data,start)['goal']
    def terminal(self,gid):
        return until(lambda:(g if (g:=self.store.get('goal',gid))['status'] not in ('planning','running','waiting_approval') else None))
    def test_plan_assign_report_review_done_with_real_file_and_command(self):
        goal=self.create();final=self.terminal(goal['id'])
        self.assertEqual(final['status'],'done',final.get('reason'))
        item=self.manager.items(goal['id'])[0]
        self.assertEqual(item['status'],'done');self.assertTrue(item['accepted_criteria'])
        self.assertTrue(any(e['kind']=='run_command' for e in item['evidence']))
        kinds={m['type'] for m in self.manager.messages(goal['id'])}
        self.assertTrue({'ASSIGN','REPORT','REVIEW','NOTE'}<=kinds)
        self.assertTrue(self.manager.ledger.rows(goal['id']))
        self.assertIn('Spend by provider',final['final_report'])
    def test_manual_queue_does_not_block_other_items(self):
        self.providers.count=2
        goal=self.create(policy={'mode':'manual','gpus':[]})
        requests=until(lambda:(r if len(r:=self.manager.approvals(goal['id']))==2 else None))
        self.assertEqual(len({r['plan_item_id'] for r in requests}),2)
        for request in requests:self.manager.decide(goal['id'],request['id'],True)
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
        self.assertEqual(self.providers.peak,1)
    def test_rework_three_times_blocks_item_and_escalates(self):
        self.providers.rework=True
        goal=self.create();final=self.terminal(goal['id'])
        item=self.manager.items(goal['id'])[0]
        self.assertEqual(item['status'],'blocked')
        self.assertEqual(item['reworks'],3)
        self.assertTrue(any(m['type']=='ESCALATE' for m in self.manager.messages(goal['id'])))
    def test_bad_plan_schema_retries_once_then_blocks(self):
        self.providers.invalid=True;goal=self.create();final=self.terminal(goal['id'])
        self.assertEqual(final['status'],'blocked')
        self.assertEqual(len([c for c in self.providers.calls if c['provider']=='baseten']),2)
    def test_budget_exhaustion_pauses_before_provider_call(self):
        goal=self.create(budgets={'baseten':{'total':45,'reserve':45}})
        self.assertEqual(self.terminal(goal['id'])['status'],'paused')
        self.assertEqual(self.providers.calls,[])
    def test_product_reserve_is_not_available_for_development(self):
        goal=self.create(start=False,budgets={'baseten':{'total':1,'reserve':.45}})
        row=self.manager.ledger.reserve(goal,'baseten','development',.50,model=BASETEN_MODEL)
        self.manager.ledger.settle(row,actual_cost=.50)
        with self.assertRaises(GoalLimit):self.manager.ledger.reserve(goal,'baseten','development',.06,model=BASETEN_MODEL)
        self.manager.ledger.reserve(goal,'baseten','product',.10,model=BASETEN_MODEL)
    def test_autonomous_command_allowlist_and_hard_denials(self):
        (self.folder/'script.py').write_text('print(1)')
        auto=policy({'mode':'autonomous'})
        for argv in (['python','-m','pytest'],['python','-m','unittest'],['python','script.py']):
            self.assertTrue(command_decision(self.folder,self.store.root,argv,settings=auto))
        self.assertFalse(command_decision(self.folder,self.store.root,['python','-m','compileall'],settings=auto))
        for argv in (['git','push'],['python','-c','print(1)'],['powershell','Get-Content .env'],['git','-c','alias.x=!git push','x'],['python','../outside.py']):
            with self.assertRaises((PermissionError,ValueError)):command_decision(self.folder,self.store.root,argv,settings=auto)
    def test_outside_allowlist_is_queued_in_autonomous(self):
        self.providers.command=['python','-m','compileall','-q','.']
        goal=self.create();request=until(lambda:(r[0] if (r:=self.manager.approvals(goal['id'])) else None))
        self.assertEqual(request['kind'],'command')
        self.manager.decide(goal['id'],request['id'],True)
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
    def test_deadline_uses_utc_and_language_timezone_prompts(self):
        goal=self.create(start=False)
        self.assertEqual(goal['output_language'],'English');self.assertEqual(goal['timezone'],'America/New_York')
        for role in ('Planner','Builder'):
            prompt=system(goal,role)
            self.assertIn('English',prompt);self.assertIn('America/New_York',prompt)
            self.assertNotIn('Korean-speaking',prompt);self.assertIn('Write large artifacts',prompt)
        other=self.create(start=False,output_language='Korean',timezone='Asia/Seoul')
        self.assertIn('Korean',system(other,'Builder'));self.assertIn('Asia/Seoul',other['timezone'])
        self.store.update('goal',goal['id'],deadline_epoch=time.time()-1)
        with self.assertRaises(GoalLimit):self.manager.guard(goal['id'])
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')
    def test_dst_requires_explicit_ambiguous_offset(self):
        with self.assertRaisesRegex(ValueError,'ambiguous'):deadline('2026-11-01T01:30:00','America/New_York',0)
        with self.assertRaisesRegex(ValueError,'does not exist'):deadline('2026-03-08T02:30:00','America/New_York',0)
        a=deadline('2026-11-01T01:30:00-04:00','America/New_York',0)
        b=deadline('2026-11-01T01:30:00-05:00','America/New_York',0)
        self.assertEqual(b['deadline_epoch']-a['deadline_epoch'],3600)
    def test_restart_marks_interrupted_then_can_resume(self):
        goal=self.create(start=False)
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'interrupted')
        self.manager.control(goal['id'],'resume')
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
    def test_cached_tokens_and_continuations_are_all_metered(self):
        goal=self.create(start=False)
        replies=iter([response('A',finish='length',tokens=3),response('B',finish='length',tokens=4),response('C',tokens=5)])
        self.providers.complete=lambda *args,**kwargs:next(replies)
        result=self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'test'}],model=BASETEN_MODEL)
        self.assertEqual(result.choices[0].message.content,'ABC')
        rows=self.manager.ledger.rows(goal['id'])
        self.assertEqual(len(rows),3);self.assertEqual(sum(r['output_tokens'] for r in rows),12)
        self.assertEqual(sum(r['cached_input_tokens'] for r in rows),30)
    def test_output_roles_are_capped(self):
        self.assertEqual(output_limit('baseten','Planner',BASETEN_MODEL),16000)
        self.assertEqual(output_limit('meta','Builder','muse-spark-1.3'),16000)
        self.assertEqual(output_limit('baseten','Chat',BASETEN_MODEL),16000)
        self.assertLessEqual(output_limit('meta','Builder','unknown',1000000),4096)
    def test_memory_tools_add_goal_and_role_concepts(self):
        goal=self.create(start=False)
        self.manager.memory_tool(goal,'Planner','memory_note',{'title':'Decision','text':'Use the bounded unit test.','concepts':['Verification']})
        result=self.manager.memory_tool(goal,'Builder','memory_search',{'query':'bounded unit test'})
        self.assertTrue(result)
        source=self.manager.memory_tool(goal,'Builder','memory_read',{'source_id':result[0]['chat_id']})
        self.assertIn(goal['id'],source['concepts']);self.assertIn('Planner',source['concepts'])
    def test_proxy_auth_redaction_reserve_and_no_key_env(self):
        goal=self.create(start=False)
        fake_token='synthetic-goal-proxy-token-0000000001';register_secret(fake_token)
        self.manager.proxy_tokens[goal['id']]=fake_token
        with self.assertRaises(PermissionError):self.manager.proxy('wrong',{'messages':[]})
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-provider-secret-1234'}):
            self.providers.complete=lambda *args,**kwargs:response('synthetic-provider-secret-1234 '+fake_token)
            result=self.manager.proxy(fake_token,{'model':BASETEN_MODEL,'messages':[{'role':'user','content':'Product check'}]})
            self.assertNotIn('synthetic-provider-secret-1234',json.dumps(result))
            self.assertNotIn(fake_token,json.dumps(result))
            self.assertNotIn('MODEL_API_KEY',child_environment())
        rows=self.manager.ledger.rows(goal['id'])
        self.assertEqual(rows[0]['purpose'],'product');self.assertGreater(rows[0]['estimated_usd'],0)
        self.assertEqual(set(self.manager.proxy_environment(goal['id'])),{'MUSE_PROXY_BASE_URL','MUSE_PROXY_TOKEN'})

    def test_no_new_evidence_for_three_cycles_pauses(self):
        self.providers.rework=True
        # Authored claims alone cannot count as execution evidence.
        original=self.providers.complete
        def completion(provider,*args,**kwargs):
            return response('The work is complete.') if provider=='meta' else original(provider,*args,**kwargs)
        self.providers.complete=completion
        goal=self.create();final=self.terminal(goal['id'])
        self.assertEqual(final['status'],'paused')
        self.assertGreaterEqual(final['no_progress'],3)
        self.assertFalse(any(i['status']=='done' for i in self.manager.items(goal['id'])))
    def test_single_builder_gate_across_goals(self):
        a=self.create();b=self.create()
        self.assertEqual(self.terminal(a['id'])['status'],'done')
        self.assertEqual(self.terminal(b['id'])['status'],'done')
        self.assertEqual(self.providers.peak,1)
    def test_gpu_estimate_reservation_and_actual_runtime_cost(self):
        goal=self.create(start=False)
        estimate=self.manager.gpu_estimate({'gpu':'T4','timeout':120})
        row=self.manager.ledger.reserve(goal,'modal','development',estimate,gpu='T4')
        runtime=30
        final=self.manager.ledger.settle(row,actual_cost=runtime*self.manager.ledger.prices['gpu_hourly']['T4']/3600,gpu_job_id='offline-job',runtime_s=runtime)
        self.assertAlmostEqual(final['estimated_usd'],estimate/4)
        self.assertEqual(final['gpu_job_id'],'offline-job')

    def test_late_provider_response_cannot_advance_after_utc_deadline(self):
        goal=self.create(start=False)
        def late(*args,**kwargs):
            self.store.update('goal',goal['id'],deadline_epoch=time.time()-1)
            return response('Late answer')
        self.providers.complete=late
        with self.assertRaises(GoalLimit):self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'test'}],model=BASETEN_MODEL)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')
        self.assertEqual(self.manager.ledger.rows(goal['id'])[0]['state'],'settled')


class GoalHTTPTests(unittest.TestCase):
    def setUp(self):
        from workbench.server import Application,LocalHTTPServer,make_handler
        self.temp=tempfile.TemporaryDirectory();self.folder=Path(self.temp.name)/'workspace';self.folder.mkdir()
        with patch('workbench.server.refresh_keys'),patch('workbench.providers.refresh_keys'):
            self.app=Application(Path(self.temp.name)/'state')
        self.app.goals.providers=FakeProviders()
        self.goal=self.app.goals.create({'title':'Proxy test','objective':'Proxy test','workspace':str(self.folder),'deadline':time.time()+3600},start=False)['goal']
        self.token='synthetic-http-goal-proxy-token-00249';register_secret(self.token);self.app.goals.proxy_tokens[self.goal['id']]=self.token
        self.server=LocalHTTPServer(('127.0.0.1',0),make_handler(self.app));self.url='http://127.0.0.1:'+str(self.server.server_address[1])
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.app.stop()
        self.app.memory.pending.join();self.app.store.db.close();self.temp.cleanup()
    def request(self,token=None,**extra):
        from urllib.request import Request
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        return Request(self.url+'/proxy/v1/chat/completions',data=json.dumps({'model':BASETEN_MODEL,'messages':[{'role':'user','content':'Proxy check'}],**extra}).encode(),headers=headers)
    def test_no_token_and_wrong_token_are_unauthorized(self):
        from urllib.request import urlopen
        from urllib.error import HTTPError
        for token in (None,'wrong-token-0000000000000'):
            with self.assertRaises(HTTPError) as caught:urlopen(self.request(token))
            self.assertEqual(caught.exception.code,401);caught.exception.close()
    def test_proxy_records_product_usage_and_redacts(self):
        from urllib.request import urlopen
        fake_secret='synthetic-raw-provider-secret-00944'
        self.app.goals.providers.complete=lambda *args,**kwargs:response(fake_secret+' '+self.token)
        with patch.dict(os.environ,{'MODEL_API_KEY':fake_secret}):
            with urlopen(self.request(self.token)) as result:body=result.read().decode()
            self.assertNotIn(fake_secret,body);self.assertNotIn(self.token,body)
        rows=self.app.goals.ledger.rows(self.goal['id'])
        self.assertEqual(rows[0]['purpose'],'product');self.assertEqual(rows[0]['provider'],'baseten')
    def test_proxy_buffered_stream_is_sse_and_fixed_model_cannot_change(self):
        from urllib.request import urlopen
        from urllib.error import HTTPError
        with urlopen(self.request(self.token,stream=True)) as result:
            self.assertIn('text/event-stream',result.headers['Content-Type']);self.assertIn('[DONE]',result.read().decode())
        with self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,model='another-baseten-model'))
        self.assertEqual(caught.exception.code,400);caught.exception.close()
    def test_goal_control_requires_existing_local_session_token(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        request=Request(self.url+'/api/goals/control',data=json.dumps({'goal_id':self.goal['id'],'action':'stop'}).encode(),headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as caught:urlopen(request)
        self.assertEqual(caught.exception.code,403);caught.exception.close()
    def test_decision_and_dependency_endpoints_require_session_and_record_answer(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        goal=self.goal;decision=self.app.goals.create_decision(goal,{'question':'Continue?','options':['Yes','No'],'recommendation':'Yes','reversible':True})
        payload={'goal_id':goal['id'],'decision_id':decision['id'],'option':'Yes'}
        for path in ('decision','dependencies'):
            request=Request(self.url+'/api/goals/'+path,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            with self.assertRaises(HTTPError) as caught:urlopen(request)
            self.assertEqual(caught.exception.code,403);caught.exception.close()
        request=Request(self.url+'/api/goals/decision',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-Local-Token':self.app.token})
        with urlopen(request) as result:value=json.load(result)
        self.assertEqual(value['decisions'][0]['state'],'answered')


if __name__=='__main__':unittest.main()
