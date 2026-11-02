"""Bounded static workspace index. Parse code/data, never import or execute it."""
import ast
import hashlib
import json
from pathlib import Path

from .security import protected,is_link,contains_key
from .structured_files import load_yaml,json_value

SUFFIXES={'.py','.json','.yaml','.yml'}


def first_doc(node):
    return (ast.get_docstring(node) or '').split('\n',1)[0][:200]


def python_map(text):
    tree=ast.parse(text);entries=[]
    def visit(node,prefix=''):
        for child in ast.iter_child_nodes(node):
            if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                name=prefix+child.name
                if isinstance(child,ast.ClassDef):signature='class '+child.name+'('+', '.join(ast.unparse(b) for b in child.bases)+')'
                else:signature=('async ' if isinstance(child,ast.AsyncFunctionDef) else '')+'def '+child.name+'('+ast.unparse(child.args)+')'+(' -> '+ast.unparse(child.returns) if child.returns else '')
                entries.append({'name':name,'kind':'class' if isinstance(child,ast.ClassDef) else 'function','signature':signature[:700],
                    'docstring':first_doc(child),'line':child.lineno,'end_line':child.end_lineno})
                visit(child,name+'.')
            else:visit(child,prefix)
    visit(tree)
    return {'module_docstring':first_doc(tree),'symbols':entries,'total_lines':len(text.splitlines())}


def structured_map(text,suffix):
    data=json_value(json.loads(text) if suffix=='.json' else load_yaml(text))
    if isinstance(data,dict):
        keys=[{'key':key,'type':type(value).__name__,**({'child_keys':list(value)[:15]} if isinstance(value,dict) else {'length':len(value)} if isinstance(value,list) else {})} for key,value in list(data.items())[:100]]
        return {'top_level_keys':keys,'omitted_keys':max(0,len(data)-len(keys))}
    return {'root_type':type(data).__name__,**({'length':len(data)} if isinstance(data,list) else {})}


class CodeMap:
    def __init__(self,store):self.store=store

    def build(self,ws,paths=None,limit=20000):
        paths=['.'] if paths is None else paths
        if not isinstance(paths,list) or not 1<=len(paths)<=20 or any(not isinstance(p,str) or not p for p in paths):raise ValueError('code_map paths must contain 1–20 workspace-relative files/folders.')
        files={};visited=0;truncated=False
        def walk(path,depth=0):
            nonlocal visited,truncated
            if visited>=2000 or len(files)>=200:truncated=True;return
            if is_link(path) or protected(path.name):return
            visited+=1
            if path.is_file():
                if path.suffix.lower() in SUFFIXES:files[path.relative_to(ws.root).as_posix()]=path
            elif path.is_dir():
                if depth>=12:truncated=True;return
                for child in sorted(path.iterdir(),key=lambda p:p.name.lower()):walk(child,depth+1)
        for requested in paths:walk(ws.path(requested,True))
        result={'read_mode':'code_map','files':[],'truncated':truncated,'cache_hits':0,'parsed_files':0,'files_discovered':len(files),'files_omitted':0}
        total=0
        for name,path in sorted(files.items()):
            try:
                if total+path.stat().st_size>2_000_000:result['truncated']=True;result['files_omitted']+=1;continue
                raw=ws.read(name);total+=len(raw['content'].encode('utf-8'))
                key=hashlib.sha256(json.dumps(['v1',str(ws.root),name,raw['sha256']]).encode()).hexdigest()
                try:
                    cached=self.store.get('code_map_cache',key);mapped=cached['map'];result['cache_hits']+=1
                except ValueError:
                    try:mapped=python_map(raw['content']) if path.suffix.lower()=='.py' else structured_map(raw['content'],path.suffix.lower())
                    except SyntaxError as error:mapped={'error':'Python syntax error','line':error.lineno}
                    except (ValueError,RecursionError):mapped={'error':'Structured/code syntax or safe parsing limit exceeded.'}
                    self.store.save('code_map_cache',{'id':key,'workspace':str(ws.root),'path':name,'sha256':raw['sha256'],'map':mapped})
                    result['parsed_files']+=1
                result['files'].append({'path':name,'sha256':raw['sha256'],**mapped})
            except (ValueError,PermissionError,OSError):
                result['files'].append({'path':name,'error':'File omitted: protected, non-text, unavailable, or over the size limit.'})
        # Always return valid bounded JSON, with explicit omissions rather than a raw tail slice.
        encode=lambda:json.dumps(result,ensure_ascii=False)
        while len(encode())>limit:
            result['truncated']=True
            row=result['files'][-1]
            if row.get('symbols'):
                row['symbols'].pop();row['omitted_symbols']=row.get('omitted_symbols',0)+1
            elif row.get('top_level_keys'):
                row['top_level_keys'].pop();row['omitted_keys']=row.get('omitted_keys',0)+1
            else:result['files'].pop();result['files_omitted']+=1
        if contains_key(encode()):raise ValueError('Code map contains protected content.')
        return result
