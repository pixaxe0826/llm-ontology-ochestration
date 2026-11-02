"""Local-command cwd coercion only; file paths keep their original safety contract."""
from .security import contains_key,redact

MISSING=object()
ROOT_ALIASES=frozenset(('', 'null', 'None', 'none', 'undefined', './', '.\\'))
ROOT_HINT="Use '.' for the workspace root or a relative folder path."


def normalize_cwd(value=MISSING):
    if value is MISSING:return '.', "cwd normalized: <missing> -> '.'"
    if value is None:return '.', "cwd normalized: null (JSON) -> '.'"
    if not isinstance(value,str):raise ValueError("cwd must be a relative folder string or null. "+ROOT_HINT)
    if value in ROOT_ALIASES:return '.', f"cwd normalized: {value!r} -> '.'"
    if len(value)>=2 and value[0]==value[-1] and value[0] in ('\"',"'") and value[1:-1] in ROOT_ALIASES|{'.'}:
        return '.',f"cwd normalized: {value!r} -> '.'"
    return value,None


def command_folder(workspace,value=MISSING):
    cwd,notice=normalize_cwd(value)
    if contains_key(cwd):raise ValueError('cwd contains a protected value.')
    # Check containment, traversal, secret/internal paths and links before existence.
    try:
        folder=workspace.path(cwd)
        if not folder.exists():raise ValueError(redact(f"cwd {cwd!r} does not exist in the workspace. "+ROOT_HINT))
        if not folder.is_dir():raise ValueError(redact(f"cwd {cwd!r} is not a directory in the workspace. "+ROOT_HINT))
    except OSError:raise ValueError('cwd has invalid path syntax or cannot be inspected in the workspace. '+ROOT_HINT) from None
    return cwd,folder,notice


def with_cwd_notice(result,notice):
    return {**result,'cwd':'.','cwd_normalization':notice} if notice else result


def tool_cwd_notice(result,arguments):
    """Also annotate denied/failed tool results without suppressing their original errors."""
    try:_,notice=normalize_cwd(arguments.get('cwd',MISSING))
    except (ValueError,AttributeError):return result
    return with_cwd_notice(result,notice)
