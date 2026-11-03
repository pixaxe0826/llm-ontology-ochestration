"""Read-only hashes obey the same workspace/credential boundary as other tools."""
import hashlib,os,unittest
from unittest.mock import patch
import test_goal_gpu_recovery as F
from workbench.file_hashes import file_hashes
from workbench.workspace import Workspace
from workbench.goal_files import file_tools
from workbench.agent import TOOLS
from workbench.goals import PLANNER_ALLOWED


class FileHashTests(unittest.TestCase):
    setUp=F.GoalGPURecoveryTests.setUp;tearDown=F.GoalGPURecoveryTests.tearDown
    create=F.GoalGPURecoveryTests.create;reviewed=F.GoalGPURecoveryTests.reviewed;fixture=F.GoalGPURecoveryTests.fixture

    def test_goal_tool_hashes_binary_and_text_without_commands_or_mutations(self):
        goal,item,task,job=self.fixture();(self.folder/'binary.glb').write_bytes(b'glTF\x00\xff');(self.folder/'text.txt').write_text('UTF-8 한글',encoding='utf-8')
        before={p.name:(p.stat().st_mtime_ns,p.read_bytes()) for p in (self.folder/'binary.glb',self.folder/'text.txt')}
        with patch('workbench.workspace.Workspace.run',side_effect=AssertionError('No shell')),patch.object(self.manager.gpu,'submit',side_effect=AssertionError('No GPU')):
            result=self.manager.builders.execute(task['id'],'file_hash',{'paths':['binary.glb','text.txt']})
        for value in result['files']:
            stamp,data=before[value['path']];self.assertEqual(value['sha256'],hashlib.sha256(data).hexdigest());self.assertEqual(value['size'],len(data))
            self.assertEqual((self.folder/value['path']).stat().st_mtime_ns,stamp)
        self.assertEqual(self.store.get('task',task['id'])['events'],[])
        self.assertIn('file_hash',{t['function']['name'] for t in file_tools(TOOLS)});self.assertNotIn('file_hash',{t['function']['name'] for t in TOOLS});self.assertNotIn('file_hash',PLANNER_ALLOWED)

    def test_secret_outside_missing_directory_and_oversized_hashes_refused(self):
        ws=Workspace(self.folder,self.store.root)
        for paths in ([],['.env'],['../outside.txt'],[str(self.folder/'absolute.txt')],['.git/blob'],['missing.txt'],['.'],['x']*21,[None]):
            with self.subTest(paths=paths),self.assertRaises((ValueError,OSError)):file_hashes(ws,paths)
        (self.folder/'token.txt').write_text('synthetic-file-hash-secret',encoding='utf-8')
        with patch.dict(os.environ,{'MODEL_API_KEY':'synthetic-file-hash-secret'}),self.assertRaisesRegex(ValueError,'credential'):file_hashes(ws,['token.txt'])
        with patch('workbench.file_hashes.MAX_FILE',2),self.assertRaisesRegex(ValueError,'limit'):file_hashes(ws,['token.txt'])
        for name in ('a','b'):(self.folder/name).write_bytes(b'1234')
        with patch('workbench.file_hashes.MAX_TOTAL',7),self.assertRaisesRegex(ValueError,'limit'):file_hashes(ws,['a','b'])

    def test_copy_refusal_supplies_destination_hash_and_one_corrected_call_succeeds(self):
        goal,item,task,job=self.fixture();destination=self.folder/'copy.png';destination.write_bytes(b'old destination')
        current=hashlib.sha256(destination.read_bytes()).hexdigest();args={'job_id':job['id'],'path':'frame.png','destination':'copy.png'}
        for field in ({},{'expected_sha256':'stale'}):
            with self.assertRaises(ValueError) as raised:self.manager.builders.execute(task['id'],'copy_gpu_artifact',{**args,**field})
            self.assertIn('current_sha256='+current,str(raised.exception));self.assertIn('Retry once',str(raised.exception));self.assertNotIn(str(self.folder),str(raised.exception));self.assertEqual(destination.read_bytes(),b'old destination')
        result=self.manager.builders.execute(task['id'],'copy_gpu_artifact',{**args,'expected_sha256':current})
        self.assertTrue(result['copied']);self.assertEqual(result['destination_sha256'],hashlib.sha256(destination.read_bytes()).hexdigest())

    def test_file_hash_is_a_read_for_stall_document_and_provisional_guards(self):
        goal,item,task,job=self.fixture();(self.folder/'data.txt').write_text('safe',encoding='utf-8')
        self.manager.before_builder_tool(task,'file_hash','hash-chunk');self.assertEqual(self.manager.chunk_state(task,'hash-chunk')['consecutive_reads'],1)
        self.store.update('plan_item',item['id'],provisional=True,document_cycle_active=True)
        self.manager.builders.execute(task['id'],'file_hash',{'paths':['data.txt']})

if __name__=='__main__':unittest.main()
