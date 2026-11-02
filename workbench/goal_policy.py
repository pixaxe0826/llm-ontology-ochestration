"""Goal-only command policy. Uses the existing workspace and credential boundaries."""
import json
from pathlib import Path
import re

from .security import contains_key, protected
from .workspace import Workspace
from .command_cwd import MISSING,command_folder

DEFAULT_COMMANDS = ['python -m pytest', 'python -m unittest', 'python <workspace.py>',
                    'git status', 'git diff', 'git log', 'git add', 'git commit']


def policy(value=None):
    value = value or {}
    if not isinstance(value,dict):raise ValueError('정책 설정은 객체여야 합니다.')
    mode = value.get('mode','manual')
    if mode not in ('manual','autonomous'):
        raise ValueError('정책은 manual 또는 autonomous입니다.')
    commands = value.get('commands', DEFAULT_COMMANDS)
    if not isinstance(commands,list) or any(c not in DEFAULT_COMMANDS for c in commands):
        raise ValueError('MVP의 자동 명령 목록은 기본 제공 명령 중에서 선택하세요. 다른 명령은 개별 승인합니다.')
    gpus = value.get('gpus',['T4','L4'])
    from .gpu import GPUS
    if not isinstance(gpus,list) or any(g not in GPUS for g in gpus):
        raise ValueError('허용 GPU 목록을 확인하세요.')
    from .goal_costs import number
    return {'mode':mode,'commands':list(dict.fromkeys(commands)),'gpus':list(dict.fromkeys(gpus)),
            'gpu_per_job':number(value.get('gpu_per_job',1),'GPU 작업당 상한')}


def command_assessment(root, state, argv, cwd=MISSING, settings=None):
    settings = settings or policy()
    ws = Workspace(root,state)
    cwd,_,_=command_folder(ws,cwd)
    if not isinstance(argv,list) or not argv or len(argv)>64 or any(not isinstance(a,str) or '\x00' in a for a in argv):
        raise PermissionError('명령은 문자열 배열이어야 합니다.')
    if contains_key(json.dumps(argv)):
        raise PermissionError('비밀값이 포함된 명령은 금지됩니다.')
    exe = Path(argv[0]).name.lower().removesuffix('.exe')
    words = [a.lower() for a in argv[1:]]
    joined = ' '.join(words)
    if exe in ('cmd','powershell','pwsh','bash','sh','wscript','cscript'):
        raise PermissionError('Goal에서는 셸 래퍼를 통한 정책 우회를 허용하지 않습니다.')
    if any(re.search(r'(^|[^a-z])(push|deploy|publish)([^a-z]|$)', a) for a in words) or (exe in ('twine','gh') and any(a in ('upload','release') for a in words)):
        raise PermissionError('Goal에서 push·배포·게시 명령은 항상 금지됩니다.')
    if any(word in joined for word in ('api_key','proxy_token','token_secret','get-childitem env','winreg','reg query','credential')):
        raise PermissionError('키 읽기·출력 명령은 항상 금지됩니다.')
    for arg in argv[1:]:
        raw = arg.split('=',1)[-1] if '=' in arg else arg
        if raw.startswith(('http://','https://')):
            continue
        path = Path(raw)
        if path.is_absolute() or path.drive or raw.startswith(('/', '\\')):
            if not path.resolve().is_relative_to(ws.root):
                raise PermissionError('명령의 작업 폴더 밖 경로는 금지됩니다.')
            relative = path.resolve().relative_to(ws.root)
            ws.path(str(relative))
        elif '..' in path.parts or any(protected(p) for p in path.parts):
            raise PermissionError('명령의 폴더 밖·비밀 경로는 금지됩니다.')
    allowed = None
    argument_reason = None
    def relative_arguments(arguments):
        for argument in arguments:
            raw=argument.split('=',1)[-1]
            if re.search(r'[;|&><`$\x00-\x1f]',argument):
                return 'Arguments contain shell special characters; individual approval is required.'
            if '..' in raw or raw.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:',raw):
                return 'Automatic approval requires relative workspace paths without .. or absolute paths.'
            # Resolve path-like arguments relative to cwd, including non-existent outputs.
            if '/' in raw or '\\' in raw or Path(raw).suffix or (ws.root/Path(cwd)/raw).exists():
                prefix=re.split(r'[*?\[]',raw,maxsplit=1)[0]
                try:ws.path(str(Path(cwd)/prefix))
                except (ValueError,OSError):return 'An argument is not a permitted relative workspace path.'
        return None
    if exe in ('python','python3'):
        if any(a in ('-c','--command') for a in words):
            raise PermissionError('인라인 Python 코드는 Goal 정책에서 허용하지 않습니다. 폴더 안 스크립트로 작성하세요.')
        if len(argv)>=3 and argv[1]=='-m' and argv[2] in ('pytest','unittest'):
            allowed = 'python -m '+argv[2]
            argument_reason=relative_arguments(argv[3:])
            if any(a.startswith(('-p','--override-ini')) and a not in ('-p',) for a in argv[3:]) and argv[2]=='pytest':
                allowed = None
        elif len(argv)>=2 and not argv[1].startswith('-'):
            script = ws.path(str(Path(cwd)/argv[1]),True)
            if script.suffix.lower()=='.py' and script.is_file():
                ws.read(str(script.relative_to(ws.root)))
                allowed = 'python <workspace.py>'
                argument_reason=relative_arguments(argv[1:])
    elif exe == 'git':
        if not words or words[0].startswith('-') or any(a in ('-c','--config-env','--exec-path') or a.startswith(('--git-dir','--work-tree')) for a in words):
            raise PermissionError('Git 경로·설정 우회 옵션은 Goal에서 금지됩니다.')
        if words[0] in ('status','diff','log','add','commit'):
            if words[0]=='commit' and any(a in ('--amend','--allow-empty','--no-verify','-a','--all') for a in words[1:]):
                allowed = None
            else:
                allowed = 'git '+words[0]
    if settings['mode']!='autonomous':reason='Manual policy requires individual approval.'
    elif argument_reason:reason=argument_reason
    elif Path(argv[0]).is_absolute() or Path(argv[0]).drive:reason='An absolute executable path requires individual approval.'
    elif allowed is None:reason='The command does not match an automatic approval rule.'
    elif allowed not in settings['commands']:reason='The matching command rule is not selected in this Goal.'
    else:return {'auto':True,'reason':'Matches the selected autonomous command rule.','rule':allowed}
    return {'auto':False,'reason':reason,'rule':allowed}


def command_decision(root,state,argv,cwd=MISSING,settings=None):
    return command_assessment(root,state,argv,cwd,settings)['auto']
