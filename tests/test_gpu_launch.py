"""Offline reproduction of incompatible Blender ENTRYPOINT and launch cancellation."""
import hashlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch

from workbench.gpu import GPUManager,normalize_spec
from workbench.gpu_launch import image_lines,classify_failure,startup_failure
from workbench.remote_runner import blender_command
from workbench.store import Store


class GPULaunchTests(unittest.TestCase):
    def test_custom_image_adds_python_then_clears_application_entrypoint(self):
        image=Mock();image.entrypoint.return_value=image;image.run_commands.return_value=image
        modal=SimpleNamespace(Image=SimpleNamespace(from_registry=Mock(return_value=image)))
        source='\n'.join(image_lines({'program':'custom','image':'blendergrid/blender:4.2','packages':[]}))
        exec(source,{'modal':modal})
        modal.Image.from_registry.assert_called_once_with('blendergrid/blender:4.2',add_python='3.12')
        image.entrypoint.assert_called_once_with([])
        self.assertIn('import sys',image.run_commands.call_args.args[0])

    def test_blender_script_and_legacy_blend(self):
        base={'program':'blender','gpu':'T4','files':['tools/my scene.py'],'entry':'tools/my scene.py','args':['output name']}
        spec=normalize_spec(base);command=blender_command(spec,Path('/output'))
        self.assertEqual(command,['/opt/blender/blender','-b','--python-exit-code','1','--python','tools/my scene.py','--','output name'])
        old=blender_command({**spec,'entry':'scene.blend','frame':2},Path('/output'))
        self.assertEqual(old[2],'scene.blend');self.assertEqual(old[-2:],['--cycles-device','CUDA']);self.assertIn('2',old)
        with self.assertRaises(ValueError):normalize_spec({**base,'entry':'scene.txt'})

    def test_script_must_be_explicitly_uploaded_and_stay_in_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.py').write_text('pass');store=Store(root/'state')
            manager=GPUManager(store)
            try:
                for entry in ('../other.py','missing.py'):
                    with self.assertRaises(ValueError):manager.preview(root,{'program':'blender','entry':entry,'files':['task.py']})
            finally:store.db.close()

    def test_failure_categories_use_actual_logs(self):
        values=[('Error: Cannot read file "/python": No such file or directory','image'),
                ('python: not found','image'),('manifest unknown','image'),('CUDA driver version is insufficient','driver'),
                ('waiting to be scheduled: concurrency limits are reached','allocation'),('command exited with exit_code 1','command')]
        for logs,category in values:
            with self.subTest(logs=logs):
                result=classify_failure(logs);self.assertEqual(result['category'],category);self.assertFalse(result['automatic_retry'])

    def test_first_startup_failure_cancels_once_without_resubmission(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'task.py').write_text('pass');store=Store(root/'state');manager=GPUManager(store)
            preview=manager.preview(root,{'program':'custom','image':'blendergrid/blender:4.2','command':['blender','-b','-P','task.py'],'files':['task.py'],'gpu':'T4','timeout':60})
            job=store.save('gpu',{'workspace':preview['workspace'],'spec':preview['spec'],'manifest':preview['files'],'status':'queued','logs':''})
            manager.cancels[job['id']]=threading.Event();manager.active.add(job['id'])
            proc=Mock();proc.stdout=io.StringIO('MUSE_CALL_ID:fc-offline\nError: Cannot read file "/python": No such file or directory\nRunner failed with exit code: 1\n')
            proc.poll.return_value=None;proc.wait.return_value=1
            cancel=Mock(return_value=True)
            try:
                with patch('workbench.gpu.subprocess.Popen',return_value=proc) as popen,patch.object(manager,'_cancel_remote',cancel):
                    manager._run(job['id'])
                result=store.get('gpu',job['id'])
                self.assertEqual(result['status'],'failed');self.assertEqual(result['failure']['code'],'image_entrypoint')
                self.assertTrue(result['remote_cancel_confirmed']);self.assertEqual(popen.call_count,1);cancel.assert_called_once()
                driver=(store.root/'gpu-jobs'/job['id']/'modal_driver.py').read_text()
                self.assertIn('retries=0',driver);self.assertIn('startup_timeout=90',driver);self.assertIn('call.cancel(terminate_containers=True)',driver)
                compile(driver,'modal_driver.py','exec')
            finally:store.db.close()

    def test_abort_cancellation_error_is_visible_and_does_not_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);manager=GPUManager(store);job=store.save('gpu',{'status':'running'})
            try:
                with patch.object(manager,'_cancel_remote',side_effect=RuntimeError('offline cancellation failed')) as cancel:
                    for _ in range(2):manager._abort_launch(job['id'],classify_failure('failed during startup',startup=True))
                self.assertFalse(store.get('gpu',job['id'])['remote_cancel_confirmed']);self.assertEqual(cancel.call_count,1)
            finally:store.db.close()


if __name__=='__main__':unittest.main()
