"""Offline Goal GPU path, result views, artifact copy, caps and failed-chunk accounting."""
import copy,hashlib,json,os,threading
from pathlib import Path
from unittest.mock import patch
import unittest
import test_planner_reviews as fixtures
from workbench.goals import COPY_GPU_TOOL,PLANNER_ALLOWED
from workbench.agent import TOOLS
from workbench.goal_gpu import copy_artifact,submission_status,check_submission
from workbench.builder_failures import recover_failed_chunk,failure_kind
from workbench.file_views import tool_text,observation_text


class GoalGPURecoveryTests(unittest.TestCase):
    setUp=fixtures.PlannerReviewTests.setUp
    tearDown=fixtures.PlannerReviewTests.tearDown
    create=fixtures.PlannerReviewTests.create
    reviewed=fixtures.PlannerReviewTests.reviewed

    def fixture(self):
        goal,item=self.reviewed()
        task=self.store.save('task',{'id':'gpu-owner','goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
            'decision_revision':0,'execution_cycle':item.get('execution_cycle',0),'workspace':str(self.folder),'allow_write':True,'status':'succeeded','events':[]})
        (self.folder/'render.py').write_text('print("offline render input")\n',encoding='utf-8')
        manifest=[{'path':'render.py','sha256':hashlib.sha256((self.folder/'render.py').read_bytes()).hexdigest()}]
        job=self.store.save('gpu',{'id':'gpu-result','task_id':task['id'],'workspace':str(self.folder),'status':'succeeded',
            'manifest':manifest,
            'spec':{'program':'blender','entry':'render.py','gpu':'T4'},'result':{'exit_code':0,'logs':'long render log\n'*3000,'summary':{'duration_s':2}},
            'artifacts':['frame.png','model.glb']})
        artifact=self.store.root/'gpu-jobs/gpu-result/artifacts';artifact.mkdir(parents=True)
        (artifact/'frame.png').write_bytes(b'\x89PNG\r\n\x1a\n'+b'x'*310000);(artifact/'model.glb').write_bytes(b'glTF'+b'x'*24)
        return goal,item,task,job

    def test_large_gpu_output_preserves_success_id_and_artifact_list(self):
        goal,item,task,job=self.fixture();original=copy.deepcopy(job)
        view=json.loads(tool_text(json.dumps(job)))
        self.assertEqual(view['id'],job['id']);self.assertEqual(view['status'],'succeeded');self.assertEqual(view['artifacts'],job['artifacts'])
        self.assertLess(len(json.dumps(view,ensure_ascii=False)),8000)
        self.assertIn('copy_gpu_artifact',view['artifact_guidance']);self.assertEqual(job,original)
        event={'kind':'tool_result','name':'get_gpu_job','result':job}
        shown=json.loads(observation_text(event));self.assertEqual(shown['result']['artifacts'],job['artifacts'])

    def test_goal_binary_copy_requires_no_gpu_submission_or_local_command(self):
        goal,item,task,job=self.fixture()
        with patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU call')),patch('workbench.workspace.Workspace.run',side_effect=AssertionError('No command')):
            result=copy_artifact(self.manager,task,{'job_id':job['id'],'path':'frame.png','destination':'review/spike.png'})
        self.assertEqual((self.folder/'review/spike.png').stat().st_size,310008)
        self.assertEqual(result['sha256'],hashlib.sha256((self.folder/'review/spike.png').read_bytes()).hexdigest())
        self.assertEqual(self.store.get('task',task['id'])['events'][-1]['kind'],'change')
        self.assertEqual(COPY_GPU_TOOL['function']['name'],'copy_gpu_artifact')
        self.assertNotIn('copy_gpu_artifact',PLANNER_ALLOWED);self.assertNotIn('copy_gpu_artifact',{t['function']['name'] for t in TOOLS})

    def test_copy_rejects_outside_secret_paths_wrong_goal_and_stale_overwrite(self):
        goal,item,task,job=self.fixture();args={'job_id':job['id'],'path':'frame.png','destination':'review/a.png'}
        for path in ('../outside.png','.env','C:/outside.png','.git/blob'):
            with self.subTest(path=path),self.assertRaises(ValueError):copy_artifact(self.manager,task,{**args,'destination':path})
        result=copy_artifact(self.manager,task,args)
        self.assertTrue(copy_artifact(self.manager,task,args)['verified_present'])
        (self.folder/'review/a.png').write_bytes(b'new local contents')
        with self.assertRaises(ValueError):copy_artifact(self.manager,task,args)
        changed=copy_artifact(self.manager,task,{**args,'expected_sha256':hashlib.sha256(b'new local contents').hexdigest()});self.assertEqual(changed['sha256'],result['sha256'])
        self.store.update('task',task['id'],goal_id='different-goal')
        with self.assertRaises(PermissionError):copy_artifact(self.manager,task,args)

    def test_copy_refuses_registered_key_bytes_and_failed_job(self):
        goal,item,task,job=self.fixture();source=self.store.root/'gpu-jobs/gpu-result/artifacts/frame.png'
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-artifact-credential'}):
            source.write_bytes(b'synthetic-artifact-credential')
            with self.assertRaises(ValueError):copy_artifact(self.manager,task,{'job_id':job['id'],'path':'frame.png','destination':'frame.png'})
        self.assertFalse((self.folder/'frame.png').exists())
        self.store.update('gpu',job['id'],status='failed')
        with self.assertRaises(ValueError):copy_artifact(self.manager,task,{'job_id':job['id'],'path':'frame.png','destination':'frame.png'})

    def test_current_cycle_cap_counts_all_states_and_human_new_cycle_gets_one_slot(self):
        goal,item,task,job=self.fixture();item=self.store.update('plan_item',item['id'],max_gpu_jobs_per_cycle=1)
        self.assertEqual(submission_status(self.manager,goal,item)['remaining'],0)
        with self.assertRaises(PermissionError):check_submission(self.manager,goal,item)
        self.store.update('gpu',job['id'],status='failed')
        with self.assertRaises(PermissionError):check_submission(self.manager,goal,item)
        item=self.store.update('plan_item',item['id'],execution_cycle=1)
        self.assertEqual(check_submission(self.manager,goal,item)['remaining'],1)

    def test_goal_submit_blender_python_uses_real_validation_autonomous_policy_and_one_cap(self):
        goal,item=self.reviewed();self.manager.save(goal,policy={**goal['policy'],'mode':'autonomous','gpus':['T4'],'gpu_per_job':1})
        item=self.store.update('plan_item',item['id'],max_gpu_jobs_per_cycle=1)
        (self.folder/'render.py').write_text('pass')
        task=self.store.save('task',{'id':'submit-owner','goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'decision_revision':0,
            'execution_cycle':0,'workspace':str(self.folder),'allow_write':True,'status':'running','events':[]})
        self.manager.builders.cancels[task['id']]=threading.Event()
        def submit(preview,task_id):
            return self.store.save('gpu',{'workspace':preview['workspace'],'spec':preview['spec'],'manifest':preview['files'],'task_id':task_id,
                'status':'succeeded','result':{'exit_code':0,'summary':{'duration_s':2}},'artifacts':[]})
        args={'program':'blender','entry':'render.py','files':['render.py'],'gpu':'T4','timeout':600,'args':[],'packages':[],'volume':''}
        with patch.object(self.manager.gpu,'submit',side_effect=submit) as cloud:
            result=self.manager.builders.execute(task['id'],'submit_gpu_job',args)
            self.assertEqual(result['status'],'succeeded');self.assertEqual(cloud.call_count,1)
            with self.assertRaises(PermissionError):self.manager.builders.execute(task['id'],'submit_gpu_job',args)
            self.assertEqual(cloud.call_count,1)
        self.assertEqual(len(self.store.items('goal_approval',None)),0);self.assertEqual(len(self.manager.ledger.rows(goal['id'])),1)

    def test_provider_failed_continuation_waives_only_one_chunk_and_preserves_history(self):
        goal,item,task,job=self.fixture();item=self.store.update('plan_item',item['id'],attempts=4,bundles=3,bundle_reported=3,reworks=2)
        events=[{'kind':'chunk_started','chunk_id':'first'},{'kind':'chunk_started','chunk_id':'last','attempt_charged':False},
            {'kind':'gpu','job_id':job['id']},{'kind':'error','diagnostic':{'id':'local-diag','exception_type':'APIConnectionError'}}]
        task=self.store.update('task',task['id'],status='failed',events=events)
        recovery=recover_failed_chunk(self.manager,goal,item,task);current=self.store.get('plan_item',item['id'])
        self.assertEqual((current['attempts'],current['bundles'],current['bundle_reported'],current['reworks']),(4,2,2,2))
        self.assertEqual(recover_failed_chunk(self.manager,goal,current,task)['id'],recovery['id'])
        self.assertEqual(self.store.get('task',task['id']),task);self.assertEqual(self.store.get('gpu',job['id']),job)
        self.assertEqual(failure_kind('APIConnectionError'),'provider');self.assertEqual(failure_kind('AttributeError'),'platform')

    def test_initial_provider_failure_without_actions_refunds_attempt_and_old_error_is_not_reused(self):
        goal,item,task,job=self.fixture();item=self.store.update('plan_item',item['id'],attempts=1,bundles=1,reworks=0)
        events=[{'kind':'chunk_started','chunk_id':'initial','attempt_charged':True},{'kind':'error','diagnostic':{'exception_type':'APIConnectionError'}}]
        task=self.store.update('task',task['id'],status='failed',events=events)
        recover_failed_chunk(self.manager,goal,item,task);current=self.store.get('plan_item',item['id'])
        self.assertEqual((current['attempts'],current['bundles']),(0,0))
        task=self.store.update('task',task['id'],events=[*events,{'kind':'chunk_started','chunk_id':'new'}])
        self.assertIsNone(recover_failed_chunk(self.manager,goal,current,task))


if __name__=='__main__':unittest.main()
