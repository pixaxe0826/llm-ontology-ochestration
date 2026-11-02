"""Per-command inputs/outputs, bounded review catalogs and scoped evidence reuse."""
import hashlib,json,sys
import unittest
from unittest.mock import patch
import test_planner_reviews as fixtures
from test_goals import response
from workbench.workspace import Workspace
from workbench.review_evidence import packet
from workbench.command_files import file_hash


class CommandEvidenceInputTests(unittest.TestCase):
    setUp=fixtures.PlannerReviewTests.setUp
    tearDown=fixtures.PlannerReviewTests.tearDown
    create=fixtures.PlannerReviewTests.create
    reviewed=fixtures.PlannerReviewTests.reviewed
    verdict=fixtures.PlannerReviewTests.verdict

    def command(self,goal,item,argv,identifier='command'):
        ws=Workspace(self.folder,self.store.root);result=ws.run(argv,timeout=10)
        self.assertEqual(result['exit_code'],0,result['output'])
        task=self.store.save('task',{'id':identifier,'goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),
            'status':'succeeded','events':[{'kind':'tool','name':'run_command','arguments':{'argv':argv,'cwd':'.'}},
            {'kind':'tool_result','name':'run_command','result':result}]})
        self.store.update('plan_item',item['id'],task_ids=[*item.get('task_ids',[]),identifier])
        self.manager.collect_evidence(self.store.get('plan_item',item['id']),task)
        return self.store.get('plan_item',item['id']),result

    def script(self):
        (self.folder/'input.yaml').write_text('label: conveyor',encoding='utf-8')
        (self.folder/'helper.py').write_text('VALUE=1',encoding='utf-8')
        (self.folder/'run.py').write_text('from pathlib import Path\nimport helper\nPath("output.json").write_text(Path("input.yaml").read_text(),encoding="utf-8")\nprint("done",helper.VALUE)\n',encoding='utf-8')

    def test_self_outputs_and_unrelated_changes_only_stale_the_output_ref(self):
        goal,item=self.reviewed();self.script()
        item,result=self.command(goal,item,[sys.executable,'run.py'])
        self.assertEqual(set(result['command_files']['inputs']),{'run.py','input.yaml','helper.py'})
        self.assertEqual(set(result['command_files']['outputs']),{'output.json'})
        self.assertEqual(result['command_files']['outputs']['output.json'],file_hash(Workspace(self.folder,self.store.root),'output.json'))
        (self.folder/'artifact.txt').write_text('unrelated review document')
        (self.folder/'output.json').write_text('new output')
        observed=self.manager.observed_evidence(goal,item)
        self.assertTrue(observed['command:1']['supports_acceptance'])
        self.assertFalse(observed['command:1/output:output.json']['supports_acceptance'])
        self.assertIn('Output changed: output.json',observed['command:1/output:output.json']['stale_reason'])
        (self.folder/'run.py').write_text('print("modified")')
        observed=self.manager.observed_evidence(goal,item)
        self.assertFalse(observed['command:1']['supports_acceptance']);self.assertIn('Input changed: run.py',observed['command:1']['stale_reason'])

    def test_input_data_and_imported_module_changes_stale_execution(self):
        goal,item=self.reviewed();self.script();item,_=self.command(goal,item,[sys.executable,'run.py'])
        (self.folder/'input.yaml').write_text('changed input')
        (self.folder/'helper.py').write_text('VALUE=2')
        observed=self.manager.observed_evidence(goal,item)['command:1']
        self.assertIn('Input changed: input.yaml',observed['stale_reason']);self.assertIn('Input changed: helper.py',observed['stale_reason'])

    def test_legacy_cumulative_metrics_does_not_stale_execution_and_keeps_original_script_hash(self):
        goal,item=self.reviewed();self.script();ws=Workspace(self.folder,self.store.root)
        reads=[{'kind':'tool_result','name':'read_file','result':ws.read(p)} for p in ('run.py','input.yaml','helper.py','artifact.txt')]
        (self.folder/'output.json').write_text('old output')
        reads.append({'kind':'tool_result','name':'read_file','result':ws.read('output.json')})
        events=reads+[{'kind':'tool','name':'run_command','arguments':{'argv':['python','run.py'],'cwd':'.'}},
            {'kind':'tool_result','name':'run_command','result':{'exit_code':0,'output':'done','interrupted':False}}]
        task=self.store.save('task',{'id':'legacy','goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),'events':events})
        self.manager.collect_evidence(item,task);item=self.store.get('plan_item',item['id']);identifier='legacy:'+str(len(events)-1)
        (self.folder/'output.json').write_text('new output');(self.folder/'artifact.txt').write_text('unrelated note')
        self.assertIn(identifier,self.manager.valid_evidence(goal,item))
        old=next(e for e in item['evidence'] if e['id']==identifier)['files']['run.py']
        (self.folder/'run.py').write_text('print("changed script")')
        self.manager.collect_evidence(item,task)
        current=self.store.get('plan_item',item['id']);self.assertEqual(next(e for e in current['evidence'] if e['id']==identifier)['files']['run.py'],old)
        self.assertNotIn(identifier,self.manager.valid_evidence(goal,current))

    def test_parent_review_collects_child_execution_and_split_child_can_reuse_parent(self):
        goal,parent=self.reviewed();self.script()
        self.manager.add_items(goal,[{'key':'child','title':'Child','acceptance':['run succeeds'],'parent':parent['id']}])
        child=next(i for i in self.manager.items(goal['id']) if i['id']!=parent['id'])
        child=self.store.update('plan_item',child['id'],parent=parent['id'],split_from=parent['id'])
        child,_=self.command(goal,child,[sys.executable,'run.py'],'child-run')
        observed=self.manager.observed_evidence(goal,parent)
        self.assertTrue(observed['child-run:1']['supports_acceptance']);self.assertEqual(observed['child-run:1']['source_plan_item_id'],child['id'])
        parent,_=self.command(goal,parent,[sys.executable,'run.py'],'parent-run')
        self.assertIn('parent-run:1',self.manager.valid_evidence(goal,child))
        self.store.update('task','child-run',goal_id='another-goal')
        self.assertNotIn('child-run:1',self.manager.observed_evidence(goal,parent))

    def test_catalog_prioritizes_recent_valid_commands_and_explains_staleness(self):
        observed={str(i):{'id':str(i),'kind':'run_command','supports_acceptance':False,'stale':True,'stale_reason':'Input changed: old.py','execution':{'output_excerpt':'x'*1500}} for i in range(50)}
        for i in range(4):observed['fresh'+str(i)]={'id':'fresh'+str(i),'kind':'run_command','supports_acceptance':True,'stale':False,'stale_reason':'Current: inputs unchanged.','observed_at':100+i,'execution':{'success':True,'exit_code':0}}
        catalog=packet(observed,limit=2500);ids=[r['id'] for r in catalog['observed']]
        self.assertTrue(all('fresh'+str(i) in ids for i in range(4)));self.assertEqual(ids[0],'fresh3')
        self.assertTrue(all('stale_reason' in r for r in catalog['observed']));self.assertGreater(catalog['omitted_count'],0)

    def test_python_module_command_preserves_arguments_and_proxy_secret_filter(self):
        goal,item=self.reviewed();(self.folder/'test_sample.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertEqual(1,1)\n')
        item,result=self.command(goal,item,[sys.executable,'-m','unittest','test_sample','-q'])
        self.assertIn('test_sample.py',result['command_files']['inputs'])

    def test_review_reindexes_child_tasks_before_building_valid_id_list(self):
        goal,parent=self.reviewed();self.script()
        parent=self.store.update('plan_item',parent['id'],acceptance=['Run succeeds.'])
        self.manager.add_items(goal,[{'key':'child','title':'Child','acceptance':['Run succeeds.']}])
        child=next(i for i in self.manager.items(goal['id']) if i['id']!=parent['id'])
        child=self.store.update('plan_item',child['id'],parent=parent['id'])
        child,_=self.command(goal,child,[sys.executable,'run.py'],'child-run')
        self.store.update('plan_item',child['id'],evidence=[],status='done')  # Parent review starts after child completion.
        prompts=[]
        def complete(provider,messages,*args,**kwargs):
            prompts.append(messages[-1]['content'])
            return response(json.dumps(self.verdict(parent,criteria=[{'criterion':'Run succeeds.','evidence_ids':['child-run:1']}])) )
        self.providers.complete=complete
        self.assertTrue(self.manager.review(goal,parent,review_only=True))
        self.assertIn('child-run:1',self.store.items('goal_review_attempt',None)[-1]['valid_evidence_ids'])
        self.assertIn('source_plan_item_id',prompts[-1])

    def test_identical_new_execution_has_its_own_stable_id(self):
        goal,item=self.reviewed();self.script()
        item,_=self.command(goal,item,[sys.executable,'run.py'],'first')
        item,_=self.command(goal,item,[sys.executable,'run.py'],'second')
        valid=self.manager.valid_evidence(goal,item)
        self.assertIn('first:1',valid);self.assertIn('second:1',valid)

    def test_missing_input_is_still_visible_with_a_stale_reason(self):
        goal,item=self.reviewed();self.script();item,_=self.command(goal,item,[sys.executable,'run.py'])
        (self.folder/'input.yaml').unlink()
        ref=self.manager.observed_evidence(goal,item)['command:1']
        self.assertTrue(ref['stale']);self.assertIn('Input missing or unreadable: input.yaml',ref['stale_reason'])


if __name__=='__main__':unittest.main()
