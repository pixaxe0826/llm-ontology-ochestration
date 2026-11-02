"""Real local Python children; no provider, GPU or operating-system settings changes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from workbench.workspace import Workspace
from workbench.goal_prompts import system
from workbench.agent import SYSTEM


class CommandUTF8Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.folder=self.base/'workspace with spaces';self.folder.mkdir()
        self.ws=Workspace(self.folder,self.base/'state')

    def tearDown(self):self.temp.cleanup()

    def child(self,code,**kwargs):
        result=self.ws.run([sys.executable,'-c',code],timeout=10,**kwargs)
        self.assertEqual(result['exit_code'],0,result['output']);self.assertFalse(result['interrupted'])
        return json.loads(result['output'])

    def test_child_utf8_mode_overrides_legacy_parent_without_mutating_it(self):
        with patch.dict(os.environ,{'PYTHONUTF8':'0','PYTHONIOENCODING':'cp949'}):
            before=dict(os.environ)
            result=self.child('import json,sys,os; print(json.dumps({"mode":sys.flags.utf8_mode,"io":os.environ["PYTHONIOENCODING"],"utf8":os.environ["PYTHONUTF8"]}))')
            self.assertEqual(result,{'mode':1,'io':'utf-8','utf8':'1'})
            self.assertTrue(dict(os.environ)==before,'Parent environment changed.')

    def test_utf8_yaml_with_non_ascii_reads_without_explicit_encoding(self):
        value='컨베이어 → 압력 µ'
        (self.folder/'spec.yaml').write_text('label: '+json.dumps(value,ensure_ascii=False)+'\n',encoding='utf-8')
        with patch.dict(os.environ,{'PYTHONUTF8':'0','PYTHONIOENCODING':'cp949'}):
            result=self.child('import json,yaml; f=open("spec.yaml"); value=yaml.safe_load(f)["label"]; print(json.dumps({"value":value,"encoding":f.encoding},ensure_ascii=False)); f.close()')
        self.assertEqual(result['value'],value);self.assertEqual(result['encoding'].lower().replace('-',''),'utf8')

    def test_key_removal_and_goal_proxy_injection_are_preserved(self):
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-utf8-provider-secret','MODAL_PROXY_TOKEN':'synthetic-utf8-modal-secret'}):
            result=self.child('import os,json,sys; print(json.dumps({"mode":sys.flags.utf8_mode,"provider":("MODEL_API_KEY" in os.environ or "MODAL_PROXY_TOKEN" in os.environ),"proxy":bool(os.environ.get("MUSE_PROXY_TOKEN")),"base":os.environ.get("MUSE_PROXY_BASE_URL")}))',
                extra_env={'MUSE_PROXY_BASE_URL':'http://127.0.0.1:8765/proxy/v1','MUSE_PROXY_TOKEN':'synthetic-goal-proxy'})
        self.assertEqual(result,{'mode':1,'provider':False,'proxy':True,'base':'http://127.0.0.1:8765/proxy/v1'})
        with self.assertRaisesRegex(ValueError,'Only Goal proxy'):
            self.ws.run([sys.executable,'-c','pass'],extra_env={'PYTHONUTF8':'0'})

    def test_utf8_file_rule_is_goal_builder_only(self):
        goal={'output_language':'English','timezone':'America/New_York'}
        rule="Always open text files with encoding='utf-8'."
        self.assertIn(rule,system(goal,'Builder'));self.assertNotIn(rule,system(goal,'Planner'));self.assertNotIn(rule,SYSTEM)


if __name__=='__main__':unittest.main()
