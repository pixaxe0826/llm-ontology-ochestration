"""Range-aware Goal file views and bounded carryover; no real providers or GPUs."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_goals import GoalTests,response
from workbench.agent import Agents,TOOLS
from workbench.file_views import bounded_result,encoded,tool_text,observation_text
from workbench.goal_files import file_tools
from workbench.goal_prompts import system
from workbench.workspace import Workspace


class FileViewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'workspace';self.root.mkdir()
        self.ws=Workspace(self.root,Path(self.temp.name)/'state')
        self.text=''.join(f'{i:04}: value "quoted" \\ section '+('x'*70)+'\n' for i in range(1,701))
        (self.root/'large.txt').write_text(self.text,encoding='utf-8')
    def tearDown(self):self.temp.cleanup()

    def test_existing_workspace_read_retains_exact_legacy_contract(self):
        result=self.ws.read('large.txt')
        self.assertEqual(set(result),{'path','content','sha256'});self.assertEqual(result['content'],self.text)
        self.assertEqual(result['sha256'],hashlib.sha256((self.root/'large.txt').read_bytes()).hexdigest())

    def test_one_based_inclusive_range_total_and_empty_file(self):
        result=self.ws.read('large.txt',11,13)
        self.assertEqual((result['total_lines'],result['start_line'],result['end_line']),(700,11,13))
        self.assertEqual(result['content'],''.join(self.text.splitlines(True)[10:13]))
        self.assertFalse(result['truncated']);self.assertEqual(result['next_start_line'],14)
        (self.root/'empty.txt').write_text('')
        empty=self.ws.read('empty.txt',include_lines=True)
        self.assertEqual((empty['total_lines'],empty['start_line'],empty['end_line']),(0,0,0));self.assertIsNone(empty['next_start_line'])

    def test_full_view_is_valid_json_at_most_8000_with_head_and_next_line(self):
        raw=self.ws.read('large.txt',include_lines=True);view=bounded_result(raw)
        self.assertLessEqual(len(encoded(view)),8000);self.assertTrue(view['truncated'])
        self.assertTrue(view['content'].startswith('0001:'));self.assertEqual(view['next_start_line'],view['end_line']+1)
        self.assertEqual(json.loads(tool_text(encoded(raw)))['content'],view['content'])

    def test_range_view_uses_20000_and_paginates_without_losing_lines(self):
        start=100;pieces=[]
        while True:
            view=bounded_result(self.ws.read('large.txt',start,550))
            self.assertLessEqual(len(encoded(view)),20000);pieces.append(view['content'])
            if not view['truncated']:break
            self.assertGreater(len(encoded(view)),8000);self.assertEqual(view['next_start_line'],view['end_line']+1)
            start=view['next_start_line']
        self.assertEqual(''.join(pieces),''.join(self.text.splitlines(True)[99:550]))

    def test_invalid_ranges_and_existing_file_size_limit(self):
        for start,end in ((0,2),(-1,None),(True,2),(1,False),(1,1.5),(10,9),(701,None)):
            with self.assertRaises(ValueError):self.ws.read('large.txt',start,end)
        (self.root/'too-big.txt').write_text('x'*200001)
        with self.assertRaises(ValueError):self.ws.read('too-big.txt',1,1)

    def test_single_oversized_line_is_explicitly_partial_not_a_false_complete_read(self):
        (self.root/'one.json').write_text(json.dumps({'value':'가'*25000},ensure_ascii=False),encoding='utf-8')
        view=bounded_result(self.ws.read('one.json',1,1))
        self.assertLessEqual(len(encoded(view)),20000);self.assertTrue(view['truncated']);self.assertTrue(view['partial_line'])
        self.assertEqual(view['next_start_line'],1);self.assertIn('read_structured',view['guidance'])

    def test_structured_yaml_json_key_paths_lists_and_missing_key(self):
        data={'conveyors':{'CV1_infeed':{'length':42,'lanes':[{'name':'A'}]}},'settings':{'enabled':True}}
        (self.root/'spec.json').write_text(json.dumps(data))
        (self.root/'spec.yaml').write_text('conveyors:\n  CV1_infeed:\n    length: 42\n    lanes:\n      - name: A\nsettings:\n  enabled: true\n')
        for path in ('spec.json','spec.yaml'):
            self.assertEqual(self.ws.read_structured(path,'conveyors.CV1_infeed')['value'],data['conveyors']['CV1_infeed'])
            self.assertEqual(self.ws.read_structured(path,'conveyors.CV1_infeed.lanes[0].name')['value'],'A')
            missing=self.ws.read_structured(path,'conveyors.missing')
            self.assertFalse(missing['found']);self.assertEqual(missing['top_level_keys'],['conveyors','settings']);self.assertNotIn('value',missing)

    def test_yaml_safe_aliases_dates_and_unsafe_or_recursive_nodes(self):
        (self.root/'safe.yaml').write_text('base: &base\n  length: 42\ncopy:\n  <<: *base\ndate: 2026-11-01\n')
        self.assertEqual(self.ws.read_structured('safe.yaml','copy.length')['value'],42)
        self.assertEqual(self.ws.read_structured('safe.yaml','date')['value'],'2026-11-01')
        (self.root/'unsafe.yaml').write_text('!!python/object/apply:os.system ["untrusted-command"]')
        with patch('os.system') as execute:
            with self.assertRaises(ValueError):self.ws.read_structured('unsafe.yaml','')
            execute.assert_not_called()
        (self.root/'recursive.yaml').write_text('root: &loop [*loop]')
        with self.assertRaises(ValueError):self.ws.read_structured('recursive.yaml','root')

    def test_oversized_structured_value_has_a_bounded_preview_and_children(self):
        (self.root/'big.json').write_text(json.dumps({'payload':{'section':'x'*40000}}))
        raw=self.ws.read_structured('big.json','payload');view=bounded_result(raw)
        self.assertEqual(raw['value']['section'],'x'*40000);self.assertIsNone(view['value'])
        self.assertTrue(view['truncated']);self.assertEqual(view['child_keys'],['section']);self.assertLessEqual(len(encoded(view)),20000)

    def test_secrets_and_path_boundary_are_checked_before_partial_or_structured_reads(self):
        secret='synthetic-file-secret-204944'
        (self.root/'secret.json').write_text(json.dumps({'safe':'ok','private':secret}))
        with patch.dict(os.environ,{'MODEL_API_KEY':secret}):
            with self.assertRaises(ValueError):self.ws.read('secret.json',1,1)
            with self.assertRaises(ValueError):self.ws.read_structured('secret.json','safe')
        for path in ('../outside.json','.env','C:\\Windows\\file.json'):
            with self.assertRaises(ValueError):self.ws.read_structured(path,'')

    def test_legacy_clipped_json_and_list_observations_stay_valid(self):
        self.assertTrue(json.loads(tool_text('{"content":"unfinished'))['legacy_result_unavailable'])
        value=json.loads(observation_text([{'path':'source.txt','line':1,'text':'match'}]))
        self.assertIsInstance(value,list)


class GoalFileTests(GoalTests):
    def reading(self):
        goal=self.create(start=False)
        self.manager.add_items(goal,[{'key':'read','title':'Read section','acceptance':['The section exists.']}])
        item=self.manager.items(goal['id'])[0]
        task=self.store.save('task',{'id':'reading','goal_id':goal['id'],'generation':goal['generation'],'plan_item_id':item['id'],
            'workspace':str(self.folder),'provider':'meta','status':'running','events':[]})
        self.store.update('plan_item',item['id'],task_ids=[task['id']])
        (self.folder/'spec.txt').write_text(''.join(f'{i:04} '+('x'*95)+'\n' for i in range(1,701)))
        return goal,self.store.get('plan_item',item['id']),task

    def read(self,task,start=None,end=None):
        args={'path':'spec.txt'}
        if start is not None:args['start_line']=start
        if end is not None:args['end_line']=end
        view=self.manager.builders.execute(task['id'],'read_file',args)
        self.store.event(task['id'],'tool_result',name='read_file',result=view)
        return view

    def test_goal_tool_retains_original_and_returns_range_metadata(self):
        goal,item,task=self.reading();view=self.read(task,100,600)
        self.assertLessEqual(len(encoded(view)),20000);self.assertGreater(len(encoded(view)),8000)
        self.assertTrue(view['content'].startswith('0100'));self.assertTrue(view['truncated'])
        raw=self.store.get('goal_file_read',view['read_id'])
        self.assertEqual(len(raw['result']['content'].splitlines()),501)
        self.assertEqual(raw['shown']['end_line'],view['end_line'])

    def test_model_request_keeps_range_head_and_20000_budget_instead_of_last_8000(self):
        goal,item,task=self.reading();view=self.read(task,100,600)
        seen=[]
        def complete(provider,messages,*args,**kwargs):seen.append((messages,args[1]));return response('OK')
        self.providers.complete=complete;self.manager.builders.providers.local.task_id=task['id']
        messages=[{'role':'system','content':''},{'role':'assistant','content':'','tool_calls':[{'id':'read-call','type':'function','function':{'name':'read_file','arguments':'{}'}}]},
                  {'role':'tool','tool_call_id':'read-call','content':encoded(view)}]
        self.manager.builders.providers.completion('meta',messages,TOOLS)
        tool_message=next(m for m in seen[0][0] if m['role']=='tool');content=json.loads(tool_message['content'])
        self.assertTrue(content['content'].startswith('0100'));self.assertGreater(len(tool_message['content']),8000)
        self.assertLessEqual(len(tool_message['content']),20000)
        functions={t['function']['name']:t['function'] for t in seen[0][1]}
        self.assertIn('start_line',functions['read_file']['parameters']['properties']);self.assertIn('read_structured',functions)
        self.assertNotIn('start_line',next(t['function'] for t in TOOLS if t['function']['name']=='read_file')['parameters']['properties'])

    def test_whole_file_and_meta_work_legacy_read_keep_their_respective_contracts(self):
        goal,item,task=self.reading();view=self.read(task)
        self.assertLessEqual(len(encoded(view)),8000);self.assertFalse(view['ranged'])
        base=Agents(self.store,self.providers,self.manager.gpu)
        raw=base.execute(task['id'],'read_file',{'path':'spec.txt'})
        self.assertEqual(set(raw),{'path','content','sha256'});self.assertGreater(len(raw['content']),20000)

    def test_planner_keeps_full_original_but_model_receives_a_valid_bounded_range(self):
        goal,item,task=self.reading()
        original=self.manager.planner_tool(goal,item,'read_file',{'path':'spec.txt','start_line':100,'end_line':600})
        self.assertGreater(len(original['result']['content']),20000)
        captured=[]
        def complete(provider,messages,*args,**kwargs):
            captured.append(messages)
            return response(calls=[('read_file',{'path':'spec.txt','start_line':100,'end_line':600})]) if len(captured)==1 else response('{}')
        self.providers.complete=complete;self.manager.phase(goal,item,'Inspect the bounded range.')
        tool_message=next(m for m in captured[1] if m['role']=='tool')
        view=json.loads(tool_message['content'])['result']
        self.assertEqual(view['start_line'],100);self.assertTrue(view['truncated'])
        self.assertGreater(len(tool_message['content']),8000);self.assertLessEqual(len(tool_message['content']),20000)

    def test_read_structured_becomes_file_evidence_and_original_is_retained(self):
        goal,item,task=self.reading();(self.folder/'spec.yaml').write_text('conveyors:\n  CV1_infeed:\n    length: 42\n')
        view=self.manager.builders.execute(task['id'],'read_structured',{'path':'spec.yaml','key_path':'conveyors.CV1_infeed'})
        self.assertEqual(view['value'],{'length':42})
        self.store.event(task['id'],'tool_result',name='read_structured',result=view)
        refs,_=self.manager.collect_evidence(item,self.store.get('task',task['id']))
        self.assertEqual(refs[-1]['kind'],'read_structured');self.assertEqual(refs[-1]['path'],'spec.yaml')
        self.assertIn('value',self.store.get('goal_file_read',view['read_id'])['result'])

    def test_read_ranges_merge_only_for_same_sha_and_legacy_clipping_is_unknown(self):
        goal,item,task=self.reading();self.read(task,1,5);self.read(task,4,9)
        context=self.manager.reading_context(self.store.get('plan_item',item['id']))
        self.assertEqual([(r['start_line'],r['end_line']) for r in context['already_read']],[(1,9)])
        ws=Workspace(self.folder,self.store.root);old=ws.read('spec.txt');ws.write('spec.txt',old['content']+'changed\n',old['sha256'])
        self.read(task,1,5)
        context=self.manager.reading_context(self.store.get('plan_item',item['id']))
        self.assertEqual(len({r['sha256'] for r in context['already_read']}),2)
        self.store.event(task['id'],'tool_result',name='read_file',result={'path':'legacy.txt','sha256':'legacy','content':'x'*9000})
        context=self.manager.reading_context(self.store.get('plan_item',item['id']))
        legacy=next(r for r in context['already_read'] if r['path']=='legacy.txt')
        self.assertTrue(legacy['legacy_range_unknown']);self.assertIsNone(legacy['end_line'])

    def test_builder_prompt_includes_range_rule_and_prefix_preserves_valid_json_views(self):
        goal,item,task=self.reading();view=self.read(task,100,600)
        self.manager.collect_evidence(item,self.store.get('task',task['id']))
        context=self.manager.context(goal,self.store.get('plan_item',item['id']),'Builder')
        self.assertIn('Do not re-read the same range in one chunk.',context[0]['content'])
        payload=json.loads(context[3]['content'].split('\n',1)[1]);self.assertTrue(payload['reading_context']['already_read'])
        observations=json.loads(context[-1]['content'].split('\n',1)[1]);event=json.loads(observations[-1]['observed'])
        self.assertEqual(event['result']['start_line'],100);self.assertGreater(len(observations[-1]['observed']),8000)

    def test_next_real_fake_provider_chunk_receives_summary_and_prior_reads_without_summary_llm(self):
        (self.folder/'spec.yaml').write_text('conveyors:\n  CV1_infeed:\n    length: 42\n')
        original=self.providers.complete;meta=[]
        def complete(provider,messages,model,*args,**kwargs):
            if provider=='baseten' and 'initial plan' in messages[-1]['content']:
                return response(json.dumps({'items':[{'key':'read','title':'Read structured spec','acceptance':['The conveyor section exists.']}],'summary':'Read the requested section.'}))
            if provider=='meta':
                meta.append(messages)
                if len(meta)==1:return response(text='Locating the section.',calls=[('read_file',{'path':'spec.yaml','start_line':1,'end_line':4})])
                if len(meta)==2:return response(text='The section is identified.',calls=[('read_structured',{'path':'spec.yaml','key_path':'conveyors.CV1_infeed'})])
                return response(json.dumps({'report_status':'completed','summary':'The requested conveyor section was read. See spec.yaml.'}))
            return original(provider,messages,model,*args,**kwargs)
        self.providers.complete=complete;goal=self.create(max_steps=2)
        self.assertEqual(self.terminal(goal['id'])['status'],'done')
        item=self.manager.items(goal['id'])[0];self.assertEqual(item['bundles'],2)
        carry=json.loads(next(m['content'] for m in meta[2] if m['content'].startswith('CURRENT ITEM AND RECENT MESSAGES')).split('\n',1)[1])['reading_context']
        self.assertIsNotNone(carry['previous_chunk_summary']);self.assertTrue(carry['already_read'])
        self.assertEqual(carry['previous_chunk_summary']['summary'],'The section is identified.')
        self.assertEqual(len(self.store.items('goal_chunk_summary',None)),2)
        self.assertEqual(len(self.manager.ledger.rows(goal['id'])),5)


for name in dir(GoalTests):
    if name.startswith('test_') and name not in GoalFileTests.__dict__:setattr(GoalFileTests,name,None)
del GoalTests,name
if __name__=='__main__':unittest.main()
