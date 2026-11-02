"""Private, workspace-scoped reference files with shared provenance and Git exclusion."""
import hashlib,json
from .security import contains_key

def reference_folder(ws):
    ignore=ws.path('.gitignore');old=ws.read('.gitignore') if ignore.exists() else None
    content=old['content'] if old else ''
    if not content.splitlines() or content.splitlines()[-1].strip()!='/references/':
        ws.write('.gitignore',content+('' if not content or content.endswith('\n') else '\n')+'\n# Private visual references; never publish as assets.\n/references/\n',old['sha256'] if old else None)
    folder=ws.path('references');folder.mkdir(exist_ok=True);ws.path('references',True)
    return folder

def save_reference(ws,data,extension):
    if extension not in ('.html','.pdf','.png','.jpg','.webp','.txt') or len(data)>15*1024*1024:raise ValueError('Invalid reference file type/size.')
    if contains_key(data.decode('latin1')):raise ValueError('Reference contains protected credential content.')
    reference_folder(ws);sha=hashlib.sha256(data).hexdigest();relative='references/'+sha+extension;path=ws.path(relative)
    if path.exists():
        if not path.is_file() or path.stat().st_size>15*1024*1024 or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:raise ValueError('Existing reference path has unexpected content; it was preserved.')
    else:
        with path.open('xb') as stream:stream.write(data)
    return {'path':relative,'sha256':sha,'bytes':len(data)}

def reference_index(ws,*,images=(),documents=()):
    reference_folder(ws);path='references/INDEX.json';prior=ws.read(path) if ws.path(path).exists() else None
    data=json.loads(prior['content']) if prior else {'version':1,'images':[]}
    if not isinstance(data,dict) or not isinstance(data.get('images'),list) or not isinstance(data.get('documents',[]),list):
        raise ValueError('references/INDEX.json must contain images/documents lists; existing data was preserved.')
    data['images'].extend(images)
    if documents:data.setdefault('documents',[]).extend(documents)
    ws.write(path,json.dumps(data,ensure_ascii=False,indent=2)+'\n',prior['sha256'] if prior else None)
