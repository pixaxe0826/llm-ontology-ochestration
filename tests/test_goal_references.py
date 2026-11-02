"""Fake providers/network only: research quotas, accounting, SSRF and transient vision."""
import base64,contextlib,io,json,os,socket,time,unittest,queue
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
import test_goals as fixtures
from workbench.goal_references import NAMES,reference_limits,search_result
from workbench.product_images import download_public_image,MAX_IMAGE_BYTES
from workbench.providers import Providers
from workbench.agent import TOOLS as WORK_TOOLS
from workbench.goals import BuilderProviders,PLANNER_ALLOWED
from workbench.goal_prompts import system
from workbench.goal_costs import GoalLimit
from workbench.workspace import Workspace

PNG=b'\x89PNG\r\n\x1a\n'+b'fake-image-contents'

def search_response(count=1,usage=True):
    return {'status':'completed','output':[
        {'type':'reasoning','content':None},
        {'type':'web_search_call','status':'completed','results':[{'title':'Manufacturer drawing','url':'https://maker.example/conveyor','snippet':'Factory dimension drawing.'}]},
        {'type':'message','content':[{'type':'output_text','text':'Manufacturer layout shows two side rails.',
            'annotations':[{'type':'url_citation','title':'Manufacturer drawing','url':'https://maker.example/conveyor','start_index':0,'end_index':19}]}]}],
        'usage':{'input_tokens':100,'output_tokens':50,'input_tokens_details':{'cached_tokens':20},'output_tokens_details':{'reasoning_tokens':30},
            'tool_usage':{'web_search':{'requests':count}}} if usage else None}


class ReferenceTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create

    def fixture(self,limits=None):
        goal=self.create(start=False,reference_limits=limits)
        self.manager.add_items(goal,[{'key':str(i),'title':'Geometry '+str(i),'acceptance':['Document observations.'],'depends_on':[],'blocked_by_decision':[]} for i in range(2)])
        item=self.manager.items(goal['id'])[0]
        task=self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
            'workspace':str(self.folder),'status':'succeeded','events':[],'messages':[]})
        self.providers.reference_search=MagicMock(return_value=search_response())
        return goal,item,task

    def test_search_format_usage_and_estimated_query_cost(self):
        goal,item,task=self.fixture();result=self.manager.builders.execute(task['id'],'reference_search',{'query':'manufacturer conveyor drawing'})
        self.assertEqual(result['results'][0]['title'],'Manufacturer drawing');self.assertEqual(result['results'][0]['url'],'https://maker.example/conveyor')
        self.assertEqual(result['results'][0]['description'],'Factory dimension drawing.');self.assertTrue(result['citations']);self.assertTrue(result['searched'])
        row=self.manager.ledger.rows(goal['id'])[0]
        self.assertEqual((row['input_tokens'],row['cached_input_tokens'],row['output_tokens'],row['reasoning_tokens']),(100,20,50,30))
        self.assertEqual(row['search_count'],1);self.assertEqual(row['search_count_source'],'usage')
        self.assertAlmostEqual(row['estimated_usd'],(80*1.25+20*.15+50*4.25)/1e6+.0025)
        self.assertAlmostEqual(row['search_estimated_usd'],.0025);self.assertTrue(row['search_cost_estimated'])
        self.assertIn(result['evidence_id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))
        self.assertEqual(self.providers.reference_search.call_args.args[1],goal['builder_model'])

    def test_custom_search_rate_and_missing_usage_are_conservative(self):
        goal,item,task=self.fixture();self.manager.ledger.prices['web_search']['meta_usd_per_query']=.01
        value=search_response(usage=False);self.providers.reference_search.return_value=value
        result=self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        row=self.manager.ledger.rows(goal['id'])[0]
        self.assertTrue(row['usage_estimated']);self.assertEqual(row['search_count_source'],'output_items')
        self.assertAlmostEqual(row['search_estimated_usd'],.01);self.assertEqual(result['quota']['goal_used'],1)

    def test_goal_and_item_quotas_and_persisted_settings(self):
        goal,item,task=self.fixture({'goal':3,'item':1})
        self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        with self.assertRaisesRegex(ValueError,'quota'):self.manager.reference_tool(task,'reference_search',{'query':'another'})
        other=self.manager.items(goal['id'])[1];task={**task,'plan_item_id':other['id']}
        self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.manager.save(goal,status='paused');self.manager.update(goal['id'],{'reference_limits':{'goal':2,'item':8}})
        self.manager.save(goal,status='running')
        with self.assertRaisesRegex(ValueError,'quota'):self.manager.reference_tool(task,'reference_search',{'query':'another'})
        self.assertEqual(self.providers.reference_search.call_count,2)
        self.assertEqual(self.manager.detail(goal['id'])['references']['goal_used'],2)

    def test_zero_quota_bad_limits_and_no_search_still_consumes_an_attempt(self):
        for value in ({'goal':-1},{'item':True},{'item':1.2},{'goal':1001},{'unexpected':2}):
            with self.subTest(value=value),self.assertRaises(ValueError):reference_limits(value)
        goal,item,task=self.fixture({'goal':0})
        with self.assertRaisesRegex(ValueError,'quota'):self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.providers.reference_search.assert_not_called()
        self.manager.save(goal,reference_limits={'goal':1,'item':1})
        self.providers.reference_search.return_value={'output':None,'usage':{'input_tokens':10,'output_tokens':10},'status':'completed'}
        result=self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.assertFalse(result['searched']);self.assertEqual(result['search_count'],0);self.assertEqual(result['quota']['goal_used'],1)
        self.assertEqual(self.manager.ledger.rows(goal['id'])[0]['search_estimated_usd'],0)

    def test_provider_error_no_retry_quota_consumed_and_no_raw_error_leak(self):
        goal,item,task=self.fixture();secret='synthetic-reference-key-42154'
        self.providers.reference_search.side_effect=RuntimeError(secret+' raw body https://internal')
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}),self.assertRaisesRegex(ValueError,'RuntimeError.*no automatic retry') as error:
            self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.assertNotIn(secret,str(error.exception));self.assertEqual(self.providers.reference_search.call_count,1)
        row=self.manager.ledger.rows(goal['id'])[0];self.assertEqual(row['state'],'settled');self.assertEqual(row['search_quota_units'],1)
        self.assertTrue(row['usage_estimated']);self.assertNotIn(secret,json.dumps(row))

    def test_budget_and_provisional_guards_prevent_paid_calls(self):
        goal,item,task=self.fixture();self.manager.save(goal,budgets=self.manager.ledger.budgets({'meta':{'total':0}}))
        with self.assertRaises(GoalLimit):self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.providers.reference_search.assert_not_called();self.assertEqual(self.store.get('goal',goal['id'])['status'],'paused')

    def download(self,task,url='https://images.example/conveyor.png'):
        with patch('workbench.goal_references.download_public_image',return_value={'data':PNG,'url':url,'content_type':'image/png'}):
            return self.manager.reference_tool(task,'fetch_reference_image',{'url':url})

    def test_download_index_hash_ignore_idempotence_and_existing_files_preserved(self):
        goal,item,task=self.fixture();(self.folder/'.gitignore').write_text('*.tmp\n!references/\n',encoding='utf-8')
        result=self.download(task);self.assertEqual((self.folder/result['path']).read_bytes(),PNG)
        first=(self.folder/'.gitignore').read_text();self.assertTrue(first.endswith('/references/\n'))
        again=self.download(task);self.assertEqual((self.folder/'.gitignore').read_text(),first);self.assertEqual(result['path'],again['path'])
        index=json.loads((self.folder/'references/INDEX.json').read_text());self.assertEqual(len(index['images']),2)
        self.assertEqual(index['images'][0]['original_url'],'https://images.example/conveyor.png');self.assertEqual(index['images'][0]['sha256'],result['sha256'])
        self.assertEqual(index['images'][0]['bytes'],len(PNG));self.assertIn('downloaded_at_utc',index['images'][0])
        self.assertNotIn('data:image',json.dumps(self.store.items('goal_evidence',None)))

    def test_local_image_analysis_input_four_images_and_no_image_body_persistence(self):
        goal,item,task=self.fixture();download=self.download(task);calls=[]
        def complete(*args,**kwargs):calls.append((args,kwargs));return fixtures.response('Observed two side rails; absolute scale is NOT VERIFIED.')
        self.providers.complete=complete
        result=self.manager.builders.execute(task['id'],'analyze_images',{'paths':[download['path']]*4,'question':'Compare proportions and layout.'})
        args,options=calls[0];self.assertEqual((args[0],args[2]),('meta',goal['builder_model']))
        parts=args[1][1]['content'];images=[part for part in parts if part['type']=='image_url']
        self.assertEqual(len(images),4);self.assertEqual(base64.b64decode(images[0]['image_url']['url'].split(',')[1]),PNG)
        self.assertEqual(options['reasoning_effort'],'high');self.assertEqual(result['status'],'completed')
        rows=self.manager.ledger.rows(goal['id']);self.assertEqual(rows[0]['image_count'],4);self.assertEqual(rows[0]['input_tokens'],20)
        self.memory.pending.join()
        persisted='\n'.join(raw for raw, in self.store.db.execute('SELECT data FROM records'))
        self.assertNotIn('data:image',persisted);self.assertNotIn(base64.b64encode(PNG).decode(),persisted)
        self.assertIn(result['evidence_id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))

    def test_bad_local_paths_size_gif_secret_rejected_before_model(self):
        goal,item,task=self.fixture();(self.folder/'large.png').write_bytes(PNG+b'x'*MAX_IMAGE_BYTES)
        (self.folder/'bad.gif').write_bytes(b'GIF89a'+b'x');(self.folder/'bad.png').write_bytes(b'not an image')
        self.providers.complete=MagicMock()
        for paths in ([],['large.png'],['bad.gif'],['bad.png'],['../escape.png'],[str(self.folder/'bad.png')],['bad.png']*5):
            with self.subTest(paths=paths),self.assertRaises(ValueError):self.manager.reference_tool(task,'analyze_images',{'paths':paths,'question':'layout'})
        self.providers.complete.assert_not_called();self.assertFalse(self.manager.ledger.rows(goal['id']))

    def test_secret_redaction_and_reflected_image_response_is_not_saved(self):
        goal,item,task=self.fixture();download=self.download(task);secret='synthetic-vision-key-98227'
        self.providers.complete=lambda *a,**k:fixtures.response(secret+' safe text')
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):
            result=self.manager.reference_tool(task,'analyze_images',{'paths':[download['path']],'question':'layout'})
            self.assertNotIn(secret,json.dumps(result));self.assertIn('[REDACTED]',result['content'])
            with self.assertRaises(ValueError):self.manager.reference_tool(task,'reference_search',{'query':secret})
        self.providers.complete=lambda *a,**k:fixtures.response(base64.b64encode(PNG).decode())
        with self.assertRaisesRegex(ValueError,'not stored'):self.manager.reference_tool(task,'analyze_images',{'paths':[download['path']],'question':'layout'})
        persisted='\n'.join(raw for raw, in self.store.db.execute('SELECT data FROM records'))
        self.assertNotIn(secret,persisted);self.assertNotIn(base64.b64encode(PNG).decode(),persisted)

    def test_tools_are_builder_only_and_planner_work_unchanged(self):
        goal,item,task=self.fixture();provider=BuilderProviders(self.manager);provider.local.task_id=task['id']
        with patch.object(self.manager,'call',return_value=fixtures.response()) as call:
            provider.completion('meta',[{'role':'system','content':'test'}],WORK_TOOLS,goal['builder_model'])
        available={tool['function']['name'] for tool in call.call_args.args[5]}
        self.assertTrue(NAMES<=available);self.assertFalse(NAMES&PLANNER_ALLOWED)
        self.assertFalse(NAMES&{tool['function']['name'] for tool in WORK_TOOLS})
        self.assertIn('VISUAL_REFERENCE.md',system(goal,'Builder'));self.assertIn('never copy/embed',system(goal,'Builder'))

    def test_meta_sdk_responses_options_are_isolated_from_chat(self):
        provider=Providers(self.store);client=MagicMock();client.__enter__.return_value=client
        with patch.object(provider,'client',return_value=client):provider.reference_search('drawing','muse-spark-1.3',16000,'high',25,'public source facts')
        opts=client.responses.create.call_args.kwargs
        self.assertEqual(opts['model'],'muse-spark-1.3');self.assertEqual(opts['tools'][0]['type'],'web_search')
        self.assertEqual(opts['max_tool_calls'],1);self.assertFalse(opts['store']);self.assertEqual(opts['include'],['web_search_call.results'])
        self.assertEqual(opts['reasoning'],{'effort':'high'});client.chat.completions.create.assert_not_called()

    def test_live_observed_missing_snippets_and_citations_are_not_fabricated(self):
        value=search_response();value['output'][1]['results'][0]['snippet']=None
        value['output'][2]['content'][0]['annotations']=None
        result=search_result(value)
        self.assertTrue(result['searched']);self.assertFalse(result['citations_available']);self.assertEqual(result['citations'],[])
        self.assertFalse(result['results'][0]['description_available']);self.assertEqual(result['results'][0]['description'],'')
        self.assertIn('not claim-level verification',result['notice'])
        value['output'][1]['results'][0]['text']='A public dimension drawing.'
        self.assertEqual(search_result(value)['results'][0]['description'],'A public dimension drawing.')

    def test_provisional_and_waiting_decision_do_not_spend_on_references(self):
        goal,item,task=self.fixture();self.store.update('plan_item',item['id'],provisional_scope='local_files_and_tests')
        decision=self.manager.create_decision(goal,{'question':'Which local shape?','options':['A','B'],'recommendation':'A','reason':'Reversible local default.','reversible':True},item_id=item['id'])
        with self.assertRaises(PermissionError):self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.store.update('decision',decision['id'],state='withdrawn');self.manager.save(goal,status='waiting_decision')
        with self.assertRaises(GoalLimit):self.manager.reference_tool(task,'reference_search',{'query':'drawing'})
        self.providers.reference_search.assert_not_called();self.assertFalse(self.manager.ledger.rows(goal['id']))

    def test_analyze_error_does_not_reflect_request_image_or_provider_body(self):
        goal,item,task=self.fixture();download=self.download(task);encoded=base64.b64encode(PNG).decode()
        def fail(*args,**kwargs):raise RuntimeError('data:image/png;base64,'+encoded)
        self.providers.complete=MagicMock(side_effect=fail)
        with self.assertRaises(ValueError) as caught:self.manager.reference_tool(task,'analyze_images',{'paths':[download['path']],'question':'layout'})
        self.assertNotIn(encoded,str(caught.exception));self.assertEqual(self.providers.complete.call_count,1)
        persisted='\n'.join(raw for raw, in self.store.db.execute('SELECT data FROM records'))
        self.assertNotIn(encoded,persisted);self.assertEqual(self.manager.ledger.rows(goal['id'])[0]['state'],'settled')

    def test_analysis_becomes_stale_when_input_image_changes(self):
        goal,item,task=self.fixture();download=self.download(task);self.providers.complete=lambda *a,**k:fixtures.response('Two side rails.')
        result=self.manager.reference_tool(task,'analyze_images',{'paths':[download['path']],'question':'layout'})
        (self.folder/download['path']).write_bytes(PNG+b'modified')
        observed=self.manager.observed_evidence(goal,self.store.get('plan_item',item['id']))[result['evidence_id']]
        self.assertTrue(observed['stale']);self.assertIn('Input changed',observed['stale_reason'])


class ReferenceNetworkTests(unittest.TestCase):
    def remote(self,body=PNG,status=200,mime='image/png',length=None,location=None):
        response=MagicMock();response.status=status
        response.getheader.side_effect=lambda name,default=None:{'Content-Length':length,'Content-Type':mime,'Location':location}.get(name,default)
        response.read.side_effect=io.BytesIO(body).read;connection=MagicMock();connection.getresponse.return_value=response
        return connection,response

    def fetch(self,connection,url='https://images.example/a.png',deadline=None):
        target=(NS(scheme='https',path='/a.png',query=''), 'images.example',443,'8.8.8.8')
        with patch('workbench.product_images.public_target',return_value=target),patch('workbench.product_images.http.client.HTTPSConnection',return_value=connection):
            return download_public_image(url,deadline or time.monotonic()+5,{'image/png','image/jpeg','image/webp'})

    def test_public_download_returns_bytes_and_final_url(self):
        connection,_=self.remote();result=self.fetch(connection)
        self.assertEqual(result['data'],PNG);self.assertEqual(result['content_type'],'image/png')
        self.assertEqual(set(connection.request.call_args.kwargs['headers']),{'Accept','User-Agent'})

    def test_private_addresses_mixed_dns_and_non_public_urls(self):
        for addresses in (['127.0.0.1'],['10.0.0.1'],['::1'],['169.254.169.254'],['8.8.8.8','127.0.0.1']):
            answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in addresses]
            with self.subTest(addresses=addresses),patch('workbench.product_images.socket.getaddrinfo',return_value=answers),self.assertRaises(ValueError):
                download_public_image('https://images.example/a.png',time.monotonic()+5)
        for url in ('file:///C:/secret.png','http://user:pass@images.example/a.png','http://localhost/a.png','http://127.0.0.1/a.png'):
            with self.subTest(url=url),self.assertRaises(ValueError):download_public_image(url,time.monotonic()+5)

    def test_redirect_to_private_is_rejected_before_connection(self):
        connection,_=self.remote(status=302,location='http://127.0.0.1/a.png')
        with patch('workbench.product_images.public_target',side_effect=[(NS(scheme='https',path='/a.png',query=''),'images.example',443,'8.8.8.8'),ValueError('Image URL must resolve only to public HTTP(S) addresses on standard ports.')]),patch('workbench.product_images.http.client.HTTPSConnection',return_value=connection) as create:
            with self.assertRaises(ValueError):download_public_image('https://images.example/a.png',time.monotonic()+5)
        self.assertEqual(create.call_count,1);connection.close.assert_called_once()

    def test_oversized_header_stream_wrong_signature_gif_and_mime_mismatch(self):
        for kwargs in ({'length':str(MAX_IMAGE_BYTES+1)},{'body':PNG+b'x'*MAX_IMAGE_BYTES},{'body':b'html, not a PNG'},
            {'body':b'GIF89a','mime':'image/gif'},{'mime':'image/jpeg'}):
            connection,_=self.remote(**kwargs)
            with self.subTest(kind=list(kwargs)),self.assertRaises(ValueError):self.fetch(connection)

    def test_total_time_limit_is_enforced(self):
        connection,_=self.remote()
        with self.assertRaisesRegex(ValueError,'timed out'):self.fetch(connection,deadline=time.monotonic()-1)
        connection.request.assert_not_called()

    def test_dns_timeout_is_bounded_and_secret_redirect_is_rejected(self):
        with patch('workbench.product_images.socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]),patch('workbench.product_images.queue.Queue') as queues:
            queues.return_value.get.side_effect=queue.Empty
            with self.assertRaises(ValueError):download_public_image('https://images.example/a.png',time.monotonic()+.01)
            self.assertLessEqual(queues.return_value.get.call_args.kwargs['timeout'],.01)
        secret='synthetic-reference-redirect-key-12345'
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}),self.assertRaises(ValueError):
            download_public_image('https://images.example/'+secret+'.png',time.monotonic()+1)


if __name__=='__main__':unittest.main()
