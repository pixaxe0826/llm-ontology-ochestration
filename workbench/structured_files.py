"""Read selected JSON/YAML data without evaluation, custom constructors or unbounded aliases."""
from datetime import date,datetime
import json
import math
import re


def json_value(value,stack=None,budget=None,depth=0):
    stack=set() if stack is None else stack;budget=[10000] if budget is None else budget
    budget[0]-=1
    if budget[0]<0 or depth>64:raise ValueError('Structured data exceeds the safe node/depth limit; use a line range.')
    if value is None or isinstance(value,(str,bool,int)):return value
    if isinstance(value,float):
        if not math.isfinite(value):raise ValueError('Non-finite numbers are not supported; use a line range.')
        return value
    if isinstance(value,(datetime,date)):return value.isoformat()
    if not isinstance(value,(dict,list,tuple)):raise ValueError('This YAML type is not JSON-compatible; use a line range.')
    if id(value) in stack:raise ValueError('Recursive structured data is not supported; use a line range.')
    stack.add(id(value))
    try:
        if isinstance(value,dict):
            result={}
            for key,item in value.items():
                name=key if isinstance(key,str) else str(key)
                if name in result:raise ValueError('Mapping keys are ambiguous after JSON conversion.')
                result[name]=json_value(item,stack,budget,depth+1)
            return result
        return [json_value(item,stack,budget,depth+1) for item in value]
    finally:stack.remove(id(value))


def load_yaml(text):
    import yaml
    class BoundedLoader(yaml.SafeLoader):
        nodes=0
        level=0
        def compose_node(self,parent,index):
            self.nodes+=1;self.level+=1
            try:
                if self.nodes>10000 or self.level>64:raise ValueError('YAML exceeds the safe node/depth limit; use a line range.')
                return super().compose_node(parent,index)
            finally:self.level-=1
    loader=BoundedLoader(text)
    try:
        root=loader.get_single_node();remaining=[10000]
        def check(node,stack):
            if node is None:return
            remaining[0]-=1
            if remaining[0]<0 or id(node) in stack:raise ValueError('YAML alias expansion is too large or recursive; use a line range.')
            stack.add(id(node))
            try:
                if isinstance(node,yaml.MappingNode):
                    for key,value in node.value:check(key,stack);check(value,stack)
                elif isinstance(node,yaml.SequenceNode):
                    for child in node.value:check(child,stack)
            finally:stack.remove(id(node))
        check(root,set())
        return loader.construct_document(root) if root is not None else None
    except yaml.YAMLError as error:
        mark=getattr(error,'problem_mark',None)
        suffix=f' at line {mark.line+1}, column {mark.column+1}' if mark is not None else ''
        raise ValueError('Invalid or unsupported YAML'+suffix+'.') from None
    finally:loader.dispose()


def structured_result(raw,suffix,key_path):
    if suffix not in ('.yaml','.yml','.json'):raise ValueError('read_structured supports .yaml, .yml and .json only.')
    if not isinstance(key_path,str) or len(key_path)>500:raise ValueError('key_path must be a string of at most 500 characters.')
    try:data=json.loads(raw['content']) if suffix=='.json' else load_yaml(raw['content'])
    except json.JSONDecodeError as error:raise ValueError(f'Invalid JSON at line {error.lineno}, column {error.colno}.') from None
    except RecursionError:raise ValueError('Structured data exceeds the safe depth limit.') from None
    # Normalize before returning so dates and repeated aliases cannot break storage/JSON serialization.
    data=json_value(data);keys=list(data) if isinstance(data,dict) else [str(i) for i in range(len(data))] if isinstance(data,list) else []
    current=data;found=True
    normalized=re.sub(r'\[(\d+)\]',r'.\1',key_path)
    segments=normalized.split('.') if normalized else []
    if len(segments)>64 or any(not part for part in segments):raise ValueError('Use dot-separated keys and numeric list indexes.')
    for part in segments:
        if isinstance(current,dict) and part in current:current=current[part]
        elif isinstance(current,list) and part.isdecimal() and int(part)<len(current):current=current[int(part)]
        else:found=False;break
    result={'path':raw['path'],'sha256':raw['sha256'],'read_mode':'structured','format':'json' if suffix=='.json' else 'yaml',
            'key_path':key_path,'found':found,'top_level_keys':keys,'truncated':False}
    if found:result['value']=current
    return result
