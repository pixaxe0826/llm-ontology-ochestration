"""Root aliases use the same directory and policy without weakening path checks."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.agent import Agents,TOOLS
from workbench.command_cwd import MISSING,normalize_cwd
from workbench.goal_policy import command_assessment,policy
from workbench.goals import GoalManager
from workbench.gpu import GPUManager
from workbench.memory import Memory
from workbench.security import register_secret
from workbench.store import Store
from workbench.workspace import Workspace
from test_goals import FakeProviders,until
from test_workbench import FakeProviders as ToolProviders,response

ALIASES=('', 'null', 'None', 'none', 'undefined', './', '.\\', None, MISSING)


class CommandCwdTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.folder=self.base/'workspace with spaces';self.folder.mkdir();self.store=Store(self.base/'state')
        self.ws=Workspace(self.folder,self.store.root);self.gpu=GPUManager(self.store)
        (self.folder/'tools').mkdir();(self.folder/'review').mkdir()
        (self.folder/'tools/proxy_check.py').write_text('from pathlib import Path\nprint(Path.cwd())\n',encoding='utf-8')
        self.argv=['python','tools/proxy_check.py','review/PROXY_EVIDENCE.md'];self.auto=policy({'mode':'autonomous'})
        self.memory=Memory(self.store);self.manager=GoalManager(self.store,FakeProviders(),self.gpu,self.memory)
    def tearDown(self):
        self.manager.shutdown()
        for thread in list(self.manager.threads.values()):thread.join(3)
        until(lambda:not self.manager.builders.active)
        self.memory.pending.join();self.store.on_save=None;self.store.db.close();self.temp.cleanup()
    def test_each_root_alias_and_missing_argument_normalize_with_notice(self):
        for value in ALIASES:
            with self.subTest(value=value):
                cwd,notice=normalize_cwd() if value is MISSING else normalize_cwd(value)
                self.assertEqual(cwd,'.');self.assertIn('cwd normalized:',notice)
        self.assertEqual(normalize_cwd('null')[1],"cwd normalized: 'null' -> '.'")
    def test_real_process_uses_root_and_preserves_stdout_for_every_alias(self):
        for value in ALIASES:
            with self.subTest(value=value):
                args={} if value is MISSING else {'cwd':value}
                result=self.ws.run([sys.executable,'tools/proxy_check.py'],timeout=5,**args)
                self.assertEqual(result['exit_code'],0);self.assertEqual(result['output'].strip(),str(self.folder))
                self.assertIn('cwd normalized:',result['cwd_normalization']);self.assertEqual(result['cwd'],'.')
                self.assertNotIn('cwd normalized:',result['output'])
    def test_real_relative_directory_and_canonical_root_are_unchanged(self):
        sub=self.folder/'null folder';sub.mkdir()
        for value in ('.','null folder'):
            result=self.ws.run([sys.executable,'-c','from pathlib import Path; print(Path.cwd())'],cwd=value,timeout=5)
            self.assertEqual(result['output'].strip(),str(self.folder if value=='.' else sub))
            self.assertNotIn('cwd_normalization',result)
    def test_quoted_root_alias_from_real_integration_normalizes_before_policy_and_execution(self):
        for value in ('"."',"'.'",'"./"',"'.\\'",'"null"',"'None'",'""'):
            with self.subTest(value=value):
                self.assertTrue(command_assessment(self.folder,self.store.root,self.argv,value,self.auto)['auto'])
                result=self.ws.run([sys.executable,'tools/proxy_check.py'],cwd=value,timeout=5)
                self.assertEqual(result['exit_code'],0);self.assertEqual(result['output'].strip(),str(self.folder))
                self.assertIn('cwd normalized:',result['cwd_normalization'])
    def test_quote_handling_does_not_unquote_arbitrary_paths_or_weaken_boundaries(self):
        for value in ('"../outside"','"C:\\Windows"','".env"','"missing"','"unterminated'):
            self.assertEqual(normalize_cwd(value),(value,None))
            with self.subTest(value=value),self.assertRaises(ValueError):self.ws.run([sys.executable],cwd=value)
    def test_non_path_types_are_rejected_without_coercion(self):
        for value in (False,0,[],{},['.']):
            with self.subTest(value=value),self.assertRaisesRegex(ValueError,'relative folder string or null'):normalize_cwd(value)
    def test_bad_cwd_still_rejected_before_any_process_starts(self):
        for value in ('..','../null','null/../../outside','.env','.git','secrets',str(self.folder),'C:\\Windows','x\x00y'):
            with self.subTest(value=value),patch('workbench.workspace.subprocess.Popen') as proc:
                with self.assertRaises(ValueError):self.ws.run([sys.executable,'tools/proxy_check.py'],cwd=value)
                proc.assert_not_called()
    def test_missing_directory_has_specific_english_error_in_execution_and_policy(self):
        message="cwd 'missing folder' does not exist in the workspace. Use '.' for the workspace root or a relative folder path."
        for invoke in (lambda:self.ws.run([sys.executable],cwd='missing folder'),lambda:command_assessment(self.folder,self.store.root,self.argv,'missing folder',self.auto)):
            with self.subTest(invoke=invoke),self.assertRaises(ValueError) as caught:invoke()
            self.assertEqual(str(caught.exception),message)
    def test_file_is_not_accepted_as_command_directory(self):
        with self.assertRaisesRegex(ValueError,'is not a directory in the workspace'):self.ws.run([sys.executable],cwd='tools/proxy_check.py')
    def test_normalized_auto_approval_matches_root_and_keeps_hard_denials(self):
        expected=command_assessment(self.folder,self.store.root,self.argv,'.',self.auto)
        for value in ALIASES:
            kwargs={} if value is MISSING else {'cwd':value}
            with self.subTest(value=value):
                self.assertEqual(command_assessment(self.folder,self.store.root,self.argv,settings=self.auto,**kwargs),expected)
                self.assertFalse(command_assessment(self.folder,self.store.root,self.argv,settings=policy({'mode':'manual'}),**kwargs)['auto'])
                for argv in (['git','push'],['python','../outside.py'],['python','.env']):
                    with self.assertRaises((ValueError,PermissionError)):command_assessment(self.folder,self.store.root,argv,settings=self.auto,**kwargs)
    def test_secret_values_are_not_echoed_in_cwd_errors(self):
        secret='synthetic-cwd-protected-value-293944';register_secret(secret)
        with self.assertRaises(ValueError) as caught:self.ws.run([sys.executable],cwd=secret)
        self.assertNotIn(secret,str(caught.exception));self.assertIn('protected value',str(caught.exception))
    def test_tool_schema_and_approval_use_optional_normalized_cwd(self):
        schema=next(t['function']['parameters'] for t in TOOLS if t['function']['name']=='run_command')
        self.assertNotIn('cwd',schema['required']);self.assertEqual(schema['properties']['cwd']['type'],['string','null'])
        agents=Agents(self.store,ToolProviders(),self.gpu)
        task=self.store.save('task',{'workspace':str(self.folder),'status':'running','allow_write':True,'events':[]})
        agents.cancels[task['id']]=threading.Event()
        with patch.object(agents,'approval',return_value=False) as approve:
            result=agents.execute(task['id'],'run_command',{'argv':self.argv,'cwd':'null','timeout':5})
        self.assertTrue(result['denied']);self.assertEqual(approve.call_args.args[2]['cwd'],'.')
        self.assertEqual(result['cwd_normalization'],"cwd normalized: 'null' -> '.'")
    def test_model_sees_normalization_on_execution_failure_too(self):
        providers=ToolProviders([response([('run_command',{'argv':['nonexistent-executable-for-cwd-test'],'cwd':'null','timeout':5})]),response(text='Observed failure.',finish='stop')])
        agents=Agents(self.store,providers,self.gpu)
        with patch.object(agents,'approval',return_value=True):
            task=agents.start(str(self.folder),'Test failure metadata.')
            until(lambda:task['id'] not in agents.active)
        event=next(e for e in self.store.get('task',task['id'])['events'] if e['kind']=='tool_result')
        self.assertIn('error',event['result']);self.assertEqual(event['result']['cwd_normalization'],"cwd normalized: 'null' -> '.'")
    def test_goal_builder_original_null_failure_is_fixed_without_approval(self):
        providers=self.manager.providers;original=providers.complete
        def complete(*args,**kwargs):
            result=original(*args,**kwargs)
            for call in result.choices[0].message.tool_calls or []:
                if call.function.name=='run_command':
                    data=json.loads(call.function.arguments);data['cwd']='null';call.function.arguments=json.dumps(data)
            return result
        providers.complete=complete
        goal=self.manager.create({'title':'Cwd reproduction','objective':'Implement and verify a small unit test.','workspace':str(self.folder),
            'policy':{'mode':'autonomous','gpus':[]}},start=True)['goal']
        final=until(lambda:(g if (g:=self.store.get('goal',goal['id']))['status']=='done' else None))
        self.assertEqual(final['status'],'done');self.assertEqual(self.manager.approvals(goal['id']),[])
        task=self.store.get('task',self.manager.items(goal['id'])[0]['task_id'])
        result=next(e['result'] for e in task['events'] if e['kind']=='tool_result' and e['name']=='run_command')
        self.assertEqual(result['exit_code'],0);self.assertEqual(result['cwd_normalization'],"cwd normalized: 'null' -> '.'")
    def test_recovery_evidence_accepts_normalized_root_alias(self):
        goal=self.manager.create({'title':'Recovery cwd','objective':'Verify a bounded script.','workspace':str(self.folder)},start=False)['goal']
        sha=self.ws.read('tools/proxy_check.py')['sha256']
        item={'rerun_requirement':{'script':'tools/proxy_check.py','script_sha256':sha,'argv':self.argv,'stale_task_ids':[]}}
        for value in ALIASES:
            arguments={'argv':self.argv,**({} if value is MISSING else {'cwd':value})}
            task=self.store.save('task',{'events':[{'kind':'tool','name':'run_command','arguments':arguments},
                {'kind':'tool_result','name':'run_command','result':{'exit_code':0}}]})
            valid={'observed':{'kind':'run_command','task_id':task['id'],'event_index':1}}
            self.assertTrue(self.manager.fresh_rerun_observed(goal,item,valid))


if __name__=='__main__':unittest.main()
