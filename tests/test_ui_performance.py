"""Display optimizations must preserve source records and security semantics."""
import gzip,json,sqlite3,tempfile,threading,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
from workbench.store import Store
from workbench.server import Application,LocalHTTPServer,make_handler
from workbench.ui_cache import ViewResponses
from workbench.security import register_secret
from workbench.knowledge import KnowledgeVault

class ReadCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.row=self.store.save('demo',{'id':'a','nested':{'values':[1]}})
    def tearDown(self):self.store.db.close();self.temp.cleanup()
    def test_private_copies_and_request_lifetime(self):
        with self.store.read_cache():
            self.store.items('demo',None)[0]['nested']['values'].append(2)
            self.assertEqual(self.store.items('demo',None)[0]['nested']['values'],[1])
            self.store.get('demo','a')['nested']['values'].append(3)
            self.assertEqual(self.store.get('demo','a')['nested']['values'],[1])
        self.assertEqual(self.store.get('demo','a')['nested']['values'],[1])
    def test_write_delete_create_invalidate_without_missing_records(self):
        with self.store.read_cache(),self.store.display_read():
            self.store.items('demo',None);self.store.update('demo','a',v=2)
            self.assertEqual(self.store.get('demo','a')['v'],2)
            self.store.save('demo',{'id':'b'});self.assertEqual(len(self.store.items('demo',None)),2)
            self.store.delete('demo','a')
            self.assertEqual([r['id'] for r in self.store.items('demo',None)],['b'])
            with self.assertRaises(ValueError):self.store.get('demo','a')
    def test_external_connection_invalidates(self):
        with self.store.read_cache():
            self.store.items('demo',None)
            db=sqlite3.connect(Path(self.temp.name)/'workbench.sqlite3')
            db.execute('update records set data=? where kind=? and id=?',(json.dumps({**self.row,'external':True}),'demo','a'));db.commit();db.close()
            self.assertTrue(self.store.get('demo','a')['external'])
    def test_thread_local_and_no_shared_formatter_state_leak(self):
        got=[]
        with self.store.read_cache(),self.store.display_read():
            row=self.store.get('demo','a');row['local_only']=True
            thread=threading.Thread(target=lambda:got.append(self.store.get('demo','a')));thread.start();thread.join()
        self.assertNotIn('local_only',got[0]);self.assertNotIn('local_only',self.store.get('demo','a'))
    def test_sort_and_limit_compatible(self):
        self.store.save('demo',{'id':'b'})
        expected=self.store.items('demo',1)
        with self.store.read_cache():
            self.store.items('demo',None);self.assertEqual(self.store.items('demo',1),expected)
            self.assertEqual(len(self.store.items('demo',0)),0)

class DisplayHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=Application(self.temp.name);self.app.store.on_save=None
        self.app.notifications.tick=lambda:None
        self.task=self.app.store.save('task',{'id':'1234567890abcdef','title':'UI sample','provider':'meta','mode':'work','status':'succeeded','created':time.time(),
            'events':[{'kind':'assistant','text':'x'*3000}], 'messages':[{'role':'assistant','content':'original prompt'}]})
        self.server=LocalHTTPServer(('127.0.0.1',0),make_handler(self.app));self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.server_close();self.app.stop();self.app.store.db.close();self.temp.cleanup()
    def request(self,path='/api/tasks',headers=None):
        try:response=urlopen(Request(self.base+path,headers=headers or {}),timeout=5)
        except HTTPError as error:response=error
        with response:return response.status,dict(response.headers),response.read()
    def test_legacy_tasks_equal_single_detail_and_batch_costs_once(self):
        for n in range(3):self.app.store.save('task',{**self.task,'id':f'{n:016x}'})
        self.app.store.save('ledger',{'goal_id':'other','task_id':self.task['id'],'provider':'meta','state':'settled','estimated_usd':.25})
        with patch.object(self.app.goals.ledger,'rows',wraps=self.app.goals.ledger.rows) as rows:
            result=self.app.public_tasks();self.assertEqual(rows.call_count,1)
        self.assertEqual(next(t for t in result if t['id']==self.task['id']),self.app.public_task(self.task))
        self.assertEqual(next(t for t in result if t['id']==self.task['id'])['cost']['estimated_usd'],.25)
    def test_compact_history_has_every_id_and_detail_retains_all_content(self):
        compact=self.app.handle('GET','/api/ui/history',{},{});task=compact['tasks'][0]
        self.assertEqual(task['id'],self.task['id']);self.assertNotIn('events',task);self.assertNotIn('messages',task)
        status,_,body=self.request('/api/tasks/'+task['id']);self.assertEqual(status,200)
        self.assertEqual(json.loads(body)['events'],self.task['events'])
    def test_etag_304_and_updates_deletes_never_lose_changes(self):
        status,headers,body=self.request();tag=headers['ETag'];self.assertEqual(status,200)
        self.assertEqual(self.request(headers={'If-None-Match':tag})[0],304)
        self.app.store.update('task',self.task['id'],title='Updated')
        status,changed,body=self.request(headers={'If-None-Match':tag});self.assertEqual(status,200);self.assertNotEqual(tag,changed['ETag'])
        self.assertEqual(json.loads(body)[0]['title'],'Updated')
        self.app.store.delete('task',self.task['id']);status,_,body=self.request(headers={'If-None-Match':changed['ETag']})
        self.assertEqual(status,200);self.assertEqual(json.loads(body),[])
    def test_gzip_preserves_body_and_honors_q_zero(self):
        _,_,plain=self.request();status,headers,body=self.request(headers={'Accept-Encoding':'gzip'})
        self.assertEqual(headers['Content-Encoding'],'gzip');self.assertEqual(gzip.decompress(body),plain)
        self.assertNotIn('Content-Encoding',self.request(headers={'Accept-Encoding':'gzip;q=0'})[1])
        self.assertEqual(self.request(headers={'Accept-Encoding':'gzip;q=invalid'})[0],200)
    def test_origin_and_proxy_auth_checked_before_cache(self):
        _,headers,_=self.request()
        self.assertEqual(self.request(headers={'Origin':'https://outside.invalid','If-None-Match':headers['ETag']})[0],403)
        self.assertEqual(self.request('/proxy/v1/models',{'If-None-Match':headers['ETag']})[0],401)
        self.assertEqual(self.request('/proxy/v1/models',{'Authorization':'Bearer wrong'})[0],401)
    def test_timing_metadata_only_and_disabled(self):
        key='fixture-ui-secret-value-xxxxxxxx';register_secret(key)
        status,headers,_=self.request('/api/tasks?ignored='+key,{'X-Private':key})
        self.assertIn('db;dur=',headers['Server-Timing']);self.assertIn('file;dur=',headers['Server-Timing']);self.assertIn('lock;dur=',headers['Server-Timing'])
        path=Path(self.temp.name)/'diagnostics/requests.jsonl'
        end=time.time()+2
        while not path.exists() and time.time()<end:time.sleep(.01)
        text=path.read_text();self.assertNotIn(key,text);self.assertNotIn('ignored=',text);self.assertNotIn('X-Private',text)
        self.app.ui_metrics.enabled=False;self.assertNotIn('Server-Timing',self.request()[1])
    def test_new_registered_secret_scrubbed_from_cached_bytes(self):
        key='future-ui-secret-aaaaaaaa';self.app.store.update('task',self.task['id'],title=key)
        _,headers,body=self.request();self.assertIn(key.encode(),body)
        register_secret(key);status,new,body=self.request(headers={'If-None-Match':headers['ETag']})
        self.assertEqual(status,200);self.assertNotIn(key.encode(),body);self.assertNotEqual(headers['ETag'],new['ETag'])
    def test_cache_disabled_and_static_validation(self):
        self.app.ui_metrics.config.update(response_cache=False,conditional_get=False,gzip=False)
        status,headers,body=self.request();self.assertEqual(status,200);self.assertNotIn('ETag',headers)
        self.app.ui_metrics.config['conditional_get']=True
        _,headers,_=self.request('/app.js');self.assertEqual(headers['Cache-Control'],'private, no-cache')
        status,headers,body=self.request('/app.js',{'If-None-Match':headers['ETag']});self.assertEqual(status,304);self.assertEqual(body,b'')
    def test_lru_is_bounded(self):
        cache=ViewResponses(maximum=20)
        cache.put('a',b'a'*15);cache.put('b',b'b'*15)
        self.assertIsNone(cache.get('a'));self.assertEqual(cache.get('b'),b'b'*15);self.assertLessEqual(cache.size,20)

class MemoryGraphCacheTests(unittest.TestCase):
    def test_current_files_sharing_definitions_and_deletion_invalidate(self):
        with tempfile.TemporaryDirectory() as folder:
            vault=KnowledgeVault(folder);vault.ui_graph_cache=True
            import uuid
            identifier=str(uuid.uuid4());saved,_=vault.save({'id':identifier,'title':'GPU example','shared':True,'revision':0,'concepts':['GPU'],'messages':[]})
            first=vault.graph();first['chats'].clear();self.assertEqual(vault.graph()['chat_count'],1)
            saved['shared']=False;vault.save(saved);self.assertEqual(vault.graph()['shared_count'],0)
            vault.set_definitions([{'label':'GPU','aliases':['CUDA']}]);self.assertEqual(vault.graph()['definitions'][0]['aliases'],['CUDA'])
            vault.path(identifier).unlink();self.assertEqual(vault.graph()['chat_count'],0)

if __name__=='__main__':unittest.main()
