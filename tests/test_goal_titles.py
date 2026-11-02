"""Display metadata cannot alter Goal work or model cache inputs. Offline only."""
import copy
import json
import unittest

from test_goals import GoalTests, GoalHTTPTests
from workbench.goals import GoalManager
from workbench.goal_titles import suggested_title, validate_title


class GoalTitleTests(GoalTests):
    def test_new_goal_title_is_required_and_limited_to_120(self):
        for title in (None,'','   ','x'*121,'line\nbreak',True):
            with self.subTest(title=title),self.assertRaises(ValueError):self.create(start=False,title=title)
        with self.assertRaises(ValueError):self.manager.create({'objective':'# Objective','workspace':str(self.folder)},start=False)
        goal=self.create(start=False,title='한'*120)
        self.assertEqual(goal['title'],'한'*120)
        self.assertEqual(validate_title('  My title  '),'My title')

    def test_heading_suggestion_removes_hashes_skips_fenced_code_and_limits_length(self):
        self.assertEqual(suggested_title('preamble\n\n### GOAL — Example ###\nbody'),'GOAL — Example')
        self.assertEqual(suggested_title('```python\n# Not a heading\n```\n## Real heading\ntext'),'Real heading')
        self.assertEqual(suggested_title('plain objective\nsecond line'),'plain objective')
        self.assertEqual(len(suggested_title('# '+'x'*150)),120)

    def test_legacy_backfill_changes_only_title_and_preserves_timestamp_and_cache(self):
        goal=self.create(start=False,objective='# GOAL — Existing\n\n## Keep\nRaw body\n')
        goal.pop('title');goal['status']='paused';self.store.save('goal',goal)
        before=self.store.get('goal',goal['id']);prompts={role:self.manager.context(before,None,role) for role in ('Planner','Builder')}
        self.manager=GoalManager(self.store,self.providers,self.manager.gpu,self.memory)
        after=self.store.get('goal',goal['id'])
        self.assertEqual(after['title'],'GOAL — Existing')
        self.assertEqual({k:v for k,v in after.items() if k!='title'},before)
        for role in prompts:self.assertEqual(self.manager.context(after,None,role),prompts[role])

    def test_title_only_edit_preserves_body_plan_messages_cost_state_and_both_prompts(self):
        objective='\n# GOAL — Body remains\n\n- exact Markdown\n\n'
        goal=self.create(start=False,title='Old',objective=objective)
        self.manager.add_items(goal,[{'key':'one','parent':None,'title':'Inspect','acceptance':['A file is inspected.']}])
        item=self.manager.items(goal['id'])[0]
        self.manager.message(goal,'Planner','Builder','NOTE','Unchanged message')
        self.manager.ledger.settle(self.manager.ledger.reserve(goal,'baseten','development',.01),actual_cost=.01)
        self.manager.save(goal,status='paused');before=self.manager.detail(goal['id'])
        prompts={role:self.manager.context(before['goal'],item,role) for role in ('Planner','Builder')}
        result=self.manager.rename(goal['id'],{'goal_id':goal['id'],'title':'New display title'})
        self.assertEqual(result['goal']['title'],'New display title');self.assertEqual(result['goal']['objective'],objective)
        for key in ('items','messages','ledger','costs','approvals'):self.assertEqual(result[key],before[key])
        for key in set(before['goal'])-{'title','updated'}:self.assertEqual(result['goal'][key],before['goal'][key])
        for role in prompts:self.assertEqual(self.manager.context(result['goal'],item,role),prompts[role])
        self.assertEqual(self.providers.calls,[])

    def test_rename_rejects_other_fields_and_invalid_titles_without_partial_mutation(self):
        goal=self.create(start=False)
        for data in ({'title':'new','objective':'changed'},{'title':'new','status':'running'},{'title':''},{'title':'x'*121}):
            with self.assertRaises(ValueError):self.manager.rename(goal['id'],data)
            self.assertEqual(self.store.get('goal',goal['id']),goal)

    def test_metadata_rename_is_safe_for_a_live_goal_without_starting_work(self):
        goal=self.create(start=False)
        self.manager.rename(goal['id'],{'title':'Renamed while planning'})
        after=self.store.get('goal',goal['id'])
        self.assertEqual(after['status'],'planning');self.assertEqual(after['generation'],goal['generation'])
        self.assertEqual(self.providers.calls,[])


class GoalTitleHTTPTests(GoalHTTPTests):
    def test_title_api_keeps_session_token_and_changes_only_title(self):
        from urllib.request import Request,urlopen
        from urllib.error import HTTPError
        url=self.url+'/api/goals/title';data=json.dumps({'goal_id':self.goal['id'],'title':'Updated title'}).encode()
        with self.assertRaises(HTTPError) as caught:urlopen(Request(url,data=data,headers={'Content-Type':'application/json'}))
        self.assertEqual(caught.exception.code,403);caught.exception.close()
        before=self.app.store.get('goal',self.goal['id'])
        with urlopen(Request(url,data=data,headers={'Content-Type':'application/json','X-Local-Token':self.app.token})) as result:after=json.load(result)['goal']
        self.assertEqual(after['title'],'Updated title')
        for key in set(before)-{'title','updated'}:self.assertEqual(after[key],before[key])


for target,base in ((GoalTitleTests,GoalTests),(GoalTitleHTTPTests,GoalHTTPTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in target.__dict__:setattr(target,name,None)
del target,base,name,GoalTests,GoalHTTPTests

if __name__=='__main__':unittest.main()
