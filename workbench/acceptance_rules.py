"""Typed acceptance metadata while keeping historical string criteria and stable keys."""
from pathlib import Path,PureWindowsPath
import re
from .security import protected
from .document_roles import ROLES


EXECUTION_WORDS=re.compile(r'\b(run|runs|running|passed|passing|executed|execution|renders?|rendered|retrieves?|retrieved|simulation)\b|실행|통과|렌더|시뮬레이션',re.I)


def validate_rule(raw):
    if not isinstance(raw,dict) or set(raw)-{'kind','doc_role','required_sections','required_keys','document_paths','format','approval_paths'}:raise ValueError('Invalid acceptance rule fields.')
    kind=raw.get('kind')
    if kind not in ('execution','document','human_approval'):raise ValueError('Acceptance kind must be execution, document or human_approval.')
    result={'kind':kind}
    if kind=='human_approval':
        paths=raw.get('approval_paths')
        if not isinstance(paths,list) or not 1<=len(paths)<=20 or any(not isinstance(p,str) or not p or len(p)>500 for p in paths):raise ValueError('human_approval requires 1–20 approval_paths.')
        for path in paths:
            windows=PureWindowsPath(path)
            if windows.is_absolute() or windows.drive or ':' in path or '\x00' in path or '..' in windows.parts or any(protected(p) for p in windows.parts):raise ValueError('Approval paths must be public workspace-relative files.')
        paths=[PureWindowsPath(p).as_posix() for p in paths]
        if len(set(paths))!=len(paths):raise ValueError('Duplicate approval paths.')
        result['approval_paths']=paths
    elif 'approval_paths' in raw:raise ValueError('approval_paths belong to human_approval criteria only.')
    if 'doc_role' in raw:
        if kind!='document' or raw['doc_role'] not in ROLES:raise ValueError('Document role must be results, reference, design or static on a document criterion.')
        result['doc_role']=raw['doc_role']
    for key,limit in (('required_sections',24),('required_keys',24),('document_paths',8)):
        if key not in raw:continue
        values=raw[key]
        if not isinstance(values,list) or len(values)>limit or any(not isinstance(v,str) or not v.strip() or len(v)>300 for v in values) or len(set(values))!=len(values):raise ValueError('Invalid '+key+'.')
        if kind!='document' and values:raise ValueError('Document requirements belong to document criteria only.')
        if key=='document_paths':
            for name in values:
                path=Path(name);windows=PureWindowsPath(name)
                if path.is_absolute() or windows.is_absolute() or windows.drive or ':' in name or '\x00' in name or '..' in windows.parts or any(protected(p) for p in windows.parts):raise ValueError('Document paths must be safe workspace-relative paths.')
            values=[PureWindowsPath(v).as_posix() for v in values]
            if len(set(values))!=len(values):raise ValueError('Duplicate normalized document paths.')
        result[key]=values
    if 'format' in raw:
        if kind!='document' or raw['format'] not in ('markdown','text','json','yaml','toml','csv'):raise ValueError('Document format must be markdown, text, json, yaml, toml or csv.')
        result['format']=raw['format']
    return result


def normalize_criteria(criteria,rule_updates=None,previous=None):
    if not isinstance(criteria,list) or not 1<=len(criteria)<=8:raise ValueError('Observable acceptance criteria required.')
    texts=[];rules={}
    previous=previous or {}
    for value in criteria:
        if isinstance(value,str):text=value;rule=previous.get(text)
        elif isinstance(value,dict):
            if set(value)-{'text','criterion','kind','doc_role','required_sections','required_keys','document_paths','format','approval_paths'}:raise ValueError('Invalid typed acceptance fields.')
            text=value.get('text',value.get('criterion'));rule={k:v for k,v in value.items() if k not in ('text','criterion')}
        else:raise ValueError('Acceptance must be a string or typed criterion.')
        if not isinstance(text,str) or not text.strip() or len(text)>1500:raise ValueError('Observable acceptance text required.')
        if text in texts:raise ValueError('Duplicate acceptance criteria.')
        texts.append(text)
        if rule is not None:rules[text]=validate_rule(rule)
    if rule_updates is not None:
        if not isinstance(rule_updates,dict) or any(key not in texts for key in rule_updates):raise ValueError('Acceptance rule references an unknown criterion.')
        for text,rule in rule_updates.items():rules[text]=validate_rule(rule)
    return texts,rules


def rule_for(item,text):
    raw=(item.get('acceptance_rules') or {}).get(text)
    return validate_rule(raw) if raw is not None else {'kind':'execution','legacy':True}


def needs_execution(item,text):
    rule=rule_for(item,text)
    if rule['kind']!='execution':return False
    # Historical unclassified strings retain the exact pre-existing validation.
    return bool(EXECUTION_WORDS.search(text)) if rule.get('legacy') else True


def has_documents(item):return any(rule_for(item,c)['kind']=='document' for c in item['acceptance'])
