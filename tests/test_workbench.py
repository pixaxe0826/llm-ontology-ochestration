"""Bounded offline tests; never allocate cloud GPUs or call model APIs."""
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from workbench.agent import Agents
from workbench.gpu import GPUManager, normalize_spec
from workbench.providers import Providers
from workbench.security import KEY_NAMES, redact
from workbench.server import Application, LocalHTTPServer, make_handler
from workbench.store import Store
from workbench.workspace import Workspace


class FakeProviders:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = 0
    def settings(self):
        return {"meta": {"ready": True, "model": "offline-model", "key_env": "MODEL_API_KEY"}}
    def completion(self, *args):
        self.calls += 1
        return self.replies.pop(0)


def response(calls=None, text="", finish="tool_calls"):
    tools = [NS(id=str(i),function=NS(name=name,arguments=json.dumps(args))) for i,(name,args) in enumerate(calls or [])]
    return NS(choices=[NS(message=NS(content=text,tool_calls=tools,model_extra={}),finish_reason=finish)],usage=None)


def until(predicate, timeout=5):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.03)
    raise AssertionError("timed out waiting for local test")


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.store = Store(self.base / "state")
        self.ws = Workspace(self.root, self.store.root)
        self.gpu = GPUManager(self.store)
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()
    def test_path_traversal_and_secret_files(self):
        for path in ("../outside", "sub/../../outside", ".env", ".git/config", "C:\\Windows\\file", "key.pem"):
            with self.assertRaises(ValueError,msg=path):
                self.ws.path(path)
    def test_optimistic_edit_backup_and_diff(self):
        self.ws.write("app.py", "print(1)\n")
        original = self.ws.read("app.py")
        with self.assertRaises(ValueError):
            self.ws.write("app.py", "print(2)\n")
        result = self.ws.write("app.py", "print(2)\n", original["sha256"])
        self.assertIn("+print(2)",result["diff"])
        self.assertEqual((self.store.root/"backups"/result["backup_id"]).read_text(),"print(1)\n")
        with self.assertRaises(ValueError):
            self.ws.write("app.py","print(3)",original["sha256"])
    def test_secret_output_and_persistence_redaction(self):
        synthetic = "synthetic-workbench-test-value-00239"
        with patch.dict(os.environ,{"MODEL_API_KEY":synthetic}):
            self.assertNotIn(synthetic,redact(synthetic))
            record = self.store.save("demo",{"text":synthetic})
            self.assertNotIn(synthetic,json.dumps(record))
            with self.assertRaises(ValueError):
                self.ws.write("unsafe.txt",synthetic)
    def test_command_environment_and_timeout(self):
        with patch.dict(os.environ,{"MODEL_API_KEY":"synthetic-command-key"}):
            result = self.ws.run([sys.executable,"-c","import os; print('MODEL_API_KEY' in os.environ)"], timeout=5)
        self.assertEqual(result["exit_code"],0)
        self.assertIn("False",result["output"])
        result = self.ws.run([sys.executable,"-c","import time; time.sleep(10)"], timeout=1)
        self.assertTrue(result["interrupted"])
    def test_gpu_auto_choice_and_package_validation(self):
        spec = {"program":"python","gpu":"auto","entry":"task.py","files":["task.py"]}
        self.assertEqual(normalize_spec(spec)["gpu"],"T4")
        self.assertEqual(normalize_spec({**spec,"program":"pytorch"})["gpu"],"L4")
        self.assertEqual(normalize_spec({**spec,"min_vram_gb":80})["gpu"],"A100-80GB")
        with self.assertRaises(ValueError):
            normalize_spec({**spec,"packages":["https://untrusted.invalid/package"]})
    def test_upload_manifest_detects_same_size_change(self):
        (self.root/"task.py").write_text("print(1)")
        spec={"program":"python","entry":"task.py","files":["task.py"]}
        before = self.gpu.preview(self.root,spec)
        (self.root/"task.py").write_text("print(2)")
        after = self.gpu.preview(self.root,spec)
        self.assertNotEqual(before["digest"],after["digest"])
        self.assertEqual(before["total_bytes"],after["total_bytes"])
    def test_upload_blocks_registered_key(self):
        value="synthetic-upload-test-key-122444"
        (self.root/"task.py").write_text(value)
        with patch.dict(os.environ,{"MODEL_API_KEY":value}):
            with self.assertRaises(ValueError):
                self.gpu.preview(self.root,{"program":"python","entry":"task.py","files":["task.py"]})
    def test_models_keep_requested_baseten_id(self):
        p=Providers(self.store)
        with patch('workbench.providers.refresh_keys'):
            settings=p.settings()
            self.assertEqual(settings["baseten"]["model"],"meta-models/muse-spark-open-1026")
            with self.assertRaises(ValueError):
                p.save({"meta":{"base_url":"https://example.org/v1"}})
    def test_baseten_model_and_url_cannot_be_changed(self):
        from workbench.providers import BASETEN_MODEL, selected_model
        p=Providers(self.store)
        with patch('workbench.providers.refresh_keys'):
            self.store.save('settings',{'id':'providers','baseten':{'model':'other-model','base_url':'https://example.org/v1'}})
            self.assertEqual(p.settings()['baseten']['model'],BASETEN_MODEL)
            self.assertEqual(p.settings()['baseten']['base_url'],'https://inference.baseten.co/v1')
        with self.assertRaises(ValueError):
            p.save({'baseten':{'model':'other-model'}})
        with self.assertRaises(ValueError):
            selected_model('baseten','other-model')
        self.assertEqual(selected_model('meta','muse-spark-1.2'),'muse-spark-1.2')
        self.assertEqual(selected_model('baseten',None),BASETEN_MODEL)
    def test_agent_write_then_real_test_with_approval(self):
        providers=FakeProviders([
            response([("write_file",{"path":"result.py","content":"print('offline ok')\n","expected_sha256":None})]),
            response([("run_command",{"argv":["python","result.py"],"cwd":".","timeout":5})]),
            response(text="실제 로컬 시험 완료",finish="stop")])
        agents=Agents(self.store,providers,self.gpu)
        task=agents.start(self.root,"파일을 만들고 테스트",max_steps=4)
        pending=until(lambda: (t if (t:=self.store.get('task',task['id'])).get('approval') else None))
        self.assertEqual(pending["approval"]["kind"],"command")
        agents.decide(task['id'],pending['approval']['id'],True)
        finished=until(lambda: (t if (t:=self.store.get('task',task['id']))['status']=='succeeded' else None))
        outputs=[e['result'] for e in finished['events'] if e['kind']=='tool_result' and e['name']=='run_command']
        self.assertIn('offline ok',outputs[0]['output'])
        self.assertEqual(providers.calls,3)
    def test_denied_command_is_not_executed(self):
        providers=FakeProviders([response([('run_command',{'argv':['python','-c','raise Exception()'],'cwd':'.','timeout':5})]),response(text='실행 거절 확인',finish='stop')])
        agents=Agents(self.store,providers,self.gpu)
        task=agents.start(self.root,'실행')
        pending=until(lambda: (t if (t:=self.store.get('task',task['id'])).get('approval') else None))
        with patch.object(Workspace,'run',side_effect=AssertionError('must not run')):
            agents.decide(task['id'],pending['approval']['id'],False)
            until(lambda:self.store.get('task',task['id'])['status']=='succeeded')
    def test_cancel_while_waiting_stays_cancelled(self):
        providers=FakeProviders([response([('run_command',{'argv':['python','-c','print(1)'],'cwd':'.','timeout':5})])])
        agents=Agents(self.store,providers,self.gpu)
        task=agents.start(self.root,'실행')
        until(lambda:self.store.get('task',task['id']).get('approval'))
        agents.cancel(task['id'])
        time.sleep(.4)
        self.assertEqual(self.store.get('task',task['id'])['status'],'cancelled')
    def test_same_tool_failure_stops_after_two(self):
        missing=('read_file',{'path':'missing.py'})
        providers=FakeProviders([response([missing]),response([missing]),response(text='must not be called',finish='stop')])
        agents=Agents(self.store,providers,self.gpu)
        task=agents.start(self.root,'read missing file',max_steps=8)
        result=until(lambda: (t if (t:=self.store.get('task',task['id']))['status']=='failed' else None))
        self.assertEqual(providers.calls,2)
        self.assertIn('2회',result['error'])
    def test_remote_telemetry_keeps_allocator_and_device_memory_separate(self):
        from workbench.remote_runner import Sampler
        cuda=NS(is_initialized=lambda:True,get_device_name=lambda:'offline GPU',get_device_properties=lambda index:NS(total_memory=16*1024**3),mem_get_info=lambda:(12*1024**3,16*1024**3),memory_allocated=lambda:2*1048576,memory_reserved=lambda:4*1048576,max_memory_allocated=lambda:3*1048576,max_memory_reserved=lambda:5*1048576)
        sampler=Sampler(self.base,'offline-run')
        with patch('workbench.remote_runner.subprocess.check_output',return_value='50, 1024, 15360, 45, 30, 70, 1000, 2000, 16384, P0, 0x0'),patch.dict(sys.modules,{'torch':NS(cuda=cuda)}):
            sampler.sample()
        row=sampler.rows[0]
        self.assertEqual(row['torch_allocated_mib'],2)
        self.assertEqual(row['torch_reserved_mib'],4)
        self.assertEqual(row['nvml_vram_used_mib'],1024)
        self.assertIsNone(row['windows_shared_gpu_mib'])


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory()
        with patch('workbench.server.refresh_keys'),patch('workbench.providers.refresh_keys'):
            cls.app=Application(Path(cls.temp.name)/'state')
        cls.server=LocalHTTPServer(('127.0.0.1',0),make_handler(cls.app))
        cls.port=cls.server.server_address[1]
        cls.url='http://127.0.0.1:'+str(cls.port)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.app.store.db.close();cls.temp.cleanup()
    def test_home_has_local_csp(self):
        with urlopen(self.url) as r:
            self.assertEqual(r.status,200)
            self.assertIn("frame-ancestors 'none'",r.headers['Content-Security-Policy'])
            self.assertIn('<title>Muse Platform</title>',r.read().decode())
    def test_foreign_host_and_origin_blocked(self):
        for headers in ({'Host':'evil.invalid'},{'Origin':'https://evil.invalid'}):
            with self.assertRaises(HTTPError) as c:
                urlopen(Request(self.url+'/api/bootstrap',headers=headers))
            self.assertEqual(c.exception.code,403)
            c.exception.close()
    def test_post_requires_session_token(self):
        request=Request(self.url+'/api/settings',data=b'{}',headers={'Content-Type':'application/json'})
        with self.assertRaises(HTTPError) as c:
            urlopen(request)
        self.assertEqual(c.exception.code,403)
        c.exception.close()
    def test_workspace_files_via_http(self):
        root=Path(self.temp.name)/'folder';root.mkdir(exist_ok=True)
        request=Request(self.url+'/api/workspace',data=json.dumps({'root':str(root)}).encode(),headers={'Content-Type':'application/json','X-Local-Token':self.app.token})
        with urlopen(request) as r:
            self.assertEqual(json.load(r)['root'],str(root.resolve()))
    def test_shutdown_needs_explicit_confirmation(self):
        request=Request(self.url+'/api/shutdown',data=b'{}',headers={'Content-Type':'application/json','X-Local-Token':self.app.token})
        with self.assertRaises(HTTPError) as c:
            urlopen(request)
        self.assertEqual(c.exception.code,400)
        c.exception.close()
        with urlopen(self.url) as response:
            self.assertEqual(response.status,200)
    def test_second_listener_cannot_share_port(self):
        with self.assertRaises(OSError):
            server=LocalHTTPServer(('127.0.0.1',self.port),make_handler(self.app))
            server.server_close()


if __name__=='__main__':
    unittest.main()
