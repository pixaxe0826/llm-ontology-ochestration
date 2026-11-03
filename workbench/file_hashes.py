"""Bounded read-only hashing for public workspace files, including binary artifacts."""
import hashlib
from .security import contains_key

MAX_FILE=32*1024*1024
MAX_TOTAL=64*1024*1024


def file_hashes(ws,paths):
    if not isinstance(paths,list) or not 1<=len(paths)<=20 or any(not isinstance(p,str) or not p or len(p)>500 for p in paths):
        raise ValueError('file_hash requires 1–20 workspace-relative file paths.')
    files=[];total=0
    for path in paths:
        file=ws.path(path,True)
        if not file.is_file():raise ValueError('file_hash requires files, not directories.')
        size=file.stat().st_size;total+=size
        if size>MAX_FILE or total>MAX_TOTAL:raise ValueError('file_hash limit: 32 MiB per file and 64 MiB per request.')
        files.append((file,path))
    result=[];total=0
    for file,path in files:
        before=file.stat()
        with file.open('rb') as stream:data=stream.read(MAX_FILE+1)
        total+=len(data)
        after=ws.path(path,True).stat()
        if len(data)>MAX_FILE or total>MAX_TOTAL or (before.st_size,before.st_mtime_ns,before.st_ino)!=(after.st_size,after.st_mtime_ns,after.st_ino) or len(data)!=after.st_size:
            raise ValueError('File changed while hashing; request file_hash again before using the digest.')
        if contains_key(data.decode('utf-8',errors='ignore')):raise ValueError('Registered credential content cannot be hashed for the model.')
        result.append({'path':file.relative_to(ws.root).as_posix(),'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    return {'files':result}
