"""Offline image proxy transport, bounded inputs, usage and credential protection."""
import base64
import contextlib
import copy
import io
import json
import os
import socket
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import urlopen

from test_goals import GoalHTTPTests, response
from workbench.product_images import (MAX_IMAGE_BYTES, MAX_REQUEST_BYTES, VISION_INPUT_RESERVE,
                                     data_image_size, public_target, remote_image_size, validate_messages)
from workbench.providers import BASETEN_MODEL, Providers

PNG = b'\x89PNG\r\n\x1a\n' + b'offline-image-fixture'


def uri(data=PNG):
    return 'data:image/png;base64,'+base64.b64encode(data).decode('ascii')


def messages(url=None, count=1):
    return [{'role':'system','content':'Inspect the supplied images.'}, {'role':'user','content':[
        {'type':'text','text':'What color is this?'},
        *[{'type':'image_url','image_url':{'url':url or uri(),'detail':'low'}} for _ in range(count)]]}]


class ProductImageHTTPTests(GoalHTTPTests):
    def test_inline_image_content_reaches_fixed_provider_unchanged_and_original_usage_is_ledgered(self):
        sent=messages();snapshot=copy.deepcopy(sent);calls=[]
        usage={'prompt_tokens':417,'completion_tokens':31,'total_tokens':448,
               'prompt_tokens_details':{'cached_tokens':17,'image_tokens':384},
               'completion_tokens_details':{'reasoning_tokens':21},'image_count':1}
        def complete(provider, content, model, *args, **kwargs):
            calls.append((provider,copy.deepcopy(content),model))
            result=response('Red');result.usage=NS(model_dump=lambda:copy.deepcopy(usage));return result
        self.app.goals.providers.complete=complete
        with urlopen(self.request(self.token,messages=sent)) as result:body=json.load(result)
        self.assertEqual(calls,[('baseten',snapshot,BASETEN_MODEL)]);self.assertEqual(sent,snapshot)
        self.assertEqual(body['model'],BASETEN_MODEL);self.assertEqual(body['choices'][0]['message']['content'],'Red')
        row=self.app.goals.ledger.rows(self.goal['id'])[0]
        self.assertEqual(row['provider_usage'],usage);self.assertEqual(row['input_tokens'],417);self.assertEqual(row['output_tokens'],31)
        self.assertEqual(row['reasoning_tokens'],21);self.assertFalse(row['usage_estimated'])
        self.assertEqual(row['proxy_image_count'],1);self.assertEqual(row['proxy_image_bytes'],len(PNG))
        self.assertNotIn('data:image',json.dumps(row));self.assertLess(row['estimated_usd'],row['reserved_usd'])

    def test_public_url_content_is_forwarded_unchanged(self):
        sent=messages('https://images.example/test.png?version=2')
        with patch('workbench.product_images.remote_image_size',return_value=89) as check:
            with urlopen(self.request(self.token,messages=sent)):pass
        self.assertEqual(self.app.goals.providers.calls[-1]['messages'],sent)
        self.assertEqual(check.call_args.args[0],sent[1]['content'][1]['image_url']['url'])

    def test_four_full_size_images_fit_http_envelope_and_reservation_ignores_base64_length(self):
        data=PNG+b'x'*(MAX_IMAGE_BYTES-len(PNG));sent=messages(uri(data),4)
        with urlopen(self.request(self.token,messages=sent),timeout=15) as result:self.assertEqual(result.status,200)
        row=self.app.goals.ledger.rows(self.goal['id'])[0]
        self.assertEqual(row['proxy_image_count'],4);self.assertEqual(row['proxy_image_bytes'],4*MAX_IMAGE_BYTES)
        expected=(VISION_INPUT_RESERVE*1.25+16000*4.25)/1_000_000
        self.assertAlmostEqual(row['reserved_usd'],expected)

    def test_fifth_image_across_messages_is_rejected_before_fetch_or_charge(self):
        sent=messages('https://images.example/a.png',3)+messages('https://images.example/b.png',2)[1:]
        with patch('workbench.product_images.remote_image_size') as fetch:
            with self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,messages=sent))
            self.assertEqual(caught.exception.code,400);self.assertIn('4-image',json.load(caught.exception)['error']['message']);caught.exception.close()
            fetch.assert_not_called()
        self.assertFalse(self.app.goals.providers.calls);self.assertFalse(self.app.goals.ledger.rows(self.goal['id']))

    def test_one_decoded_byte_above_limit_is_rejected_before_provider(self):
        data=PNG+b'x'*(MAX_IMAGE_BYTES+1-len(PNG))
        with self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,messages=messages(uri(data))))
        self.assertEqual(caught.exception.code,400);self.assertIn('5 MiB',json.load(caught.exception)['error']['message']);caught.exception.close()
        self.assertFalse(self.app.goals.providers.calls);self.assertFalse(self.app.goals.ledger.rows(self.goal['id']))

    def test_oversized_http_request_and_wrong_token_rejected_before_read(self):
        for token,expected in ((self.token,400),('wrong-token-00000000000',401)):
            request=self.request(token);request.add_header('Content-Length',str(MAX_REQUEST_BYTES+1))
            with self.assertRaises(HTTPError) as caught:urlopen(request,timeout=5)
            self.assertEqual(caught.exception.code,expected);caught.exception.close()
        self.assertFalse(self.app.goals.providers.calls)

    def test_secret_in_image_metadata_decoded_image_or_provider_output_never_leaks(self):
        secret='synthetic-image-provider-secret-59130';logs=io.StringIO()
        self.app.goals.providers.complete=lambda *a,**k:response(secret+' '+self.token)
        with patch.dict(os.environ,{'BASETEN_API_KEY':secret}),contextlib.redirect_stdout(logs),contextlib.redirect_stderr(logs):
            for url in ('https://images.example/'+secret+'.png',uri(PNG+secret.encode())):
                with self.assertRaises(HTTPError) as caught:urlopen(self.request(self.token,messages=messages(url)))
                wire=caught.exception.read().decode();caught.exception.close();self.assertNotIn(secret,wire)
            with urlopen(self.request(self.token,messages=messages())) as result:wire=result.read().decode()
            self.assertNotIn(secret,wire);self.assertNotIn(self.token,wire)
            combined=logs.getvalue()+json.dumps(self.app.goals.ledger.rows(self.goal['id']))
            self.assertNotIn(secret,combined);self.assertNotIn(self.token,combined)

    def test_each_image_continuation_keeps_original_usage_and_image_content(self):
        calls=[]
        def complete(provider,content,*args,**kwargs):
            calls.append(copy.deepcopy(content));return response('A' if len(calls)==1 else 'B',finish='length' if len(calls)==1 else 'stop',tokens=len(calls)*7)
        self.app.goals.providers.complete=complete
        with urlopen(self.request(self.token,messages=messages(),stream=True)) as result:wire=result.read().decode()
        self.assertIn('[DONE]',wire);self.assertEqual(len(calls),2);self.assertEqual(calls[0][1],calls[1][1])
        rows=self.app.goals.ledger.rows(self.goal['id']);self.assertEqual(len(rows),2)
        self.assertEqual(sum(r['provider_usage']['completion_tokens'] for r in rows),21)
        self.assertEqual(sum(r['output_tokens'] for r in rows),21)

    def test_sdk_receives_content_arrays_without_conversion(self):
        provider=Providers(self.app.store);client=MagicMock();client.__enter__.return_value=client;sent=messages()
        with patch.object(provider,'client',return_value=client):provider.complete('baseten',sent,BASETEN_MODEL,max_tokens=1024,role='Product')
        self.assertEqual(client.chat.completions.create.call_args.kwargs['messages'],sent)
        self.assertEqual(client.chat.completions.create.call_args.kwargs['model'],BASETEN_MODEL)


class ProductImageValidationTests(unittest.TestCase):
    def test_malformed_parts_and_non_image_data_are_rejected(self):
        for sent in (messages('data:image/png;base64,%%%'),messages('data:text/plain;base64,eA=='),
                     messages('data:image/png;base64,eA=='),[{'role':'system','content':messages()[1]['content']}],
                     [{'role':'user','content':[{'type':'image_url','image_url':{'url':uri(),'detail':'invalid'}}]}],
                     [{'role':'user','content':[{'type':'input_audio','input_audio':{}}]}]):
            with self.subTest(sent_type=type(sent).__name__),self.assertRaises(ValueError):validate_messages(sent)
        self.assertEqual(validate_messages([{'role':'user','content':[{'type':'text','text':'Text only'}]}])['proxy_image_count'],0)

    def test_private_addresses_mixed_dns_credentials_and_local_schemes_are_rejected(self):
        for url in ('file:///C:/private.png','http://user:password@images.example/a.png','http://images.example:8765/a.png','http://images.example/\r\nsecret'):
            with self.assertRaises(ValueError):public_target(url)
        for address in ('127.0.0.1','10.1.2.3','169.254.169.254','::1','fc00::1','::ffff:127.0.0.1','192.0.2.1'):
            with patch('workbench.product_images.socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,443))]),self.assertRaises(ValueError):public_target('https://images.example/a.png')
        with patch('workbench.product_images.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443)),(2,1,6,'',('127.0.0.1',443))]),self.assertRaises(ValueError):public_target('https://images.example/a.png')

    def remote(self,header_length=None,body=PNG,status=200,location=None):
        response=MagicMock();response.status=status
        response.getheader.side_effect=lambda name,default=None:{'Content-Length':header_length,'Content-Type':'image/png','Location':location}.get(name,default)
        stream=io.BytesIO(body);response.read.side_effect=stream.read
        connection=MagicMock();connection.getresponse.return_value=response
        return connection,response

    def test_remote_oversize_header_or_stream_is_rejected_with_no_body_reflection(self):
        for header,body in ((str(MAX_IMAGE_BYTES+1),b''),(None,PNG+b'x'*MAX_IMAGE_BYTES)):
            connection,response=self.remote(header,body)
            with patch('workbench.product_images.public_target',return_value=(NS(scheme='https',path='/x.png',query=''), 'images.example',443,'8.8.8.8')),patch('workbench.product_images.http.client.HTTPSConnection',return_value=connection):
                with self.assertRaisesRegex(ValueError,'5 MiB'):remote_image_size('https://images.example/x.png',time.monotonic()+5)
            connection.close.assert_called_once()
            if header:response.read.assert_not_called()

    def test_remote_connection_pins_checked_address_and_sends_no_credentials(self):
        connection,_=self.remote()
        with patch('workbench.product_images.public_target',return_value=(NS(scheme='https',path='/x.png',query='v=1'), 'images.example',443,'8.8.8.8')),patch('workbench.product_images.http.client.HTTPSConnection',return_value=connection) as create,patch('workbench.product_images.socket.create_connection') as connect:
            self.assertEqual(remote_image_size('https://images.example/x.png?v=1',time.monotonic()+5),len(PNG))
            connection._create_connection(('images.example',443),2,None)
            connect.assert_called_once_with(('8.8.8.8',443),2,None)
        self.assertEqual(create.call_args.args[:2],('images.example',443))
        self.assertEqual(set(connection.request.call_args.kwargs['headers']),{'Accept','User-Agent'})

    def test_redirect_is_revalidated_before_private_connection(self):
        connection,_=self.remote(status=302,location='http://127.0.0.1/private.png')
        with patch('workbench.product_images.public_target',side_effect=[(NS(scheme='https',path='/x.png',query=''),'images.example',443,'8.8.8.8'),ValueError('Image URL must resolve only to public HTTP(S) addresses on standard ports.')]) as target,patch('workbench.product_images.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaisesRegex(ValueError,'public HTTP'):remote_image_size('https://images.example/x.png',time.monotonic()+5)
        self.assertEqual(target.call_args.args[0],'http://127.0.0.1/private.png');connection.close.assert_called_once()

    def test_remote_errors_and_expired_validation_do_not_echo_url_or_exception(self):
        with patch('workbench.product_images.public_target',side_effect=OSError('raw sensitive URL')):
            with self.assertRaisesRegex(ValueError,'^Image URL validation failed.$'):remote_image_size('https://images.example/private',time.monotonic()+1)
        with patch('workbench.product_images.public_target',return_value=(None,None,None,None)):
            with self.assertRaisesRegex(ValueError,'timed out'):remote_image_size('https://images.example/x.png',time.monotonic()-1)


for name in dir(GoalHTTPTests):
    if name.startswith('test_') and name not in ProductImageHTTPTests.__dict__:setattr(ProductImageHTTPTests,name,None)
del GoalHTTPTests,name

if __name__=='__main__':unittest.main()
