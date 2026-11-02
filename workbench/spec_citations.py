"""Workspace-scoped current-value citations. No network, execution or inferred historical values."""
import json,re,tomllib
from pathlib import Path
from .workspace import Workspace
from .structured_files import structured_result,json_value

SPEC=re.compile(r'spec:([^\s()#;,]+)#([\w.\[\]-]+)',re.UNICODE)


def resolve_spec(manager,goal,identifier):
    match=SPEC.fullmatch(identifier)
    if not match:raise ValueError('Use spec:workspace-relative-data-path#exact.key.path.')
    path,key=match.groups();ws=Workspace(goal['workspace'],manager.store.root);raw=ws.read(path);suffix=Path(path).suffix.lower()
    if suffix in ('.yaml','.yml','.json'):selected=structured_result(raw,suffix,key)
    elif suffix=='.toml':
        data=json_value(tomllib.loads(raw['content']));value=data;found=True
        for part in re.sub(r'\[(\d+)\]',r'.\1',key).split('.'):
            if isinstance(value,dict) and part in value:value=value[part]
            elif isinstance(value,list) and part.isdecimal() and int(part)<len(value):value=value[int(part)]
            else:found=False;break
        selected={'found':found,'value':value}
    else:raise ValueError('Spec citations support workspace YAML, JSON and TOML data only.')
    if not selected['found']:raise ValueError('Spec key does not exist: '+key)
    value=selected['value']
    if isinstance(value,dict) or isinstance(value,list) and (len(value)>40 or any(isinstance(v,(dict,list)) for v in value)):
        raise ValueError('Cite an exact scalar key or a short scalar list, not a whole data object.')
    if len(json.dumps(value,ensure_ascii=False))>2000:raise ValueError('Spec citation value is too large.')
    return {'id':identifier,'kind':'spec_value','path':raw['path'],'file_sha256':raw['sha256'],'spec_key_path':key,
        'spec_value':value,'stale':False,'supports_acceptance':True,'stale_reason':'Current workspace key/value and file hash verified.'}


def add_spec_sources(manager,goal,refs,identifiers):
    for identifier in identifiers:
        if not identifier.startswith('spec:'):continue
        try:refs[identifier]=resolve_spec(manager,goal,identifier)
        except (ValueError,OSError,RecursionError) as error:
            refs[identifier]={'id':identifier,'kind':'spec_value','stale':True,'supports_acceptance':False,
                'stale_reason':'Spec citation unavailable/protected, invalid syntax or key missing.','error_type':type(error).__name__}
    return refs
