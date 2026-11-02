"""Evidence-scoped, local-only accepted-item checkpoints. No hooks, shell or user staging."""
import fnmatch,hashlib,json,os,re,subprocess,tempfile,time
from pathlib import Path
from .security import child_environment,contains_key,is_link,protected
from .workspace import Workspace

MAX_FILE=64*1024*1024
MAX_TOTAL=128*1024*1024
IGNORE=['references/','.venv/','venv/','.env*','*.key','*.pem','*.pfx','*.p12','.workbench/',
        '.ssh/','.aws/','.codex/','.modal.toml','credentials.json','secrets.json','credentials','id_rsa','id_ed25519','__pycache__/','*.pyc','node_modules/']

def enabled(value=True):
    if type(value) is not bool:raise ValueError('Automatic local commit must be a boolean.')
    return value

def private(path):
    return contains_key(path) or any(protected(part) or part.lower()=='references' or part.lower().endswith('.pyc') for part in Path(path).parts)

class CheckpointWarning(ValueError):pass

class Git:
    def __init__(self,root,state):
        self.root=root;self.until=time.monotonic()+25
        self.env={k:v for k,v in child_environment().items() if not k.upper().startswith('GIT_')}
        self.env.update(GIT_TERMINAL_PROMPT='0',GIT_OPTIONAL_LOCKS='0',GIT_NO_LAZY_FETCH='1')
        hooks=state/'git-checkpoints'/'no-hooks';hooks.mkdir(parents=True,exist_ok=True)
        if is_link(hooks) or any(hooks.iterdir()):raise CheckpointWarning('The disabled-hooks directory is not empty/regular; no Git command was run.')
        self.prefix=['git','-c','core.fsmonitor=false','-c','core.hooksPath='+str(hooks),'-c','commit.gpgsign=false','-c','i18n.commitEncoding=UTF-8','-c','core.quotePath=false','-c','protocol.allow=never']
        for protocol in ('http','https','ssh','git','file','ext'):self.prefix+=['-c','protocol.'+protocol+'.allow=never']
    def run(self,*args,data=None,index=None,okay=(0,)):
        remaining=self.until-time.monotonic()
        if remaining<=0:raise CheckpointWarning('Automatic commit time limit reached; no command was retried.')
        env=dict(self.env)
        if index:env['GIT_INDEX_FILE']=str(index)
        try:result=subprocess.run([*self.prefix,*args],cwd=self.root,env=env,input=data,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                shell=False,timeout=min(5,remaining),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except (OSError,subprocess.TimeoutExpired):raise CheckpointWarning('Git could not finish; raw output omitted.') from None
        if result.returncode not in okay:raise CheckpointWarning('Git '+args[0]+' failed (exit '+str(result.returncode)+'); raw output omitted.')
        if len(result.stdout)>16*1024*1024:raise CheckpointWarning('Git metadata exceeds the checkpoint size limit.')
        return result.stdout
    def head(self):
        value=self.run('rev-parse','--verify','HEAD',okay=(0,128,1)).strip().decode('ascii')
        if value and not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',value):raise CheckpointWarning('Unexpected Git HEAD metadata.')
        return value

def entries(git,index=None):
    result={}
    for raw in git.run('ls-files','--stage','-z',index=index).split(b'\0'):
        if not raw:continue
        meta,name=raw.split(b'\t',1);mode,oid,stage=meta.decode('ascii').split()
        if stage!='0':raise CheckpointWarning('The index has unresolved conflicts; resolve them before automatic commits.')
        result[name.decode('utf-8')]={'mode':mode,'oid':oid}
    return result

def ignored_many(git,paths):
    result={p:False for p in paths}
    if not result:return result
    raw=git.run('check-ignore','--no-index','-v','-z','--stdin',data=b'\0'.join(p.encode() for p in result)+b'\0',okay=(0,1)).split(b'\0')
    for start in range(0,len(raw)-3,4):
        source,_,pattern,path=raw[start:start+4];name=source.decode('utf-8').replace('\\','/')
        local=name=='.gitignore' or name.endswith('/.gitignore') and not Path(name).is_absolute() and '..' not in Path(name).parts
        result[path.decode('utf-8')]=local and not pattern.startswith(b'!')
    return result

def ignored(git,path):return ignored_many(git,[path])[path]

def harden(git,ws,record):
    path=ws.path('.gitignore');prior=ws.read('.gitignore') if path.exists() else None
    content=prior['content'] if prior else ''
    probes={}
    for pattern in IGNORE:
        name=pattern.replace('*','muse_probe')
        if name.endswith('/'):name+='muse_probe'
        probes[pattern]=[name,'muse_probe/'+name]
    for line in content.splitlines():
        if not line.startswith('!'):continue
        witness=line[1:].lstrip('/').replace('**','muse_probe').replace('*','muse_probe').replace('?','x')
        witness=re.sub(r'\[([^]]+)\]',lambda m:m[1].lstrip('!^')[:1] or 'x',witness)
        for pattern,paths in probes.items():
            if pattern.endswith('/') and pattern[:-1] in Path(witness).parts or not pattern.endswith('/') and any(fnmatch.fnmatchcase(part,pattern) for part in Path(witness).parts):paths.append(witness.rstrip('/')+('/muse_probe' if witness.endswith('/') else ''))
    tested=ignored_many(git,[p for paths in probes.values() for p in paths])
    missing=[pattern for pattern,paths in probes.items() if not all(tested[p] for p in paths)]
    if missing:
        result=ws.write('.gitignore',content+('' if not content or content.endswith('\n') else '\n')+'\n# Private Goal workspace files; never publish.\n'+'\n'.join(missing)+'\n',prior['sha256'] if prior else None)
        record.update(ignore_added=missing,ignore_sha256=result['sha256'])
        if not all(ignored_many(git,[p for paths in probes.values() for p in paths]).values()):
            raise CheckpointWarning('Private Git exclusions could not be verified; no commit was made.')
        return missing,result['sha256']
    return [],None

def snapshots(ws,item):
    selected={}
    for ref in item.get('evidence',[]):
        if ref.get('kind') not in ('change','command_output') or not ref.get('path'):continue
        path=ref['path']
        if private(path):continue
        # Preserve the latest observed version; never attribute unrelated user changes.
        selected[path]=ref.get('file_sha256')
    return selected

def commit(git,ws,item,record):
    meta=ws.root/'.git';index=meta/'index';lock=meta/'index.lock'
    if not meta.is_dir() or is_link(meta) or is_link(index):raise CheckpointWarning('Use a regular workspace-root Git repository; nested, linked or missing repositories require a manual commit.')
    root=Path(git.run('rev-parse','--show-toplevel').decode('utf-8').strip()).resolve()
    if root!=ws.root:raise CheckpointWarning('Git root differs from the Goal workspace; no parent repository was changed.')
    for name in ('MERGE_HEAD','CHERRY_PICK_HEAD','REVERT_HEAD','rebase-merge','rebase-apply','sequencer'):
        if (meta/name).exists():raise CheckpointWarning('A merge/rebase/sequencer is active; finish it before automatic commits.')
    for name in ('user.name','user.email'):
        value=git.run('config','--get',name,okay=(0,1)).strip()
        if not value or contains_key(value.decode('utf-8',errors='replace')):raise CheckpointWarning('Git user.name and user.email must both be configured; author settings were not changed.')
    try:stream=lock.open('xb')
    except FileExistsError:raise CheckpointWarning('The Git index is locked; existing lock and staging were preserved.') from None
    keep_lock=False
    try:
        original=index.read_bytes() if index.exists() else None
        if original and len(original)>16*1024*1024:raise CheckpointWarning('Git index exceeds the checkpoint size limit.')
        head=git.head();record['head_before']=head or None
        current=entries(git)
        patterns,ignore_sha=harden(git,ws,record);record['ignore_added']=patterns
        if any(private(path) for path in current):raise CheckpointWarning('Private paths are already tracked. Remove them from Git tracking manually; local files were preserved.')
        selected=snapshots(ws,item)
        if patterns:selected['.gitignore']=ignore_sha
        if not selected:return None
        if len(selected)>200:raise CheckpointWarning('More than 200 item files need a checkpoint; commit manually.')
        with tempfile.TemporaryDirectory(prefix='index-',dir=ws.state/'git-checkpoints') as temp:
            base=Path(temp);target=base/'commit-index';merged=base/'user-index'
            if head:git.run('read-tree',head,index=target)
            else:git.run('read-tree','--empty',index=target)
            parent=entries(git,target)
            if any(private(p) for p in parent):raise CheckpointWarning('Private paths exist in HEAD; clean up tracking before committing.')
            if any(current.get(p)!=parent.get(p) for p in selected):raise CheckpointWarning('An item file has existing staged changes; user staging was preserved. Commit it manually.')
            if original:merged.write_bytes(original)
            else:git.run('read-tree','--empty',index=merged)
            changes=[];total=0;raw_hashes={}
            for path,expected in sorted(selected.items()):
                file=ws.path(path)
                if private(path) or ignored(git,path):continue
                if not file.exists():
                    if path not in parent:continue
                    line=b'0 '+b'0'*len(parent[path]['oid'])+b'\t'+path.encode()+b'\0'
                else:
                    if not file.is_file() or file.stat().st_size>MAX_FILE:raise CheckpointWarning('An item file is not a regular file or exceeds 64 MiB; commit manually.')
                    with file.open('rb') as source:data=source.read(MAX_FILE+1)
                    total+=len(data)
                    if len(data)>MAX_FILE or total>MAX_TOTAL:raise CheckpointWarning('Checkpoint snapshots exceed their 64/128 MiB safety limits.')
                    sha=hashlib.sha256(data).hexdigest()
                    if expected and expected!=sha:raise CheckpointWarning('An item file changed after its observed evidence; no unreviewed version was committed.')
                    if any(contains_key(data.decode(enc,errors='ignore')) for enc in ('latin1','utf-8','utf-16-le','utf-16-be')):
                        raise CheckpointWarning('Protected credential content was detected; no file bytes were written to Git.')
                    # Snapshot stdin is immutable; never let Git reread mutable workspace bytes.
                    # Reject external clean filters before the built-in text/encoding conversion.
                    attrs=git.run('check-attr','-z','filter','--stdin',data=path.encode()+b'\0').split(b'\0')
                    if len(attrs)>2 and attrs[2] not in (b'unspecified',b'unset'):raise CheckpointWarning('An item uses a Git content filter; automatic execution of filters is not permitted.')
                    oid=git.run('hash-object','-w','--stdin','--path='+path,data=data).strip().decode('ascii')
                    mode=parent.get(path,{}).get('mode','100644')
                    if mode not in ('100644','100755'):raise CheckpointWarning('Submodules and links require a manual commit.')
                    line=(mode+' '+oid+'\t'+path).encode()+b'\0';raw_hashes[path]=sha
                    if parent.get(path)=={'mode':mode,'oid':oid}:continue
                for dest in (target,merged):git.run('update-index','-z','--index-info',data=line,index=dest)
                changes.append(path)
            if not changes:return None
            tree=git.run('write-tree',index=target).strip().decode('ascii')
            message='[goal] '+item['title']+' accepted ('+item.get('key',item['id'])+')'
            if contains_key(message):raise CheckpointWarning('Commit message contains protected content.')
            args=['commit-tree',tree]+(['-p',head] if head else [])
            sha=git.run(*args,data=(message+'\n').encode('utf-8')).strip().decode('ascii')
            record.update(commit_hash=sha,files=changes,file_hashes=raw_hashes,message=message)
            if (index.read_bytes() if index.exists() else None)!=original:raise CheckpointWarning('User index changed concurrently; no reference was updated.')
            stream.write(merged.read_bytes());stream.flush();os.fsync(stream.fileno());stream.close()
            # CAS prevents a concurrent commit from being overwritten; hooks never run.
            git.run('update-ref','-m','Goal accepted checkpoint','HEAD',sha,head or '0'*len(sha))
            record['reference_updated']=True
            try:os.replace(lock,index)
            except OSError:
                keep_lock=True;raise CheckpointWarning('Commit created, but index replacement failed. The prepared index.lock was preserved for manual recovery.') from None
            return sha
    finally:
        stream.close()
        if not keep_lock:lock.unlink(missing_ok=True)

class GoalCommits:
    def auto_commit_item(self,goal_id,item_id,acceptance_id):
        with self.lock:
            goal=self.store.get('goal',goal_id);item=self.store.get('plan_item',item_id)
            if not goal.get('auto_commit',True):return None
            prior=next((r for r in self.store.items('goal_commit',None) if r['goal_id']==goal_id and r['plan_item_id']==item_id and r['acceptance_id']==acceptance_id),None)
            if prior:return prior
            record=self.store.save('goal_commit',{'goal_id':goal_id,'plan_item_id':item_id,'acceptance_id':acceptance_id,'state':'running','created':time.time()})
            values={};held=False
            try:
                if item['status']!='done':raise CheckpointWarning('Only an accepted item can create a checkpoint.')
                held=self.builder_gate.acquire(blocking=False)
                if not held:raise CheckpointWarning('A Builder is active; automatic checkpoint skipped to preserve its work.')
                if any(self.store.get('goal',r['goal_id'])['workspace']==goal['workspace'] for r in self.long_commands.active()):
                    raise CheckpointWarning('A long command is writing this workspace; checkpoint skipped.')
                ws=Workspace(goal['workspace'],self.store.root)
                if not (ws.root/'.git').is_dir() or is_link(ws.root/'.git'):raise CheckpointWarning('Workspace has no regular Git repository; run git init/configure Git manually.')
                sha=commit(Git(ws.root,ws.state),ws,item,values)
                state='committed' if sha else 'no_changes';warning=None
            except Exception as error:
                sha=values.get('commit_hash') if values.get('reference_updated') else None
                state='committed_with_warning' if sha else 'warning'
                warning=str(error) if isinstance(error,CheckpointWarning) else 'Automatic local commit failed ('+type(error).__name__+'); raw exception omitted.'
            finally:
                if held:self.builder_gate.release()
            if not values.get('reference_updated'):values.pop('commit_hash',None)
            record=self.store.update('goal_commit',record['id'],state=state,finished=time.time(),warning=warning,**values)
            item=self.store.get('plan_item',item_id);history=[*item.get('commit_records',[]),record['id']]
            self.store.update('plan_item',item_id,commit_records=history,last_commit_state=state,commit_warning=warning,
                **({'commit_hash':sha} if sha else {}))
            if sha:
                payload={key:record[key] for key in ('commit_hash','files','file_hashes','message','ignore_added') if key in record}
                observation=self.store.save('goal_evidence',{'goal_id':goal_id,'plan_item_id':item_id,'created':time.time(),'tool':'git_commit','result':payload})
                ref={'id':'observation:'+observation['id'],'observation_id':observation['id'],'kind':'git_commit','commit_hash':sha,
                    'sha256':hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest(),'files':{},'observed_at':time.time(),'supports_acceptance':False}
                item=self.store.get('plan_item',item_id);self.store.update('plan_item',item_id,evidence=[*item.get('evidence',[]),ref])
            body={'event':'auto_commit','state':state,'warning':warning,'commit_hash':sha,'ignore_added':values.get('ignore_added',[])}
            self.message(goal,'System','Planner','NOTE',json.dumps(body),item_id,[record['id']])
            return record
