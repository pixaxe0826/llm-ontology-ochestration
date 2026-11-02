"""Document acceptance uses existing grounded observations; never run a command/GPU."""
import hashlib,json
import unittest
from unittest.mock import patch
import test_planner_reviews as fixtures
from test_goals import response
from workbench.acceptance_rules import normalize_criteria,rule_for,needs_execution
from workbench.document_evidence import DocumentEvidenceError,document_bundle,compact_bundle
from workbench.goal_prompts import system
from workbench.agent import SYSTEM
from workbench.workspace import Workspace


class DocumentAcceptanceTests(unittest.TestCase):
    setUp=fixtures.PlannerReviewTests.setUp
    tearDown=fixtures.PlannerReviewTests.tearDown
    create=fixtures.PlannerReviewTests.create

    def fixture(self,body='9 tests passed (run-evidence:1).',exit_code=0,output='9 passed in 1.25s',path='review/REPORT.md',fmt='markdown',required=None):
        goal=self.create(start=False)
        criterion='Phase 1 report summarizes existing execution and measured test results.'
        rule={'text':criterion,'kind':'document','document_paths':[path],'required_sections':['Summary','Evidence'] if required is None else required,'format':fmt}
        self.manager.add_items(goal,[{'key':'run','title':'Existing execution','acceptance':[{'text':'Run checks.','kind':'execution'}]},
            {'key':'report','title':'Phase 1 report','acceptance':[rule]}])
        source=next(i for i in self.manager.items(goal['id']) if i['key']=='run');item=next(i for i in self.manager.items(goal['id']) if i['key']=='report')
        ws=Workspace(self.folder,self.store.root);change=ws.write('runner.py','print("checks")\n')
        task=self.store.save('task',{'id':'run-evidence','goal_id':goal['id'],'plan_item_id':source['id'],'workspace':str(self.folder),'status':'succeeded' if exit_code==0 else 'failed',
            'events':[{'kind':'change','change':change},{'kind':'tool_result','name':'run_command','result':{'exit_code':exit_code,'output':output,'duration_s':1.25,'interrupted':False}}]})
        self.manager.collect_evidence(source,task)
        content='# Phase 1 report\n\n## Summary\n'+body+'\n\n## Evidence\nExisting observations are referenced above.\n' if fmt=='markdown' else body
        self.write_doc(goal,item,content,path)
        return goal,self.store.get('plan_item',item['id']),source

    def write_doc(self,goal,item,content,path='review/REPORT.md'):
        ws=Workspace(self.folder,self.store.root);file=ws.path(path);sha=ws.read(path)['sha256'] if file.exists() else None
        change=ws.write(path,content,sha)
        task=self.store.save('task',{'id':'doc-evidence','goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),'status':'succeeded','events':[{'kind':'change','change':change}]})
        self.manager.collect_evidence(self.store.get('plan_item',item['id']),task)
        self.store.update('plan_item',item['id'],task_ids=[task['id']])

    def verdict(self,item,ids=None):
        return {'decision':'accept','defects':[],'criteria':[{'criterion':item['acceptance'][0],'evidence_ids':['doc-evidence:0'] if ids is None else ids}],
            'summary':'Existing evidence reviewed.','next_items':[],'goal_complete':False}

    def test_typed_plan_is_canonical_and_rules_survive_existing_updates(self):
        goal,item,_=self.fixture()
        self.assertIsInstance(item['acceptance'][0],str);self.assertEqual(rule_for(item,item['acceptance'][0])['kind'],'document')
        self.manager.add_items(goal,[{'key':item['key'],'title':'Updated title only'}])
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['acceptance_rules'],item['acceptance_rules'])
        self.assertEqual(current['acceptance'],item['acceptance'])

    def test_invalid_kind_missing_kind_duplicate_text_and_unsafe_paths_rejected(self):
        for values in ([{'text':'A','kind':'unknown'}],[{'text':'A'}],['A',{'text':'A','kind':'document'}],
            [{'text':'A','kind':'document','document_paths':['../secret.md']}],[{'text':'A','kind':'document','format':'executable'}]):
            with self.subTest(values=values),self.assertRaises(ValueError):normalize_criteria(values)

    def test_valid_document_cites_another_items_execution_without_new_run(self):
        goal,item,_=self.fixture();before_tasks=len(self.store.items('task',None))
        with patch.object(Workspace,'run',side_effect=AssertionError('No execution')),patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')):
            accepted,valid,_=self.manager.validate_review(goal,item,self.verdict(item))
        self.assertIn(item['acceptance'][0],accepted);self.assertIn('run-evidence:1',valid)
        self.assertEqual(len(self.store.items('task',None)),before_tasks)

    def test_document_with_no_execution_claims_needs_only_current_file(self):
        goal,item,_=self.fixture('Architecture notes describe the existing folder structure.')
        self.store.update('task','run-evidence',events=[])
        accepted,_,_=self.manager.validate_review(goal,item,self.verdict(item));self.assertTrue(accepted)

    def test_unsupported_success_and_numeric_claim_rejected(self):
        goal,item,_=self.fixture('9 tests passed.')
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing parenthesized'):self.manager.validate_review(goal,item,self.verdict(item))

    def test_seven_vs_nine_tests_is_specific_content_rework(self):
        goal,item,_=self.fixture('7 tests passed (run-evidence:1).')
        with self.assertRaisesRegex(DocumentEvidenceError,'tests_passed claimed 7; observed 9'):self.manager.validate_review(goal,item,self.verdict(item))

    def test_stale_and_failed_citations_are_rejected(self):
        goal,item,_=self.fixture();original=(self.folder/'runner.py').read_bytes();(self.folder/'runner.py').write_text('print("modified")')
        with self.assertRaisesRegex(DocumentEvidenceError,'Stale citation'):self.manager.validate_review(goal,item,self.verdict(item))
        (self.folder/'runner.py').write_bytes(original)
        task=self.store.get('task','run-evidence');task['events'][1]['result']['exit_code']=1;self.store.save('task',task)
        with self.assertRaisesRegex(DocumentEvidenceError,'not successful evidence'):self.manager.validate_review(goal,item,self.verdict(item))

    def test_unknown_and_foreign_goal_citations_are_rejected(self):
        goal,item,_=self.fixture('9 tests passed (foreign:1).')
        self.store.save('task',{'id':'foreign','goal_id':'another-goal','plan_item_id':'other','events':[{}, {'kind':'tool_result','name':'run_command','result':{'exit_code':0,'output':'9 passed'}}]})
        with self.assertRaisesRegex(DocumentEvidenceError,'Unknown evidence ID'):self.manager.validate_review(goal,item,self.verdict(item))

    def test_self_citation_cannot_launder_success_claim(self):
        goal,item,_=self.fixture('9 tests passed (doc-evidence:0).')
        with self.assertRaisesRegex(DocumentEvidenceError,'cannot cite its own'):self.manager.validate_review(goal,item,self.verdict(item))

    def test_missing_required_sections_and_stale_document_are_rejected(self):
        goal,item,_=self.fixture();self.write_doc(goal,item,'# Summary\n9 tests passed (run-evidence:1).\n')
        item=self.store.get('plan_item',item['id'])
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing required section: Evidence'):self.manager.validate_review(goal,item,self.verdict(item))
        (self.folder/'review/REPORT.md').write_text('changed outside observed write')
        with self.assertRaises(ValueError):self.manager.validate_review(goal,item,self.verdict(item))

    def test_read_only_document_file_observation_is_sufficient(self):
        goal,item,_=self.fixture();result=Workspace(self.folder,self.store.root).read('review/REPORT.md')
        task=self.store.get('task','doc-evidence');task['events']=[{'kind':'tool_result','name':'read_file','result':result}];self.store.save('task',task)
        self.manager.collect_evidence(item,task);item=self.store.get('plan_item',item['id'])
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])

    def test_cost_duration_hash_and_table_rows_use_actual_existing_values(self):
        goal,item,_=self.fixture();row=self.manager.ledger.reserve(goal,'modal','development',.25,role='Executor');self.manager.ledger.settle(row,actual_cost=.25,runtime_s=1.25)
        sha=Workspace(self.folder,self.store.root).read('runner.py')['sha256']
        body=f'# Summary\n| Tests | Cost | Actual seconds | Script hash | Evidence |\n|---|---|---|---|---|\n| 9 passed | USD 0.25 | actual 1.25 seconds | {sha} | (run-evidence:1, ledger:{row["id"]}) |\n\n## Evidence\nObserved results only.\n'
        self.write_doc(goal,item,body);item=self.store.get('plan_item',item['id'])
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
        self.write_doc(goal,item,body.replace('USD 0.25','USD 0.99'))
        with self.assertRaisesRegex(DocumentEvidenceError,'cost claimed 0.99'):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))

    def test_structured_json_document_checks_keys_without_value_citations(self):
        content=json.dumps({'summary':{'tests_passed':9,'evidence_ids':['run-evidence:1']},'evidence':['run-evidence:1']})
        goal,item,_=self.fixture(content,path='review/REPORT.json',fmt='json',required=['summary','summary.tests_passed','evidence'])
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
        self.write_doc(goal,item,json.dumps({'summary':{'tests_passed':7,'evidence_ids':['run-evidence:1']},'evidence':['run-evidence:1']}),'review/REPORT.json')
        self.assertTrue(self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))[0])
        self.write_doc(goal,item,json.dumps({'summary':{'tests_passed':7}}),'review/REPORT.json')
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing required section/key: evidence'):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))

    def test_execution_kind_and_unclassified_legacy_execution_keep_command_requirement(self):
        goal,item,_=self.fixture();name=item['acceptance'][0]
        for rules in ({},{name:{'kind':'execution'}}):
            current=self.store.update('plan_item',item['id'],acceptance_rules=rules)
            self.assertTrue(needs_execution(current,name))
            with self.assertRaisesRegex(ValueError,'successful command or GPU'):self.manager.validate_review(goal,current,self.verdict(current))
        self.assertEqual(rule_for({'acceptance':['Run the simulation.']},'Run the simulation.')['kind'],'execution')

    def test_planner_receives_checks_and_bad_document_accept_becomes_rework_not_format_error(self):
        goal,item,_=self.fixture('Tests passed: 7 (run-evidence:1).');prompts=[]
        def complete(provider,messages,*args,**kwargs):prompts.append(messages[-1]['content']);return response(json.dumps(self.verdict(item)))
        self.providers.complete=complete
        with patch.object(Workspace,'run',side_effect=AssertionError('No new run')),patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')):
            self.assertTrue(self.manager.review(goal,item,review_only=True))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['status'],'rework');self.assertEqual(current['reworks'],1)
        self.assertEqual(current.get('review_error_count',0),0);self.assertEqual(len(prompts),1)
        self.assertIn('DOCUMENT VALIDATION',prompts[0]);self.assertNotIn('ACCEPT IS NOT AVAILABLE',prompts[0])
        attempt=self.store.items('goal_review_attempt',None)[0];self.assertTrue(attempt['server_enforced_rework']);self.assertFalse(attempt['document_validation']['passed'])

    def test_document_packet_keeps_file_and_sources_and_stays_bounded(self):
        goal,item,_=self.fixture();prompts=[]
        self.providers.complete=lambda provider,messages,*args,**kwargs:prompts.append(messages[-1]['content']) or response(json.dumps(self.verdict(item)))
        self.assertTrue(self.manager.review(goal,item,review_only=True))
        attempt=self.store.items('goal_review_attempt',None)[0]
        self.assertIn('doc-evidence:0',attempt['valid_evidence_ids']);self.assertIn('run-evidence:1',attempt['valid_evidence_ids'])
        self.assertTrue(attempt['document_validation']['passed']);self.assertLessEqual(len(json.dumps(compact_bundle(attempt['document_validation']),ensure_ascii=False)),5500)

    def test_goal_prompts_explain_document_citations_without_changing_work_system(self):
        goal,item,_=self.fixture()
        self.assertIn('document-kind',system(goal,'Builder'));self.assertIn('required_sections',system(goal,'Planner'))
        self.assertNotIn('document-kind',SYSTEM)

    def test_repeated_document_reads_do_not_crowd_cited_execution_out(self):
        goal,item,_=self.fixture();task=self.store.get('task','doc-evidence')
        result=Workspace(self.folder,self.store.root).read('review/REPORT.md')
        task['events']=[{'kind':'tool_result','name':'read_file','result':{**result,'start_line':n+1,'end_line':n+1}} for n in range(70)]
        self.store.save('task',task);self.manager.collect_evidence(item,task)
        item=self.store.get('plan_item',item['id'])
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item)))
        self.assertTrue(self.manager.review(goal,item,review_only=True))
        attempt=self.store.items('goal_review_attempt',None)[0]
        self.assertIn('run-evidence:1',attempt['valid_evidence_ids'])
        self.assertIn('doc-evidence:69',attempt['valid_evidence_ids'])
        self.assertEqual(self.store.get('plan_item',item['id'])['status'],'done')

    def test_document_builder_gets_sibling_sources_and_accounting_without_running(self):
        goal,item,_=self.fixture();row=self.manager.ledger.reserve(goal,'modal','development',.25,role='Executor')
        self.manager.ledger.settle(row,actual_cost=.25,runtime_s=1.25)
        with patch.object(Workspace,'run',side_effect=AssertionError('No new run')),patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')):
            messages=self.manager.context(goal,item,'Builder')
        catalog=next(m['content'] for m in messages if m['content'].startswith('DOCUMENT SOURCE CATALOG'))
        self.assertIn('run-evidence:1',catalog);self.assertIn('ledger:'+row['id'],catalog)
        self.assertLessEqual(len(catalog),8200)
        source=next(i for i in self.manager.items(goal['id']) if i['key']=='run')
        self.assertFalse(any(m['content'].startswith('DOCUMENT SOURCE CATALOG') for m in self.manager.context(goal,source,'Builder')))


if __name__=='__main__':unittest.main()
