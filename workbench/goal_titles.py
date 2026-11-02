"""Display-only Goal titles; never part of the agent brief or cache prefix."""
import re
from .security import contains_key


def validate_title(value):
    if not isinstance(value,str):raise ValueError('제목은 1~120자로 입력하세요.')
    value=value.strip()
    if not 1<=len(value)<=120 or any(ord(c)<32 or ord(c)==127 for c in value) or contains_key(value):
        raise ValueError('제목은 비밀값·줄바꿈 없이 1~120자로 입력하세요.')
    return value


def suggested_title(objective):
    fallback='';fence=None
    for line in str(objective or '').splitlines():
        marker=re.match(r'^ {0,3}(`{3,}|~{3,})',line)
        if marker:
            token=marker[1]
            if fence is None:fence=(token[0],len(token))
            elif token[0]==fence[0] and len(token)>=fence[1]:fence=None
            continue
        if fence:continue
        if not fallback and line.strip():fallback=line.strip()
        heading=re.match(r'^ {0,3}#{1,6}[ \t]+(.*)$',line)
        if heading:
            title=re.sub(r'[ \t]+#+[ \t]*$','',heading[1]).strip()
            if title:return title[:120]
    return (fallback or 'Untitled Goal')[:120]
