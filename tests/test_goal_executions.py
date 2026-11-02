"""Display joins and metadata only: no provider calls, commands or GPUs."""
import hashlib,json,os,sqlite3,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError
from workbench.execution_index import ExecutionIndex
from workbench.store import Store
from workbench.gpu import GPUManager
from workbench.agent import Agents
from workbench.server import Application,LocalHTTPServer,make_handler
from workbench.security import register_secret


class ExecutionIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(Path(self.temp.name)/'state');self.index=ExecutionIndex(self.store)
        self.save('goal','g',title='Goal title',objective='Body',timezone='UTC')
        self.save('plan_item','p',goal_id='g',title='Render',key='P5',task_ids=['t'])
        self.save('task','t',goal_id='g',plan_item_id='p',title='Builder',status='succeeded',events=[],created=1)
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def save(self,kind,identifier,**kw):return self.store.save(kind,{'id':identifier,**kw})
    def gpu(self,identifier='j',**kw):return self.save('gpu',identifier,task_id='t',status='succeeded',spec={'gpu':'T4','program':'blender','entry':'render.py'},started=10,finished=20,**kw)
    def test_legacy_gpu_and_builder_full_links_are_display_only(self):
        self.gpu();before=self.store.db.execute('select kind,id,data,updated from records order by kind,id').fetchall();changes=self.store.db.total_changes
        for kind,id in [('gpu','j'),('task','t')]:
            c=self.index.context(kind,id);self.assertEqual([c[k] for k in ('goal_id','plan_item_id','task_id')],['g','p','t']);self.assertEqual(c['source'],'display_index')
        self.index.goal('g');self.index.report()
        self.assertEqual(self.store.db.total_changes,changes);self.assertEqual(before,self.store.db.execute('select kind,id,data,updated from records order by kind,id').fetchall())
    def test_legacy_missing_task_fields_recover_from_item_then_gpu(self):
        self.store.update('task','t',goal_id=None,plan_item_id=None);self.gpu()
        self.assertEqual(self.index.context('task','t')['status'],'linked');self.assertEqual(self.index.context('gpu','j')['plan_item_id'],'p')
    def test_evidence_and_ledger_recover_deleted_task_without_rewriting(self):
        self.store.delete('task','t');self.gpu()
        self.store.update('plan_item','p',evidence=[{'gpu_job_id':'j','task_id':'t'}])
        self.save('ledger','l',goal_id='g',plan_item_id='p',task_id='t',gpu_job_id='j',provider='modal',workload='gpu',state='settled',estimated_usd=.1)
        self.assertEqual(self.index.context('gpu','j')['status'],'linked');self.assertEqual(self.index.goal('g')['gpu_summary']['estimated_usd'],.1)
    def test_ambiguous_legacy_hints_are_not_guessed(self):
        self.store.delete('task','t');self.gpu();self.save('plan_item','q',goal_id='g',title='Other',evidence=[{'gpu_job_id':'j','task_id':'u'}]);self.store.update('plan_item','p',evidence=[{'gpu_job_id':'j','task_id':'t'}])
        self.assertEqual(self.index.context('gpu','j')['status'],'conflict');self.assertEqual(self.index.goal('g')['gpu_summary']['total'],0)
    def test_borrowed_parent_evidence_does_not_override_recorded_ownership(self):
        self.gpu();self.save('plan_item','q',goal_id='g',title='Parent',evidence=[{'gpu_job_id':'j','task_id':'t'}])
        self.assertEqual(self.index.context('gpu','j')['plan_item_id'],'p');self.assertEqual(self.index.context('task','t')['plan_item_id'],'p')
    def test_direct_work_and_direct_gpu_stay_direct_despite_chat_ledger(self):
        self.save('task','direct',title='Work',status='succeeded',events=[])
        self.save('gpu','manual',task_id=None,status='succeeded',spec={})
        self.save('ledger','chat',goal_id=None,task_id='direct',provider='meta',workload='llm')
        self.assertEqual(self.index.context('task','direct')['status'],'direct');self.assertEqual(self.index.context('gpu','manual')['status'],'direct')
    def test_counts_duration_cost_and_artifact_copy_are_not_double_counted(self):
        self.gpu(result={'summary':{'duration_s':7},'retained_outputs':[{'path':'render.png'},{'path':'large.glb','size':100}]},artifacts=['render.png'])
        self.store.update('task','t',events=[{'kind':'gpu','job_id':'j'},{'kind':'tool_result','name':'get_gpu_job','result':{'id':'j'}},{'kind':'change','change':{'gpu_job_id':'j','path':'review/render.png','sha256':'abc','source_artifact':'render.png'}}])
        self.save('ledger','l',goal_id='g',gpu_job_id='j',provider='modal',workload='gpu',state='settled',estimated_usd=.2)
        self.save('ledger','unmatched',goal_id='g',provider='modal',workload='gpu',state='settled',estimated_usd=.3)
        self.save('ledger','llm',goal_id='g',provider='modal',workload='llm',state='settled',estimated_usd=10)
        self.save('ledger','reserved',goal_id='g',gpu_job_id='j',provider='modal',workload='gpu',state='reserved',reserved_usd=.4)
        self.save('ledger','released',goal_id='g',gpu_job_id='j',provider='modal',workload='gpu',state='released',estimated_usd=1)
        result=self.index.goal('g');summary=result['gpu_summary'];self.assertEqual(summary['total'],1);self.assertEqual(summary['succeeded'],1);self.assertAlmostEqual(summary['estimated_usd'],.5);self.assertEqual(summary['reserved_usd'],.4);self.assertEqual(summary['unattributed_usd'],.3)
        job=next(e for e in result['entries'] if e['kind']=='gpu');self.assertEqual(job['runtime_s'],7);self.assertEqual(job['elapsed_s'],10);self.assertEqual(len(job['files']),2);self.assertEqual(job['copies'][0]['path'],'review/render.png')
    def test_commands_and_long_commands_are_chronological_and_linked_once(self):
        self.store.update('task','t',events=[{'kind':'tool','name':'run_command','arguments':{'argv':['python','test.py'],'cwd':'.'}}, {'kind':'tool_result','name':'run_command','time':4,'result':{'exit_code':1,'timed_out':True,'duration_s':120}}, {'kind':'tool_result','name':'run_command','time':6,'long_command_id':'long','result':{'exit_code':0,'long_command_id':'long'}}])
        self.save('goal_long_command','long',goal_id='g',plan_item_id='p',task_id='t',status='succeeded',created=2,started=2,finished=6,argv=['python','long.py'],result={'exit_code':0})
        entries=self.index.goal('g')['entries'];self.assertEqual([e['kind'] for e in entries],['builder','long_command','command']);cmd=entries[-1];self.assertEqual(cmd['plan_item_id'],'p');self.assertTrue(cmd['timed_out']);self.assertEqual(cmd['status'],'failed');self.assertEqual(cmd['evidence_id'],'t:1')
    def test_cache_invalidates_for_store_and_external_connection(self):
        self.gpu();self.assertEqual(self.index.context('gpu','j')['goal_title'],'Goal title');self.store.update('goal','g',title='New');self.assertEqual(self.index.context('gpu','j')['goal_title'],'New')
        with closing(sqlite3.connect(self.store.root/'workbench.sqlite3')) as db:
            row=json.loads(db.execute("select data from records where kind='goal' and id='g'").fetchone()[0]);row['title']='External';db.execute("update records set data=? where kind='goal' and id='g'",(json.dumps(row),));db.commit()
        self.assertEqual(self.index.context('gpu','j')['goal_title'],'External')
    def test_none_optional_results_and_status_summary(self):
        for identifier,status in [('a','succeeded'),('b','failed'),('c','cancelled')]:self.gpu(identifier,result=None,artifacts=None);self.store.update('gpu',identifier,status=status)
        summary=self.index.goal('g')['gpu_summary'];self.assertEqual((summary['total'],summary['succeeded'],summary['failed'],summary['cancelled']),(3,1,1,1));self.assertEqual(summary['unknown_cost_jobs'],3)
    def test_sparse_legacy_task_without_status_is_displayable(self):
        self.save('task','sparse',goal_id='g',plan_item_id='p',events=[])
        self.assertEqual(next(e for e in self.index.goal('g')['entries'] if e['id']=='sparse')['status'],'unknown')
    def test_display_does_not_reintroduce_registered_secret(self):
        secret='SYNTHETIC_execution_display_secret_719dbd'
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):
            self.store.update('goal','g',title='Goal '+secret);self.gpu();payload=json.dumps(self.index.goal('g'))
        self.assertNotIn(secret,payload)
    def test_new_gpu_records_have_all_three_ids_before_thread_starts(self):
        with patch('workbench.gpu.refresh_keys'),patch.dict(os.environ,{'MODAL_TOKEN_ID':'test','MODAL_TOKEN_SECRET':'test'}),patch('workbench.gpu.threading.Thread'):
            job=GPUManager(self.store).submit({'workspace':self.temp.name,'spec':{},'files':[],'total_bytes':0},'t')
        self.assertEqual([job[k] for k in ('goal_id','plan_item_id','task_id')],['g','p','t'])
    def test_new_builder_records_include_self_task_id(self):
        providers=NS(settings=lambda:{'meta':{'ready':True,'model':'muse-spark-1.3'}})
        root=Path(self.temp.name)/'workspace';root.mkdir()
        with patch('workbench.agent.threading.Thread'):
            task=Agents(self.store,providers,GPUManager(self.store)).start(str(root),'Implement',context={'goal_id':'g','plan_item_id':'p','system_prompt':'Goal'})
        self.assertEqual(task['task_id'],task['id']);self.assertEqual(task['goal_id'],'g');self.assertEqual(task['plan_item_id'],'p')


class ExecutionHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with patch('workbench.server.refresh_keys'),patch('workbench.providers.refresh_keys'):cls.app=Application(Path(cls.temp.name)/'state')
        cls.app.store.save('goal',{'id':'g','title':'Title','status':'paused'})
        cls.app.store.save('plan_item',{'id':'p','goal_id':'g','title':'Render','key':'P5'})
        cls.app.store.save('task',{'id':'t','title':'Builder','goal_id':'g','plan_item_id':'p','provider':'meta','status':'succeeded','events':[]})
        cls.app.store.save('gpu',{'id':'j','task_id':'t','spec':{'entry':'render.py','gpu':'T4','program':'blender'},'status':'succeeded','artifacts':['render.png']})
        cls.app.memory.pending.join()
        cls.folder=cls.app.store.root/'gpu-jobs/j/artifacts';cls.folder.mkdir(parents=True);(cls.folder/'render.png').write_bytes(b'\x89PNG\r\n\x1a\n'+b'fixture')
        cls.server=LocalHTTPServer(('127.0.0.1',0),make_handler(cls.app));cls.url='http://127.0.0.1:'+str(cls.server.server_port);cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.app.memory.pending.join();cls.app.store.on_save=None;cls.app.store.db.close();cls.temp.cleanup()
    def test_public_job_task_and_index_expose_breadcrumb_not_secret(self):
        for path in ('/api/jobs','/api/tasks'):
            with urlopen(self.url+path) as response:row=json.load(response)[0]
            self.assertEqual(row['goal_context']['goal_title'],'Title');self.assertEqual(row['goal_context']['item_title'],'Render')
        with urlopen(self.url+'/api/execution-index') as response:self.assertEqual(json.load(response)['gpu']['linked'],1)
    def test_preview_is_png_inline_bounded_and_download_remains_binary(self):
        with urlopen(self.url+'/api/artifact?job_id=j&path=render.png&preview=1') as response:
            self.assertEqual(response.headers['Content-Type'],'image/png');self.assertIsNone(response.headers.get('Content-Disposition'))
        with urlopen(self.url+'/api/artifact?job_id=j&path=render.png') as response:
            self.assertEqual(response.headers['Content-Type'],'application/octet-stream');self.assertIn('attachment',response.headers['Content-Disposition'])
        for name,content in [('bad.png',b'not png'),('large.png',b'\x89PNG\r\n\x1a\n'+b'x'*(5*1024*1024)),('text.txt',b'not png')]:
            (self.folder/name).write_bytes(content)
            with self.assertRaises(HTTPError) as error:urlopen(self.url+'/api/artifact?job_id=j&preview=1&path='+name)
            self.assertEqual(error.exception.code,400);error.exception.close()
