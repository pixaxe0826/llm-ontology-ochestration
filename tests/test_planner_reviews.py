"""Planner schema failures, plan-key edits and preservation of pre-fix P2 history."""
import copy
import json
import os
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from test_goals import GoalTests,response,until
from workbench.goal_recovery import recover_proxy_item,ERROR
from workbench.plan_updates import prepare_plan,plan_key
from workbench.workspace import Workspace


class PlannerReviewTests(GoalTests):
    def reviewed(self):
        goal=self.create(start=False)
        self.manager.add_items(goal,[{'key':'existing','title':'Existing artifact','acceptance':['artifact.txt exists']}])
        item=self.manager.items(goal['id'])[0];ws=Workspace(self.folder,self.store.root)
        change=ws.write('artifact.txt','observed')
        task=self.store.save('task',{'id':'observed','goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),
            'status':'succeeded','events':[{'kind':'change','change':change}]})
        self.manager.collect_evidence(item,task)
        self.store.update('plan_item',item['id'],status='review',attempts=1,bundles=1)
        return goal,self.store.get('plan_item',item['id'])

    def verdict(self,item,**changes):
        return {'decision':'accept','defects':[],'criteria':[{'criterion':item['acceptance'][0],'evidence_ids':[item['evidence'][0]['id']]}],
                'summary':'Observed artifact reviewed.','next_items':[],'goal_complete':False,**changes}

    def test_invalid_json_retries_once_with_feedback_and_accounts_for_both_calls(self):
        goal,item=self.reviewed();answers=iter(['not JSON',json.dumps(self.verdict(item))]);instructions=[]
        def complete(provider,messages,*args,**kwargs):
            instructions.append(messages[-1]['content']);return response(next(answers))
        self.providers.complete=complete
        self.assertTrue(self.manager.review(goal,item))
        saved=self.store.get('plan_item',item['id'])
        self.assertEqual((saved['status'],saved['reworks'],saved['attempts'],saved['bundles']),('done',0,1,1))
        self.assertEqual(len(instructions),2);self.assertIn('failed server validation',instructions[1])
        self.assertEqual(len(self.manager.ledger.rows(goal['id'])),2)
        attempts=self.store.items('goal_review_attempt',None)
        self.assertEqual({r['state'] for r in attempts},{'review_error','validated'})
        self.assertTrue(all('response' in r for r in attempts))

    def test_two_invalid_outputs_keep_item_and_builder_counters_then_third_cycle_escalates(self):
        goal,item=self.reviewed();self.providers.complete=lambda *a,**k:response('{"decision":"not-a-verdict"}')
        before=copy.deepcopy(item)
        for count in range(1,4):
            self.assertFalse(self.manager.review(goal,self.store.get('plan_item',item['id'])))
            saved=self.store.get('plan_item',item['id'])
            for key in ('status','attempts','bundles','reworks','acceptance','evidence'):self.assertEqual(saved[key],before[key])
            self.assertEqual(saved['review_error_count'],count)
            escalations=[m for m in self.manager.messages(goal['id']) if m['type']=='ESCALATE']
            self.assertEqual(len(escalations),int(count==3))
        self.assertIn('"platform_error": true',escalations[0]['body']);self.assertEqual(escalations[0]['from'],'System')
        self.assertEqual(len(self.manager.ledger.rows(goal['id'])),6)
        self.assertFalse(self.manager.review(goal,saved));self.assertEqual(len(self.manager.ledger.rows(goal['id'])),6)

    def test_review_backoff_and_explicit_reset_do_not_charge_builder_retries(self):
        goal,item=self.reviewed();self.providers.complete=lambda *a,**k:response('bad JSON')
        self.manager.review(goal,item);saved=self.store.get('plan_item',item['id'])
        self.assertFalse(self.manager.review_ready(saved))
        with patch('workbench.goal_reviews.time.time',return_value=saved['review_error']['retry_at']+1):self.assertTrue(self.manager.review_ready(saved))
        self.manager.save(goal,status='paused')
        self.manager.update(goal['id'],{'retry_review_item':item['id']})
        saved=self.store.get('plan_item',item['id'])
        self.assertTrue(self.manager.review_ready(saved));self.assertEqual((saved['attempts'],saved['bundles'],saved['reworks']),(1,1,0))
        self.assertEqual(len(self.store.items('goal_review_attempt',None)),2)

    def test_distinct_new_rows_with_duplicate_keys_no_longer_rework_the_builder(self):
        goal,item=self.reviewed()
        rows=[{'key':'followup','title':title,'acceptance':['Observable result.']} for title in ('One new task','Another new task')]
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item,next_items=rows)))
        self.assertTrue(self.manager.review(goal,item))
        items=self.manager.items(goal['id']);self.assertEqual(len(items),3)
        self.assertEqual({plan_key(i) for i in items},{'existing','followup','followup-2'})
        self.assertEqual(self.store.get('plan_item',item['id'])['reworks'],0)

    def test_existing_key_is_an_update_and_explicit_create_collision_gets_unique_key(self):
        goal,item=self.reviewed()
        rows=[{'key':'existing','title':'Updated existing title'},
              {'key':'existing','operation':'create','title':'A distinct new item','acceptance':['New result.']}]
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item,next_items=rows)))
        self.manager.review(goal,item)
        items=self.manager.items(goal['id']);self.assertEqual(len(items),2)
        saved=self.store.get('plan_item',item['id'])
        self.assertEqual(saved['title'],'Updated existing title');self.assertEqual(saved['evidence'],item['evidence'])
        self.assertEqual((saved['attempts'],saved['bundles'],saved['reworks']),(1,1,0))
        self.assertEqual(next(i for i in items if i['id']!=item['id'])['key'],'existing-2')

    def test_conflicting_existing_updates_retry_without_partial_plan_mutation(self):
        goal,item=self.reviewed();rows=[{'key':'existing','title':'A'},{'key':'existing','title':'B'}]
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item,next_items=rows)))
        self.assertFalse(self.manager.review(goal,item))
        saved=self.store.get('plan_item',item['id']);self.assertEqual(saved['title'],item['title']);self.assertEqual(saved['status'],'review')
        self.assertIn('Conflicting updates',saved['review_error']['message']);self.assertEqual(saved['reworks'],0)

    def test_duplicate_parent_is_ambiguous_and_cycles_are_rejected_before_writes(self):
        for rows in ([{'key':'new','title':'A','acceptance':['A']},{'key':'new','title':'B','acceptance':['B']},
                      {'key':'child','parent':'new','title':'Child','acceptance':['C']}],
                     [{'key':'a','parent':'b','title':'A','acceptance':['A']},{'key':'b','parent':'a','title':'B','acceptance':['B']}]):
            with self.assertRaises(ValueError):prepare_plan([],rows)

    def test_identical_rows_coalesce_and_suffixes_do_not_steal_explicit_future_keys(self):
        row={'key':'a','title':'A','acceptance':['A']}
        self.assertEqual(len(prepare_plan([],[row,copy.deepcopy(row)])['creates']),1)
        rows=[row,{'key':'a','title':'Another','acceptance':['B']},{'key':'a-2','title':'Explicit','acceptance':['C']}]
        self.assertEqual([r['key'] for r in prepare_plan([],rows)['creates']],['a','a-3','a-2'])

    def test_legacy_ids_are_stable_keys_and_update_does_not_duplicate_old_items(self):
        goal,item=self.reviewed();legacy=copy.deepcopy(item);legacy.pop('key');self.store.save('plan_item',legacy)
        self.manager.add_items(goal,[{'key':item['id'],'title':'Legacy title updated'}])
        saved=self.store.get('plan_item',item['id'])
        self.assertEqual(saved['id'],item['id']);self.assertEqual(len(self.manager.items(goal['id'])),1)
        context=self.manager.context(goal,saved,'Planner')
        self.assertIn(item['id'],context[0]['content']);self.assertIn('existing keys',context[0]['content'])

    def test_initial_plan_retains_normalized_keys(self):
        goal=self.create(start=False)
        rows=[{'key':'same','title':title,'acceptance':['Observed result.']} for title in ('First','Second')]
        self.providers.complete=lambda *a,**k:response(json.dumps({'items':rows,'summary':'Two tasks.'}))
        self.manager.plan(goal)
        self.assertEqual([i['key'] for i in self.manager.items(goal['id'])],['same','same-2'])

    def test_genuine_rework_decision_still_consumes_exactly_one_builder_retry(self):
        goal,item=self.reviewed()
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item,decision='rework',criteria=[],defects=['The observed output is incorrect.'])))
        self.manager.review(goal,item);saved=self.store.get('plan_item',item['id'])
        self.assertEqual((saved['status'],saved['reworks']),('rework',1));self.assertIsNone(saved['review_error'])

    def test_review_source_is_redacted_before_persisting(self):
        goal,item=self.reviewed();secret='synthetic-planner-output-secret-29132'
        self.providers.complete=lambda *a,**k:response('invalid JSON '+secret)
        with patch.dict(os.environ,{'BASETEN_API_KEY':secret}):self.manager.review(goal,item)
        rows=self.store.items('goal_review_attempt',None)
        self.assertTrue(all(secret not in json.dumps(row) and '[REDACTED]' in row['response'] for row in rows))

    def test_bad_review_does_not_prevent_another_todo_item_from_finishing(self):
        goal,bad=self.reviewed();self.manager.add_items(goal,[{'key':'other','title':'Another task','acceptance':['A real passing test is recorded.']}])
        other=next(i for i in self.manager.items(goal['id']) if i['id']!=bad['id']);original=self.providers.complete
        def complete(provider,messages,*args,**kwargs):
            if provider=='baseten' and 'Review this REPORT' in messages[-1]['content']:
                current=next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES'))
                if json.loads(current.split('\n',1)[1])['item']['id']==bad['id']:return response('invalid review')
            return original(provider,messages,*args,**kwargs)
        self.providers.complete=complete;self.manager.launch(goal['id'])
        until(lambda:self.store.get('plan_item',other['id'])['status']=='done')
        self.manager.control(goal['id'],'pause')
        self.assertEqual(self.store.get('plan_item',bad['id'])['reworks'],0)
        self.assertEqual(self.store.get('plan_item',bad['id'])['review_error_count'],1)


class ProxyRecoveryTests(GoalTests):
    def fixture(self):
        goal=self.create(start=False);self.manager.save(goal,status='paused')
        self.manager.add_items(goal,[{'key':'P2','title':'Product proxy check','acceptance':['Proxy response is saved.']}])
        item=self.manager.items(goal['id'])[0]
        (self.folder/'tools').mkdir();(self.folder/'review').mkdir()
        (self.folder/'tools/proxy_check.py').write_text('print("OK")\n')
        (self.folder/'review/PROXY_EVIDENCE.md').write_text('Historical HTTP 404 failure')
        tasks=[]
        for n,status in enumerate(('paused','succeeded','cancelled')):
            task=self.store.save('task',{'id':'old-'+str(n),'goal_id':goal['id'],'plan_item_id':item['id'],'status':status,'events':[]})
            tasks.append(task['id'])
        self.store.update('plan_item',item['id'],task_ids=tasks,task_id=tasks[-1],attempts=3,bundles=3,bundle_reported=2,reworks=1,
                          status='in_progress',defects=[ERROR],report={'summary':'Old platform failed; no live success.'})
        self.manager.message(goal,'Planner','Builder','REVIEW',json.dumps({'decision':'rework','defects':[ERROR]}),item['id'])
        self.manager.ledger.settle(self.manager.ledger.reserve(goal,'baseten','product',.01),actual_cost=.01)
        return goal,self.store.get('plan_item',item['id'])

    def test_recovery_preserves_files_evidence_history_spend_and_paused_goal(self):
        goal,item=self.fixture();before=self.manager.detail(goal['id'])
        paths=['tools/proxy_check.py','review/PROXY_EVIDENCE.md'];files={p:(self.folder/p).read_bytes() for p in paths}
        result=recover_proxy_item(self.store,goal['id'],item['id']);saved=self.store.get('plan_item',item['id'])
        self.assertEqual((saved['status'],saved['reworks'],saved['attempts'],saved['bundles']),('todo',0,0,0))
        self.assertIsNone(saved['task_id']);self.assertEqual(saved['task_ids'],item['task_ids'])
        self.assertEqual(saved['evidence'],item['evidence']);self.assertEqual(saved['report'],item['report'])
        after=self.manager.detail(goal['id'])
        for key in ('goal','messages','ledger'):self.assertEqual(before[key],after[key])
        for p,data in files.items():self.assertEqual((self.folder/p).read_bytes(),data)
        self.assertEqual(recover_proxy_item(self.store,goal['id'],item['id'])['id'],result['id'])
        self.assertEqual(len(self.store.items('goal_recovery',None)),1);self.assertEqual(self.providers.calls,[])

    def test_recovery_does_not_erase_a_real_builder_defect(self):
        goal,item=self.fixture()
        self.manager.message(goal,'Planner','Builder','REVIEW',json.dumps({'decision':'rework','defects':['Real code defect.']}),item['id'])
        with self.assertRaises(ValueError):recover_proxy_item(self.store,goal['id'],item['id'])
        self.assertEqual(self.store.get('plan_item',item['id']),item)

    def test_recovered_item_requires_fresh_exact_script_execution_and_keeps_output_evidence_current(self):
        goal,item=self.fixture();recover_proxy_item(self.store,goal['id'],item['id']);item=self.store.get('plan_item',item['id'])
        self.assertFalse(self.manager.fresh_rerun_observed(goal,item,{}))
        ws=Workspace(self.folder,self.store.root);old=ws.read('review/PROXY_EVIDENCE.md')
        ws.write('review/PROXY_EVIDENCE.md','New live response: OK',old['sha256'])
        task=self.store.save('task',{'id':'new-run','goal_id':goal['id'],'plan_item_id':item['id'],'status':'succeeded',
            'events':[{'kind':'tool_result','name':'read_file','result':old},
                      {'kind':'tool','name':'run_command','arguments':{'argv':['python','tools/proxy_check.py','review/PROXY_EVIDENCE.md'],'cwd':'.'}},
                      {'kind':'tool_result','name':'run_command','result':{'exit_code':0,'output':'PASS'}},
                      {'kind':'tool_result','name':'read_file','result':ws.read('review/PROXY_EVIDENCE.md')}]})
        self.manager.collect_evidence(item,task);item=self.store.get('plan_item',item['id']);valid=self.manager.valid_evidence(goal,item)
        self.assertIn('new-run:2',valid);self.assertIn('new-run:3',valid)
        self.assertTrue(self.manager.fresh_rerun_observed(goal,item,valid))
        ws.write('tools/proxy_check.py','print("changed")\n',ws.read('tools/proxy_check.py')['sha256'])
        self.assertFalse(self.manager.fresh_rerun_observed(goal,item,valid))


for target in (PlannerReviewTests,ProxyRecoveryTests):
    for name in dir(GoalTests):
        if name.startswith('test_') and name not in target.__dict__:setattr(target,name,None)
del GoalTests,target,name
if __name__=='__main__':unittest.main()
