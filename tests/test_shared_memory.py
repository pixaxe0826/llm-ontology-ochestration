import json
from pathlib import Path
import tempfile
import time
import unittest

from workbench.agent import Agents
from workbench.gpu import GPUManager
from workbench.memory import Memory, uid
from workbench.store import Store


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.temp.name)/'state')
        self.memory=Memory(self.store)
    def tearDown(self):
        self.memory.pending.join()
        self.store.on_save=None
        self.store.db.close()
        self.temp.cleanup()
    def task(self,provider='meta',status='succeeded',text='아폴로 학습 프로젝트의 batch size는 32입니다.',shared=True,source_id='meta-test'):
        return {'id':source_id,'status':status,'provider':provider,'title':'아폴로 학습 설정','workspace':'','share_memory':shared,'events':[{'kind':'user','text':'아폴로 학습 설정을 기억해 줘.'},{'kind':'assistant','text':text}]}
    def test_meta_and_modal_sources_retrieved_by_baseten_context(self):
        self.memory.sync('task',self.task())
        folder=self.store.root/'gpu-jobs'/'modal-test'/'artifacts'
        folder.mkdir(parents=True)
        (folder/'run.log').write_text('Measured prefix - Tesla T4',encoding='utf-8')
        self.memory.sync('gpu',{'id':'modal-test','status':'succeeded','workspace':'','spec':{'entry':'apollo_train.py','program':'pytorch','gpu':'L4'},'result':{'exit_code':0,'summary':{'loss':0.12}},'artifacts':['metrics.json']})
        sources=self.memory.context('아폴로 학습 L4 결과와 batch size', 'new-baseten-chat')
        ids={s['chat_id'] for s in sources}
        self.assertIn(uid('task','meta-test'),ids)
        self.assertIn(uid('gpu','modal-test'),ids)
        observed=next(s for s in sources if s['chat_id']==uid('gpu','modal-test'))
        self.assertIn('작업 ID: modal-test',observed['excerpt'])
        self.assertIn('Measured prefix - Tesla T4',observed['excerpt'])
        self.assertTrue(all(s['message_id'] for s in sources))
        graph=self.memory.graph()
        self.assertEqual(graph['services']['meta'],1)
        self.assertEqual(graph['services']['modal'],1)
        self.assertTrue(graph['mentions'])
    def test_unshared_and_failed_answers_excluded(self):
        self.memory.sync('task',self.task(text='unfindable-failed-answer',status='failed'))
        self.assertEqual(self.memory.search('unfindable-failed-answer'),[])
        self.memory.sync('task',self.task(text='private-answer-identity',shared=False,source_id='private'))
        self.assertEqual(self.memory.search('private-answer-identity'),[])
    def test_aliases_and_source_citations(self):
        self.memory.definitions([{'label':'아폴로','aliases':['Apollo']}])
        self.memory.sync('task',self.task())
        sources=self.memory.context('Apollo batch', 'new-chat')
        self.assertTrue(sources)
        node=next(n for n in self.memory.graph()['nodes'] if n['label']=='아폴로')
        self.assertEqual(node['aliases'],['Apollo'])
    def test_share_toggle_updates_search(self):
        record=self.task();self.store.save('task',record);self.memory.sync('task',record)
        self.memory.share(uid('task',record['id']),False)
        self.assertEqual(self.memory.search('아폴로 학습'),[])
        self.memory.share(uid('task',record['id']),True)
        self.assertTrue(self.memory.search('아폴로 학습'))
    def test_baseten_chat_independent_without_workspace(self):
        class Provider:
            def settings(self):
                return {'baseten':{'ready':True,'model':'meta-models/muse-spark-open-1026'}}
            def stream_chat(self,provider,messages,model):
                assert provider=='baseten'
                assert model=='meta-models/muse-spark-open-1026'
                yield {'delta':'독립 ','finish_reason':None}
                yield {'delta':'대화 성공','finish_reason':'stop'}
        self.store.on_save=self.memory.enqueue
        agent=Agents(self.store,Provider(),GPUManager(self.store),self.memory)
        task=agent.start('', 'Baseten 독립 대화',provider='baseten',mode='chat')
        end=time.monotonic()+4
        while time.monotonic()<end:
            result=self.store.get('task',task['id'])
            if result['status']=='succeeded':break
            time.sleep(.02)
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['messages'][-1]['content'],'독립 대화 성공')
        self.memory.pending.join()
        self.assertEqual(self.memory.graph()['services']['baseten'],1)
    def test_reference_markdown_vault_import_preserves_sources(self):
        incoming=Path(self.temp.name)/'old-vault'
        from workbench.knowledge import KnowledgeVault
        from uuid import uuid4
        vault=KnowledgeVault(incoming)
        chat={'id':str(uuid4()),'title':'기존 Baseten 대화','revision':0,'shared':True,'concepts':['Blender'],'messages':[{'id':str(uuid4()),'role':'user','content':'렌더링 설정은 24fps입니다.','status':'complete'}]}
        vault.save(chat)
        imported=self.memory.import_folder(incoming)
        self.assertEqual(imported['imported'],1)
        self.assertTrue(self.memory.search('렌더링 설정'))
        repeated=self.memory.import_folder(incoming)
        self.assertEqual(repeated['skipped'],1)


if __name__=='__main__':
    unittest.main()
