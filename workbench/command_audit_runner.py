"""Trusted child-side Python IO observer. No provider keys or file contents are saved."""
import hashlib,json,os,runpy,sys,threading
from pathlib import Path
from security import protected,is_link


def main():
    destination,root,*arguments=sys.argv[1:];root=Path(root).resolve();inputs={};outputs=set();guard=threading.local()
    def relative(raw):
        if not isinstance(raw,(str,bytes,os.PathLike)):return None
        try:
            path=Path(os.fsdecode(raw)).absolute();name=path.relative_to(root)
            if '..' in name.parts or any(protected(p) for p in name.parts):return None
            current=root
            for part in name.parts:
                current=current/part
                if is_link(current):return None
            return name.as_posix()
        except (ValueError,OSError):return None
    def digest(name):
        file=root/name
        if not file.is_file():return None
        value=hashlib.sha256()
        with file.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
        return value.hexdigest()
    def audit(event,args):
        if event!='open' or getattr(guard,'active',False):return
        guard.active=True
        try:
            raw=args[0]
            if isinstance(raw,(str,bytes,os.PathLike)):
                candidate=Path(os.fsdecode(raw))
                if candidate.suffix=='.pyc' and candidate.parent.name=='__pycache__':
                    source=candidate.parent.parent/(candidate.name.split('.')[0]+'.py')
                    if source.is_file():raw=source
            path=relative(raw)
            if path is None:return
            mode,flags=args[1:3];flags=flags or 0
            write=bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
            read=not (flags & os.O_WRONLY) or isinstance(mode,str) and ('r' in mode or '+' in mode)
            if write:outputs.add(path)
            if read and path not in inputs:
                source=Path(path)
                if source.suffix=='.pyc' and source.parent.name=='__pycache__':
                    candidate=source.parent.parent/(source.name.split('.')[0]+'.py')
                    if (root/candidate).is_file():path=candidate.as_posix()
                value=digest(path)
                if value:inputs[path]=value
        except (ValueError,OSError):pass
        finally:guard.active=False
    sys.addaudithook(audit)
    try:
        if arguments[0]=='-m':
            module=arguments[1];sys.argv=[module,*arguments[2:]];sys.path[0]=os.getcwd()
            runpy.run_module(module,run_name='__main__',alter_sys=True)
        else:
            script=Path(arguments[0]).absolute();sys.argv=arguments;sys.path[0]=str(script.parent)
            # Record the source even if runpy serves a cached code object.
            guard.active=True;inputs[script.relative_to(root).as_posix()]=digest(script.relative_to(root));guard.active=False
            runpy.run_path(str(script),run_name='__main__')
    finally:
        guard.active=True
        data={'inputs':{p:h for p,h in inputs.items() if p not in outputs and h},'outputs':{p:h for p in outputs if (h:=digest(p))},'method':'Python audit hook'}
        Path(destination).write_text(json.dumps(data),encoding='utf-8')


if __name__=='__main__':main()
