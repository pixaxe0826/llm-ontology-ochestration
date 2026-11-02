"""User-authoritative reasoning, provider compatibility and Planner boundaries."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock,patch

from test_goals import GoalTests,response
from workbench.generation import settings,output_limit,validate_reasoning
from workbench.providers import Providers,BASETEN_MODEL
from workbench.goal_prompts import system
from workbench.workspace import Workspace


class ReasoningPlannerTests(GoalTests):
    # Inherit the fixture only, not the base test cases (removed below).
    def test_strength_limits_and_invalid_values(self):
        expected={'minimal':8000,'low':8000,'medium':12000,'high':16000,'xhigh':24000}
        for effort,limit in expected.items():
            self.assertEqual(output_limit('meta','Builder','muse-spark-1.3',reasoning_effort=effort),limit)
            self.assertEqual(output_limit('baseten','Planner',BASETEN_MODEL,reasoning_effort=effort),limit)
        for invalid in ('max','none','HIGH','arbitrary',123):
            with self.assertRaises(ValueError):validate_reasoning(invalid)
        with self.assertRaises(ValueError):self.manager.create({'objective':'test','workspace':str(self.folder),'reasoning':{'Planner':'invalid'}})
    def test_role_selections_persist_and_never_follow_complexity_or_phase(self):
        goal=self.create(start=False,reasoning={'Planner':'low','Builder':'minimal'})
        self.assertEqual(self.manager.defaults()['reasoning'],{'Planner':'low','Builder':'minimal'})
        self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'ordinary review'}],model=BASETEN_MODEL)
        self.assertEqual(self.providers.calls[-1]['reasoning_effort'],'low')
        self.assertEqual(self.providers.calls[-1]['limit'],8000)
        self.manager.set_reasoning({'goal_id':goal['id'],'reasoning':{'Planner':'xhigh','Builder':'medium'}})
        fresh=self.store.get('goal',goal['id'])
        self.assertEqual(fresh['reasoning']['Builder'],'medium')
        self.manager.call(fresh,'baseten','development','Planner',[{'role':'user','content':'milestone review'}],model=BASETEN_MODEL)
        self.assertEqual(self.providers.calls[-1]['reasoning_effort'],'xhigh')
        self.assertEqual(self.providers.calls[-1]['limit'],24000)
    def test_provider_saves_reasoning_independently_and_keeps_baseten_locked(self):
        provider=Providers(self.store)
        with patch('workbench.providers.refresh_keys'):
            provider.save({'meta':{'reasoning_effort':'low'}})
            provider.save({'baseten':{'reasoning_effort':'xhigh'}})
            result=provider.settings()
        self.assertEqual(result['meta']['reasoning_effort'],'low');self.assertEqual(result['baseten']['reasoning_effort'],'xhigh')
        self.assertEqual(result['baseten']['model'],BASETEN_MODEL)
        self.assertEqual(result['baseten']['base_url'],'https://inference.baseten.co/v1')
        with self.assertRaises(ValueError):provider.save({'meta':{'reasoning_effort':'max'}})
    def test_selected_effort_is_sent_and_unsupported_baseten_omits_it(self):
        provider=Providers(self.store);client=MagicMock();client.__enter__.return_value=client
        with patch.object(provider,'client',return_value=client):
            provider.complete('meta',[{'role':'user','content':'test'}],'muse-spark-1.3',reasoning_effort='xhigh')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['reasoning_effort'],'xhigh')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['max_completion_tokens'],24000)
        config=copy.deepcopy(settings());config['capabilities']['baseten_reasoning']=False
        with patch('workbench.generation.settings',return_value=config),patch.object(provider,'client',return_value=client):
            provider.complete('baseten',[{'role':'user','content':'test'}],BASETEN_MODEL,reasoning_effort='high')
            self.assertFalse(provider.settings()['baseten']['reasoning_supported'])
        self.assertNotIn('reasoning_effort',client.chat.completions.create.call_args.kwargs)
    def test_planner_forbidden_tools_are_refused_before_execution(self):
        goal=self.create(start=False)
        with patch.object(Workspace,'write',side_effect=AssertionError('must not write')),patch.object(Workspace,'run',side_effect=AssertionError('must not run')):
            for name in ('write_file','run_command','submit_gpu_job'):
                with self.assertRaises(PermissionError):self.manager.planner_tool(goal,None,name,{})
        self.assertIn('Never claim an action that has no tool result.',system(goal,'Planner'))
        self.assertEqual(self.manager.items(goal['id']),[])
    def test_planner_read_file_is_scoped_recorded_and_input_evidence_is_capped(self):
        goal=self.create(start=False)
        (self.folder/'large.txt').write_text('evidence line\n'*9000)
        result=self.manager.planner_tool(goal,None,'read_file',{'path':'large.txt'})
        self.assertGreater(len(result['result']['content']),20000)
        observations=self.store.items('goal_evidence',None)
        self.assertEqual(observations[0]['result']['content'],result['result']['content'])
        messages=[{'role':'system','content':'fixed'},{'role':'user','content':'RECENT OBSERVED TOOL RESULTS\n'+'A'*30000},
                  {'role':'tool','tool_call_id':'read','content':json.dumps(result)}]
        limited=self.manager.limit_planner_evidence(messages)
        self.assertLessEqual(sum(len(m['content']) for m in limited if m['role']=='tool' or m['content'].startswith('RECENT OBSERVED TOOL RESULTS')),20000)
        self.assertEqual(limited[0],messages[0])
        captured=[]
        self.providers.complete=lambda *args,**kwargs:(captured.append(args[1]) or response('Reviewed.'))
        self.manager.call(goal,'baseten','development','Planner',messages,model=BASETEN_MODEL)
        self.assertLessEqual(sum(len(m['content']) for m in captured[0] if m['role']=='tool' or m['content'].startswith('RECENT OBSERVED TOOL RESULTS')),20000)
        with self.assertRaises(ValueError):self.manager.planner_tool(goal,None,'read_file',{'path':'../outside'})
    def test_reasoning_tokens_are_separate_but_not_double_charged(self):
        usage={'prompt_tokens':100,'completion_tokens':200,'completion_tokens_details':{'reasoning_tokens':150}}
        cost=self.manager.ledger.token_cost('meta','muse-spark-1.3',usage)
        self.assertEqual(cost['reasoning_tokens'],150)
        self.assertAlmostEqual(cost['estimated_usd'],(100*1.25+200*4.25)/1_000_000)
        self.assertTrue(cost['reasoning_tokens_known'])
    def test_native_forbidden_request_returns_a_denial_result_without_state_mutation(self):
        goal=self.create(start=False)
        replies=iter([response(calls=[('write_file',{'path':'forbidden.txt','content':'no'})]),response('{"summary":"No state mutation claimed."}')])
        calls=[]
        def complete(*args,**kwargs):calls.append(args);return next(replies)
        self.providers.complete=complete
        self.manager.phase(goal,None,'Review only.')
        self.assertFalse((self.folder/'forbidden.txt').exists())
        self.assertIn('Planner tool forbidden',calls[1][1][-1]['content'])
        names={t['function']['name'] for t in calls[0][3]}
        self.assertFalse({'write_file','run_command','submit_gpu_job'}&names)
    def test_json_fallback_retries_schema_once_then_blocks(self):
        goal=self.create(start=False);self.providers.invalid=True
        config=copy.deepcopy(settings());config['capabilities']['baseten_tools']=False
        with patch('workbench.goals.generation_settings',return_value=config):self.manager.plan(goal)
        self.assertEqual(self.store.get('goal',goal['id'])['status'],'blocked')
        self.assertEqual(len(self.providers.calls),2)
        self.assertTrue(all(c['tools'] is None for c in self.providers.calls))
    def test_static_unittest_file_criterion_does_not_require_execution(self):
        goal=self.create(start=False)
        self.manager.add_items(goal,[{'key':'file','parent':None,'title':'Create a test file','acceptance':['test_example.py exists with standard unittest cases']}])
        item=self.manager.items(goal['id'])[0]
        change=Workspace(self.folder,self.store.root).write('test_example.py','import unittest\n')
        task=self.store.save('task',{'id':'observed-file','status':'succeeded','provider':'meta','workspace':str(self.folder),'events':[{'kind':'change','change':change}]})
        self.manager.collect_evidence(item,task)
        self.manager.review(goal,self.store.get('plan_item',item['id']))
        self.assertEqual(self.store.get('plan_item',item['id'])['status'],'done')
    def test_same_test_output_after_changed_file_is_new_current_evidence(self):
        goal=self.create(start=False)
        self.manager.add_items(goal,[{'key':'file','parent':None,'title':'Verify file','acceptance':['Tests pass.']}])
        item=self.manager.items(goal['id'])[0];ws=Workspace(self.folder,self.store.root)
        first=ws.write('app.py','value = 1\n')
        def observed(key,change):
            return self.store.save('task',{'id':key,'status':'succeeded','provider':'meta','workspace':str(self.folder),'events':[{'kind':'change','change':change},{'kind':'tool_result','name':'run_command','result':{'exit_code':0,'output':'OK\n','duration_s':.1}}]})
        self.manager.collect_evidence(item,observed('first',first))
        second=ws.write('app.py','value = 2\n',first['sha256'])
        self.manager.collect_evidence(self.store.get('plan_item',item['id']),observed('second',second))
        valid=self.manager.valid_evidence(goal,self.store.get('plan_item',item['id']))
        self.assertIn('second:1',valid);self.assertNotIn('first:1',valid)


# Reuse setup/helpers while keeping only the new cases in this suite.
for name in list(dir(GoalTests)):
    if name.startswith('test_') and name not in ReasoningPlannerTests.__dict__:
        setattr(ReasoningPlannerTests,name,None)

del GoalTests

if __name__=='__main__':unittest.main()
