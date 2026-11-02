"""Child scheduling, bounded document cycles and decimal grounding, offline only."""
import copy,json,unittest
from unittest.mock import patch
import test_goal_scheduler as scheduling
import test_document_acceptance as documents
from test_goals import response
from workbench.document_evidence import check_claim,source_facts
from workbench.document_cycles import signature
from workbench.workspace import Workspace


class ParentSchedulingTests(unittest.TestCase):
    setUp=scheduling.GoalSchedulerTests.setUp
    tearDown=scheduling.GoalSchedulerTests.tearDown

    def family(self):
        self.manager.add_items(self.goal,[{'key':'parent','title':'Parent','acceptance':['A file exists.'],'depends_on':[],'blocked_by_decision':[]},
            {'key':'child','parent':'parent','title':'Child','acceptance':['A file exists.'],'depends_on':[],'blocked_by_decision':[]}])
        rows={r['key']:r for r in self.manager.items(self.goal['id'])}
        return rows['parent'],rows['child']

    def test_parent_is_not_assigned_or_limit_reviewed_without_split_marker(self):
        parent,child=self.family();self.store.update('plan_item',parent['id'],status='review',bundles=4)
        self.manager.refresh_split_reviews(self.goal);parent=self.store.get('plan_item',parent['id'])
        self.assertEqual(parent['status'],'waiting_children');self.assertEqual(parent['bundles'],4)
        self.assertFalse(self.manager.candidate_ready(self.goal,parent));self.assertFalse(self.manager.review_ready(parent))
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('Parent Builder forbidden')):
            self.manager.assign(self.goal,parent);self.manager.prepare_limit_review(self.goal,parent)
        self.assertEqual(self.manager.next_builder(self.goal)['id'],child['id'])
        view=self.manager.execution_view(self.goal,parent)
        self.assertEqual((view['execution_status'],view['children_done'],view['children_total']),('waiting_children',0,1))

    def test_blocked_child_keeps_parent_waiting_without_parent_decision(self):
        parent,child=self.family();self.store.update('plan_item',child['id'],status='blocked')
        self.manager.refresh_split_reviews(self.goal)
        self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'waiting_children')
        self.assertEqual(self.manager.pending_decisions(self.goal['id']),[])

    def test_completed_children_trigger_one_parent_review_using_child_evidence_no_builder(self):
        parent,child=self.family();change=Workspace(self.folder,self.store.root).write('child.txt','observed')
        task=self.store.save('task',{'id':'child-result','goal_id':self.goal['id'],'plan_item_id':child['id'],'workspace':str(self.folder),'status':'succeeded','events':[{'kind':'change','change':change}]})
        self.manager.collect_evidence(child,task)
        self.store.update('plan_item',child['id'],status='done',task_ids=[task['id']])
        self.manager.refresh_split_reviews(self.goal);parent=self.store.get('plan_item',parent['id'])
        self.assertEqual((parent['status'],parent['review_trigger']),('review','children_completed'))
        self.providers.complete=lambda *a,**k:response(json.dumps({'decision':'accept','defects':[],
            'criteria':[{'criterion':parent['acceptance'][0],'evidence_ids':['child-result:0']}],'summary':'Reviewed child file.','next_items':[]}))
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No Builder')):
            self.assertTrue(self.manager.review(self.goal,parent))
        self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'done')
        self.manager.refresh_split_reviews(self.goal)
        self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'done')

    def test_reindex_timestamp_alone_does_not_reopen_parent_review(self):
        parent,child=self.family();self.store.update('plan_item',child['id'],status='done')
        self.manager.refresh_split_reviews(self.goal)
        self.store.update('plan_item',parent['id'],status='blocked')
        self.store.update('plan_item',child['id'],evidence=[])  # Same data, new updated timestamp.
        self.manager.refresh_split_reviews(self.goal)
        self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'blocked')

    def test_parent_scope_gap_creates_child_without_parent_limit_decision(self):
        parent,child=self.family();self.store.update('plan_item',child['id'],status='done')
        parent=self.store.update('plan_item',parent['id'],bundles=4,reworks=3)
        self.manager.refresh_split_reviews(self.goal);parent=self.store.get('plan_item',parent['id'])
        value={'decision':'rework','defects':['One aggregate artifact remains.'],'criteria':[],
            'summary':'Complete the artifact in one child.','next_items':[{'key':'bounded-gap','parent':'parent','title':'Write missing aggregate artifact','acceptance':['artifact.txt exists.']}]}
        self.providers.complete=lambda *a,**k:response(json.dumps(value))
        self.assertTrue(self.manager.review(self.goal,parent))
        self.assertEqual(self.store.get('plan_item',parent['id'])['status'],'waiting_children')
        self.assertFalse(self.manager.pending_decisions(self.goal['id']))
        self.assertEqual(self.manager.next_builder(self.goal)['key'],'bounded-gap')

    def test_parent_rework_without_bounded_child_is_invalid(self):
        parent,child=self.family();self.store.update('plan_item',child['id'],status='done')
        with self.assertRaisesRegex(ValueError,'bounded child deliverables'):
            self.manager.validate_review(self.goal,parent,{'decision':'rework','defects':['Missing artifact.'],'criteria':[],'next_items':[]})


class DocumentCycleTests(unittest.TestCase):
    setUp=documents.DocumentAcceptanceTests.setUp
    tearDown=documents.DocumentAcceptanceTests.tearDown
    create=documents.DocumentAcceptanceTests.create
    fixture=documents.DocumentAcceptanceTests.fixture
    write_doc=documents.DocumentAcceptanceTests.write_doc

    def ready(self):
        goal,item,_=self.fixture('Architecture notes.')
        self.store.update('plan_item',item['id'],bundles=4,status='review',review_trigger='batch_limit')
        item=self.store.get('plan_item',item['id'])
        value={'decision':'rework','defects':[{'kind':'document_fix','text':'Fix only the cost wording in review/REPORT.md.'}],
            'criteria':[],'summary':'Only document wording remains.','next_items':[]}
        return goal,item,value

    def test_document_only_cycle_opened_without_human_or_builder_and_audited(self):
        goal,item,value=self.ready();before=item['reworks'];self.providers.complete=lambda *a,**k:response(json.dumps(value))
        with patch.object(self.manager.builders,'start',side_effect=AssertionError('No immediate Builder')):
            self.assertTrue(self.manager.review(goal,item))
        saved=self.store.get('plan_item',item['id'])
        self.assertEqual((saved['status'],saved['bundles'],saved['document_cycles_used'],self.manager.bundle_limit(saved)),('rework',0,1,2))
        self.assertEqual(saved['reworks'],before);self.assertFalse(self.manager.pending_decisions(goal['id']))
        self.assertEqual(len(self.store.items('goal_document_cycle',None)),1)
        self.assertTrue(any('document-only' in m['body'] for m in self.manager.messages(goal['id']) if m['type']=='NOTE'))

    def test_repeated_defect_two_cycles_and_non_document_defect_are_ineligible(self):
        goal,item,value=self.ready()
        self.assertIsNotNone(self.manager.document_cycle_assessment(goal,item,value))
        for changes in ({'document_cycles_used':2},{'document_cycle_defect_signature':signature(value['defects'])}):
            self.assertIsNone(self.manager.document_cycle_assessment(goal,{**item,**changes},value))
        for detail in ('Missing tests.',{'kind':'execution','text':'Run missing checks.'}):
            self.assertIsNone(self.manager.document_cycle_assessment(goal,item,{**value,'defects':[detail]}))
        self.assertIsNone(self.manager.document_cycle_assessment(goal,{**item,'bundles':3},value))

    def test_execution_proof_is_required_for_every_execution_criterion(self):
        goal,item,value=self.ready();item={**item,'acceptance':[*item['acceptance'],'Run the actual test.']}
        self.assertIsNone(self.manager.document_cycle_assessment(goal,item,value))
        value['criteria']=[{'criterion':'Run the actual test.','evidence_ids':['doc-evidence:0']}]
        self.assertIsNone(self.manager.document_cycle_assessment(goal,item,value))

    def test_runtime_guard_blocks_execution_and_unspecified_file_writes(self):
        goal,item,value=self.ready();assessment=self.manager.document_cycle_assessment(goal,item,value)
        self.manager.open_document_cycle(goal,item,assessment,'test')
        task={'goal_id':goal['id'],'plan_item_id':item['id']}
        for name,args in [('run_command',{}),('submit_gpu_job',{}),('start_long_command',{}),('fetch_reference_page',{}),('write_file',{'path':'runner.py'}),('edit_file',{'path':'../REPORT.md'})]:
            with self.subTest(name=name),self.assertRaises(PermissionError):self.manager.document_cycle_guard(task,name,args)
        for name,args in [('read_file',{'path':'runner.py'}),('write_file',{'path':'review/REPORT.md'}),('edit_file',{'path':'review/REPORT.md'}),('report_item',{})]:self.manager.document_cycle_guard(task,name,args)

    def test_new_document_cycle_is_capped_at_two_and_uses_new_task_history(self):
        goal,item,value=self.ready();self.manager.open_document_cycle(goal,item,self.manager.document_cycle_assessment(goal,item,value),'test')
        item=self.store.update('plan_item',item['id'],bundles=2)
        self.manager.assign(goal,item)
        saved=self.store.get('plan_item',item['id'])
        self.assertEqual((saved['status'],saved['review_trigger']),('review','batch_limit'))
        self.assertFalse(self.manager.builders.active)

    def test_valid_execution_criteria_allow_cycle_but_missing_one_refuses_it(self):
        goal,item,value=self.ready()
        source=self.store.get('task','run-evidence');source.update(id='own-execution',plan_item_id=item['id'])
        task=self.store.save('task',source);self.manager.collect_evidence(item,task)
        item=self.store.update('plan_item',item['id'],acceptance=[*item['acceptance'],'Run the actual test.'])
        value['criteria']=[{'criterion':'Run the actual test.','evidence_ids':['own-execution:1']}]
        self.assertIsNotNone(self.manager.document_cycle_assessment(goal,item,value))
        item['acceptance'].append('Run another acceptance test.')
        self.assertIsNone(self.manager.document_cycle_assessment(goal,item,value))

    def test_repeat_or_exhaustion_creates_human_decision_without_third_cycle(self):
        goal,item,value=self.ready();self.manager.open_document_cycle(goal,item,self.manager.document_cycle_assessment(goal,item,value),'first')
        item=self.store.update('plan_item',item['id'],status='review',review_trigger='batch_limit',bundles=2)
        self.providers.complete=lambda *a,**k:response(json.dumps({**value,'batch_action':'defer'}))
        self.manager.review(goal,item)
        self.assertEqual(self.store.get('plan_item',item['id'])['document_cycles_used'],1)
        self.assertEqual(len(self.manager.pending_decisions(goal['id'])),1)

    def test_second_distinct_cycle_is_allowed_and_third_is_not(self):
        goal,item,value=self.ready();first=self.manager.document_cycle_assessment(goal,item,value)
        self.manager.open_document_cycle(goal,item,first,'first')
        item=self.store.update('plan_item',item['id'],status='review',review_trigger='batch_limit',bundles=2)
        value['defects']=[{'kind':'document_fix','text':'Add the missing Evidence heading.'}]
        second=self.manager.document_cycle_assessment(goal,item,value);self.assertIsNotNone(second)
        self.manager.open_document_cycle(goal,item,second,'second')
        item=self.store.update('plan_item',item['id'],status='review',review_trigger='batch_limit',bundles=2)
        value['defects']=[{'kind':'document_fix','text':'Correct a different citation.'}]
        self.assertIsNone(self.manager.document_cycle_assessment(goal,item,value));self.assertEqual(item['document_cycles_used'],2)

    def test_explicit_human_rework_exits_automatic_document_restriction(self):
        goal,item,value=self.ready();self.manager.open_document_cycle(goal,item,self.manager.document_cycle_assessment(goal,item,value),'test')
        item=self.store.get('plan_item',item['id'])
        decision=self.manager.create_decision(goal,{'question':'New scope?','options':['Rework'],'recommendation':'Rework','reason':'Human directs new work.','reversible':False},item_id=item['id'])
        self.store.update('decision',decision['id'],state='answered',answer='Implement a new test.')
        self.manager.apply_decision_action(goal,item,'rework','Implement a new test.',decision['id'])
        saved=self.store.get('plan_item',item['id']);self.assertFalse(saved['document_cycle_active']);self.assertEqual(self.manager.bundle_limit(saved),4)

    def test_planner_and_builder_context_expose_lifecycle_without_touching_work_prompt(self):
        goal,item,value=self.ready();self.manager.open_document_cycle(goal,item,self.manager.document_cycle_assessment(goal,item,value),'test')
        item=self.store.get('plan_item',item['id'])
        context=self.manager.context(goal,item,'Builder')
        current=json.loads(next(m['content'] for m in context if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])['item']
        self.assertTrue(current['document_cycle_active']);self.assertEqual(current['bundle_limit'],2)

    def test_document_cycle_second_stalled_chunk_goes_to_review_without_scope_shrink(self):
        goal,item,value=self.ready();self.manager.open_document_cycle(goal,item,self.manager.document_cycle_assessment(goal,item,value),'test')
        item=self.store.update('plan_item',item['id'],bundles=2)
        task=self.store.update('task','doc-evidence',builder_report={'chunk_id':None,'status':'progress','summary':'Still editing prose.','verification':'No execution needed.'})
        with patch.object(self.manager,'handle_stalled_batch',side_effect=AssertionError('No document-cycle scope detour')):
            self.manager.finished_batch(goal,item,task)
        item=self.store.get('plan_item',item['id']);self.assertEqual((item['status'],item['review_trigger']),('review','batch_limit'))

    def test_legacy_document_without_paths_uses_current_document_observation(self):
        goal,item,value=self.ready();rules=copy.deepcopy(item['acceptance_rules']);rules[item['acceptance'][0]].pop('document_paths')
        item=self.store.update('plan_item',item['id'],acceptance_rules=rules)
        self.assertEqual(self.manager.document_cycle_assessment(goal,item,value)['paths'],['review/REPORT.md'])


class NumericGroundingTests(unittest.TestCase):
    setUp=documents.DocumentAcceptanceTests.setUp
    tearDown=documents.DocumentAcceptanceTests.tearDown
    create=documents.DocumentAcceptanceTests.create
    fixture=documents.DocumentAcceptanceTests.fixture
    write_doc=documents.DocumentAcceptanceTests.write_doc

    def claim(self,text,output,role='results'):
        if not hasattr(self,'claim_fixture'):self.claim_fixture=self.fixture('Architecture notes.',output=output)
        goal,item,source=self.claim_fixture
        task=self.store.get('task','run-evidence');task['events'][1]['result']['output']=output;self.store.save('task',task)
        self.manager.collect_evidence(source,task)
        refs=self.manager.valid_evidence(goal,source)
        return check_claim(self.manager,text+' (run-evidence:1).',1,refs,set(),{},role)

    def test_decimal_zero_thousands_trailing_zeros_and_cost_alias(self):
        for text,output in [('Estimated proxy cost was 0 USD.','{"total_cost_usd_estimated":0.0}'),
            ('There were 1,000 episodes.','{"episodes":1000}'),('The value was 51.50.','{"value":51.5}')]:
            with self.subTest(text=text):self.assertTrue(self.claim(text,output)['passed'])

    def test_rounding_uses_document_digits_and_counts_remain_exact(self):
        for text in ('Estimated cost $0.047.','Estimated cost $0.05.'):
            self.assertTrue(self.claim(text,'{"cost_usd":0.04722}')['passed'])
        result=self.claim('7 tests passed.','9 passed')
        self.assertFalse(result['passed']);self.assertIn('tests_passed claimed 7; observed 9',result['defects'][0]);self.assertIn('comparison: exact numeric count',result['defects'][0])
        self.assertFalse(self.claim('Estimated cost $0.05.','{"cost_usd":0.044}')['passed'])

    def test_units_percent_and_paired_alternate_unit_value(self):
        self.assertTrue(self.claim('Measured width 5 in (127 mm).','Measured width 127 mm.')['passed'])
        self.assertTrue(self.claim('Measured width 127 mm / 5 in.','Measured width 5 in.')['passed'])
        self.assertFalse(self.claim('Measured width 5 in.','Measured width 127 mm.')['passed'])
        for text in ('Measured rate 51.50%.','Measured speed 51.50 fpm.','Measured pressure 51.50 psi.'):
            self.assertTrue(self.claim(text,text.replace('51.50','51.5'))['passed'])

    def test_failed_comparison_explains_value_evidence_and_method(self):
        result=self.claim('Estimated cost $2.50.','{"cost_usd":0.04722}')
        self.assertFalse(result['passed']);message=' '.join(result['defects'])
        for part in ('2.50','0.04722','comparison:','ROUND_HALF_UP','2 decimal places'):self.assertIn(part,message)


if __name__=='__main__':unittest.main()
