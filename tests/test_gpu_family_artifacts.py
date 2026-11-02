"""Goal-family authorization, byte-presence proofs and current provenance."""
import copy,hashlib,json,os,unittest
from unittest.mock import patch
import test_goal_gpu_recovery as fixtures
from workbench.goal_gpu import copy_artifact,read_artifact
from workbench.review_evidence import packet


class GPUFamilyArtifactTests(unittest.TestCase):
    setUp=fixtures.GoalGPURecoveryTests.setUp
    tearDown=fixtures.GoalGPURecoveryTests.tearDown
    create=fixtures.GoalGPURecoveryTests.create
    reviewed=fixtures.GoalGPURecoveryTests.reviewed
    fixture=fixtures.GoalGPURecoveryTests.fixture

    def other(self,goal,**values):
        row={'goal_id':goal['id'],'key':'relative','parent':None,'title':'Related item','status':'todo','acceptance':['Recover artifacts.'],
             'role':'Builder','bundles':0,'attempts':0,'reworks':0,'evidence':[],'task_ids':[],'depends_on':[],'blocked_by_decision':[],
             'provisional_scope':'none','priority':100,'created':1,**values}
        return self.store.save('plan_item',row)

    def reader(self,goal,item):
        return self.store.save('task',{'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],'decision_revision':0,
            'workspace':str(self.folder),'allow_write':True,'status':'succeeded','events':[]})

    def args(self,job):return {'job_id':job['id'],'path':'frame.png','destination':'review/copy.png'}

    def test_same_item_parent_child_and_sibling_can_copy_and_read(self):
        goal,item,owner,job=self.fixture()
        source=self.store.root/'gpu-jobs/gpu-result/artifacts/run.txt';source.write_text('GPU details',encoding='utf-8')
        self.store.update('gpu',job['id'],artifacts=[*job['artifacts'],'run.txt'])
        for relationship in ('self','parent','child','sibling','root_sibling','legacy_split'):
            with self.subTest(relationship=relationship):
                self.store.update('plan_item',item['id'],parent=None,split_item_ids=[])
                target=item
                if relationship=='parent':target=self.other(goal);self.store.update('plan_item',item['id'],parent=target['id'])
                elif relationship=='child':target=self.other(goal,parent=item['id'])
                elif relationship=='sibling':
                    parent=self.other(goal);self.store.update('plan_item',item['id'],parent=parent['id']);target=self.other(goal,parent=parent['id'])
                elif relationship=='root_sibling':target=self.other(goal)
                elif relationship=='legacy_split':target=self.other(goal);self.store.update('plan_item',item['id'],split_item_ids=[target['id']])
                task=owner if relationship=='self' else self.reader(goal,target)
                result=copy_artifact(self.manager,task,self.args(job))
                self.assertEqual(result['source_job_id'],job['id']);self.assertEqual(result['submitting_item_id'],item['id']);self.assertEqual(result['copying_item_id'],target['id'])
                self.assertEqual(result['source_sha256'],result['destination_sha256'])
                read=self.manager.builders.execute(task['id'],'read_gpu_artifact',{'job_id':job['id'],'path':'run.txt'})
                self.assertEqual(read['content'],'GPU details');self.assertEqual(read['submitting_item_id'],item['id']);self.assertEqual(read['reading_item_id'],target['id'])

    def test_other_goal_and_unrelated_branch_cannot_copy_or_read(self):
        goal,item,owner,job=self.fixture();other_goal=self.store.save('goal',{**goal,'id':'foreign-goal'})
        other_item=self.other(other_goal);foreign=self.reader(other_goal,other_item)
        for task in (foreign,):
            with self.assertRaises(PermissionError):copy_artifact(self.manager,task,self.args(job))
            with self.assertRaises(PermissionError):self.manager.builders.execute(task['id'],'read_gpu_artifact',{'job_id':job['id'],'path':'model.glb'})
        parent=self.other(goal);target=self.other(goal,parent=parent['id']);task=self.reader(goal,target)
        with self.assertRaises(PermissionError):copy_artifact(self.manager,task,self.args(job))
        with self.assertRaises(PermissionError):read_artifact(self.manager,task,{'job_id':job['id'],'path':'model.glb'})

    def test_verified_present_changes_no_file_or_backups_but_creates_valid_proof(self):
        goal,item,owner,job=self.fixture();target=self.other(goal,parent=item['id']);task=self.reader(goal,target)
        source=self.store.root/'gpu-jobs/gpu-result/artifacts/frame.png';destination=self.folder/'review/copy.png';destination.parent.mkdir();destination.write_bytes(source.read_bytes())
        before=(destination.stat().st_mtime_ns,destination.read_bytes());original_job=copy.deepcopy(job)
        with (patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')),
              patch('workbench.workspace.Workspace.run',side_effect=AssertionError('No command')),
              patch('workbench.goal_gpu.shutil.copy2',side_effect=AssertionError('No backup or copy'))):
            proof=copy_artifact(self.manager,task,{**self.args(job),'expected_sha256':'stale-ignored-for-read-only-verification'})
        self.assertEqual(before,(destination.stat().st_mtime_ns,destination.read_bytes()));self.assertEqual(self.store.get('gpu',job['id']),original_job)
        self.assertTrue(proof['verified_present']);self.assertFalse(proof['copied']);self.assertFalse(proof['created'])
        events=self.store.get('task',task['id'])['events'];self.assertEqual([e['kind'] for e in events],['gpu_artifact_copy'])
        refs,_=self.manager.collect_evidence(target,self.store.get('task',task['id']));observed=self.manager.observed_evidence(goal,self.store.get('plan_item',target['id']))
        ref=next(r for r in refs if r['kind']=='copy_gpu_artifact');self.assertEqual(ref['source_job_id'],job['id']);self.assertEqual(ref['submitting_item_id'],item['id']);self.assertEqual(ref['copying_item_id'],target['id'])
        self.assertTrue(observed[ref['id']]['supports_acceptance'])
        self.assertEqual(packet(observed)['observed'][0]['artifact_recovery']['operation'],'verified_present')
        destination.write_bytes(b'edited destination');self.assertTrue(self.manager.observed_evidence(goal,self.store.get('plan_item',target['id']))[ref['id']]['stale'])

    def test_copy_proof_becomes_stale_when_source_changes(self):
        goal,item,task,job=self.fixture();copy_artifact(self.manager,task,self.args(job));self.manager.collect_evidence(item,self.store.get('task',task['id']))
        source=self.store.root/'gpu-jobs/gpu-result/artifacts/frame.png';source.write_bytes(b'new source bytes')
        ref=next(r for r in self.manager.observed_evidence(goal,self.store.get('plan_item',item['id'])).values() if r['kind']=='copy_gpu_artifact')
        self.assertTrue(ref['stale']);self.assertFalse(ref['supports_acceptance'])

    def test_conflicting_ownership_or_foreign_goal_is_not_hidden_by_shared_workspace(self):
        goal,item,task,job=self.fixture()
        for changes in ({'goal_id':'foreign'},{'plan_item_id':'foreign-item'}):
            self.store.update('gpu',job['id'],**changes)
            with self.assertRaises(PermissionError):copy_artifact(self.manager,task,self.args(job))
            self.store.save('gpu',job)

    def test_verified_proof_and_existing_hash_check_allow_review_without_new_execution(self):
        goal,item,owner,job=self.fixture();target=self.other(goal,parent=item['id']);task=self.reader(goal,target)
        source=self.store.root/'gpu-jobs/gpu-result/artifacts/frame.png';destination=self.folder/'review/copy.png';destination.parent.mkdir();destination.write_bytes(source.read_bytes())
        # Saved successful hash-check result, not a new command by the copy helper.
        self.store.event(task['id'],'tool_result',name='run_command',result={'exit_code':0,'output':'Saved hash check passed.',
            'command_files':{'inputs':{},'outputs':{},'method':'Offline saved-command fixture'}})
        with patch('workbench.workspace.Workspace.run',side_effect=AssertionError('No new execution')):
            proof=copy_artifact(self.manager,task,self.args(job))
            refs,_=self.manager.collect_evidence(target,self.store.get('task',task['id']))
            value={'decision':'accept','defects':[],'criteria':[{'criterion':target['acceptance'][0],'evidence_ids':[r['id'] for r in refs]}],
                'summary':'Current bytes match the job output; the existing check succeeded.','next_items':[],'goal_complete':False}
            accepted,_,_=self.manager.validate_review(goal,self.store.get('plan_item',target['id']),value)
        self.assertTrue(proof['verified_present']);self.assertIn(target['acceptance'][0],accepted)


if __name__=='__main__':unittest.main()
