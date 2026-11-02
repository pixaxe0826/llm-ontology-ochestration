"""Data/config parameters are not prose claims. All checks use fake suppliers."""
import json,unittest
from unittest.mock import patch
import test_document_acceptance as docs
import test_dependency_refresh as deps
from workbench.document_evidence import DocumentEvidenceError,format_check,document_bundle
from workbench.acceptance_rules import normalize_criteria
from workbench.goal_prompts import system


class DataAcceptanceTests(unittest.TestCase):
    setUp=docs.DocumentAcceptanceTests.setUp
    tearDown=docs.DocumentAcceptanceTests.tearDown
    create=docs.DocumentAcceptanceTests.create
    fixture=docs.DocumentAcceptanceTests.fixture
    write_doc=docs.DocumentAcceptanceTests.write_doc
    verdict=docs.DocumentAcceptanceTests.verdict
    def test_yaml_parameters_require_keys_and_hash_not_citations(self):
        goal,item,_=self.fixture('faults:\n  - normal_value: 1.0\n    faulted_value: 0.0\n    symptoms: 3D case slip\nenergy_sources: [DS1, V1]\n',path='faults/catalogue.yaml',fmt='yaml',required=['faults.0.normal_value','energy_sources'])
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No Builder')):
            self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0])
        bundle=document_bundle(self.manager,goal,item);self.assertTrue(bundle['passed']);self.assertEqual(bundle['criteria'][0]['files'][0]['claims'],[])
        self.write_doc(goal,item,'faults: []\n','faults/catalogue.yaml')
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing required section/key'):self.manager.validate_review(goal,self.store.get('plan_item',item['id']),self.verdict(item))
    def test_json_config_requires_no_citations_but_fresh_file(self):
        goal,item,_=self.fixture('{"normal_value":90,"faulted_range":[0,15],"status":"completed"}',path='config.json',fmt='json',required=['normal_value','faulted_range'])
        self.assertTrue(self.manager.validate_review(goal,item,self.verdict(item))[0]);(self.folder/'config.json').write_text('{"normal_value":91}',encoding='utf-8')
        with self.assertRaises(ValueError):self.manager.validate_review(goal,item,self.verdict(item))
    def test_csv_and_toml_formats_validate_structure_and_required_keys(self):
        for path,body,keys in [('config.toml','[fault]\nnormal_value=90\nfaulted_value=0\n',['fault.normal_value']),('faults.csv','id,normal_value,faulted_value\nf1,90,0\n',['id','normal_value'])]:
            fmt,errors=format_check(body,path,{'required_keys':keys});self.assertFalse(errors)
            self.assertIn('Missing required section/key: absent',format_check(body,path,{'required_keys':['absent']})[1])
        self.assertTrue(format_check('id,value\nf1,2,3\n','faults.csv',{})[1]);self.assertTrue(format_check('[broken','config.toml',{})[1])
        self.assertTrue(normalize_criteria([{'text':'Config exists','kind':'document','format':'toml','required_keys':['fault'],'document_paths':['config.toml']}])[1])
    def test_markdown_cannot_bypass_prose_claims_by_wrong_format_label(self):
        goal,item,_=self.fixture('9 tests passed.',path='review/REPORT.md')
        name=item['acceptance'][0];item=self.store.update('plan_item',item['id'],acceptance_rules={name:{**item['acceptance_rules'][name],'format':'yaml'}})
        with self.assertRaisesRegex(DocumentEvidenceError,'Missing parenthesized'):self.manager.validate_review(goal,item,self.verdict(item))
    def test_prompts_distinguish_config_and_prose(self):
        goal,item,_=self.fixture()
        for role in ('Planner','Builder'):self.assertIn('YAML/JSON/TOML/CSV',system(goal,role));self.assertNotIn('put source IDs in an evidence_ids array',system(goal,role))


class HumanClassificationTests(unittest.TestCase):
    setUp=deps.DependencyRefreshTests.setUp
    tearDown=deps.DependencyRefreshTests.tearDown
    items=deps.DependencyRefreshTests.items
    decision=deps.DependencyRefreshTests.decision
    answer=deps.DependencyRefreshTests.answer
    def test_human_kind_change_is_refreshed_validated_and_audited(self):
        item=self.items('T1-3')[0];text=item['acceptance'][0]
        self.store.update('plan_item',item['id'],status='blocked',acceptance_rules={text:{'kind':'document','document_paths':['faults/catalogue.yaml'],'format':'yaml'}})
        decision=self.decision(item);self.answer(decision,answer='Reclassify this criterion to kind execution. Do not restructure the plan. Reopen T1-3 for review using existing evidence.')
        self.assertTrue(self.store.get('goal',self.goal['id'])['decision_replan_pending'])
        value={'items':[{'key':'T1-3','acceptance_rules':{text:{'kind':'execution'}}}], 'actions':[], 'summary':'Human classification applied.'}
        with patch.object(self.manager,'phase',return_value=json.dumps(value)) as calls:self.manager.refresh_dependencies(self.store.get('goal',self.goal['id']))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['acceptance_rules'][text],{'kind':'execution'});self.assertEqual(current['depends_on'],item['depends_on']);self.assertEqual(calls.call_count,1)
        self.assertIn('acceptance_rules',calls.call_args.args[2]);self.assertFalse(current['acceptance_replan_pending']);self.assertEqual(self.store.items('goal_acceptance_change',None)[0]['decision_id'],decision['id'])
    def test_requested_kind_change_cannot_be_silently_ignored(self):
        item=self.items('T1-3')[0];decision=self.decision(item);self.answer(decision,answer='Reclassify this criterion to kind execution.')
        with patch.object(self.manager,'phase',return_value='{"items":[],"summary":"Ignored"}') as calls:self.manager.refresh_dependencies(self.store.get('goal',self.goal['id']))
        self.assertEqual(calls.call_count,2);attempts=self.store.items('goal_dependency_attempt',None);self.assertTrue(all('Human requested acceptance-kind change' in a['error'] for a in attempts))
        self.assertTrue(self.store.get('plan_item',item['id'])['acceptance_replan_pending']);self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])
