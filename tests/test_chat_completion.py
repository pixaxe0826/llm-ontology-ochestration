"""Baseten termination/length handling, without paid calls."""
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch

from workbench.agent import Agents
from workbench.gpu import GPUManager
from workbench.providers import Providers, BASETEN_MODEL
from workbench.store import Store


class ChatCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state')
    def tearDown(self):
        self.store.db.close();self.temp.cleanup()
    def chat(self,chunks):
        task=self.store.save('task',{'provider':'baseten','model':BASETEN_MODEL,'workspace':'','mode':'chat','status':'running','messages':[], 'events':[]})
        provider=NS(stream_chat=lambda *args:iter(chunks))
        agent=Agents(self.store,provider,GPUManager(self.store))
        agent.cancels[task['id']]=threading.Event()
        return agent,task
    def test_stop_and_usage_complete(self):
        agent,task=self.chat([{'delta':'Complete','finish_reason':None},{'delta':'','finish_reason':'stop'},{'usage':{'prompt_tokens':3,'completion_tokens':1}}])
        agent._chat(task['id'],[],[])
        saved=self.store.get('task',task['id'])
        self.assertEqual(saved['status'],'succeeded')
        self.assertEqual(saved['events'][0]['finish_reason'],'stop')
        self.assertEqual(saved['events'][1]['kind'],'usage')
    def test_length_after_three_continuations_is_interrupted(self):
        agent,task=self.chat([{'delta':'Partial answer','finish_reason':'length'}])
        agent._chat(task['id'],[],[])
        saved=self.store.get('task',task['id'])
        self.assertEqual(saved['status'],'interrupted')
        self.assertEqual(saved['events'][0]['message_status'],'interrupted')
        self.assertEqual(saved['events'][0]['text'],'Partial answer'*4)
        self.assertEqual(saved['events'][0]['continuations'],3)
        self.assertEqual(len([e for e in saved['events'] if e['kind']=='usage']),4)
    def test_length_then_stop_joins_one_answer_and_records_each_usage(self):
        agent,task=self.chat([])
        responses=iter([[{'delta':'first ','finish_reason':'length','usage':{'prompt_tokens':2,'completion_tokens':3}}],
                        [{'delta':'last','finish_reason':'stop','usage':{'prompt_tokens':4,'completion_tokens':5}}]])
        calls=[]
        def stream(*args):calls.append(args[1]);return iter(next(responses))
        agent.providers.stream_chat=stream
        agent._chat(task['id'],[],[])
        saved=self.store.get('task',task['id'])
        self.assertEqual(saved['status'],'succeeded')
        self.assertEqual(saved['events'][0]['text'],'first last')
        self.assertEqual(saved['messages'][-1]['content'],'first last')
        self.assertEqual(len([e for e in saved['events'] if e['kind']=='assistant']),1)
        self.assertEqual(sum(e['usage']['completion_tokens'] for e in saved['events'] if e['kind']=='usage'),8)
        self.assertEqual(calls[1][-2]['content'],'first ')
    def test_missing_finish_is_not_promoted_to_success(self):
        agent,task=self.chat([{'delta':'Partial','finish_reason':None}])
        with self.assertRaisesRegex(ValueError,'종료 신호'):
            agent._chat(task['id'],[],[])
        self.assertEqual(self.store.get('task',task['id'])['events'][0]['message_status'],'failed')
    def test_baseten_uses_documented_token_limit(self):
        provider=Providers(self.store)
        client=MagicMock();client.__enter__.return_value=client
        stream=MagicMock();stream.__enter__.return_value=[]
        client.chat.completions.create.return_value=stream
        with patch.object(provider,'client',return_value=client):
            list(provider.stream_chat('baseten',[{'role':'user','content':'test'}],BASETEN_MODEL))
        options=client.chat.completions.create.call_args.kwargs
        self.assertEqual(options['model'],BASETEN_MODEL)
        self.assertEqual(options['max_tokens'],16000)
        self.assertEqual(options['reasoning_effort'],'high')
        self.assertNotIn('max_completion_tokens',options)
        self.assertTrue(options['stream_options']['include_usage'])


if __name__=='__main__':unittest.main()
