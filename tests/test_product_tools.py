"""Native product function calls: real HTTP boundary, fake upstream SDK."""
import copy
import json
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

import test_goals as goals
from test_product_images import messages as images
from workbench.product_images import validate_messages, VISION_INPUT_RESERVE
from workbench.product_tools import request_tools, assistant_message
from workbench.providers import Providers, BASETEN_MODEL

TOOLS=[{'type':'function','function':{'name':'sum_values','description':'Add two integers.',
       'parameters':{'type':'object','properties':{'a':{'type':'integer'},'b':{'type':'integer'}},
                     'required':['a','b'],'additionalProperties':False},'strict':True}}]
CALL={'id':'call_native_1','type':'function','function':{'name':'sum_values','arguments':json.dumps({'a':2,'b':3})}}


def tool_response():
    result=goals.response(calls=[('sum_values',{'a':2,'b':3})])
    result.choices[0].message.content=None
    result.choices[0].message.tool_calls[0].id=CALL['id']
    result.choices[0].message.model_extra={'reasoning_content':'Use the supplied function.'}
    return result


class ProductToolsHTTPTests(goals.GoalHTTPTests):
    def sdk(self, responses):
        provider=Providers(self.app.store);client=MagicMock();client.__enter__.return_value=client
        client.chat.completions.create.side_effect=responses
        self.app.goals.providers=provider
        return provider,client

    def test_two_turn_native_roundtrip_reaches_sdk_and_ledgers_both_calls(self):
        provider,client=self.sdk([tool_response(),goals.response('5')])
        sent=[{'role':'user','content':'Add 2 and 3 using the function.'}]
        choice={'type':'function','function':{'name':'sum_values'}}
        with patch.object(provider,'client',return_value=client):
            with urlopen(self.request(self.token,messages=sent,tools=TOOLS,tool_choice=choice,parallel_tool_calls=False,max_tokens=32)) as wire:
                first=json.load(wire);self.assertEqual(wire.headers['X-Muse-Proxy-Adjusted'],'max_tokens 32->1024')
            message=first['choices'][0]['message']
            self.assertEqual(first['choices'][0]['finish_reason'],'tool_calls');self.assertEqual(message['tool_calls'],[CALL])
            self.assertEqual(message['reasoning_content'],'Use the supplied function.')
            sent+=[message,{'role':'tool','tool_call_id':CALL['id'],'content':'{"sum":5}'}]
            with urlopen(self.request(self.token,messages=sent,tools=TOOLS,tool_choice='none',parallel_tool_calls=True,max_completion_tokens=32)) as wire:
                second=json.load(wire);self.assertIsNone(wire.headers.get('X-Muse-Proxy-Adjusted'))
        self.assertEqual(second['choices'][0]['message']['content'],'5')
        calls=client.chat.completions.create.call_args_list;self.assertEqual(len(calls),2)
        self.assertEqual(calls[0].kwargs['tools'],TOOLS);self.assertEqual(calls[0].kwargs['tool_choice'],choice)
        self.assertFalse(calls[0].kwargs['parallel_tool_calls']);self.assertTrue(calls[1].kwargs['parallel_tool_calls'])
        self.assertEqual(calls[1].kwargs['messages'],sent);self.assertEqual(calls[1].kwargs['max_tokens'],32)
        rows=self.app.goals.ledger.rows(self.goal['id']);self.assertEqual(len(rows),2)
        self.assertTrue(all(r['state']=='settled' and r['purpose']=='product' and r['provider']=='baseten' and r['output_tokens']==5 for r in rows))
        self.assertEqual(sorted(r['proxy_adjusted'] for r in rows),[False,True])

    def test_image_array_and_native_tools_are_forwarded_together(self):
        provider,client=self.sdk([tool_response()]);sent=images();snapshot=copy.deepcopy(sent)
        with patch.object(provider,'client',return_value=client),urlopen(self.request(self.token,messages=sent,tools=TOOLS,tool_choice='required')) as wire:
            self.assertEqual(json.load(wire)['choices'][0]['finish_reason'],'tool_calls')
        request=client.chat.completions.create.call_args.kwargs
        self.assertEqual(request['messages'],snapshot);self.assertEqual(request['tools'],TOOLS)
        self.assertNotIn('parallel_tool_calls',request)
        row=self.app.goals.ledger.rows(self.goal['id'])[0]
        self.assertEqual(row['proxy_image_count'],1)
        self.assertGreater(row['reserved_usd'],(VISION_INPUT_RESERVE*1.25+16000*4.25)/1_000_000)
        self.assertNotIn('data:image',json.dumps(row))

    def test_stream_tool_delta_has_index_and_reasoning_with_single_ledger_row(self):
        self.app.goals.providers.complete=lambda *a,**k:tool_response()
        with urlopen(self.request(self.token,tools=TOOLS,stream=True)) as wire:raw=wire.read().decode()
        chunk=json.loads(raw.split('data: ',1)[1].split('\n\n',1)[0])
        choice=chunk['choices'][0];self.assertEqual(choice['finish_reason'],'tool_calls')
        self.assertEqual(choice['delta']['tool_calls'],[{'index':0,**CALL}])
        self.assertIn('reasoning_content',choice['delta']);self.assertIn('[DONE]',raw)
        self.assertEqual(len(self.app.goals.ledger.rows(self.goal['id'])),1)

    def test_unknown_request_fields_are_named_without_values_before_provider_or_reservation(self):
        for field in ('temperature','base_url','api_key','response_format'):
            with self.subTest(field=field),self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,**{field:'do-not-echo-this-value'}))
            self.assertEqual(caught.exception.code,400);error=json.load(caught.exception)['error']['message'];caught.exception.close()
            self.assertIn('지원하지 않는 필드: '+field,error);self.assertNotIn('do-not-echo-this-value',error)
        self.assertFalse(self.app.goals.providers.calls);self.assertFalse(self.app.goals.ledger.rows(self.goal['id']))

    def test_tool_payload_and_reasoning_secrets_rejected_and_upstream_output_redacted(self):
        secret='synthetic-tool-secret-536612';self.app.goals.providers.complete=lambda *a,**k:tool_response()
        with patch.dict(os.environ,{'BASETEN_API_KEY':secret}):
            for extra in ({'tools':[{'type':'function','function':{'name':'x','description':secret}}]},
                          {'messages':[{'role':'assistant','content':'OK','reasoning_content':secret}]},
                          {'messages':[{'role':'tool','tool_call_id':'x','content':secret}]}):
                with self.subTest(extra=extra),self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,**extra))
                self.assertNotIn(secret,caught.exception.read().decode());caught.exception.close()
            output=tool_response();output.choices[0].message.model_extra['reasoning_content']=secret
            output.choices[0].message.tool_calls[0].function.arguments=json.dumps({'secret':secret})
            self.app.goals.providers.complete=lambda *a,**k:output
            with urlopen(self.request(self.token,tools=TOOLS)) as wire:self.assertNotIn(secret,wire.read().decode())
            self.assertNotIn(secret,json.dumps(self.app.goals.ledger.rows(self.goal['id'])))

    def test_modal_route_keeps_native_tools_and_modal_llm_accounting(self):
        endpoint='muse-spark-open-tools.us-west.modal.direct';routes=self.app.goals.muse_routes
        routes.providers.modal_models=lambda:[{'id':endpoint}];routes.discover();routes.save({'route':'modal','endpoint_id':endpoint})
        seen=[]
        def complete(*args,**kwargs):seen.append((args,kwargs));return tool_response()
        self.app.goals.providers.complete=complete
        with urlopen(self.request(self.token,tools=TOOLS,tool_choice='auto',parallel_tool_calls=True)) as wire:
            result=json.load(wire)
        self.assertEqual(result['model'],endpoint);self.assertEqual(seen[0][0][0],'modal_llm')
        self.assertEqual(seen[0][0][3],TOOLS);self.assertEqual(seen[0][1]['tool_options'],{'tool_choice':'auto','parallel_tool_calls':True})
        row=self.app.goals.ledger.rows(self.goal['id'])[0];self.assertEqual(row['provider'],'modal');self.assertEqual(row['route'],'modal')


class ProductToolValidationTests(unittest.TestCase):
    def test_bad_tools_choices_and_messages_are_rejected(self):
        for extra in ({'tools':'bad'},{'tools':[{}]},{'tools':TOOLS*2},{'tools':TOOLS,'tool_choice':'bad'},
                      {'tools':TOOLS,'parallel_tool_calls':'true'},{'tool_choice':'required'},
                      {'tools':TOOLS,'tool_choice':{'type':'function','function':{'name':'unknown'}}}):
            with self.subTest(extra=extra),self.assertRaises(ValueError):request_tools(extra)
        for message in ({'role':'tool','content':'result'}, {'role':'assistant','content':None},
                        {'role':'assistant','tool_calls':[]}, {'role':'user','content':'x','tool_calls':[CALL]},
                        {'role':'assistant','tool_calls':[CALL,CALL]},
                        {'role':'assistant','tool_calls':[CALL],'reasoning_content':4}):
            with self.subTest(message=message),self.assertRaises(ValueError):validate_messages([message])

    def test_null_or_absent_assistant_content_and_reasoning_object_variants(self):
        for content in ({},{'content':None},{'content':''}):
            self.assertEqual(validate_messages([{'role':'assistant','tool_calls':[CALL],**content}])['proxy_image_count'],0)
        for message in ({'content':'x','tool_calls':None,'reasoning_content':'direct'},
                        NS(content='x',tool_calls=None,model_extra=None,reasoning_content='direct')):
            self.assertEqual(assistant_message(message),{'role':'assistant','content':'x','reasoning_content':'direct'})
        self.assertEqual(assistant_message(NS(content=None,tool_calls=None)),{'role':'assistant','content':''})

    def test_ordinary_goal_tool_defaults_are_unchanged(self):
        provider=Providers(MagicMock());provider.settings=lambda:{'baseten':{'model':BASETEN_MODEL,'reasoning_effort':'high'}}
        client=MagicMock();client.__enter__.return_value=client
        with patch.object(provider,'client',return_value=client):
            provider.complete('baseten',[{'role':'user','content':'x'}],tools=TOOLS)
            self.assertFalse(client.chat.completions.create.call_args.kwargs['parallel_tool_calls'])


if __name__=='__main__':unittest.main()
