"""Review success vs failure observations; no cloud calls or GPU execution."""
import hashlib,json,sys
from pathlib import Path
import unittest
from unittest.mock import patch
import test_planner_reviews as fixtures
from test_goals import response
from workbench.workspace import Workspace


class ReviewObservationTests(unittest.TestCase):
    setUp=fixtures.PlannerReviewTests.setUp
    tearDown=fixtures.PlannerReviewTests.tearDown
    create=fixtures.PlannerReviewTests.create
    reviewed=fixtures.PlannerReviewTests.reviewed
    verdict=fixtures.PlannerReviewTests.verdict

    def item(self,criterion='Simulation run completed and artifacts retrieved.'):
        goal,item=self.reviewed()
        item=self.store.update('plan_item',item['id'],acceptance=[criterion])
        return goal,item

    def task(self,goal,item,events):
        task=self.store.save('task',{'id':'exec','goal_id':goal['id'],'plan_item_id':item['id'],'workspace':str(self.folder),'status':'succeeded','events':events})
        self.store.update('plan_item',item['id'],task_ids=[task['id']])
        self.manager.collect_evidence(self.store.get('plan_item',item['id']),task)
        return task,self.store.get('plan_item',item['id'])

    def test_rework_allows_unmet_criteria_empty_ids_and_partial_file_evidence(self):
        goal,item=self.item()
        for refs in ([],[item['evidence'][0]['id']]):
            verdict=self.verdict(item,decision='rework',defects=['No simulation command or metrics/video outputs.'],criteria=[{'criterion':item['acceptance'][0],'evidence_ids':refs}])
            accepted,_,_=self.manager.validate_review(goal,item,verdict)
            self.assertEqual(accepted,{})
        with self.assertRaises(ValueError):self.manager.validate_review(goal,item,self.verdict(item))

    def test_failed_command_and_timeout_are_observations_never_acceptance(self):
        goal,item=self.item()
        results=[{'exit_code':1,'interrupted':False,'timed_out':False,'output':'UnicodeDecodeError: cp949'},
                 {'exit_code':-9,'interrupted':True,'timed_out':True,'timeout_s':120,'output':'partial log'}]
        _,item=self.task(goal,item,[{'kind':'tool_result','name':'run_command','result':r} for r in results])
        observed=self.manager.observed_evidence(goal,item);valid=self.manager.valid_evidence(goal,item)
        for n in range(2):self.assertIn('exec:'+str(n),observed);self.assertNotIn('exec:'+str(n),valid)
        self.assertFalse(observed['exec:0']['execution']['timeout']);self.assertTrue(observed['exec:1']['execution']['timeout'])
        report=self.manager.observed_report({},list(observed.values()),{'summary':'failed','status':'progress'})
        self.assertTrue(all(r['result'].startswith('FAIL') for r in report['test_results']))
        verdict=self.verdict(item,decision='rework',defects=['Fix the observed decoding failure.'],criteria=[{'criterion':item['acceptance'][0],'evidence_ids':['exec:0']}])
        self.manager.validate_review(goal,item,verdict)
        verdict['decision']='accept';verdict['defects']=[]
        with self.assertRaises(ValueError):self.manager.validate_review(goal,item,verdict)

    def test_actual_local_timeout_metadata_is_preserved(self):
        goal,item=self.item();ws=Workspace(self.folder,self.store.root)
        outcome=ws.run([sys.executable,'-c','import time; print("started",flush=True); time.sleep(5)'],timeout=1)
        self.assertTrue(outcome['interrupted']);self.assertTrue(outcome['timed_out']);self.assertFalse(outcome['cancelled'])
        _,item=self.task(goal,item,[{'kind':'tool_result','name':'run_command','result':outcome}])
        observed=self.manager.observed_evidence(goal,item)['exec:0']
        self.assertTrue(observed['execution']['timeout']);self.assertEqual(observed['execution']['timeout_s'],1)

    def test_blender_gpu_result_and_local_artifact_hashes_are_collected(self):
        goal,item=self.item('Modal job renders Blender scene and retrieves PNG/GLB.')
        script=self.folder/'render.py';script.write_text('pass')
        data={'id':'gpu-fixture','task_id':'exec','workspace':str(self.folder),'status':'succeeded',
            'spec':{'program':'blender','entry':'render.py','gpu':'T4'},'call_id':'fc-offline',
            'result':{'exit_code':0,'summary':{'duration_s':3}},'artifacts':['frame.png','model.glb'],
            'manifest':[{'path':'render.py','sha256':hashlib.sha256(b'pass').hexdigest()}]}
        self.store.save('gpu',data)
        folder=self.store.root/'gpu-jobs/gpu-fixture/artifacts';folder.mkdir(parents=True)
        (folder/'frame.png').write_bytes(b'synthetic-image');(folder/'model.glb').write_bytes(b'glTF-synthetic')
        prior_read=Workspace(self.folder,self.store.root).read('artifact.txt')
        _,item=self.task(goal,item,[{'kind':'tool_result','name':'read_file','result':prior_read},{'kind':'gpu','job_id':'gpu-fixture'},{'kind':'tool_result','name':'submit_gpu_job','result':data}])
        valid=self.manager.valid_evidence(goal,item);gpu=next(e for e in valid.values() if e['kind']=='submit_gpu_job')
        self.assertEqual(gpu['gpu_job_id'],'gpu-fixture');self.assertEqual(gpu['execution']['status'],'succeeded')
        self.assertEqual(gpu['execution']['exit_code'],0)
        self.assertEqual({f['path'] for f in gpu['gpu_artifacts']},{'frame.png','model.glb'})
        self.assertEqual(gpu['gpu_artifacts'][0]['sha256'],hashlib.sha256(b'synthetic-image').hexdigest())
        (self.folder/'artifact.txt').write_text('Updated report after the GPU returned.')
        self.assertIn(gpu['id'],self.manager.valid_evidence(goal,item))
        script.write_text('changed input')
        self.assertNotIn(gpu['id'],self.manager.valid_evidence(goal,item));script.write_text('pass')
        self.manager.validate_review(goal,item,self.verdict(item,criteria=[{'criterion':item['acceptance'][0],'evidence_ids':[gpu['id']]}]))
        before=len(item['evidence']);self.manager.collect_evidence(item,self.store.get('task','exec'))
        self.assertEqual(len(self.store.get('plan_item',item['id'])['evidence']),before)
        (folder/'frame.png').write_bytes(b'changed')
        self.assertNotIn(gpu['id'],self.manager.valid_evidence(goal,self.store.get('plan_item',item['id'])))

    def test_unrelated_maintenance_gpu_cannot_be_claimed(self):
        goal,item=self.item();job={'id':'unrelated','task_id':None,'workspace':str(self.folder),'status':'succeeded','result':{'exit_code':0}}
        self.store.save('gpu',job)
        _,item=self.task(goal,item,[{'kind':'tool_result','name':'get_gpu_job','result':job}])
        self.assertNotIn('exec:0',self.manager.observed_evidence(goal,item))

    def test_accept_without_execution_gets_specific_feedback_then_rework_validates(self):
        goal,item=self.item();instructions=[]
        bad=self.verdict(item);good=self.verdict(item,decision='rework',criteria=[],defects=['Generate MJCF successfully; then run simulation and save metrics/video.'])
        answers=iter([bad,good])
        def complete(provider,messages,*args,**kwargs):
            instructions.append(messages[-1]['content']);return response(json.dumps(next(answers)))
        self.providers.complete=complete
        self.assertTrue(self.manager.review(goal,item,review_only=True));saved=self.store.get('plan_item',item['id'])
        self.assertEqual(saved['status'],'rework');self.assertEqual(saved['reworks'],1);self.assertEqual(saved['bundles'],1)
        self.assertIn('ACCEPT IS NOT AVAILABLE',instructions[0]);self.assertIn('VALID OBSERVED EVIDENCE IDS',instructions[0])
        self.assertIn('REVIEW EVIDENCE PACKET',instructions[0]);self.assertIn('accept is forbidden',instructions[1])
        self.assertEqual(len(self.store.items('goal_review_attempt',None)),2)
        self.assertEqual(self.store.items('gpu',None),[])

    def test_review_only_exhausted_item_stays_rework_without_new_human_decision(self):
        goal,item=self.item();item=self.store.update('plan_item',item['id'],bundles=4,review_trigger='batch_limit')
        self.providers.complete=lambda *a,**k:response(json.dumps(self.verdict(item,decision='rework',criteria=[],defects=['Fix failed generation and save missing outputs.'],batch_action='defer')))
        before=len(self.store.items('task',None));self.assertTrue(self.manager.review(goal,item,review_only=True))
        saved=self.store.get('plan_item',item['id']);self.assertEqual(saved['status'],'rework');self.assertEqual(saved['bundles'],4)
        self.assertEqual(len(self.store.items('task',None)),before);self.assertEqual(self.manager.pending_decisions(goal['id']),[])

    def test_review_only_denies_planner_human_decision_tool(self):
        goal,item=self.item()
        self.manager.review_scope.read_only=True
        with self.assertRaisesRegex(PermissionError,'Review-only'):
            self.manager.planner_tool(goal,item,'request_decision',{'question':'invented escalation'})
        self.assertEqual(self.manager.pending_decisions(goal['id']),[])

    def test_evidence_packet_and_recent_outputs_share_20k_budget(self):
        messages=[{'role':'user','content':'RECENT OBSERVED TOOL RESULTS\n'+'x'*22000},
                  {'role':'user','content':'Review this REPORT\nREVIEW EVIDENCE PACKET\n'+'y'*10000}]
        limited=self.manager.limit_planner_evidence(messages)
        self.assertLessEqual(len(limited[0]['content'])+10000,20000)


if __name__=='__main__':unittest.main()
