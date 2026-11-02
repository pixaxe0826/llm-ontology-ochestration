"""Offline Goal productivity contracts: real scoped files, fake Planner, no cloud/GPU."""
import ast
import hashlib
import json
import threading
import time
import unittest
from unittest.mock import patch

import test_goals as fixtures
from test_goals import response,until
from workbench.agent import TOOLS as WORK_TOOLS
from workbench.goal_files import file_tools
from workbench.goals import PLANNER_ALLOWED
from workbench.plan_updates import prepare_plan,initial_items
from workbench.goal_prompts import system
from workbench.builder_productivity import read_limits
from workbench.file_views import tool_text


class ProductivityTests(unittest.TestCase):
    setUp=fixtures.GoalTests.setUp
    tearDown=fixtures.GoalTests.tearDown
    create=fixtures.GoalTests.create

    def fixture(self,**goal_values):
        goal=self.create(start=False,**goal_values)
        self.manager.add_items(goal,[{'key':'unit','title':'Implement a bounded Python fix','acceptance':[{'text':'A real passing test is recorded.','kind':'execution'}],'depends_on':[],'blocked_by_decision':[]}])
        item=self.manager.items(goal['id'])[0]
        task=self.store.save('task',{'id':'productivity-task','goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
            'workspace':str(self.folder),'provider':'meta','status':'running','events':[],'messages':[],'allow_write':True,'execution_cycle':0})
        self.store.update('plan_item',item['id'],task_id=task['id'],task_ids=[task['id']],bundles=1,attempts=1)
        self.manager.builders.local.chunk_id='chunk-one';self.manager.builders.cancels[task['id']]=threading.Event()
        self.manager.proxy_tokens[goal['id']]='synthetic-goal-proxy-productivity'
        (self.folder/'mod.py').write_text('"""Module summary.\nSecond line."""\nvalue = 1\n\nclass Motor:\n    """Motor summary."""\n    def run(self, speed: float = 1.0) -> float:\n        """Run summary.\n        More details.\n        """\n        return speed\n',encoding='utf-8')
        return goal,item,task

    def execute(self,task,name,args):
        self.store.event(task['id'],'tool',name=name,arguments=args)
        result=self.manager.builders.execute(task['id'],name,args)
        self.store.event(task['id'],'tool_result',name=name,result=result)
        return result

    def test_edit_unique_match_records_diff_backup_and_input_output_hashes(self):
        goal,item,task=self.fixture();read=self.execute(task,'read_file',{'path':'mod.py'})
        edited=self.execute(task,'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 2'})
        self.assertEqual(edited['previous_sha256'],read['sha256']);self.assertNotEqual(edited['sha256'],read['sha256'])
        change=next(e['change'] for e in self.store.get('task',task['id'])['events'] if e['kind']=='change')
        self.assertIn('+value = 2',change['diff']);self.assertTrue(change['backup_id'])
        evidence,_=self.manager.collect_evidence(item,self.store.get('task',task['id']))
        self.assertTrue(any(e['kind']=='change' and e['path']=='mod.py' for e in evidence))

    def test_edit_zero_multiple_and_overlapping_matches_are_rejected_with_locations(self):
        _,_,task=self.fixture();(self.folder/'mod.py').write_text('aaaa\nx = 1\nx = 1\n',encoding='utf-8')
        self.execute(task,'read_file',{'path':'mod.py'})
        for old,count in [('missing',0),('x = 1',2),('aaa',2)]:
            result=self.execute(task,'edit_file',{'path':'mod.py','old_str':old,'new_str':'replacement'})
            self.assertEqual(result['match_count'],count);self.assertIn('error',result)
            self.assertEqual(len(result['match_locations']),count)
        self.assertEqual((self.folder/'mod.py').read_text(),'aaaa\nx = 1\nx = 1\n')

    def test_edit_requires_read_in_same_chunk_and_rejects_external_change(self):
        _,_,task=self.fixture()
        with self.assertRaisesRegex(ValueError,'current chunk'):self.manager.builders.execute(task['id'],'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 2'})
        self.execute(task,'read_file',{'path':'mod.py'})
        self.manager.builders.local.chunk_id='other'
        with self.assertRaisesRegex(ValueError,'current chunk'):self.manager.builders.execute(task['id'],'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 2'})
        self.manager.builders.local.chunk_id='chunk-one';(self.folder/'mod.py').write_text('value = 3\n')
        with self.assertRaisesRegex(ValueError,'changed'):self.manager.builders.execute(task['id'],'edit_file',{'path':'mod.py','old_str':'value = 3','new_str':'value = 2'})
        self.assertEqual((self.folder/'mod.py').read_text(),'value = 3\n')

    def test_edit_noop_does_not_reset_read_guard(self):
        _,_,task=self.fixture();self.execute(task,'read_file',{'path':'mod.py'})
        result=self.execute(task,'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 1'})
        self.assertTrue(result['unchanged']);state=self.manager.chunk_state(task,'chunk-one')
        self.assertEqual(state['consecutive_reads'],1);self.assertEqual(state['actions'],0)

    def test_edit_cannot_write_outside_workspace_or_read_only_task(self):
        _,_,task=self.fixture()
        with self.assertRaises(ValueError):self.manager.builders.execute(task['id'],'edit_file',{'path':'../outside.py','old_str':'a','new_str':'b'})
        self.store.update('task',task['id'],allow_write=False)
        with self.assertRaises(PermissionError):self.manager.builders.execute(task['id'],'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 2'})

    def test_provisional_edit_obeys_core_spec_prohibition(self):
        goal,item,task=self.fixture(allow_provisional=True)
        decision=self.manager.create_decision(goal,{'question':'Choose reversible design','options':['A','B'],'recommendation':'A','reason':'Local-only','reversible':True},item_id=item['id'])
        self.store.update('plan_item',item['id'],provisional_decisions=[decision['id']],blocked_by_decision=[decision['id']],provisional_scope='local_files_and_tests')
        with self.assertRaises(PermissionError):self.manager.builders.execute(task['id'],'edit_file',{'path':'spec/a.yaml','old_str':'a','new_str':'b'})

    def test_code_map_ast_signatures_docstrings_lines_without_execution(self):
        _,_,task=self.fixture();(self.folder/'unsafe.py').write_text('raise RuntimeError("must not execute")\nasync def solve(x: int) -> str:\n    """Solve one case."""\n    return str(x)\n')
        result=self.execute(task,'code_map',{})
        mod=next(f for f in result['files'] if f['path']=='mod.py');self.assertEqual(mod['module_docstring'],'Module summary.')
        run=next(s for s in mod['symbols'] if s['name']=='Motor.run')
        self.assertIn('speed: float=1.0',run['signature'].replace(' = ','='));self.assertEqual(run['docstring'],'Run summary.');self.assertEqual(run['line'],7)
        other=next(f for f in result['files'] if f['path']=='unsafe.py');self.assertTrue(other['symbols'][0]['signature'].startswith('async def'))

    def test_code_map_yaml_json_top_level_structure(self):
        _,_,task=self.fixture();(self.folder/'a.yaml').write_text('machine:\n  speed: 1\nparts: [a,b]\n');(self.folder/'b.json').write_text('{"settings":{"v":2}}')
        result=self.execute(task,'code_map',{'paths':['a.yaml','b.json']})
        self.assertEqual(result['files'][0]['top_level_keys'][0]['child_keys'],['speed'])
        self.assertEqual(result['files'][1]['top_level_keys'][0]['key'],'settings')

    def test_code_map_hash_cache_and_invalidation(self):
        _,_,task=self.fixture()
        with patch('workbench.code_map.python_map',wraps=__import__('workbench.code_map',fromlist=['python_map']).python_map) as parse:
            first=self.manager.code_map_tool(task,{'paths':['mod.py']});second=self.manager.code_map_tool(task,{'paths':['mod.py']})
            self.assertEqual(parse.call_count,1);self.assertEqual(second['cache_hits'],1)
            with (self.folder/'mod.py').open('a') as stream:stream.write('\ndef added(): pass\n')
            third=self.manager.code_map_tool(task,{'paths':['mod.py']});self.assertEqual(parse.call_count,2)
            self.assertNotEqual(first['files'][0]['sha256'],third['files'][0]['sha256'])

    def test_code_map_bounded_json_and_tool_view_preserves_20k(self):
        _,_,task=self.fixture();(self.folder/'many.py').write_text('\n'.join(f'def function_{i}(argument: int = 1):\n    """A long documented function signature {i}."""\n    return argument\n' for i in range(600)))
        result=self.manager.code_map_tool(task,{'paths':['many.py']});encoded=json.dumps(result,ensure_ascii=False)
        self.assertLessEqual(len(encoded),20000);self.assertTrue(result['truncated']);self.assertGreater(len(encoded),8000)
        self.assertEqual(json.loads(tool_text(encoded))['read_mode'],'code_map')
        with self.assertRaises(ValueError):self.manager.code_map_tool(task,{'paths':['../']})

    def test_code_map_excludes_secret_files_and_registered_secret_content(self):
        _,_,task=self.fixture();secret='productivity-secret-not-for-map-211'
        from workbench.security import register_secret
        register_secret(secret);(self.folder/'private.py').write_text('# '+secret);(self.folder/'.env').write_text(secret)
        result=self.manager.code_map_tool(task,{})
        self.assertNotIn(secret,json.dumps(result));self.assertNotIn('.env',[r['path'] for r in result['files']])
        self.assertNotIn(secret,json.dumps(self.store.items('code_map_cache')))

    def test_read_guard_warns_at_8_allows_12_blocks_13_without_read(self):
        _,_,task=self.fixture()
        for n in range(1,13):
            result=self.execute(task,'read_file',{'path':'mod.py'})
            self.assertEqual(bool(result.get('read_stall')),n>=8)
        with patch.object(self.manager,'read_tool',side_effect=AssertionError('blocked read executed')):
            result=self.execute(task,'read_file',{'path':'mod.py'})
        self.assertIn('blocked',result['error']);self.assertIn('next action',result['system_notice'])
        self.assertEqual(self.manager.chunk_state(task,'chunk-one')['blocked_reads'],1)

    def test_successful_edit_resets_guard_but_rejected_edit_does_not(self):
        _,_,task=self.fixture(read_loop_limits={'warn':1,'block':2})
        self.execute(task,'read_file',{'path':'mod.py'});self.execute(task,'read_file',{'path':'mod.py'})
        rejected=self.execute(task,'edit_file',{'path':'mod.py','old_str':'missing','new_str':'x'});self.assertIn('error',rejected)
        self.assertEqual(self.manager.chunk_state(task,'chunk-one')['consecutive_reads'],2)
        self.execute(task,'edit_file',{'path':'mod.py','old_str':'value = 1','new_str':'value = 2'})
        self.assertEqual(self.manager.chunk_state(task,'chunk-one')['consecutive_reads'],0)
        self.assertNotIn('error',self.execute(task,'read_file',{'path':'mod.py'}))

    def test_nonzero_executed_command_resets_but_denied_command_does_not(self):
        _,_,task=self.fixture();(self.folder/'fail.py').write_text('raise SystemExit(1)')
        self.execute(task,'read_file',{'path':'mod.py'})
        result=self.execute(task,'run_command',{'argv':['python','fail.py'],'cwd':'.','timeout':10})
        self.assertEqual(result['exit_code'],1);self.assertEqual(self.manager.chunk_state(task,'chunk-one')['consecutive_reads'],0)
        self.execute(task,'read_file',{'path':'mod.py'})
        self.manager.after_builder_tool(task,'run_command',{'denied':True},'chunk-one')
        self.assertEqual(self.manager.chunk_state(task,'chunk-one')['consecutive_reads'],1)

    def test_read_limits_validation_settings_and_per_chunk_reset(self):
        goal,item,task=self.fixture()
        for value in ({'warn':9,'block':8},{'warn':0,'block':12},{'warn':True,'block':12},{'warn':1.5,'block':12},{'warn':8,'block':101}):
            with self.assertRaises(ValueError):read_limits(value)
        self.manager.update(goal['id'],{'read_loop_limits':{'warn':2,'block':3}})
        self.execute(task,'code_map',{});self.assertEqual(self.manager.chunk_state(task,'chunk-one')['consecutive_reads'],1)
        self.manager.builders.local.chunk_id='chunk-two';self.assertEqual(self.manager.chunk_state(task,'chunk-two')['consecutive_reads'],0)

    def stalled(self,goal,item,task,chunk):
        self.manager.builders.local.chunk_id=chunk
        self.store.event(task['id'],'chunk_started',chunk_id=chunk)
        self.execute(task,'read_file',{'path':'mod.py'})
        self.store.update('task',task['id'],status='paused')
        return self.manager.record_chunk_summary(task['id'],chunk)

    def test_stalled_summary_idempotency_carryover_and_no_false_provider_stall(self):
        goal,item,task=self.fixture();summary=self.stalled(goal,item,task,'first')
        self.assertEqual(summary['status'],'stalled');self.assertTrue(summary['stalled'])
        self.manager.record_chunk_summary(task['id'],'first');self.assertEqual(len(self.store.items('goal_chunk_summary')),1)
        context=self.manager.productivity_context(goal,self.store.get('plan_item',item['id']))
        self.assertTrue(context['previous_chunk_stalled']);self.assertIn('Implement immediately',context['instruction'])
        self.store.update('task',task['id'],status='failed');self.assertFalse(self.manager.record_chunk_summary(task['id'],'first')['stalled'])

    def test_two_stalls_request_one_automatic_scope_split_before_human(self):
        goal,item,task=self.fixture();self.stalled(goal,item,task,'one');self.stalled(goal,item,task,'two')
        self.assertTrue(self.manager.handle_stalled_batch(goal,item,task));item=self.store.get('plan_item',item['id'])
        self.assertTrue(item['stall_replan_pending']);self.assertEqual(self.manager.pending_decisions(goal['id']),[])
        hints={'files_to_read':['mod.py'],'files_to_write':['mod.py'],'commands_to_run':[['python','-m','pytest']]}
        reply=json.dumps({'items':[{'key':'child','title':'Fix one function','acceptance':['A real passing test is recorded.'],**hints}],'summary':'One function and one test.'})
        with patch.object(self.manager,'phase',return_value=reply) as planner:
            self.assertTrue(self.manager.shrink_stalled_item(goal,item));self.assertEqual(planner.call_count,1)
            self.assertFalse(self.manager.shrink_stalled_item(goal,self.store.get('plan_item',item['id'])))
        parent=self.store.get('plan_item',item['id']);self.assertEqual(parent['wait_reason'],'split_children');self.assertEqual(parent['reworks'],0)
        child=next(i for i in self.manager.items(goal['id']) if i['id']!=parent['id']);self.assertEqual(child['parent'],parent['id'])
        self.assertEqual(child['files_to_read'],['mod.py']);self.assertTrue(child['stall_replan_attempted'])
        self.assertEqual(self.manager.pending_decisions(goal['id']),[])

    def test_stall_after_scope_reduction_escalates_without_another_planner_call(self):
        goal,item,task=self.fixture();self.stalled(goal,item,task,'one');self.stalled(goal,item,task,'two')
        item=self.store.update('plan_item',item['id'],stall_replan_attempted=True)
        with patch.object(self.manager,'phase',side_effect=AssertionError('second automatic replan')):
            self.assertTrue(self.manager.handle_stalled_batch(goal,item,task))
        self.assertEqual(len(self.manager.pending_decisions(goal['id'])),1)

    def test_scope_json_format_retry_once_preserves_one_logical_replan(self):
        goal,item,task=self.fixture();item=self.store.update('plan_item',item['id'],stall_replan_pending=True)
        reply=json.dumps({'items':[{'key':'child','title':'One edit','acceptance':['Implement original criterion'],
            'files_to_read':['mod.py'],'files_to_write':['mod.py'],'commands_to_run':[]}],'summary':'One bounded edit.'})
        with patch.object(self.manager,'phase',side_effect=['not JSON',reply]) as planner:
            self.assertTrue(self.manager.shrink_stalled_item(goal,item));self.assertEqual(planner.call_count,2)
            self.assertIn('previous scope JSON failed validation',planner.call_args.args[2])
        self.assertEqual(len(self.store.items('goal_stall_replan')),1)
        self.assertEqual(len(self.store.items('goal_stall_replan')[0]['attempts']),2)
        self.assertEqual(self.manager.pending_decisions(goal['id']),[])

    def test_scope_reduction_cannot_update_existing_items_or_create_parent_cycle(self):
        goal,item,task=self.fixture();item=self.store.update('plan_item',item['id'],stall_replan_pending=True)
        before=self.manager.items(goal['id'])
        reply=json.dumps({'items':[{'key':'bad','title':'bad','acceptance':['pass'],'depends_on':[item['id']],
            'files_to_read':['mod.py'],'files_to_write':['mod.py'],'commands_to_run':[]}],'summary':'invalid'})
        with patch.object(self.manager,'phase',return_value=reply):self.assertFalse(self.manager.shrink_stalled_item(goal,item))
        self.assertEqual(len(self.manager.items(goal['id'])),len(before));self.assertEqual(len(self.manager.pending_decisions(goal['id'])),1)

    def test_hints_validate_preserve_and_reach_builder_context(self):
        goal,item,task=self.fixture();hints={'files_to_read':['mod.py'],'files_to_write':['mod.py'],'commands_to_run':[['python','-m','pytest']]}
        self.manager.apply_plan_patch(goal,prepare_plan(self.manager.items(goal['id']),[{'key':'unit',**hints}]))
        item=self.store.get('plan_item',item['id']);messages=self.manager.context(goal,item,'Builder')
        payload=json.loads(next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])
        self.assertEqual(payload['item']['files_to_read'],['mod.py']);self.assertIn('code_map',payload['productivity'])
        self.assertIn('edit_file',messages[0]['content']);self.assertIn('actual',system(goal,'Planner'))
        for bad in ({'files_to_read':['mod.py']*6},{'files_to_write':['../out.py']},{'commands_to_run':['python script.py']}):
            with self.assertRaises(ValueError):prepare_plan([item],[{'key':'unit',**bad}])
        normalized=initial_items([{'key':'new','title':'bounded','acceptance':['pass'],**hints}]);self.assertEqual(normalized[0]['commands_to_run'],hints['commands_to_run'])

    def test_goal_only_tools_and_restart_of_claimed_scope_does_not_replay(self):
        names={t['function']['name'] for t in file_tools(WORK_TOOLS)}
        self.assertTrue({'edit_file','code_map'}<=names);self.assertFalse({'edit_file','code_map'}&PLANNER_ALLOWED)
        self.assertFalse({'edit_file','code_map'}&{t['function']['name'] for t in WORK_TOOLS})
        goal,item,task=self.fixture();item=self.store.update('plan_item',item['id'],status='waiting',wait_reason='stall_replan',stall_replan_attempted=True,stall_replan_pending=False)
        self.store.save('goal_stall_replan',{'goal_id':goal['id'],'plan_item_id':item['id'],'state':'requesting'})
        with patch.object(self.manager,'phase',side_effect=AssertionError('replayed')):self.manager.recover_stall_request(goal,item)
        self.assertEqual(self.store.items('goal_stall_replan')[0]['state'],'interrupted');self.assertEqual(len(self.manager.pending_decisions(goal['id'])),1)

    def test_real_builder_loop_two_read_only_chunks_then_one_planner_split_and_done(self):
        goal=self.create(start=False,max_steps=2)
        (self.folder/'source.py').write_text('value=1\n')
        self.manager.add_items(goal,[{'key':'parent','title':'Implement the fix','acceptance':['A real passing test is recorded.'],'depends_on':[],'blocked_by_decision':[]}])
        counters={};scope_calls=[]
        def complete(provider,messages,model,tools=None,max_tokens=None,**kwargs):
            last=messages[-1].get('content') or ''
            if provider=='baseten':
                if 'AUTOMATIC READ-STALL' in last:
                    scope_calls.append(last)
                    return response(json.dumps({'items':[{'key':'child','title':'One explicit implementation and test','acceptance':['A real passing test is recorded.'],
                        'files_to_read':['source.py'],'files_to_write':['test_success.py'],'commands_to_run':[['python','-m','unittest','test_success.py']], 'depends_on':[],'blocked_by_decision':[]}],'summary':'One test file, one command.'}))
                if 'Review this REPORT' in last:
                    current=json.loads(next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])['item']
                    refs=json.loads(last.split('VALID OBSERVED EVIDENCE IDS: ',1)[1])
                    return response(json.dumps({'decision':'accept','defects':[],'criteria':[{'criterion':c,'evidence_ids':refs} for c in current['acceptance']], 'summary':'Observed command passed.','next_items':[],'goal_complete':True}))
                raise AssertionError('Unexpected Planner phase: '+last[:60])
            current=json.loads(next(m['content'] for m in messages if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])['item']
            step=counters.get(current['id'],0);counters[current['id']]=step+1
            if current['title']=='Implement the fix':
                if step%2==0:return response(calls=[('read_file',{'path':'source.py'})])
                return response(calls=[('report_item',{'status':'progress','summary':'Read existing source.','verification':'NOT RUN: implementation.'})])
            if step==0:return response(calls=[('write_file',{'path':'test_success.py','content':'import unittest\nclass Check(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1+1,2)\n','expected_sha256':None})])
            if step==1:return response(calls=[('run_command',{'argv':['python','-m','unittest','test_success.py'],'cwd':'.','timeout':10})])
            return response(calls=[('report_item',{'status':'completed','summary':'Implemented test and observed exit 0.','verification':'Observed command result.'})])
        with patch.object(self.providers,'complete',side_effect=complete):
            self.manager.launch(goal['id'])
            final=until(lambda:(g if (g:=self.store.get('goal',goal['id']))['status'] in ('done','blocked','paused','waiting_decision') else None),timeout=20)
        self.assertEqual(final['status'],'done',json.dumps({'reason':final.get('reason'),'decisions':self.manager.pending_decisions(goal['id']),
            'replan':self.store.items('goal_stall_replan'),'review_errors':[{k:i.get(k) for k in ('title','status','review_error','stall_reason')} for i in self.manager.items(goal['id'])]},ensure_ascii=False));self.assertEqual(len(scope_calls),1)
        self.assertEqual(len([s for s in self.store.items('goal_chunk_summary') if s.get('stalled')]),2)
        self.assertEqual(self.manager.pending_decisions(goal['id']),[])
        self.assertEqual(self.store.items('goal_stall_replan')[0]['state'],'applied')


if __name__=='__main__':unittest.main()
