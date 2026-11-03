import copy,json,os,unittest
from unittest.mock import MagicMock,patch
import test_goal_decisions as fixtures
from workbench.provider_registry import ProviderRegistry,eligible,FALLBACK_PRICE
from workbench.model_adapters import ModelAdapters,AdapterError
from workbench.response_data import usage_dict


def registration(identifier='test_provider',style='chat_completions',tools=True,json_mode=True,images=True,prices=None):
    return {'id':identifier,'name':'Offline endpoint','api_style':style,'base_url':'https://models.example.test/v1',
        'models':[{'id':'test-model','capabilities':{'tool_calling':tools,'structured_json':json_mode,'image_input':images,'reasoning_content':True,
            'web_search':style=='responses','reasoning_efforts':['low','high'],'max_context':100000,'max_output':24000},'prices':prices}],
        'limits':{'concurrency':1,'requests_per_minute':None},'budget':{'total':100,'daily':None}}


def chat(text='',calls=None):
    return {'model':'test-model','choices':[{'message':{'content':text,'tool_calls':calls,'reasoning_content':'opaque reasoning state'},'finish_reason':'tool_calls' if calls else 'stop'}],
        'usage':{'prompt_tokens':50,'completion_tokens':20,'prompt_tokens_details':None,'completion_tokens_details':{'reasoning_tokens':12}}}


class RegistryTests(unittest.TestCase):
    setUp=fixtures.GoalDecisionTests.setUp;tearDown=fixtures.GoalDecisionTests.tearDown

    def test_builtins_roles_fixed_baseten_and_unknown_modal_capabilities(self):
        registry=self.manager.model_registry;baseten,model=registry.model('baseten','meta-models/muse-spark-open-1026')
        self.assertTrue(eligible(model,'Builder'));self.assertTrue(eligible(model,'Planner'));self.assertFalse(eligible(model,'ReferenceSearch'))
        self.assertEqual(baseten['base_url'],'https://inference.baseten.co/v1');self.assertEqual(registry.get('modal_llm')['models'],[])
        bad=registration('baseten');bad['models'][0]['id']='different'
        with self.assertRaises(ValueError):registry.save(bad)

    def test_registration_contains_references_only_and_key_redaction_is_dynamic(self):
        registry=self.manager.model_registry;secret='synthetic-provider-secret-123456789'
        with patch.dict(os.environ,{'MUSE_PROVIDER_TEST_PROVIDER_API_KEY':secret}):
            registry.save(registration());public=json.dumps(registry.public());self.assertNotIn(secret,public);self.assertIn('MUSE_PROVIDER_TEST_PROVIDER_API_KEY',public)
            from workbench.security import redact,contains_key,child_environment
            self.assertEqual(redact(secret),'[REDACTED]');self.assertTrue(contains_key(secret));self.assertNotIn('MUSE_PROVIDER_TEST_PROVIDER_API_KEY',child_environment())
            with self.assertRaises(ValueError):registry.save({**registration(),'key':secret})

    def test_custom_client_uses_installed_sdk_transport_without_new_dependency(self):
        from workbench.providers import Providers
        self.manager.model_registry.save(registration())
        with patch.dict(os.environ,{'MUSE_PROVIDER_TEST_PROVIDER_API_KEY':'synthetic-client-key-393514'}):
            with Providers(self.store).goal_client('test_provider') as client:
                self.assertEqual(str(client.base_url),'https://models.example.test/v1/')
                self.assertFalse(client._client.follow_redirects);self.assertFalse(client._client.trust_env)

    def test_registration_rejects_foreign_key_refs_bad_urls_and_invalid_models(self):
        registry=self.manager.model_registry
        for changes in ({'credential_ref':'BASETEN_API_KEY'},{'base_url':'http://public.example/v1'},{'base_url':'https://user:password@host/v1'},
                        {'base_url':'http://169.254.169.254/latest'},{'api_style':'execute_python'},{'limits':{'concurrency':0}},
                        {'models':[{'id':'bad','capabilities':{'tool_calling':'yes'}}]}):
            with self.subTest(changes=changes),self.assertRaises(ValueError):registry.save({**registration(),**changes})
        registry.save({**registration(),'base_url':'http://127.0.0.1:19876/v1'})

    def test_role_filtering_prices_and_reasoning_validation(self):
        registry=self.manager.model_registry;registry.save(registration(tools=False,json_mode=False,images=False))
        for role in ('Planner','Builder','ImageAnalysis','ReferenceSearch'):
            with self.assertRaises(ValueError):registry.validate_route({'provider':'test_provider','model':'test-model'},role)
        registry.save(registration(tools=False,json_mode=True))
        self.assertEqual(registry.validate_route({'provider':'test_provider','model':'test-model'},'Builder')['reasoning_effort'],'high')
        with self.assertRaises(ValueError):registry.validate_route({'provider':'test_provider','model':'test-model','reasoning_effort':'xhigh'},'Builder')
        value=next(p for p in registry.public()['providers'] if p['id']=='test_provider')['models'][0]
        self.assertTrue(value['price_estimated']);self.assertEqual(value['effective_prices']['output'],FALLBACK_PRICE['output'])

    def adapter(self,style='chat_completions',**settings):
        self.manager.model_registry.save(registration(style=style,**settings));client=MagicMock();client.__enter__.return_value=client
        return ModelAdapters(self.manager.model_registry,lambda _:client),client

    def test_chat_round_trip_tools_images_reasoning_and_missing_details(self):
        adapter,client=self.adapter();call={'id':'call-one','type':'function','function':{'name':'read_file','arguments':'{"path":"x.py"}'}}
        client.chat.completions.create.side_effect=[chat(calls=[call]),chat('done')]
        tools=[{'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{}}}}]
        messages=[{'role':'user','content':[{'type':'text','text':'inspect'},{'type':'image_url','image_url':{'url':'data:image/png;base64,eA=='}}]}]
        response=adapter.complete('test_provider',messages,'test-model',tools,24000,'high')
        self.assertEqual(response.choices[0].finish_reason,'tool_calls');self.assertEqual(response.usage['completion_tokens_details']['reasoning_tokens'],12)
        record={'role':'assistant','content':'','tool_calls':[call],'reasoning_content':response.choices[0].message.model_extra['reasoning_content']}
        adapter.complete('test_provider',[*messages,record,{'role':'tool','tool_call_id':'call-one','content':'file'}],'test-model',tools,8000,'low')
        options=client.chat.completions.create.call_args.kwargs;self.assertEqual(options['messages'][-2]['reasoning_content'],'opaque reasoning state');self.assertEqual(options['messages'][-1]['tool_call_id'],'call-one')
        self.assertEqual(options['reasoning_effort'],'low');self.assertEqual(options['max_completion_tokens'],8000)

    def test_responses_round_trip_keeps_opaque_items_images_and_usage(self):
        adapter,client=self.adapter('responses');items=[{'type':'reasoning','id':'rs_one','encrypted_content':'opaque','summary':[]},
            {'type':'function_call','call_id':'call-one','name':'read_file','arguments':'{"path":"x.py"}'}]
        client.responses.create.side_effect=[{'model':'test-model','status':'completed','output':items,'usage':{'input_tokens':50,'output_tokens':20,'output_tokens_details':None}},
            {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}],'usage':None}]
        tools=[{'type':'function','function':{'name':'read_file','parameters':{'type':'object','properties':{}}}}]
        messages=[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,eA=='}}]}]
        response=adapter.complete('test_provider',messages,'test-model',tools,100,'high');self.assertEqual(response.choices[0].finish_reason,'tool_calls')
        extra=response.choices[0].message.model_extra
        adapter.complete('test_provider',[*messages,{'role':'assistant','content':'','_muse_response_items':extra['_muse_response_items']},{'role':'tool','tool_call_id':'call-one','content':'result'}],'test-model',tools,100,'high')
        options=client.responses.create.call_args.kwargs
        self.assertIn(items[0],options['input']);self.assertIn({'type':'function_call_output','call_id':'call-one','output':'result'},options['input'])
        self.assertEqual(options['input'][0]['content'][0]['type'],'input_image');self.assertFalse(options['store']);self.assertEqual(options['tools'][0]['name'],'read_file')
        self.assertFalse(options['tools'][0]['strict']) # Preserve optional arguments in the existing tool schemas.

    def test_structured_json_bridge_does_not_execute_unlisted_tools(self):
        adapter,client=self.adapter(tools=False);tools=[{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]
        client.chat.completions.create.return_value=chat('{"content":"read","tool_calls":[{"name":"read_file","arguments":{"path":"x"}}]}')
        response=adapter.complete('test_provider',[{'role':'user','content':'work'}],'test-model',tools,100)
        self.assertEqual(response.choices[0].message.tool_calls[0].function.name,'read_file');self.assertNotIn('tools',client.chat.completions.create.call_args.kwargs)
        client.chat.completions.create.return_value=chat('{"content":"bad","tool_calls":[{"name":"forbidden","arguments":{}}]}')
        with self.assertRaisesRegex(ValueError,'validation'):adapter.complete('test_provider',[{'role':'user','content':'work'}],'test-model',tools,100)

    def test_responses_web_search_uses_registered_result_format(self):
        from workbench.goal_references import search_result
        adapter,client=self.adapter('responses')
        client.responses.create.return_value={'status':'completed','output':[{'type':'web_search_call','action':{'type':'search','sources':[{'type':'url','url':'https://maker.example/drawing'}]}}]}
        raw=adapter.search('test_provider','conveyor drawing','test-model',100,'low',10,'Find one source.')
        self.assertIn('web_search_call.action.sources',client.responses.create.call_args.kwargs['include'])
        self.assertEqual(search_result(raw)['results'][0]['url'],'https://maker.example/drawing')

    def test_json_bridge_length_continuation_validates_the_combined_object(self):
        from workbench.generation import complete_continuing
        adapter,client=self.adapter(tools=False);tools=[{'type':'function','function':{'name':'read_file','parameters':{'type':'object'}}}]
        first=chat('{"content":"read","tool_calls":[');first['choices'][0]['finish_reason']='length'
        client.chat.completions.create.side_effect=[first,chat('{"name":"read_file","arguments":{"path":"x.py"}}]}')]
        result=complete_continuing(lambda messages,available:adapter.complete('test_provider',messages,'test-model',available,100),[{'role':'user','content':'work'}],tools,100)
        self.assertEqual(result.choices[0].message.content,'read');self.assertEqual(result.choices[0].message.tool_calls[0].function.name,'read_file')
        self.assertEqual(result._muse_continuations,1);self.assertEqual(result.usage.model_dump()['completion_tokens'],40)

    def test_responses_length_continuation_preserves_reasoning_and_text_items(self):
        from workbench.generation import complete_continuing
        adapter,client=self.adapter('responses');reason={'type':'reasoning','id':'r1','encrypted_content':'opaque','summary':[]}
        first={'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[reason,{'type':'message','role':'assistant','content':[{'type':'output_text','text':'first '}]}]}
        second={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'second'}]}]}
        client.responses.create.side_effect=[first,second]
        result=complete_continuing(lambda messages,available:adapter.complete('test_provider',messages,'test-model',available,100),[{'role':'user','content':'work'}],None,100)
        self.assertEqual(result.choices[0].message.content,'first second');self.assertIn(reason,client.responses.create.call_args.kwargs['input'])
        self.assertIn(reason,result.choices[0].message.model_extra['_muse_response_items'])

    def test_error_normalization_does_not_echo_keys_urls_or_body(self):
        adapter,client=self.adapter();failure=RuntimeError('https://host/private?key=secret');failure.status_code=503;client.chat.completions.create.side_effect=failure
        with self.assertRaises(AdapterError) as raised:adapter.complete('test_provider',[{'role':'user','content':'x'}],'test-model',max_tokens=10)
        self.assertEqual(raised.exception.status_code,503);self.assertNotIn('secret',str(raised.exception));self.assertNotIn('https://',str(raised.exception))

    def test_local_rate_and_concurrency_limits_refuse_without_upstream_cost(self):
        adapter,client=self.adapter();provider=self.manager.model_registry.get('test_provider')
        with adapter.slot(provider,.1):
            with self.assertRaises(AdapterError) as raised:
                with adapter.slot(provider,.01):pass
        self.assertEqual(raised.exception.status_code,429);self.assertEqual(raised.exception.usage['completion_tokens'],0)
        client.chat.completions.create.return_value=chat('OK')
        data=registration();data['limits']['requests_per_minute']=1;self.manager.model_registry.save(data)
        adapter.complete('test_provider',[{'role':'user','content':'one'}],'test-model',max_tokens=10)
        with self.assertRaises(AdapterError):adapter.complete('test_provider',[{'role':'user','content':'two'}],'test-model',max_tokens=10,timeout_seconds=.01)
        self.assertEqual(client.chat.completions.create.call_count,1)

if __name__=='__main__':unittest.main()
