"""Stale GPU scene/script inputs invalidate recovery, including byte-identical destinations."""
import hashlib,unittest
from unittest.mock import patch
import test_goal_gpu_recovery as fixtures
from workbench.goal_gpu import copy_artifact,input_snapshot,require_recovery,GPUArtifactEvidenceError,check_submission

class GPUInputFreshnessTests(unittest.TestCase):
    setUp=fixtures.GoalGPURecoveryTests.setUp
    tearDown=fixtures.GoalGPURecoveryTests.tearDown
    create=fixtures.GoalGPURecoveryTests.create
    reviewed=fixtures.GoalGPURecoveryTests.reviewed
    fixture=fixtures.GoalGPURecoveryTests.fixture
    def recover(self):
        goal,item,task,job=self.fixture();args={'job_id':job['id'],'path':'frame.png','destination':'review/copy.png'}
        proof=copy_artifact(self.manager,task,args);self.manager.collect_evidence(item,self.store.get('task',task['id']))
        return goal,self.store.get('plan_item',item['id']),task,job,args,proof
    def test_input_script_changed_invalidates_copy_and_verified_present_even_identical_output(self):
        goal,item,task,job,args,proof=self.recover();self.assertFalse(input_snapshot(self.manager,task,job)['gpu_stale_inputs'])
        (self.folder/'render.py').write_text('print("new openings")\n',encoding='utf-8');same=copy_artifact(self.manager,task,args);self.assertTrue(same['verified_present']);self.assertEqual(same['gpu_inputs_status'],'stale')
        self.manager.collect_evidence(item,self.store.get('task',task['id']));observed=self.manager.observed_evidence(goal,self.store.get('plan_item',item['id']))
        refs=[r for r in observed.values() if r.get('source_job_id')==job['id']];self.assertTrue(refs);self.assertTrue(all(r['stale'] for r in refs));self.assertTrue(all('render.py' in r['stale_reason'] for r in refs))
    def test_scene_changed_or_deleted_is_stale_but_unrelated_document_is_not(self):
        goal,item,task,job,args,proof=self.recover();scene=self.folder/'scene.json';scene.write_text('{"openings":true}')
        job=self.store.update('gpu',job['id'],manifest=[*job['manifest'],{'path':'scene.json','sha256':hashlib.sha256(scene.read_bytes()).hexdigest()}])
        self.assertEqual(input_snapshot(self.manager,task,job)['gpu_inputs_status'],'current');(self.folder/'unrelated.md').write_text('new note');self.assertEqual(input_snapshot(self.manager,task,job)['gpu_inputs_status'],'current')
        scene.write_text('{"openings":false}');self.assertIn('scene.json',input_snapshot(self.manager,task,job)['gpu_stale_inputs'][0]);scene.unlink();self.assertEqual(input_snapshot(self.manager,task,job)['gpu_inputs_status'],'stale')
    def test_missing_manifest_is_not_invented_and_recorded_hashes_cannot_disagree(self):
        goal,item,task,job,args,proof=self.recover();self.assertEqual(proof['gpu_input_hashes'],{'render.py':job['manifest'][0]['sha256']})
        for change in ({'manifest':[]},{'input_hashes':{'render.py':'forged'}}):
            current={**job,**change};self.assertEqual(input_snapshot(self.manager,task,current)['gpu_inputs_status'],'stale')
    def test_legacy_copy_change_also_compares_submission_inputs(self):
        goal,item,task,job,args,proof=self.recover();events=self.store.get('task',task['id'])['events'];change=next(e for e in events if e['kind']=='change');legacy=dict(change['change'])
        for name in ('source_job_id','gpu_input_hashes','destination_sha256','copying_item_id','submitting_item_id','operation'):legacy.pop(name,None)
        self.store.update('task',task['id'],events=[{'kind':'change','change':legacy}]);self.store.update('plan_item',item['id'],evidence=[]);self.manager.collect_evidence(item,self.store.get('task',task['id']));item=self.store.get('plan_item',item['id']);self.assertTrue(any(r['supports_acceptance'] for r in self.manager.observed_evidence(goal,item).values()))
        (self.folder/'render.py').write_text('pass');self.assertTrue(all(r['stale'] for r in self.manager.observed_evidence(goal,item).values()))
    def test_recovery_requirement_requires_each_current_path_from_one_job(self):
        goal,item,task,job,args,proof=self.recover();item={**item,'gpu_artifact_requirement':{'paths':{'frame.png':'review/copy.png','model.glb':'web/model.glb'}}}
        valid=self.manager.valid_evidence(goal,item);criteria=[{'evidence_ids':list(valid)}]
        with self.assertRaises(GPUArtifactEvidenceError):require_recovery(self.manager,goal,item,valid,criteria)
        copy_artifact(self.manager,task,{'job_id':job['id'],'path':'model.glb','destination':'web/model.glb'});self.manager.collect_evidence(item,self.store.get('task',task['id']));current=self.store.get('plan_item',item['id']);valid=self.manager.valid_evidence(goal,current)
        require_recovery(self.manager,goal,item,valid,[{'evidence_ids':list(valid)}])
        with self.assertRaises(GPUArtifactEvidenceError):require_recovery(self.manager,goal,item,valid,[{'evidence_ids':[]}])
    def test_preferred_current_job_prevents_duplicate_render_but_stale_inputs_allow_bounded_one(self):
        goal,item,task,job,args,proof=self.recover();item={**item,'gpu_artifact_requirement':{'preferred_job_id':job['id'],'paths':{}},'execution_cycle':1,'max_gpu_jobs_per_cycle':1}
        with self.assertRaises(PermissionError):check_submission(self.manager,goal,item)
        (self.folder/'render.py').write_text('pass');self.assertEqual(check_submission(self.manager,goal,item)['remaining'],1)

    def test_stale_acceptance_cannot_be_refreshed_by_reading_job_text(self):
        goal,item,task,job,args,proof=self.recover()
        source=self.store.root/'gpu-jobs/gpu-result/artifacts/run.txt';source.write_text('successful render',encoding='utf-8');self.store.update('gpu',job['id'],artifacts=[*job['artifacts'],'run.txt'])
        self.manager.builders.execute(task['id'],'read_gpu_artifact',{'job_id':job['id'],'path':'run.txt'})
        # The normal observed executor records the read result; reproduce that observed record.
        from workbench.goal_gpu import read_artifact
        result=read_artifact(self.manager,task,{'job_id':job['id'],'path':'run.txt'});self.store.event(task['id'],'tool_result',name='read_gpu_artifact',result=result)
        self.manager.collect_evidence(item,self.store.get('task',task['id']));item=self.store.get('plan_item',item['id']);(self.folder/'render.py').write_text('print("different geometry")')
        refs=[r for r in self.manager.observed_evidence(goal,item).values() if r['kind']=='read_gpu_artifact'];self.assertTrue(refs);self.assertTrue(all(r['stale'] for r in refs))
    def test_review_converts_missing_artifact_recovery_into_real_rework_not_platform_error(self):
        from test_goals import response
        import json
        goal,item,task,job,args,proof=self.recover()
        item=self.store.update('plan_item',item['id'],gpu_artifact_requirement={'paths':{'not-copied.glb':'web/missing.glb'}})
        verdict={'decision':'accept','criteria':[{'criterion':item['acceptance'][0],'evidence_ids':[r['id'] for r in item['evidence']]}],'defects':[],'summary':'Claimed all outputs','next_items':[],'goal_complete':False}
        with patch.object(self.manager,'inbox_phase',return_value=(json.dumps(verdict),[])):self.assertTrue(self.manager.review(goal,item))
        current=self.store.get('plan_item',item['id']);self.assertEqual(current['status'],'rework');self.assertEqual(current.get('review_error_count'),0);self.assertIn('recovery proofs',current['defects'][0])
