import json
import os
from pathlib import Path
import re
import stat
import threading

KEY_NAMES = ("MODEL_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "MODAL_PROXY_TOKEN", "BASETEN_API_KEY")
BLOCKED_DIRS = {".git", ".venv", "venv", "node_modules", ".ssh", ".aws", ".codex", ".workbench", "__pycache__"}
_ephemeral = set()
_secret_lock = threading.RLock()


def register_secret(value):
    with _secret_lock:
        _ephemeral.add(value)


def provider_key_name(name):
    return name=='OPENAI_API_KEY' or bool(re.fullmatch(r'MUSE_PROVIDER_[A-Z][A-Z0-9_]{0,39}_API_KEY',name))


def secret_values():
    with _secret_lock:
        return [*list(_ephemeral),*[value for name,value in os.environ.items() if value and (name in KEY_NAMES or provider_key_name(name))]]


def refresh_keys():
    if os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                for name in KEY_NAMES:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        if value:
                            os.environ[name] = value
                    except FileNotFoundError:
                        pass
                index=0
                while True:
                    try:name,value,_=winreg.EnumValue(key,index);index+=1
                    except OSError:break
                    if provider_key_name(name) and isinstance(value,str) and value:os.environ[name]=value
        except OSError:
            pass


def redact(value):
    text = str(value)
    for secret in secret_values():
        if secret:text=text.replace(secret,'[REDACTED]')
    return re.sub(r"(?i)(Bearer\s+)[\w.\-]{16,}", r"\1[REDACTED]", text)


def contains_key(text):
    return any(secret in text for secret in secret_values() if secret)


def clean_data(data):
    return json.loads(redact(json.dumps(data, ensure_ascii=False)))


def protected(name):
    name = name.lower()
    return (name in BLOCKED_DIRS or name.startswith(".env") or name in {
        ".modal.toml", "credentials.json", "id_rsa", "id_ed25519", "credentials", "secrets.json"
    } or name.endswith((".key", ".pem", ".pfx", ".p12")))


def is_link(path):
    try:
        info = path.lstat()
        return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def scoped_path(root, relative=".", must_exist=False):
    root = Path(root).resolve()
    raw = Path(relative)
    if raw.is_absolute() or raw.drive or ":" in str(relative) or "\x00" in str(relative):
        raise ValueError("선택 폴더 기준 상대 경로만 사용할 수 있습니다.")
    if ".." in raw.parts or any(protected(p) for p in raw.parts):
        raise ValueError("폴더 밖 경로 또는 비밀·내부 경로는 접근할 수 없습니다.")
    current = root
    for part in raw.parts:
        current = current / part
        if is_link(current):
            raise ValueError("심볼릭 링크와 junction은 접근할 수 없습니다.")
    candidate = (root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("선택한 폴더 밖 경로입니다.")
    if must_exist and not candidate.exists():
        raise ValueError("경로가 없습니다.")
    return candidate


def child_environment():
    # Local tests and user programs never inherit provider credentials.
    return {k: v for k, v in os.environ.items() if k not in KEY_NAMES and not any(
        word in k.upper() for word in ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
    )}
