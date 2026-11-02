"""Real short-lived local processes, fake model providers; no cloud or GPU execution."""
import hashlib,json,os,subprocess,sys,threading,time,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
import test_goals as fixtures
from workbench.long_commands import ACTIVE,TOOLS
from workbench.goal_costs import GoalLimit
from workbench.goal_prompts import system
from workbench.workspace import Workspace

def fixture(test,manager,folder,goal=None):
    goal=goal or manager.create({'title':'Async verification','objective':'Verify asynchronous commands.','workspace':str(folder),
        'policy':{'mode':'autonomous'},'deadline':time.time()+3600},start=False)['goal']
    manager.add_items(goal,[{'key':'async','title':'Evaluate episodes','acceptance':['Observed execution succeeds.'],'depends_on':[],'blocked_by_decision':[]}])
    item=manager.items(goal['id'])[0]
    task=manager.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'workspace':str(folder),
        'status':'running','allow_write':True,'events':[],'messages':[],'max_steps':8})
    manager.builders.cancels[task['id']]=threading.Event()
    manager.store.update('plan_item',item['id'],task_id=task['id'],task_ids=[task['id']],status='in_progress',bundles=1,attempts=1)
    return goal,item,task

def terminal(manager,identifier):
    def completed():
        with manager.lock:
            row=manager.store.get('goal_long_command',identifier)
            return row if row['status'] not in ACTIVE and identifier not in manager.long_commands.runtime else None
    return fixtures.until(completed,timeout=15)

class LongCommandTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown

    def prepare(self,source="print('done', flush=True)\n",**kwargs):
        (self.folder/'evaluate.py').write_text(source,encoding='utf-8');self.goal,self.item,self.task=fixture(self,self.manager,self.folder,**kwargs)

    def start(self,**kwargs):
        return self.manager.builders.execute(self.task['id'],'start_long_command',{'argv':['python','evaluate.py'],'cwd':'.','timeout_s':10,'label':'Episodes',**kwargs})

    def test_start_returns_immediately_and_check_exposes_incremental_output(self):
        self.prepare("import time\nprint('episode started',flush=True)\ntime.sleep(1.1)\nprint('episode done',flush=True)\n")
        started=time.monotonic();row=self.start();self.assertLess(time.monotonic()-started,.8)
        def tail():
            value=self.manager.builders.execute(self.task['id'],'check_long_command',{'id':row['id']})
            return value if 'episode started' in value['output_tail'] and value['status']=='running' else None
        value=fixtures.until(tail);self.assertGreaterEqual(value['elapsed_s'],0)
        final=terminal(self.manager,row['id']);self.assertEqual(final['status'],'succeeded');self.assertEqual(final['result']['exit_code'],0)

    def test_completion_uses_regular_command_evidence_with_input_output_hashes(self):
        self.prepare("from pathlib import Path\nvalue=Path('input.txt').read_text(encoding='utf-8')\nPath('metrics.json').write_text(value,encoding='utf-8')\nprint('PASS')\n")
        (self.folder/'input.txt').write_text('episodes=3',encoding='utf-8')
        final=terminal(self.manager,self.start()['id']);result=final['result'];self.assertEqual(result['exit_code'],0)
        files=result['command_files'];self.assertIn('evaluate.py',files['inputs']);self.assertIn('input.txt',files['inputs'])
        self.assertEqual(files['outputs']['metrics.json'],hashlib.sha256(b'episodes=3').hexdigest())
        item=self.store.get('plan_item',self.item['id']);valid=self.manager.valid_evidence(self.goal,item)
        self.assertEqual(valid[final['evidence_id']]['kind'],'run_command');self.assertTrue(valid[final['evidence_id']]['execution']['success'])
        self.assertTrue(any(e['kind']=='command_output' and e['path']=='metrics.json' for e in valid.values()))
        (self.folder/'evaluate.py').write_text('print(1)',encoding='utf-8')
        self.assertFalse(self.manager.observed_evidence(self.goal,item)[final['evidence_id']]['supports_acceptance'])

    def test_only_one_active_command_per_goal_then_slot_is_reusable(self):
        self.prepare('import time\ntime.sleep(.8)\n');row=self.start()
        with self.assertRaisesRegex(ValueError,'one long command'):self.start()
        self.assertEqual(len(self.manager.long_commands.rows(self.goal['id'])),1)
        terminal(self.manager,row['id']);terminal(self.manager,self.start()['id'])

    def test_goal_scoped_ownership_prevents_check_and_cancel_across_goals(self):
        self.prepare('import time\ntime.sleep(.6)\n');row=self.start()
        other=self.manager.create({'title':'Other','objective':'Other work.','workspace':str(self.folder),'deadline':time.time()+3600},start=False)['goal']
        _,_,task=fixture(self,self.manager,self.folder,other)
        for name in ('check_long_command','cancel_long_command'):
            with self.assertRaises(PermissionError):self.manager.builders.execute(task['id'],name,{'id':row['id']})
        terminal(self.manager,row['id'])

    def test_cancel_and_timeout_are_failed_observations_never_success(self):
        self.prepare('import time\nprint("waiting",flush=True)\ntime.sleep(20)\n')
        row=self.start();fixtures.until(lambda:self.store.get('goal_long_command',row['id'])['status']=='running')
        self.manager.builders.execute(self.task['id'],'cancel_long_command',{'id':row['id']})
        final=terminal(self.manager,row['id']);self.assertEqual(final['status'],'cancelled');self.assertTrue(final['result']['cancelled'])
        timed=terminal(self.manager,self.start(timeout_s=1)['id']);self.assertEqual(timed['status'],'timed_out');self.assertTrue(timed['result']['timed_out'])
        valid=self.manager.valid_evidence(self.goal,self.store.get('plan_item',self.item['id']));self.assertNotIn(timed['evidence_id'],valid)

    def test_pause_leaves_command_running_and_stop_terminates_it(self):
        self.prepare('import time\nfrom pathlib import Path\ntime.sleep(.8)\nPath("finished.txt").write_text("done",encoding="utf-8")\n')
        row=self.start();fixtures.until(lambda:self.store.get('goal_long_command',row['id'])['status']=='running')
        self.manager.control(self.goal['id'],'pause');self.assertTrue(self.manager.long_commands.active(self.goal['id']))
        self.assertEqual(terminal(self.manager,row['id'])['status'],'succeeded');self.assertTrue((self.folder/'finished.txt').exists())
        self.store.update('goal',self.goal['id'],status='running');row=self.start()
        self.manager.control(self.goal['id'],'stop');self.assertEqual(terminal(self.manager,row['id'])['status'],'cancelled')

    def test_shutdown_terminates_and_records_interrupted_without_replay(self):
        self.prepare('import time\ntime.sleep(20)\n');row=self.start()
        fixtures.until(lambda:self.store.get('goal_long_command',row['id'])['status']=='running')
        self.manager.shutdown();final=self.store.get('goal_long_command',row['id']);self.assertEqual(final['status'],'interrupted')
        self.assertTrue(final['result']['interrupted']);self.assertFalse(self.manager.long_commands.runtime)
        from workbench.goals import GoalManager
        with patch.object(Workspace,'run',side_effect=AssertionError('No replay')):
            other=GoalManager(self.store,self.providers,self.manager.gpu,self.memory);self.assertEqual(other.long_commands.rows(self.goal['id'])[0]['status'],'interrupted');other.shutdown()

    def test_stale_running_record_is_interrupted_on_startup_and_evidence_is_failure(self):
        self.prepare();row=self.store.save('goal_long_command',{'goal_id':self.goal['id'],'task_id':self.task['id'],'plan_item_id':self.item['id'],
            'status':'running','created':time.time()-10,'timeout_s':10,'argv':['python','evaluate.py'],'cwd':'.','label':'stale','input_files':{},'output_tail':'last output'})
        from workbench.long_commands import LongCommands
        with patch.object(Workspace,'run',side_effect=AssertionError('No replay')):restarted=LongCommands(self.manager)
        record=self.store.get('goal_long_command',row['id']);self.assertEqual(record['status'],'interrupted');self.assertIsNone(record['result']['exit_code']);restarted.shutdown()

    def test_timeout_validation_and_normal_commands_keep_120_second_cap(self):
        self.prepare()
        for value in (0,-1,1801,True,1.5,'600',None):
            with self.assertRaises(ValueError):self.start(timeout_s=value)
        row=self.start(timeout_s=1800);self.assertEqual(terminal(self.manager,row['id'])['result']['timeout_s'],1800)
        result=Workspace(str(self.folder),self.store.root).run([sys.executable,'evaluate.py'],timeout=1800)
        self.assertEqual(result['timeout_s'],120)

    def test_cwd_normalization_and_safe_policy_are_shared(self):
        self.prepare();row=self.start(cwd='null');self.assertIn('cwd normalized',json.dumps(row))
        final=terminal(self.manager,row['id']);self.assertEqual(final['cwd'],'.')
        for values in ({'cwd':'../outside'},{'argv':['git','push']},{'argv':['python','../outside.py']},{'argv':['python','-c','print(1)']}):
            with self.assertRaises((ValueError,PermissionError)):self.start(**values)

    def test_workspace_with_spaces_uses_exact_argv_without_a_shell(self):
        self.folder=self.folder/'episode workspace';self.folder.mkdir()
        self.prepare("import sys\nfrom pathlib import Path\nPath(sys.argv[1]).write_text('done',encoding='utf-8')\n")
        final=terminal(self.manager,self.start(argv=['python','evaluate.py','episode results.txt'])['id'])
        self.assertEqual(final['result']['exit_code'],0);self.assertTrue((self.folder/'episode results.txt').exists())

    def test_manual_policy_uses_existing_command_approval_and_exact_timeout(self):
        self.prepare();self.store.update('goal',self.goal['id'],policy={**self.goal['policy'],'mode':'manual'})
        with patch.object(self.manager.builders,'approval',return_value=False) as approval:
            result=self.start(timeout_s=1600)
        self.assertTrue(result['denied']);self.assertFalse(self.manager.long_commands.rows(self.goal['id']))
        self.assertEqual(approval.call_args.args[1],'command');payload=approval.call_args.args[2]
        self.assertEqual(payload['timeout'],1600);self.assertEqual(payload['argv'],['python','evaluate.py'])

    def test_child_has_utf8_and_only_command_proxy_credentials(self):
        source="""import os,sys
from pathlib import Path
print(sys.flags.utf8_mode,os.environ.get('PYTHONIOENCODING'),flush=True)
print(Path('input.yaml').read_text(),flush=True)
print('keys_removed='+str(all(not os.environ.get(k) for k in ('MODEL_API_KEY','BASETEN_API_KEY','MODAL_PROXY_TOKEN','MODAL_TOKEN_SECRET'))))
print('proxy_present='+str(bool(os.environ.get('MUSE_PROXY_TOKEN'))))
"""
        self.prepare(source);(self.folder/'input.yaml').write_text('기계: conveyor',encoding='utf-8')
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-long-meta-key-119','BASETEN_API_KEY':'synthetic-long-baseten-key-492'}):
            final=terminal(self.manager,self.start()['id'])
        output=final['result']['output'];self.assertIn('1 utf-8',output);self.assertIn('기계: conveyor',output);self.assertIn('keys_removed=True',output);self.assertIn('proxy_present=True',output)
        from workbench.security import contains_key
        self.assertFalse(contains_key(output))

    def test_pending_command_parks_item_without_review_and_independent_item_is_ready(self):
        self.prepare('import time\ntime.sleep(1.2)\n');row=self.start()
        self.goal=self.store.update('goal',self.goal['id'],no_progress=2)
        self.manager.add_items(self.goal,[{'key':'independent','title':'Independent docs','acceptance':['File exists.'],'depends_on':[],'blocked_by_decision':[]}])
        self.store.update('task',self.task['id'],status='succeeded',builder_report={'chunk_id':'chunk','status':'progress','summary':'Async evaluation is running.','verification':'Pending result.'})
        with patch.object(self.manager,'review') as review:
            self.manager.finished_batch(self.goal,self.store.get('plan_item',self.item['id']),self.store.get('task',self.task['id']))
            waiting=self.store.get('plan_item',self.item['id']);self.assertEqual(waiting['wait_reason'],'long_command')
            self.assertEqual(self.store.get('goal',self.goal['id'])['no_progress'],2)
            self.manager.reconcile_waiting(self.goal);self.assertFalse(self.manager.review_ready(waiting))
            self.assertEqual(self.manager.next_builder(self.goal)['key'],'independent');review.assert_not_called()
        terminal(self.manager,row['id']);ready=self.store.get('plan_item',self.item['id']);self.assertTrue(ready['continue_pending']);self.assertEqual(ready['wait_reason'],'continuation')
        self.assertEqual(self.store.get('goal',self.goal['id'])['no_progress'],0)

    def test_waiting_for_command_does_not_spend_builder_or_planner_calls(self):
        self.prepare('import time\ntime.sleep(.9)\n');row=self.start();self.manager.park_item(self.item,'long_command',resume='continue',continue_pending=False)
        before=len(self.providers.calls)
        for _ in range(4):
            self.manager.reconcile_waiting(self.goal);self.assertIsNone(self.manager.next_builder(self.goal));time.sleep(.05)
        self.assertEqual(len(self.providers.calls),before);terminal(self.manager,row['id'])

    def test_coordinator_waits_then_continues_once_and_finishes_with_real_command_evidence(self):
        (self.folder/'evaluate.py').write_text('import time\nprint("episode",flush=True)\ntime.sleep(1.3)\n',encoding='utf-8')
        class AsyncProviders(fixtures.FakeProviders):
            def __init__(self):super().__init__();self.meta_calls=0
            def complete(self,provider,messages,model,*args,**kwargs):
                if provider!='meta':return super().complete(provider,messages,model,*args,**kwargs)
                self.meta_calls+=1
                if self.meta_calls==1:return fixtures.response(calls=[('start_long_command',{'argv':['python','evaluate.py'],'timeout_s':10,'label':'Episodes'})])
                return fixtures.response(calls=[('report_item',{'status':'progress' if self.meta_calls==2 else 'completed','summary':'Evaluation running.' if self.meta_calls==2 else 'Evaluation finished.','verification':'Inspect observed command result.'})])
        self.providers=AsyncProviders();self.manager.providers=self.providers
        goal=self.manager.create({'title':'Async end to end','objective':'Run one episode and verify it.','workspace':str(self.folder),'policy':{'mode':'autonomous'},'deadline':time.time()+120})['goal']
        fixtures.until(lambda:any(i.get('wait_reason')=='long_command' for i in self.manager.items(goal['id'])))
        count=self.providers.meta_calls;time.sleep(.25);self.assertEqual(self.providers.meta_calls,count)
        final=fixtures.until(lambda:g if (g:=self.store.get('goal',goal['id']))['status']=='done' else None)
        self.assertEqual(final['status'],'done');self.assertEqual(self.providers.meta_calls,3)
        self.assertEqual(len(self.manager.long_commands.rows(goal['id'])),1)
        self.assertTrue(any(e['kind']=='run_command' for i in self.manager.items(goal['id']) for e in i['evidence']))

    def test_one_slot_is_independent_per_goal(self):
        self.prepare('import time\ntime.sleep(.7)\n');first=self.start()
        other=self.manager.create({'title':'Other','objective':'Independent evaluation.','workspace':str(self.folder),'policy':{'mode':'autonomous'},'deadline':time.time()+120},start=False)['goal']
        _,_,task=fixture(self,self.manager,self.folder,other)
        second=self.manager.builders.execute(task['id'],'start_long_command',{'argv':['python','evaluate.py'],'timeout_s':10,'label':'Other episode'})
        self.assertNotEqual(first['id'],second['id']);self.assertEqual(len(self.manager.long_commands.active()),2)
        terminal(self.manager,first['id']);terminal(self.manager,second['id'])

    @unittest.skipUnless(os.name=='nt','Windows Job Object supervision')
    def test_windows_server_exit_kills_supervised_process_tree(self):
        self.prepare()
        # A separate short helper owns the job, then exits abruptly. Its child's marker must never appear.
        helper=self.folder/'supervisor.py';marker=self.folder/'escaped.txt'
        helper.write_text("import os,subprocess,sys,time\nfrom workbench.command_process import ProcessTree\np=subprocess.Popen([sys.executable,'-c',\"import time;from pathlib import Path;time.sleep(1.5);Path('escaped.txt').write_text('bad')\"],cwd=os.getcwd())\nj=ProcessTree(p)\nprint('attached',flush=True)\nos._exit(0)\n",encoding='utf-8')
        from workbench.security import child_environment
        env=child_environment();env['PYTHONPATH']=str(Path(__file__).resolve().parent.parent)
        run=subprocess.run([sys.executable,str(helper)],cwd=self.folder,env=env,capture_output=True,timeout=10)
        self.assertEqual(run.returncode,0);self.assertIn(b'attached',run.stdout);time.sleep(1.7);self.assertFalse(marker.exists())

    def test_tools_are_goal_only_and_builder_prompt_explains_waiting_and_limits(self):
        from workbench.agent import TOOLS as WORK_TOOLS
        names={t['function']['name'] for t in TOOLS};self.assertEqual(names,{'start_long_command','check_long_command','cancel_long_command'})
        self.assertFalse(names&{t['function']['name'] for t in WORK_TOOLS})
        self.prepare();prompt=system(self.goal,'Builder');self.assertIn('1800',prompt);self.assertIn('start_long_command',prompt);self.assertIn('without model calls',prompt)

    def test_large_tool_view_keeps_status_elapsed_cost_hash_summary_and_output_tail(self):
        from workbench.file_views import bounded_result,encoded
        hashes={('deep-folder/'*20)+str(i)+'.py':'a'*64 for i in range(150)}
        result={'id':'command','label':'Episodes','argv':['python','evaluate.py'],'status':'succeeded','timeout_s':1800,'elapsed_s':122,
            'output_tail':'x'*8000+'important end','proxy_cost':{'estimated_usd':.25,'ledger_ids':['ledger-'+str(i) for i in range(120)]},
            'result':{'exit_code':0,'duration_s':122,'command_files':{'inputs':hashes,'outputs':{'metrics.json':'b'*64}}}}
        view=bounded_result(result);self.assertLessEqual(len(encoded(view)),8000)
        self.assertEqual(view['status'],'succeeded');self.assertEqual(view['elapsed_s'],122);self.assertEqual(view['result']['exit_code'],0)
        self.assertEqual(view['proxy_cost']['estimated_usd'],.25);self.assertEqual(view['proxy_cost']['ledger_ids_omitted'],100)
        self.assertGreater(view['result']['command_files']['inputs_omitted'],0);self.assertTrue(view['output_tail'].endswith('important end'))

    def test_running_settings_wait_for_long_stage_boundary(self):
        self.prepare('import time\ntime.sleep(.8)\n');row=self.start()
        self.store.update('goal',self.goal['id'],status='running')
        self.manager.update(self.goal['id'],{'builder_model':'muse-spark-1.2'})
        self.assertFalse(self.manager.apply_pending_settings(self.goal['id']))
        goal=self.store.get('goal',self.goal['id']);self.assertEqual(goal['builder_model'],'muse-spark-1.3');self.assertTrue(goal['pending_settings'])
        terminal(self.manager,row['id']);self.assertTrue(self.manager.apply_pending_settings(self.goal['id']))
        self.assertEqual(self.store.get('goal',self.goal['id'])['builder_model'],'muse-spark-1.2')

    def test_builder_context_retains_newest_command_when_history_exceeds_eight(self):
        self.prepare();identifiers=[]
        for index in range(12):
            row=self.store.save('goal_long_command',{'goal_id':self.goal['id'],'plan_item_id':self.item['id'],'task_id':self.task['id'],
                'status':'succeeded','created':time.time(),'argv':['python','evaluate.py'],'timeout_s':10,'label':'Episode '+str(index)})
            identifiers.append(row['id'])
        context=self.manager.context(self.goal,self.item,'Builder')
        current=json.loads(next(m['content'] for m in context if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])
        shown=[r['id'] for r in current['item']['long_commands']]
        self.assertEqual(len(shown),8);self.assertIn(identifiers[-1],shown);self.assertNotIn(identifiers[0],shown)

class LongCommandProxyTests(unittest.TestCase):
    setUp=fixtures.GoalHTTPTests.setUp
    tearDown=fixtures.GoalHTTPTests.tearDown

    def prepare(self,budget=False,sam=False):
        self.manager=self.app.goals;self.manager.proxy_base=self.url+'/proxy/v1'
        goal,self.item,self.task=fixture(self,self.manager,self.folder,self.goal);self.goal=goal
        self.manager.store.update('goal',goal['id'],policy={**goal['policy'],'mode':'autonomous'})
        if budget:self.manager.store.update('goal',goal['id'],budgets=self.manager.ledger.budgets({'baseten':{'total':0,'reserve':0}}))
        (self.folder/'proxy_episode.py').write_text("""import json,os
from urllib.request import Request,urlopen
from urllib.error import HTTPError
request=Request(os.environ['MUSE_PROXY_BASE_URL']+'/chat/completions',data=json.dumps({'model':'muse-spark-1026','messages':[{'role':'user','content':'One episode'}]}).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+os.environ['MUSE_PROXY_TOKEN']})
try:
 with urlopen(request,timeout=10) as response:print('episode HTTP '+str(response.status))
except HTTPError as error:
 print('episode HTTP '+str(error.code));error.close()
""",encoding='utf-8')

    def test_proxy_usage_is_linked_to_command_evidence_and_key_never_persists(self):
        self.prepare();row=self.manager.builders.execute(self.task['id'],'start_long_command',{'argv':['python','proxy_episode.py'],'timeout_s':10,'label':'Episode'})
        final=terminal(self.manager,row['id']);self.assertEqual(final['status'],'succeeded');self.assertIn('HTTP 200',final['result']['output'])
        ledger=self.manager.ledger.rows(self.goal['id']);self.assertEqual(len(ledger),1);entry=ledger[0]
        self.assertEqual(entry['long_command_id'],row['id']);self.assertEqual(entry['task_id'],self.task['id']);self.assertEqual(entry['plan_item_id'],self.item['id'])
        cost=final['result']['proxy_cost'];self.assertEqual(cost['ledger_ids'],[entry['id']]);self.assertEqual(cost['estimated_usd'],entry['estimated_usd'])
        event=self.manager.store.get('task',self.task['id'])['events'][final['completion_event_index']];self.assertEqual(event['result']['proxy_cost'],cost)
        persisted='\n'.join(raw for raw, in self.manager.store.db.execute('SELECT data FROM records'));self.assertNotIn(self.token,persisted);self.assertFalse(self.manager.long_commands.tokens)

    def test_budget_refusal_is_recorded_even_when_script_catches_http_error(self):
        self.prepare(budget=True);row=self.manager.builders.execute(self.task['id'],'start_long_command',{'argv':['python','proxy_episode.py'],'timeout_s':10,'label':'Blocked episode'})
        final=terminal(self.manager,row['id']);self.assertIn('HTTP 429',final['result']['output']);self.assertEqual(final['result']['exit_code'],0)
        self.assertTrue(final['result']['budget_refused']);self.assertEqual(final['result']['proxy_rejections'][0]['category'],'budget_limit')
        valid=self.manager.valid_evidence(self.goal,self.manager.store.get('plan_item',self.item['id']));self.assertNotIn(final['evidence_id'],valid)
        self.assertFalse(self.manager.ledger.rows(self.goal['id']));self.assertEqual(self.manager.store.get('goal',self.goal['id'])['status'],'paused')

if __name__=='__main__':unittest.main()
