"""Offline SAM transport, official mask parsing, boundaries and per-image budgets."""
import base64,contextlib,io,json,os,unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from PIL import Image
import test_goals as fixtures
from workbench.providers import Providers
from workbench.sam_proxy import SAM_REQUEST_BYTES,sam_image,segmentation
from workbench.product_images import MAX_IMAGE_BYTES

WIRE='<0f>0<|box;x1=4;y1=5;x2=8;y2=9;w=16;h=16;c=0.75|><|mask;x=0;y=0;c=0.75;data=5,5,!!!!!(QO(0lu8?|>'

def png():
    stream=io.BytesIO();Image.new('RGB',(16,16),'red').save(stream,format='PNG');return stream.getvalue()

def uri(data=None):return 'data:image/png;base64,'+base64.b64encode(png() if data is None else data).decode()

def response(text=WIRE,**kwargs):return NS(status='completed',output_text=text,usage=NS(images_processed=1),**kwargs)

class SAMProxyTests(unittest.TestCase):
    def setUp(self):
        fixtures.GoalHTTPTests.setUp(self)
        self.call=MagicMock(return_value=response());self.app.goals.providers.sam_segment=self.call
    tearDown=fixtures.GoalHTTPTests.tearDown

    def request(self,token=None,**values):
        headers={'Content-Type':'application/json'}
        if token:headers['Authorization']='Bearer '+token
        return Request(self.url+'/proxy/v1/sam/segment',data=json.dumps({'image':uri(),'prompt':'red square',**values}).encode(),headers=headers)

    def rejected(self,request,code=400):
        with self.assertRaises(HTTPError) as caught:urlopen(request,timeout=10)
        error=caught.exception;self.assertEqual(error.code,code);body=json.load(error);error.close();return body['error']['message']

    def test_authenticated_inline_image_returns_box_score_and_standard_rle(self):
        with urlopen(self.request(self.token)) as result:body=json.load(result)
        self.assertEqual(body['model'],'sam-3.1');self.assertEqual(body['image']['width'],16)
        segment=body['segments'][0];self.assertEqual(segment['box'],[4,5,9,10]);self.assertEqual(segment['score'],.75)
        self.assertEqual(segment['mask']['encoding'],'coco_rle');self.assertEqual(segment['mask']['size'],[5,5]);self.assertIsInstance(segment['mask']['counts'],str)
        self.assertEqual(segment['mask']['bounds'],segment['box']);self.call.assert_called_once()
        self.assertEqual(self.call.call_args.args[:2],(uri(),'red square'))
        row=self.app.goals.ledger.rows(self.goal['id'])[0]
        self.assertEqual((row['provider'],row['purpose'],row['model']),('meta','product','sam-3.1'))
        self.assertAlmostEqual(row['estimated_usd'],.0025);self.assertEqual(row['images_processed'],1)
        self.assertTrue(row['sam_response_validated']);self.assertEqual(row['workload'],'segmentation')
        self.assertEqual(row['input_tokens'],0);self.assertEqual(row['provider_usage'],{'images_processed':1})
        self.assertNotIn('data:image',json.dumps(row));self.assertNotIn('red square',json.dumps(row))

    def test_workspace_image_uses_current_goal_scope(self):
        (self.folder/'shape.png').write_bytes(png())
        with urlopen(self.request(self.token,image='shape.png')) as result:self.assertEqual(result.status,200)
        row=self.app.goals.ledger.rows(self.goal['id'])[0];self.assertEqual(row['image_info']['path'],'shape.png')
        self.assertEqual(self.call.call_args.args[0],uri())

    def test_authentication_precedes_image_read_and_budget_reservation(self):
        for token in (None,'wrong-goal-token-0000000000000000',self.app.token):
            request=self.request(token,image='../outside.png');request.add_header('Content-Length',str(SAM_REQUEST_BYTES+1))
            self.rejected(request,401)
        self.call.assert_not_called();self.assertFalse(self.app.goals.ledger.rows())

    def test_image_paths_cannot_escape_or_read_protected_files_or_fetch_urls(self):
        for path in ('../image.png',str(self.folder/'shape.png'),'.env','https://example.com/test.png','missing.png'):
            self.rejected(self.request(self.token,image=path))
        self.call.assert_not_called();self.assertFalse(self.app.goals.ledger.rows())

    def test_only_one_image_and_fixed_model_fields_are_allowed(self):
        for changes in ({'image':[uri(),uri()]},{'image':None},{'model':'other'},{'provider':'baseten'},{'images':[uri()]},{'prompt':''},{'prompt':'x'*513}):
            self.rejected(self.request(self.token,**changes))
        self.call.assert_not_called()

    def test_decoded_image_and_request_size_limits(self):
        self.assertIn('5 MiB',self.rejected(self.request(self.token,image=uri(png()+b'x'*(MAX_IMAGE_BYTES+1-len(png()))))))
        (self.folder/'large.png').write_bytes(png()+b'x'*(MAX_IMAGE_BYTES+1-len(png())))
        self.assertIn('5 MiB',self.rejected(self.request(self.token,image='large.png')))
        request=self.request(self.token);request.add_header('Content-Length',str(SAM_REQUEST_BYTES+1));self.rejected(request)
        self.call.assert_not_called()

    def test_corrupt_mismatched_and_animated_images_are_rejected(self):
        for image in (uri(b'not an image'),uri(b'\x89PNG\r\n\x1a\ncorrupt'),uri().replace('image/png','image/jpeg')):
            self.rejected(self.request(self.token,image=image))
        stream=io.BytesIO();Image.new('RGB',(4,4),'red').save(stream,format='GIF',save_all=True,append_images=[Image.new('RGB',(4,4),'blue')])
        self.rejected(self.request(self.token,image='data:image/gif;base64,'+base64.b64encode(stream.getvalue()).decode()))
        self.call.assert_not_called()

    def test_empty_completed_segmentation_is_a_valid_zero_match_result(self):
        self.call.return_value=response('')
        with urlopen(self.request(self.token)) as result:body=json.load(result)
        self.assertEqual(body['segments'],[]);self.assertEqual(body['cost']['estimated_usd'],.0025)

    def test_missing_usage_and_confidence_are_safe_and_do_not_manufacture_score(self):
        self.call.return_value=NS(status='completed',output_text=WIRE.replace(';c=0.75',''),usage=None)
        with urlopen(self.request(self.token)) as result:body=json.load(result)
        self.assertIsNone(body['segments'][0]['score']);self.assertEqual(body['usage']['images_processed'],1)
        row=self.app.goals.ledger.rows()[0];self.assertEqual(row['usage_source'],'one_input_image');self.assertEqual(row['estimated_usd'],.0025)

    def test_output_message_fallback_handles_nullable_fields(self):
        self.call.return_value=NS(status='completed',output=[NS(type='reasoning',content=None),NS(type='message',content=[NS(type='output_text',text=WIRE)])])
        with urlopen(self.request(self.token)) as result:self.assertEqual(len(json.load(result)['segments']),1)

    def test_partial_or_malformed_response_is_charged_but_never_retried(self):
        self.call.return_value=NS(status='incomplete',output_text=WIRE,usage=None)
        self.assertIn('validation failed',self.rejected(self.request(self.token)))
        self.call.assert_called_once();row=self.app.goals.ledger.rows()[0]
        self.assertFalse(row['sam_response_validated']);self.assertEqual(row['estimated_usd'],.0025)
        self.assertNotIn(WIRE,json.dumps(row))

    def test_mask_allocation_limits_and_invalid_wire_grammar(self):
        for text in (WIRE.replace('data=5,5,','data=999999999,999999999,'),WIRE*257,WIRE.replace('x2=8','x2=999'),WIRE.replace('!!!!!(QO(0lu8?','broken'),'not segmentation'):
            with self.subTest(text=text[:70]),self.assertRaises(ValueError):segmentation(response(text),{'width':16,'height':16})

    def test_goal_total_daily_and_platform_caps_block_before_provider(self):
        manager=self.app.goals
        for scope,limit in (('goal','total'),('goal','daily'),('platform','total'),('platform','daily')):
            with self.subTest(scope=scope,limit=limit):
                goal=manager.store.get('goal',self.goal['id']);budgets=manager.ledger.budgets();budgets['meta'][limit]=.002
                if scope=='goal':manager.store.update('goal',goal['id'],budgets=budgets,status='planning')
                else:
                    manager.store.update('goal',goal['id'],budgets=manager.ledger.budgets(),status='planning')
                    manager.ledger.prices['limits']['meta'][limit]=.002
                manager.proxy_tokens[goal['id']]=self.token
                self.rejected(self.request(self.token),429)
                manager.ledger.prices['limits']['meta'].update(total=150,daily=None)
        self.call.assert_not_called();self.assertFalse(manager.ledger.rows())

    def test_sam_does_not_consume_baseten_reserve_or_follow_chat_route(self):
        endpoint='muse-spark-open-test.us-west.modal.direct'
        self.app.goals.store.save('settings',{'id':'muse_open_catalog','endpoints':[{'id':endpoint}]})
        self.app.goals.muse_routes.save({'route':'modal','endpoint_id':endpoint})
        with urlopen(self.request(self.token)):pass
        rows=self.app.goals.ledger.rows();self.assertEqual([r['provider'] for r in rows],['meta'])
        self.assertEqual(self.app.goals.ledger.summary(self.goal)['baseten']['product_spent'],0)

    def test_transient_model_retry_charges_each_image_attempt(self):
        class APIConnectionError(RuntimeError):pass
        self.call.side_effect=[APIConnectionError('connection reset'),response()]
        with patch('workbench.goal_operations.DELAYS',(0,0)):
            with urlopen(self.request(self.token)):pass
        self.assertEqual(self.call.call_count,2);rows=self.app.goals.ledger.rows()
        self.assertEqual(len(rows),2);self.assertAlmostEqual(sum(r['estimated_usd'] for r in rows),.005)
        self.assertEqual(sorted(r['retry_index'] for r in rows),[0,1]);self.assertTrue(any(r['usage_estimated'] for r in rows))

    def test_registered_keys_never_appear_in_response_ledger_or_console(self):
        secret='synthetic-sam-key-590498';logs=io.StringIO()
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}),contextlib.redirect_stdout(logs),contextlib.redirect_stderr(logs):
            self.rejected(self.request(self.token,prompt=secret));self.rejected(self.request(self.token,image=uri(png()+secret.encode())))
            self.call.return_value=response(secret+' '+self.token)
            wire=self.rejected(self.request(self.token));self.assertNotIn(secret,wire);self.assertNotIn(self.token,wire)
            combined=logs.getvalue()+json.dumps(self.app.goals.ledger.rows())
            self.assertNotIn(secret,combined);self.assertNotIn(self.token,combined)

    def test_provider_http_error_omits_original_message_url_and_key(self):
        class NotFoundError(RuntimeError):status_code=404
        secret='synthetic-sam-upstream-secret-29122'
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):
            error=NotFoundError('url https://example.org/'+secret);error.body={'error':{'message':secret}};self.call.side_effect=error
            message=self.rejected(self.request(self.token));self.assertIn('404',message);self.assertNotIn(secret,message);self.assertNotIn('example.org',message)
            self.assertNotIn(secret,json.dumps(self.app.goals.ledger.rows()))
        self.call.assert_called_once()

class SAMProviderTests(unittest.TestCase):
    def test_responses_request_uses_fixed_model_one_image_and_no_chat_only_options(self):
        provider=Providers.__new__(Providers);client=MagicMock();provider.client=MagicMock();provider.client.return_value.__enter__.return_value=client
        provider.sam_segment(uri(),'red square',999)
        provider.client.assert_called_once_with('meta');data=client.responses.create.call_args.kwargs
        self.assertEqual(data['model'],'sam-3.1');self.assertFalse(data['stream']);self.assertFalse(data['store']);self.assertEqual(data['timeout'],90)
        self.assertEqual(data['metadata'],{'include_confidence':'true','mask_encoding':'one_bit'})
        self.assertEqual(data['input'][0]['content'],[{'type':'input_text','text':'red square'},{'type':'input_image','image_url':uri()}])
        for forbidden in ('reasoning_effort','max_completion_tokens','max_output_tokens','tools'):self.assertNotIn(forbidden,data)

if __name__=='__main__':unittest.main()
