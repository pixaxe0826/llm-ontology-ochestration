"""Offline human display contract; no real model, command, browser notification or GPU calls."""
import copy,json,unittest
from unittest.mock import patch
import test_goal_decisions as decisions
from test_goals import response
from workbench.human_briefs import validate_brief,HumanBriefError,make_brief,classify,defect_groups
from workbench.goal_prompts import system,brief
from workbench.dependency_contract import dependency_instruction
from workbench.notifications import Notifications
from workbench.security import register_secret,contains_key


class HumanBriefTests(unittest.TestCase):
    setUp=decisions.GoalDecisionTests.setUp
    tearDown=decisions.GoalDecisionTests.tearDown
    items=decisions.GoalDecisionTests.items
    def arguments(self,item):
        return {'question':'Choose the implementation scope.','options':['retry','defer'],'recommendation':'retry','reason':'Retry the bounded item.',
            'affected_items':[item['id']],'reversible':True}
    def model_brief(self,args,language='Korean'):
        return {'language':language,'summary':'작업 범위를 정해 주세요.' if language=='Korean' else 'Choose the work scope.',
            'what_happened':['범위를 확인했습니다.','실행 전 방향을 정해야 합니다.'] if language=='Korean' else ['The scope was inspected.','Choose a direction before execution.'],
            'cause':'scope','answer_helps':{'value':'yes','reason':'답변을 다음 계획에 반영합니다.' if language=='Korean' else 'The answer informs planning.'},
            'options':[{'value':o,'label':('다시 시도' if o=='retry' else '나중에') if language=='Korean' else o,'effect':'다음 계획에 반영합니다.' if language=='Korean' else 'Record this for the next plan.'} for o in args['options']],
            'recommendation':{'value':args['recommendation'],'reason':'작은 범위부터 확인합니다.' if language=='Korean' else 'Check a bounded scope first.'}}

    def test_schema_rejects_missing_unknown_bad_language_and_option_drift(self):
        item=self.items('one')[0];args=self.arguments(item);valid=self.model_brief(args)
        self.assertEqual(validate_brief(valid,self.goal,args),valid)
        bads=[None,{**valid,'extra':True},{**valid,'what_happened':['one']},{**valid,'language':'French'},
            {**valid,'options':[{'value':'wrong','label':'선택','effect':'이유'}]},{**valid,'cause':'made_up'},
            {**valid,'summary':'raw observation:0123456789abcdef'}]
        for bad in bads:
            with self.subTest(bad=bad),self.assertRaises((ValueError,HumanBriefError)):validate_brief(bad,self.goal,args)

    def test_valid_same_planner_tool_call_needs_no_correction_or_extra_model(self):
        item=self.items('one')[0];args=self.arguments(item);args['human_brief']=self.model_brief(args)
        outputs=[response(json.dumps({'tool_requests':[{'name':'request_decision','arguments':args}]})),response('{"summary":"Recorded."}')]
        with patch.object(self.manager.muse_routes,'target',return_value={'tools_supported':False}),patch.object(self.manager,'call',side_effect=outputs) as call:
            self.manager.phase(self.goal,item,'Plan a checkpoint.')
        self.assertEqual(call.call_count,2) # Existing tool request/result round trip, no separate summarizer.
        decision=self.manager.decisions(self.goal['id'])[0]
        self.assertEqual(decision['human_brief']['source'],'model');self.assertEqual(decision['question'],args['question'])

    def test_invalid_brief_gets_one_correction_then_template_not_another_retry(self):
        item=self.items('one')[0];args=self.arguments(item)
        tool=response(json.dumps({'tool_requests':[{'name':'request_decision','arguments':args}]}))
        with patch.object(self.manager.muse_routes,'target',return_value={'tools_supported':False}),patch.object(self.manager,'call',side_effect=[tool,tool,response('{"summary":"Recorded."}')]) as call:
            self.manager.phase(self.goal,item,'Plan a checkpoint.')
        self.assertEqual(call.call_count,3)
        decision=self.manager.decisions(self.goal['id'])[0]
        self.assertEqual(decision['human_brief']['source'],'server_fallback')
        self.assertEqual(len(self.manager.decisions(self.goal['id'])),1)
        self.assertFalse(self.manager.review_scope.validate_human_brief)

    def test_builder_question_uses_planner_brief_or_one_correction_then_template(self):
        for valid in (True,False):
            item=self.items('question-'+str(valid))[-1];args=self.arguments(item)
            payload={'answer':'The human must choose.','needs_human':True,**{k:v for k,v in args.items() if k!='question'}}
            if valid:payload['human_brief']=self.model_brief(args)
            with patch.object(self.manager,'phase',return_value=json.dumps(payload)) as phase:
                result=self.manager.question(self.goal,item['id'],args['question'])
            self.assertEqual(phase.call_count,1 if valid else 2)
            d=self.store.get('decision',result['decision_id']);self.assertEqual(d['question'],args['question'])
            self.assertEqual(d['human_brief']['source'],'model' if valid else 'server_fallback')

    def test_brief_correction_does_not_exhaust_the_existing_evidence_tool_budget(self):
        item=self.items('bounded')[0];args=self.arguments(item)
        request=lambda name,data:response(json.dumps({'tool_requests':[{'name':name,'arguments':data}]}))
        bad=request('request_decision',args)
        outputs=[request('memory_search',{'query':'test'}),bad,bad,response('{"summary":"Recorded."}')]
        with patch.object(self.manager.muse_routes,'target',return_value={'tools_supported':False}),patch.object(self.manager,'call',side_effect=outputs) as call:
            self.assertIn('Recorded',self.manager.phase(self.goal,item,'Plan a checkpoint.'))
        self.assertEqual(call.call_count,4);self.assertEqual(len(self.manager.decisions(self.goal['id'])),1)

    def test_automatic_approval_brief_explains_requested_action_and_links_escalate(self):
        item=self.items('command')[0];args=self.arguments(item)
        request=self.store.save('goal_approval',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'kind':'command','state':'pending','payload':{'argv':['python','tools/check.py']}})
        d=self.manager.create_decision(self.goal,args,item_id=item['id'],approval_id=request['id'])
        human=d['human_brief'];self.assertIn('로컬 명령 승인',human['summary']);self.assertIn('python tools/check.py',human['what_happened'][0]);self.assertEqual(human['answer_helps']['value'],'yes')
        message=self.manager.message(self.goal,'Builder','Human','ESCALATE','Approval required for command.',item['id'],[request['id']])
        self.assertEqual(message['human_brief'],human)
        self.store.update('goal_approval',request['id'],state='interrupted')
        self.assertEqual(make_brief(self.manager,self.goal,d,item['id'])['answer_helps']['value'],'uncertain')

    def test_platform_validation_forces_uncertain_and_groups_17_numeric_defects(self):
        item=self.items('Report')[0];args=self.arguments(item)
        args['question']=json.dumps({'kind':'review_error','defects':['Missing parenthesized numeric evidence on line '+str(n) for n in range(17)]})
        args['human_brief']=self.model_brief(args)
        original=copy.deepcopy(args)
        decision=self.manager.create_decision(self.goal,args,item_id=item['id'])
        human=decision['human_brief'];self.assertEqual(human['cause'],'platform');self.assertEqual(human['answer_helps']['value'],'uncertain')
        self.assertEqual(human['defect_groups'][0]['count'],17);self.assertIn('17건',' '.join(human['what_happened']))
        self.assertEqual(args,original);self.assertEqual(decision['question'],args['question'])
        self.assertNotIn('review_error',human['summary'])
        self.assertNotEqual(human['options'][0]['effect'],human['options'][1]['effect'])

    def test_template_categories_and_action_effects(self):
        for question,cause in [('Builder four-chunk limit reached.','work'),('Which requirements?','scope'),('APIConnectionError from provider','external'),('Coordinator AttributeError','platform')]:
            self.assertEqual(classify(question),cause)
        item=self.items('one')[0];args=self.arguments(item);human=make_brief(self.manager,self.goal,args,item['id'])
        self.assertNotEqual(human['options'][0]['effect'],human['options'][1]['effect'])

    def test_impact_includes_downstream_and_item_cost_does_not_include_other_items(self):
        a,b,c=self.items('a','b','c');self.store.update('plan_item',b['id'],depends_on=[a['id']])
        self.store.update('plan_item',a['id'],bundles=3,attempts=2);self.store.update('plan_item',c['id'],status='in_progress')
        for item,cost in [(a,.12),(c,1.7)]:
            self.store.save('ledger',{'goal_id':self.goal['id'],'plan_item_id':item['id'],'state':'settled','provider':'meta','purpose':'development','estimated_usd':cost,'created':0})
        human=make_brief(self.manager,self.goal,self.arguments(a),a['id'])
        self.assertEqual(human['impact']['blocked_count'],2);self.assertTrue(human['impact']['continuing']);self.assertAlmostEqual(human['cost_time']['estimated_usd'],.12)
        self.assertEqual(human['cost_time']['bundles'],3)

    def test_existing_pending_backfill_is_additive_and_idempotent(self):
        item=self.items('one')[0];decision=self.manager.create_decision(self.goal,self.arguments(item),item_id=item['id'])
        original={k:v for k,v in decision.items() if k!='human_brief'}
        self.store.save('decision',original,preserve_updated=True)
        before_item=self.store.get('plan_item',item['id']);before_calls=len(self.providers.calls)
        self.assertEqual(self.manager.backfill_human_briefs(),1);self.assertEqual(self.manager.backfill_human_briefs(),0)
        after=self.store.get('decision',decision['id']);self.assertEqual({k:v for k,v in after.items() if k!='human_brief'},original)
        self.assertEqual(self.store.get('plan_item',item['id']),before_item);self.assertEqual(len(self.providers.calls),before_calls)

    def test_display_language_setting_is_separate_and_agent_context_excludes_brief(self):
        item=self.items('one')[0];decision=self.manager.create_decision(self.goal,self.arguments(item),item_id=item['id'])
        self.store.update('goal',self.goal['id'],status='paused');original_brief=brief(self.goal)
        result=self.manager.update(self.goal['id'],{'human_display_language':'English'});goal=result['goal']
        self.assertEqual(goal['output_language'],'English');self.assertEqual(brief(goal),original_brief)
        self.assertIn('human_display_language=English',system(goal,'Planner'))
        self.assertIn('human_display_language=English',system(goal,'Builder'))
        self.assertEqual(result['decisions'][0]['human_brief']['language'],'English')
        self.assertNotIn('human_brief',dependency_instruction([item],result['decisions']))
        for role in ('Planner','Builder'):
            decisions_message=next(m['content'] for m in self.manager.context(goal,item,role) if m['content'].startswith('HUMAN DECISIONS'))
            self.assertNotIn('human_brief',decisions_message);self.assertNotIn('human_summary',decisions_message)
        with self.assertRaises(ValueError):self.manager.update(goal['id'],{'human_display_language':'unknown'})

    def test_message_body_is_preserved_and_note_has_human_paragraph(self):
        item=self.items('one')[0];body='{"kind":"review_error","defects":["Unique plan keys required"]}'
        msg=self.manager.message(self.goal,'System','Human','ESCALATE',body,item['id'])
        self.assertEqual(msg['body'],body);self.assertEqual(msg['human_brief']['cause'],'platform')
        note=self.manager.message(self.goal,'Planner','Human','NOTE','Done / In progress / Next')
        self.assertEqual(note['body'],'Done / In progress / Next');self.assertIn('완료',note['human_summary'])
        agent=self.manager.message(self.goal,'Planner','Builder','NOTE',body)
        self.assertNotIn('human_brief',agent);self.assertNotIn('human_summary',agent)
        legacy={k:v for k,v in msg.items() if k!='human_brief'}
        self.assertEqual(self.manager.human_message_view(self.goal,legacy)['body'],body)

    def test_notification_uses_summary_language_and_never_raw_json_or_registered_key(self):
        item=self.items('one')[0];args=self.arguments(item);args['question']='{"kind":"review_error","error":"Unique plan keys required"}'
        decision=self.manager.create_decision(self.goal,args,item_id=item['id'])
        notice=Notifications(self.store,self.manager.ledger,native=object())
        row=notice.emit('question','human-brief-test',goal_id=self.goal['id'],source={'kind':'decision','id':decision['id']})
        self.assertIn('검증',row['summary']);self.assertNotIn('review_error',row['summary']);self.assertNotIn(decision['id'],row['summary'])
        self.store.update('goal',self.goal['id'],human_display_language='English');self.manager.backfill_human_briefs(self.goal['id'],language_changed=True)
        shown=notice.public(row);self.assertEqual(shown['event_label'],'Decision requested');self.assertIn('validation',shown['summary'].lower())
        secret='human-brief-secret-'+('Z'*40);register_secret(secret)
        with self.assertRaises(ValueError):self.manager.create_decision(self.goal,{**args,'question':secret},item_id=item['id'])
        self.assertFalse(contains_key(json.dumps(shown)));self.assertEqual(notice.emit('question','human-brief-test',goal_id=self.goal['id'])['id'],row['id'])

if __name__=='__main__':unittest.main()
