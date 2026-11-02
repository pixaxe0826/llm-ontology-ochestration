"""Offline public document reading: real PDF parser/render worker, fake network/providers."""
import io,json,os,socket,subprocess,time,unittest
from types import SimpleNamespace as NS
from unittest.mock import MagicMock,patch
import test_goals as fixtures
from workbench.reference_pages import (html_reference,download_reference_page,page_limits,HTML_LIMIT,PDF_LIMIT)
from workbench.goal_references import NAMES
from workbench.file_views import tool_text
from workbench.providers import BASETEN_MODEL
from workbench.agent import TOOLS as WORK_TOOLS
from workbench.goals import PLANNER_ALLOWED
from workbench.goals import GoalManager
from workbench.goal_prompts import system

def pdf_fixture(count=2):
    # A small test-only PDF with a text layer and line drawing; no authoring runtime required.
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',None,b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    kids=[]
    for page in range(count):
        number=len(objects)+1;kids.append(str(number)+' 0 R')
        objects.append(('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 250] /Resources << /Font << /F1 3 0 R >> >> /Contents '+str(number+1)+' 0 R >>').encode())
        text=('BT /F1 18 Tf 25 180 Td (Conveyor reference - page '+str(page+1)+') Tj 0 -35 Td (Frame width 50 mm) Tj ET\n25 40 m 350 40 l 350 120 l 25 120 l h S').encode()
        objects.append(b'<< /Length '+str(len(text)).encode()+b' >>\nstream\n'+text+b'\nendstream')
    objects[1]=('<< /Type /Pages /Count '+str(count)+' /Kids ['+' '.join(kids)+'] >>').encode()
    data=bytearray(b'%PDF-1.4\n');offsets=[0]
    for number,value in enumerate(objects,1):offsets.append(len(data));data.extend(str(number).encode()+b' 0 obj\n'+value+b'\nendobj\n')
    start=len(data);data.extend(('xref\n0 '+str(len(objects)+1)+'\n0000000000 65535 f \n').encode())
    for offset in offsets[1:]:data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(('trailer << /Size '+str(len(objects)+1)+' /Root 1 0 R >>\nstartxref\n'+str(start)+'\n%%EOF\n').encode());return bytes(data)


class ReferencePageTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create
    def fixture(self,limits=None):
        goal=self.create(start=False,reference_page_limits=limits)
        self.manager.add_items(goal,[{'key':str(i),'title':'Read manufacturer references','acceptance':['Read source.'],'depends_on':[],'blocked_by_decision':[]} for i in range(2)])
        item=self.manager.items(goal['id'])[0]
        task=self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
            'workspace':str(self.folder),'allow_write':True,'status':'succeeded','events':[],'messages':[]})
        return goal,item,task
    def fetch(self,task,data=b'<html><main>Frame width 50 mm</main></html>',kind='html'):
        with patch('workbench.reference_pages.download_reference_page',return_value={'data':data,'url':'https://maker.example/catalog','kind':kind,'content_type':'text/html; charset=utf-8' if kind=='html' else 'application/pdf'}):
            return self.manager.builders.execute(task['id'],'fetch_reference_page',{'url':'https://maker.example/source'})

    def test_html_body_excludes_scripts_main_preferred_and_image_urls_are_absolute(self):
        data=b'<html><head><title>Catalog</title><base href="https://maker.example/assets/"></head><nav>Menu</nav><body><main><h1>Frame &amp; belt</h1><script>ignore_me()</script><style>hidden_css</style><p>Width 50 mm.</p><img src="side.png" alt=" Side &amp; top "><img src="/front.jpg" alt><img src="data:image/png;base64,unsafe"><img src="http://127.0.0.1/private.png"></main></body></html>'
        result=html_reference(data,'https://maker.example/products/p','text/html')
        self.assertIn('Width 50 mm.',result['content']);self.assertIn('Frame & belt',result['content'])
        for value in ('Menu','ignore_me','hidden_css'):self.assertNotIn(value,result['content'])
        self.assertEqual(result['images'],[{'url':'https://maker.example/assets/side.png','alt':'Side & top'},{'url':'https://maker.example/front.jpg','alt':''}])

    def test_html_text_20000_and_thirty_images_and_charset(self):
        data=('<main><p>'+'A'*24000+'</p>'+''.join('<img src="/'+str(n)+'.png" alt="Part">' for n in range(50))+'</main>').encode()
        result=html_reference(data,'https://maker.example/product','text/html')
        self.assertEqual(len(result['content']),20000);self.assertTrue(result['text_truncated']);self.assertEqual(len(result['images']),30)
        korean='<body>폭 50 밀리미터</body>'.encode('cp949');self.assertIn('밀리미터',html_reference(korean,'https://maker.example','text/html; charset=cp949')['content'])

    def test_html_original_index_and_ignore_and_no_model_call(self):
        goal,item,task=self.fixture();result=self.fetch(task)
        self.assertEqual((self.folder/result['path']).read_bytes(),b'<html><main>Frame width 50 mm</main></html>')
        self.assertTrue((self.folder/'.gitignore').read_text().endswith('/references/\n'))
        index=json.loads((self.folder/'references/INDEX.json').read_text());row=index['documents'][0]
        self.assertEqual(row['original_url'],'https://maker.example/source');self.assertIn('downloaded_at_utc',row)
        self.assertEqual(row['sha256'],result['sha256']);self.assertTrue((self.folder/result['text_path']).is_file())
        self.assertEqual(self.manager.ledger.rows(goal['id']),[]);self.assertFalse(self.providers.calls)
        self.assertIn(result['evidence_id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))

    def test_pdf_extracts_page_numbers_and_four_rendered_pngs_analyze_ready(self):
        from PIL import Image
        goal,item,task=self.fixture();result=self.fetch(task,pdf_fixture(5),'pdf')
        self.assertEqual(result['total_pages'],5);self.assertIn('[Page 1]',result['content']);self.assertIn('[Page 5]',result['content']);self.assertIn('50 mm',result['content'])
        self.assertEqual(len(result['rendered_image_paths']),4)
        for path in result['rendered_image_paths']:
            with Image.open(self.folder/path) as image:self.assertEqual(image.format,'PNG');self.assertGreater(image.width,100)
        self.providers.complete=MagicMock(return_value=fixtures.response('Observed frame and belt diagram.'))
        analysis=self.manager.reference_tool(task,'analyze_images',{'paths':result['rendered_image_paths'],'question':'Compare structure.'})
        self.assertEqual(analysis['status'],'completed');self.assertEqual(len(analysis['images']),4)
        index=json.loads((self.folder/'references/INDEX.json').read_text());self.assertEqual(len(index['images']),4)
        self.assertTrue(all(row['source_document_sha256']==result['sha256'] and row['page']<=4 for row in index['images']))

    def test_pdf_worker_environment_no_credentials_proxy_or_stdout_logging(self):
        goal,item,task=self.fixture();run=subprocess.run
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-worker-key','MUSE_PROXY_TOKEN':'synthetic-worker-token','MUSE_PROXY_BASE_URL':'http://127.0.0.1/proxy'}),patch('workbench.reference_pages.subprocess.run',wraps=run) as spawned:
            self.fetch(task,pdf_fixture(1),'pdf')
        opts=spawned.call_args.kwargs
        self.assertNotIn('MODEL_API_KEY',opts['env']);self.assertNotIn('MUSE_PROXY_TOKEN',opts['env']);self.assertNotIn('MUSE_PROXY_BASE_URL',opts['env'])
        self.assertIn('-I',spawned.call_args.args[0]);self.assertEqual(opts['stdout'],subprocess.DEVNULL);self.assertEqual(opts['stderr'],subprocess.DEVNULL)
        self.assertLessEqual(opts['timeout'],45)

    def test_invalid_pdf_and_timeout_consume_quota_without_retry(self):
        goal,item,task=self.fixture()
        with self.assertRaisesRegex(ValueError,'PDF'):self.fetch(task,b'%PDF-invalid','pdf')
        with patch('workbench.reference_pages.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',.01)) as spawned:
            with self.assertRaisesRegex(ValueError,'timed out'):self.fetch(task,pdf_fixture(1),'pdf')
        spawned.assert_called_once();self.assertEqual(self.manager.reference_page_usage(goal,item)['goal_used'],2)
        self.assertEqual({row['state'] for row in self.store.items('goal_reference_page',None)},{'failed'})

    def test_goal_item_limits_settings_and_restart_preserve_page_count(self):
        goal,item,task=self.fixture({'goal':2,'item':1});self.fetch(task)
        with self.assertRaisesRegex(ValueError,'quota'):self.fetch(task)
        other=self.manager.items(goal['id'])[1];other_task=self.store.save('task',{**task,'id':'other-task','plan_item_id':other['id']});self.fetch(other_task)
        with self.assertRaisesRegex(ValueError,'quota'):self.fetch(other_task)
        self.manager.save(goal,status='paused');self.manager.update(goal['id'],{'reference_page_limits':{'goal':3,'item':2}})
        self.assertEqual(self.manager.detail(goal['id'])['reference_pages']['limits'],{'goal':3,'item':2})
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        self.manager.save(goal,status='running');self.fetch(task);self.assertEqual(self.manager.reference_page_usage(goal,item)['goal_used'],3)

    def test_page_limits_validation_and_zero_disable(self):
        self.assertEqual(page_limits(),{'goal':30,'item':12})
        for value in ({'goal':-1},{'item':True},{'item':1.2},{'goal':1001},{'other':1}):
            with self.assertRaises(ValueError):page_limits(value)
        goal,item,task=self.fixture({'goal':0})
        with self.assertRaisesRegex(ValueError,'quota'):self.fetch(task)
        self.assertFalse(self.store.items('goal_reference_page',None))

    def test_readonly_private_paths_and_secret_content_refused(self):
        goal,item,task=self.fixture();self.store.update('task',task['id'],allow_write=False)
        with self.assertRaises(PermissionError):self.fetch(task)
        self.store.update('task',task['id'],allow_write=True)
        for url in ('file:///secret.pdf','http://127.0.0.1/a','http://user:pass@maker.example/a'):
            with patch('workbench.reference_pages.download_reference_page') as fetch,self.assertRaises(ValueError):self.manager.reference_tool(task,'fetch_reference_page',{'url':url})
            fetch.assert_not_called()
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-page-key'}):
            with self.assertRaises(ValueError):self.fetch(task,b'<body>synthetic-page-key</body>')
        self.assertFalse((self.folder/'references').exists())

    def test_image_and_document_index_entries_coexist_and_source_hash_stales(self):
        goal,item,task=self.fixture();result=self.fetch(task)
        from workbench.reference_storage import reference_index
        from workbench.workspace import Workspace
        reference_index(Workspace(self.folder,self.store.root),images=[{'path':'references/example.png','sha256':'test'}])
        self.assertIn(result['evidence_id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))
        (self.folder/result['path']).write_bytes(b'changed source')
        self.assertNotIn(result['evidence_id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))

    def test_tool_view_preserves_20000_text_and_pdf_paths_without_clipping_to_8000(self):
        result={'read_mode':'reference_page','path':'references/source.pdf','sha256':'hash','content':'X'*20000,
            'rendered_image_paths':['references/page'+str(n)+'.png' for n in range(4)],'images':[]}
        view=json.loads(tool_text(json.dumps(result)));self.assertEqual(len(view['content']),20000);self.assertEqual(view['rendered_image_paths'],result['rendered_image_paths'])

    def test_large_escaped_text_and_long_image_urls_keep_valid_view_and_pdf_paths(self):
        paths=['references/page'+str(n)+'.png' for n in range(4)]
        value={'read_mode':'reference_page','path':'references/source.pdf','sha256':'hash','content':'"'*20000,'original_url':'https://maker.example/'+('a'*8000),
            'images':[{'url':'https://maker.example/'+str(n)+'a'*1900,'alt':'part'} for n in range(30)],'rendered_image_paths':paths,
            'text_pages':[{'page':n,'characters':100,'included_characters':100} for n in range(200)]}
        shown=tool_text(json.dumps(value));view=json.loads(shown)
        self.assertLessEqual(len(shown),40000);self.assertEqual(view['rendered_image_paths'],paths);self.assertTrue(view['text_truncated'])

    def test_builder_only_tools_and_prompt_sequence(self):
        goal,item,task=self.fixture()
        self.assertIn('fetch_reference_page',NAMES);self.assertNotIn('fetch_reference_page',PLANNER_ALLOWED)
        self.assertNotIn('fetch_reference_page',{tool['function']['name'] for tool in WORK_TOOLS})
        prompt=system(goal,'Builder');self.assertIn('reference_search -> fetch_reference_page',prompt);self.assertIn('rendered_image_paths',prompt)


class PageNetworkTests(unittest.TestCase):
    def remote(self,body=b'<body>Specs</body>',mime='text/html',status=200,length=None,location=None):
        response=MagicMock();response.status=status;response.read.side_effect=io.BytesIO(body).read
        response.getheader.side_effect=lambda key,default=None:{'Content-Type':mime,'Content-Length':length,'Location':location}.get(key,default)
        connection=MagicMock();connection.getresponse.return_value=response;return connection
    def download(self,connection,deadline=None):
        with patch('workbench.product_images.public_target',return_value=(NS(scheme='https',path='/spec',query='v=1'),'maker.example',443,'8.8.8.8')),patch('workbench.reference_pages.http.client.HTTPSConnection',return_value=connection):
            return download_reference_page('https://maker.example/spec',deadline if deadline is not None else time.monotonic()+5)

    def test_html_pdf_and_pinned_address_no_auth_headers(self):
        connection=self.remote();self.assertEqual(self.download(connection)['kind'],'html')
        with patch('workbench.product_images.socket.create_connection') as connect:connection._create_connection(('maker.example',443),2,None);connect.assert_called_once_with(('8.8.8.8',443),2,None)
        self.assertEqual(set(connection.request.call_args.kwargs['headers']),{'Accept','User-Agent'})
        self.assertEqual(self.download(self.remote(pdf_fixture(1),'application/octet-stream'))['kind'],'pdf')

    def test_size_limits_headers_and_streams_and_wrong_pdf_signature(self):
        cases=[('html-header',{'length':str(HTML_LIMIT+1)}),('html-stream',{'body':b'x'*(HTML_LIMIT+1)}),
            ('pdf-header',{'mime':'application/pdf','length':str(PDF_LIMIT+1)}),('pdf-stream',{'mime':'application/pdf','body':b'%PDF-'+b'x'*PDF_LIMIT}),
            ('wrong-pdf',{'mime':'application/pdf','body':b'<html>not pdf</html>'}),('wrong-type',{'mime':'image/png'})]
        for label,kwargs in cases:
            with self.subTest(label=label),self.assertRaises(ValueError):self.download(self.remote(**kwargs))

    def test_private_dns_and_redirect_rejected(self):
        for addresses in (['127.0.0.1'],['10.0.0.1'],['::1'],['8.8.8.8','127.0.0.1']):
            with patch('workbench.product_images.socket.getaddrinfo',return_value=[(2,1,6,'',(address,443)) for address in addresses]),self.assertRaises(ValueError):download_reference_page('https://maker.example/spec',time.monotonic()+2)
        connection=self.remote(status=302,location='http://127.0.0.1/private')
        with patch('workbench.product_images.public_target',return_value=(NS(scheme='https',path='/spec',query=''),'maker.example',443,'8.8.8.8')),patch('workbench.reference_pages.http.client.HTTPSConnection',return_value=connection) as create:
            with self.assertRaises(ValueError):download_reference_page('https://maker.example/spec',time.monotonic()+2)
        self.assertEqual(create.call_count,1)

    def test_time_limit_and_errors_do_not_echo_response_body(self):
        connection=self.remote()
        with self.assertRaisesRegex(ValueError,'timed out'):self.download(connection,time.monotonic()-1)
        connection.request.assert_not_called()
        connection=self.remote(status=500,body=b'private server error')
        with self.assertRaises(ValueError) as error:self.download(connection)
        self.assertNotIn('private server error',str(error.exception))


if __name__=='__main__':unittest.main()
