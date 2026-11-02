"""Best-effort local WinRT toast delivery, with no credentials in the child process."""
import json
import os
from pathlib import Path
import subprocess

from .security import child_environment


class WindowsAlerts:
    def send(self, payload, *, toast=True, sound=False):
        if os.name!='nt':return {'toast_submitted':False,'sound_requested':False,'status':'unavailable'}
        try:
            result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned','-File',
                    str(Path(__file__).with_name('native')/'notify.ps1')],
                input=json.dumps({**payload,'toast':bool(toast),'sound':bool(sound)},ensure_ascii=False).encode('utf-8'),
                stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=child_environment(),timeout=12,
                creationflags=subprocess.CREATE_NO_WINDOW,check=False)
            if result.returncode==0:
                value=json.loads(result.stdout.decode('utf-8-sig'))
                return {'toast_submitted':bool(value.get('toast_submitted')),'sound_requested':bool(value.get('sound_requested')),
                        'status':str(value.get('status','unknown'))[:60],'history_present':value.get('history_present')}
        except (OSError,ValueError,subprocess.SubprocessError):pass
        return {'toast_submitted':False,'sound_requested':False,'status':'failed'}
