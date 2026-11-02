"""Local history persistence and deletion tests. No model or GPU calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workbench.knowledge import VaultError
from workbench.memory import uid
from workbench.server import Application


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch('workbench.server.refresh_keys'), patch('workbench.providers.refresh_keys'):
            self.app = Application(Path(self.temp.name) / 'state')

    def tearDown(self):
        self.app.memory.pending.join()
        self.app.store.on_save = None
        self.app.store.db.close()
        self.temp.cleanup()

    def record(self, kind='task', record_id='test-chat', status='succeeded'):
        if kind == 'gpu':
            data = {'id':record_id, 'status':status, 'workspace':'',
                    'spec':{'program':'python', 'gpu':'T4', 'entry':'history.py'}, 'logs':'offline only'}
        else:
            data = {'id':record_id, 'status':status, 'workspace':'', 'provider':'baseten', 'mode':'chat',
                    'title':'기록 동작 시험', 'messages':[{'role':'user','content':'고유기록지우기'}],
                    'events':[{'kind':'user','text':'고유기록지우기'}, {'kind':'assistant','text':'고유답변지우기'}]}
        result = self.app.store.save(kind, data)
        self.app.memory.pending.join()
        return result

    def manage(self, kind='task', record_id='test-chat', action='archive', **extra):
        return self.app.handle('POST', '/api/history', {'kind':kind,'id':record_id,'action':action,**extra}, {})

    def test_archive_restore_preserves_transcript_and_memory(self):
        original = self.record()
        self.manage()
        archived = self.app.store.get('task', original['id'])
        self.assertTrue(archived['archived'])
        self.assertEqual(archived['messages'], original['messages'])
        self.assertTrue(self.app.memory.search('고유답변지우기'))
        self.manage(action='restore')
        self.assertFalse(self.app.store.get('task', original['id'])['archived'])

    def test_delete_requires_confirmation_and_leaves_other_data(self):
        original = self.record()
        self.record('gpu', 'other-job')
        note = self.app.memory.add_note('별도 메모', '별도기억유지', [])
        result_file = self.app.store.root / 'gpu-jobs' / 'other-job' / 'artifacts' / 'result.txt'
        result_file.parent.mkdir(parents=True)
        result_file.write_text('keep')
        with self.assertRaises(ValueError):
            self.manage(action='delete')
        self.assertEqual(self.app.store.get('task', original['id'])['messages'], original['messages'])
        self.manage(action='delete', confirmed=True)
        with self.assertRaises(ValueError):
            self.app.store.get('task', original['id'])
        with self.assertRaises(VaultError):
            self.app.memory.source(uid('task', original['id']))
        self.assertEqual(self.app.memory.search('고유답변지우기'), [])
        # A queued stale completion must never resurrect the deleted memory.
        self.app.memory.sync('task', original)
        self.assertEqual(self.app.memory.search('고유답변지우기'), [])
        self.assertTrue(self.app.memory.search('별도기억유지'))
        self.assertEqual(result_file.read_text(), 'keep')
        self.assertEqual(self.app.store.get('gpu', 'other-job')['id'], 'other-job')

    def test_gpu_history_archive_delete_preserves_artifact_files(self):
        job = self.record('gpu', 'finished-job')
        artifact = self.app.store.root / 'gpu-jobs' / job['id'] / 'artifacts' / 'result.txt'
        artifact.parent.mkdir(parents=True)
        artifact.write_text('keep')
        self.manage('gpu', job['id'])
        self.assertTrue(self.app.store.get('gpu', job['id'])['archived'])
        self.manage('gpu', job['id'], 'delete', confirmed=True)
        with self.assertRaises(ValueError):
            self.app.store.get('gpu', job['id'])
        with self.assertRaises(VaultError):
            self.app.memory.source(uid('gpu', job['id']))
        self.assertEqual(artifact.read_text(), 'keep')

    def test_running_and_cancelling_workers_are_protected(self):
        for kind in ('task', 'gpu'):
            row = self.record(kind, kind+'-running', 'running')
            for action in ('archive', 'delete'):
                with self.assertRaises(ValueError):
                    self.manage(kind, row['id'], action, confirmed=True)
            self.app.store.update(kind, row['id'], status='cancelled')
            active = self.app.agents.active if kind == 'task' else self.app.gpu.active
            active.add(row['id'])
            with self.assertRaises(ValueError):
                self.manage(kind, row['id'], 'delete', confirmed=True)
            active.discard(row['id'])

    def test_linked_live_gpu_blocks_task_removal(self):
        self.record()
        job = self.record('gpu', 'linked-job', 'running')
        self.app.store.update('gpu', job['id'], task_id='test-chat')
        with self.assertRaises(ValueError):
            self.manage(action='delete', confirmed=True)

    def test_more_than_sixty_tasks_remain_listed(self):
        self.app.store.on_save = None
        for i in range(65):
            self.record(record_id=str(i), status='failed')
        rows = self.app.handle('GET', '/api/tasks', {}, {})
        self.assertEqual(len(rows), 65)
        self.assertTrue(all('messages' not in row for row in rows))

    def test_archived_conversation_cannot_start_model_request(self):
        self.record()
        self.manage()
        with patch.object(self.app.providers, 'settings', return_value={'baseten':{'ready':True}}), patch.object(self.app.providers, 'stream_chat') as call:
            with self.assertRaisesRegex(ValueError, '복원'):
                self.app.agents.start('', '추가 메시지', provider='baseten', mode='chat', task_id='test-chat')
            call.assert_not_called()

    def test_invalid_action_and_kind_are_rejected(self):
        self.record()
        for data in ({'kind':'settings','action':'delete','id':'providers','confirmed':True},
                     {'kind':'task','action':'unknown','id':'test-chat'}):
            with self.assertRaises(ValueError):
                self.app.handle('POST', '/api/history', data, {})

    def test_shutdown_preserves_completed_and_failed_history(self):
        import threading
        completed = self.record(record_id='completed')
        failed = self.record(record_id='failed', status='failed')
        for row in (completed, failed):
            self.app.agents.cancels[row['id']] = threading.Event()
        active = self.record(record_id='active', status='running')
        self.app.stop()
        self.assertEqual(self.app.store.get('task', completed['id']), completed)
        self.assertEqual(self.app.store.get('task', failed['id']), failed)
        self.assertEqual(self.app.store.get('task', active['id'])['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
