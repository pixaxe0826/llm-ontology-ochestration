"""Scoped command dependencies; never execute source code to discover paths."""
import ast
import hashlib
from pathlib import Path


def file_hash(ws,path):
    file=ws.path(path,True)
    if not file.is_file():raise ValueError('Not a regular workspace file.')
    digest=hashlib.sha256()
    with file.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def python_target(argv,folder):
    if not argv or Path(argv[0]).stem.lower() not in ('python','python3','python3.12','python3.13','python3.14'):return None
    if len(argv)>1 and not argv[1].startswith('-') and argv[1].lower().endswith('.py'):return folder/argv[1]
    return None


def discover(ws,argv,cwd='.'):
    """Static hints augment audit hooks for native readers/writers (e.g. MuJoCo/imageio).

    Historical commands have hints only, explicitly labelled as inferred rather than
    retroactively claiming that runtime IO was observed.
    """
    folder=ws.path(cwd,True);entry=python_target(argv,folder)
    inputs=set();outputs=set();sources=set();native=set();seen=set()
    def local(value):
        if not isinstance(value,(str,Path)):return None
        try:
            path=Path(value);path=path if path.is_absolute() else folder/path
            relative=path.absolute().relative_to(ws.root).as_posix()
            return ws.path(relative).relative_to(ws.root).as_posix()
        except (ValueError,OSError):return None
    def scan(file):
        relative=local(file)
        if relative is None or relative in seen or len(seen)>=64:return
        seen.add(relative);inputs.add(relative);sources.add(relative)
        try:tree=ast.parse(ws.read(relative)['content'])
        except (ValueError,OSError,SyntaxError):return
        values={'__file__':str(ws.path(relative))}
        def value(node):
            if isinstance(node,ast.Constant):return node.value
            if isinstance(node,ast.Name):return values.get(node.id)
            if isinstance(node,ast.Attribute):
                base=value(node.value)
                if isinstance(base,Path) and node.attr=='parent':return base.parent
                if isinstance(base,Path) and node.attr=='parents':return list(base.parents)
            if isinstance(node,ast.Subscript):
                base=value(node.value);index=value(node.slice)
                if isinstance(base,list) and isinstance(index,int) and 0<=index<len(base):return base[index]
            if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Div):
                left,right=value(node.left),value(node.right)
                if isinstance(left,(str,Path)) and isinstance(right,(str,Path)):return Path(left)/right
            if isinstance(node,ast.Call):
                name=getattr(node.func,'id',None) or getattr(node.func,'attr',None)
                args=[value(a) for a in node.args]
                if name in ('Path','str') and args and isinstance(args[0],(str,Path)):return Path(args[0]) if name=='Path' else str(args[0])
                if isinstance(node.func,ast.Attribute):
                    base=value(node.func.value)
                    if isinstance(base,Path) and name in ('resolve','absolute'):return base if base.is_absolute() else folder/base
                    if isinstance(base,Path) and name=='joinpath' and all(isinstance(a,(str,Path)) for a in args):return base.joinpath(*args)
            return None
        nodes=list(ast.walk(tree))
        # Resolve globals and constant local path aliases without evaluating user code.
        for _ in range(3):
            for node in nodes:
                if isinstance(node,ast.Assign):
                    result=value(node.value)
                    if result is not None:
                        for target in node.targets:
                            if isinstance(target,ast.Name):values[target.id]=result
        for node in nodes:
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                names=[node.module] if isinstance(node,ast.ImportFrom) and node.module else [a.name for a in node.names]
                for name in names:
                    for base in (ws.path(relative).parent,ws.root):
                        candidate=base/Path(*name.split('.'))
                        candidates=(candidate.with_suffix('.py'),candidate/'__init__.py')
                        found=next((p for p in candidates if local(p) and p.is_file()),None)
                        if found:scan(found);break
            if not isinstance(node,ast.Call):continue
            name=getattr(node.func,'id',None) or getattr(node.func,'attr',None)
            target=None;writing=False;reading=False
            if name=='open' and node.args:
                target=value(node.args[0]);mode=value(node.args[1]) if len(node.args)>1 else next((value(k.value) for k in node.keywords if k.arg=='mode'),'r')
                mode=mode if isinstance(mode,str) else 'r';writing=any(c in mode for c in 'wax+');reading='r' in mode or '+' in mode
            elif isinstance(node.func,ast.Attribute) and name in ('read_text','read_bytes','write_text','write_bytes'):
                target=value(node.func.value);writing=name.startswith('write');reading=not writing
            elif name in ('from_xml_path','read_csv','loadtxt','imread','mimsave','imwrite','savefig') and node.args:
                target=value(node.args[0]);writing=name in ('mimsave','imwrite','savefig');reading=not writing
            path=local(target)
            if path:
                if reading:inputs.add(path)
                if reading and name in ('from_xml_path','read_csv','loadtxt','imread'):native.add(path)
                if writing:outputs.add(path)
    if entry:scan(entry)
    return {'inputs':sorted(inputs-outputs),'outputs':sorted(outputs),'native_inputs':sorted(native-outputs),'sources':sorted(sources),'method':'static hints; runtime reads not observed'}


def hashes(ws,paths):
    result={}
    for path in paths:
        try:result[path]=file_hash(ws,path)
        except (ValueError,OSError):continue
    return result
