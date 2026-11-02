"""Coordinator replay, optional response fields and redacted local diagnostics; offline."""
import copy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from test_goals import GoalTests, response, until
from workbench.diagnostics import log_exception, exception_summary
from workbench.generation import complete_continuing
from workbench.providers import BASETEN_MODEL
from workbench.response_data import usage_dict
from workbench.security import KEY_NAMES, contains_key, register_secret


class CoordinatorTests(GoalTests):
    def item(self, goal):
        self.manager.add_items(goal,[{'key':'inspect','parent':None,'title':'Inspect existing workspace',
            'acceptance':['A real passing test is recorded.']}])
        return self.manager.items(goal['id'])[0]

    def test_saved_event_shape_memory_search_list_does_not_crash_collection(self):
        goal=self.create(start=False);item=self.item(goal)
        events=[{'kind':'tool_result','name':'read_file','result':{'path':'spec/irm1.yaml','content':'draft','sha256':'sample'}},
                {'kind':'tool_result','name':'memory_search','result':[{'chat_id':'source','excerpt':'prior decision'}]},
                {'kind':'tool_result','name':'search_files','result':[{'path':'spec/irm1.yaml','line':1,'text':'draft'}]},
                {'kind':'tool_result','name':'memory_search','result':None}]
        task=self.store.save('task',{'goal_id':goal['id'],'events':events})
        refs,fresh=self.manager.collect_evidence(item,task)
        self.assertEqual([r['kind'] for r in refs],['read_file','search_files'])
        self.assertEqual(len(fresh),2)
        self.assertEqual(self.store.get('task',task['id'])['events'],events)
        self.assertEqual(self.manager.collect_evidence(self.store.get('plan_item',item['id']),task)[1],[])

    def test_invalid_or_denied_tool_results_never_become_success_evidence(self):
        goal=self.create(start=False);item=self.item(goal)
        task={'id':'offline','events':[{'kind':'tool_result','name':name,'result':result}
             for name,result in [('read_file',None),('run_command',[]),('read_file','a claim'),
                                 ('run_command',{'exit_code':1}),('read_file',{'error':'missing'})]]}
        refs,fresh=self.manager.collect_evidence(item,task)
        self.assertEqual(len(refs),1);self.assertEqual(len(fresh),1)
        self.assertEqual(refs[0]['id'],'offline:3')
        self.assertEqual(refs[0]['execution']['exit_code'],1)
        self.assertFalse(refs[0]['supports_acceptance'])
        self.assertEqual(self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])),{})

    def test_first_builder_memory_search_with_existing_files_and_spaces_reaches_done(self):
        self.folder=self.folder/'existing workspace';(self.folder/'spec').mkdir(parents=True)
        original='draft: true\n';(self.folder/'spec/irm1.yaml').write_text(original)
        self.memory.add_note('Existing decision','Coordinator replay verification',['verification'])
        original_complete=self.providers.complete;called=[]
        def complete(provider,*args,**kwargs):
            if provider=='meta' and not called:
                called.append(True);return response(calls=[('memory_search',{'query':'Coordinator replay verification'})])
            return original_complete(provider,*args,**kwargs)
        self.providers.complete=complete
        goal=self.create();self.assertEqual(self.terminal(goal['id'])['status'],'done')
        task=self.store.get('task',self.manager.items(goal['id'])[0]['task_id'])
        searches=[e['result'] for e in task['events'] if e['kind']=='tool_result' and e['name']=='memory_search']
        self.assertTrue(searches[0]);self.assertIsInstance(searches[0],list)
        self.assertEqual((self.folder/'spec/irm1.yaml').read_text(),original)
        self.assertFalse(any(m['type']=='ESCALATE' for m in self.manager.messages(goal['id'])))

    def test_coordinator_exception_logs_trace_and_escalates_type_and_location_only(self):
        goal=self.create(start=False);self.item(goal)
        def failing_assignment(*args,**kwargs):return ['result'].get('error')
        self.manager.assign=failing_assignment;self.manager.launch(goal['id'])
        final=self.terminal(goal['id']);self.assertEqual(final['status'],'blocked')
        diagnostic=final['platform_error'];self.assertTrue(diagnostic['logged'])
        self.assertEqual(diagnostic['exception_type'],'AttributeError')
        # blocked is persisted before the asynchronous ESCALATE save.
        message=until(lambda:next((m for m in self.manager.messages(goal['id']) if m['type']=='ESCALATE'),None))
        self.assertEqual(message['type'],'ESCALATE');self.assertEqual(message['from'],'System')
        self.assertIn('failing_assignment',message['body']);self.assertIn('AttributeError',message['body'])
        self.assertNotIn("'list' object",message['body'])
        record=json.loads((self.store.root/'diagnostics/exceptions.jsonl').read_text())
        self.assertEqual(record['goal_id'],goal['id']);self.assertEqual(record['attribute'],'get')
        self.assertIn("'list' object has no attribute 'get'",record['traceback'])
        self.assertGreater(record['line'],0);self.assertTrue(record['file'].endswith('test_goal_coordinator.py'))

    def test_diagnostic_redacts_every_registered_key_and_never_captures_locals(self):
        secrets={name:'synthetic-private-value-'+str(i)+'-000117' for i,name in enumerate(KEY_NAMES)}
        ephemeral='synthetic-ephemeral-"quoted"-\\secret-000117';register_secret(ephemeral)
        with patch.dict(os.environ,secrets):
            try:raise RuntimeError(' '.join([*secrets.values(),ephemeral]))
            except RuntimeError as error:diagnostic=log_exception(self.store.root,error,component='Coordinator',goal_id='offline')
            text=(self.store.root/'diagnostics/exceptions.jsonl').read_text()
            self.assertFalse(contains_key(text))
            for secret in [*secrets.values(),ephemeral]:self.assertNotIn(secret,text);self.assertNotIn(secret,exception_summary(diagnostic))
        record=json.loads(text)
        self.assertNotIn('locals',record);self.assertIn('[REDACTED]',record['traceback'])

    def test_diagnostic_io_failure_preserves_safe_error_reporting(self):
        try:raise AttributeError('private exception text')
        except AttributeError as error:
            with patch.object(Path,'open',side_effect=PermissionError('blocked')):
                diagnostic=log_exception(self.store.root,error,component='Coordinator')
        self.assertFalse(diagnostic['logged']);self.assertIn('could not be written',exception_summary(diagnostic))
        self.assertNotIn('private exception text',exception_summary(diagnostic))

    def test_meta_baseten_usage_details_absent_null_and_sdk_objects(self):
        variants=[{'prompt_tokens':100,'completion_tokens':20},
                  {'prompt_tokens':100,'completion_tokens':20,'prompt_tokens_details':None,'completion_tokens_details':None},
                  NS(prompt_tokens=100,completion_tokens=20,prompt_tokens_details=NS(cached_tokens=None),completion_tokens_details=NS(reasoning_tokens=None))]
        for provider,role,model in [('meta','Builder','muse-spark-1.3'),('baseten','Planner',BASETEN_MODEL)]:
            for usage in variants:
                with self.subTest(provider=provider,usage_type=type(usage).__name__):
                    goal=self.create(start=False)
                    self.providers.complete=lambda *a,**k:NS(choices=[NS(message=NS(content='Complete'),finish_reason='stop')],usage=usage)
                    result=self.manager.call(goal,provider,'development',role,[{'role':'user','content':'check'}],model=model)
                    self.assertEqual(result.usage.model_dump()['completion_tokens'],20)
                    row=self.manager.ledger.rows(goal['id'])[0]
                    self.assertEqual(row['cached_input_tokens'],0);self.assertFalse(row['reasoning_tokens_known'])
                    self.assertAlmostEqual(row['estimated_usd'],(100*1.25+20*4.25)/1_000_000)
                    reserved=self.manager.ledger.reserve(goal,provider,'development',.01,model=model)
                    direct=self.manager.ledger.settle(reserved,usage=usage)
                    self.assertEqual(direct['estimated_usd'],row['estimated_usd'])

    def test_missing_usage_totals_retain_conservative_cost_and_allow_response(self):
        for usage in (None,{}, {'prompt_tokens':None,'completion_tokens':None}):
            with self.subTest(usage=usage):
                goal=self.create(start=False)
                self.providers.complete=lambda *a,**k:NS(choices=[NS(message=NS(content='Complete',tool_calls=None,model_extra=None),finish_reason='stop')],usage=usage)
                result=self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'check'}],model=BASETEN_MODEL)
                self.assertEqual(result.choices[0].message.content,'Complete')
                row=self.manager.ledger.rows(goal['id'])[0]
                self.assertTrue(row['usage_estimated']);self.assertEqual(row['estimated_usd'],row['reserved_usd'])

    def test_builder_missing_tool_calls_and_reasoning_fields_is_safe(self):
        for fields in ({},{'tool_calls':None,'model_extra':None,'reasoning_content':None}):
            goal=self.create(start=False);item=self.item(goal)
            self.providers.complete=lambda *a,**k:NS(choices=[NS(message=NS(content='Summary',**fields),finish_reason='stop')],usage=None)
            self.manager.assign(goal,item)
            saved=self.store.get('plan_item',item['id']);until(lambda:saved['task_id'] not in self.manager.builders.active)
            task=self.store.get('task',saved['task_id']);self.assertEqual(task['status'],'succeeded',task.get('error'))
            self.assertNotIn('reasoning_content',task['messages'][-1])

    def test_length_continuations_with_null_usage_details_are_all_metered(self):
        goal=self.create(start=False);replies=iter([response('A',finish='length'),response('B')])
        def complete(*args,**kwargs):
            reply=next(replies);reply.usage={'prompt_tokens':10,'completion_tokens':5,'prompt_tokens_details':None,'completion_tokens_details':None}
            return reply
        self.providers.complete=complete
        result=self.manager.call(goal,'baseten','development','Planner',[{'role':'user','content':'check'}],model=BASETEN_MODEL)
        self.assertEqual(result.choices[0].message.content,'AB');self.assertEqual(result.usage.model_dump()['completion_tokens'],10)
        self.assertEqual(len(self.manager.ledger.rows(goal['id'])),2)

    def test_resume_same_item_keeps_plan_messages_tasks_and_ledger(self):
        goal=self.create(start=False);item=self.item(goal)
        for key,status in [('prior-paused','paused'),('prior-cancelled','cancelled')]:
            self.store.save('task',{'id':key,'goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),
                'status':status,'events':[{'kind':'tool_result','name':'memory_search','result':[{'chat_id':'prior'}]}]})
        self.store.update('plan_item',item['id'],status='in_progress',task_id='prior-cancelled',task_ids=['prior-paused','prior-cancelled'],bundles=2,attempts=2)
        message=self.manager.message(goal,'System','Human','ESCALATE','Prior platform exception.')
        ledger=self.store.save('ledger',{'goal_id':goal['id'],'provider':'baseten','purpose':'development','model':BASETEN_MODEL,'created':0,'state':'settled','estimated_usd':.01})
        old_tasks={key:self.store.get('task',key) for key in ['prior-paused','prior-cancelled']}
        self.manager.save(goal,status='paused')
        self.manager.control(goal['id'],'resume')
        until(lambda:self.store.get('goal',goal['id'])['status']=='waiting_decision')
        # Explicit resume runs independent work; an outstanding ESCALATE still needs an answer.
        decision=self.manager.pending_decisions(goal['id'])[0]
        self.manager.answer_decision(goal['id'],{'decision_id':decision['id'],'answer':'The platform fix is applied; continue.'})
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
        self.assertEqual([i['id'] for i in self.manager.items(goal['id'])],[item['id']])
        self.assertEqual(self.manager.items(goal['id'])[0]['bundles'],3)
        self.assertEqual(self.store.get('message',message['id']),message);self.assertEqual(self.store.get('ledger',ledger['id']),ledger)
        for key,old in old_tasks.items():self.assertEqual(self.store.get('task',key),old)


for name in dir(GoalTests):
    if name.startswith('test_') and name not in CoordinatorTests.__dict__:setattr(CoordinatorTests,name,None)
del GoalTests

if __name__=='__main__':unittest.main()
