"""Goal-owned asynchronous commands: bounded execution, evidence, isolated proxy attribution."""
from contextlib import contextmanager
import json,secrets,sys,threading,time
from .agent import tool,S
from .command_cwd import MISSING,command_folder,with_cwd_notice
from .goal_policy import command_assessment
from .goal_costs import GoalLimit
from .security import contains_key,register_secret,redact
from .workspace import Workspace

ACTIVE={'queued','running','cancelling'}
NAMES={'start_long_command','check_long_command','cancel_long_command'}
TOOLS=[tool('start_long_command','Start one approved asynchronous local command (one per Goal). Use for work that may exceed 120 seconds. Same command policy/credential removal/UTF-8, max 1800 seconds. Returns immediately. Inspect with check_long_command; do other independent local work rather than polling repeatedly. Never submit a duplicate.',
    {'argv':{'type':'array','items':S},'cwd':{'type':['string','null']},'timeout_s':{'type':'integer','minimum':1,'maximum':1800},'label':S},['argv','timeout_s','label']),
    tool('check_long_command','Inspect this Goal command status, elapsed seconds, redacted output tail, result/evidence and attributed proxy cost. Does not rerun.',{'id':S},['id']),
    tool('cancel_long_command','Cancel this Goal command and its process tree. Never restarts the command.',{'id':S},['id'])]

class LongCommands:
    def __init__(self,manager):
        self.manager=manager;self.store=manager.store;self.lock=manager.lock
        self.runtime={};self.tokens={};self.local=threading.local();self.stopping=False
        for row in self.store.items('goal_long_command',None):
            if row['status'] in ACTIVE:
                self.finish(row,{'exit_code':None,'output':row.get('output_tail',''),'interrupted':True,'timed_out':False,
                    'cancelled':False,'timeout_s':row['timeout_s'],'duration_s':max(0,time.time()-row.get('started',row['created'])),
                    'error':'Server restarted; command was terminated, never automatically replayed.',
                    'command_files':{'inputs':row.get('input_files',{}),'outputs':{},'method':'Interrupted; runtime trace unavailable.'}},'interrupted')

    def rows(self,goal_id):return [r for r in self.store.items('goal_long_command',None) if r['goal_id']==goal_id]
    def active(self,goal_id=None,item_id=None):
        return [r for r in self.store.items('goal_long_command',None) if r['status'] in ACTIVE and (goal_id is None or r['goal_id']==goal_id) and (item_id is None or r['plan_item_id']==item_id)]

    def cost(self,identifier):
        rows=[r for r in self.manager.ledger.rows() if r.get('long_command_id')==identifier and r['state']!='released']
        return {'estimated_usd':sum(r.get('estimated_usd',0) for r in rows),'reserved_usd':sum(r['reserved_usd'] for r in rows if r['state']=='reserved'),
            'ledger_ids':[r['id'] for r in rows],
            'by_provider':{p:sum(r.get('estimated_usd',0) for r in rows if r['provider']==p) for p in sorted({r['provider'] for r in rows})}}

    def owned(self,goal_id,identifier):
        if not isinstance(identifier,str):raise ValueError('Long command id must be a string.')
        row=self.store.get('goal_long_command',identifier)
        if row['goal_id']!=goal_id:raise PermissionError('Only commands belonging to this Goal are accessible.')
        return row

    def view(self,row):
        with self.lock:
            live=self.runtime.get(row['id']);tail=live['output']() if live and live.get('output') else row.get('output_tail','')
            elapsed=max(0,row.get('finished',time.time())-row.get('started',row['created']))
            public={k:v for k,v in row.items() if k not in ('pid','input_files')}
            if row.get('result'):
                result=row['result'];output=result.get('output','')
                public['result']={**result,'output':output[-8000:],'output_truncated':len(output)>8000}
            return public|{'elapsed_s':round(elapsed,2),'output_tail':redact(tail)[-8000:],'proxy_cost':self.cost(row['id'])}

    def tool(self,task,name,args):
        if name=='start_long_command':return self.start(task,args)
        if not isinstance(args,dict) or set(args)!={'id'}:raise ValueError('Only a command id is accepted.')
        with self.lock:
            row=self.owned(task['goal_id'],args['id'])
            if name=='cancel_long_command':self.cancel(row['id'])
            return self.view(self.store.get('goal_long_command',row['id']))

    def start(self,task,args):
        if not isinstance(args,dict) or set(args)-{'argv','cwd','timeout_s','label'}:raise ValueError('Invalid long command arguments.')
        timeout=args.get('timeout_s');label=args.get('label')
        if type(timeout) is not int or not 1<=timeout<=1800:raise ValueError('timeout_s must be an integer from 1 to 1800.')
        if not isinstance(label,str) or not 1<=len(label.strip())<=120 or contains_key(json.dumps(args)):raise ValueError('Use a non-secret command label of 1–120 characters.')
        manager=self.manager;ws=Workspace(task['workspace'],self.store.root);cwd,_,notice=command_folder(ws,args.get('cwd',MISSING));argv=args.get('argv')
        with self.lock:
            goal,item=manager.builder_item_guard(task)
            if self.stopping or manager.operations_stopping.is_set():raise GoalLimit('Server is stopping; no new command may start.')
            if self.active(goal['id']):raise ValueError('Only one long command may run per Goal; inspect the existing command first.')
            command_assessment(str(ws.root),ws.state,argv,cwd,goal['policy'])
        payload={'argv':argv,'cwd':cwd,'timeout':timeout,'long_command':True,'label':label.strip(),
                 'notice':'Long command: same Windows user permissions, not an OS sandbox.',**({'cwd_normalization':notice} if notice else {})}
        if not manager.builders.approval(task['id'],'command',payload):return with_cwd_notice({'denied':True,'reason':'Command approval was denied.'},notice)
        with self.lock:
            goal,item=manager.builder_item_guard(task)
            if self.stopping or manager.operations_stopping.is_set() or self.active(goal['id']):raise GoalLimit('Execution slot changed before long command start; inspect existing commands.')
            automatic=bool(getattr(manager.builders.local,'command_auto',False))
            assessment=command_assessment(str(ws.root),ws.state,argv,cwd,goal['policy'])
            if automatic and not assessment['auto']:raise PermissionError('Automatic policy changed before execution; request approval again.')
            from .command_files import discover,hashes
            inputs=hashes(ws,discover(ws,argv,cwd)['inputs'])
            row=self.store.save('goal_long_command',{'goal_id':goal['id'],'plan_item_id':item['id'],'task_id':task['id'],
                'generation':task['generation'],'argv':list(argv),'cwd':cwd,'timeout_s':timeout,'label':label.strip(),'automatic':automatic,
                'status':'queued','created':time.time(),'input_files':inputs,'output_tail':'','proxy_rejections':[]})
            token=secrets.token_urlsafe(32);register_secret(token);self.tokens[token]=row['id']
            runtime={'cancel':threading.Event(),'reason':None,'output':None,'token':token};self.runtime[row['id']]=runtime
            worker=threading.Thread(target=self.run,args=(row['id'],),daemon=True);runtime['thread']=worker;worker.start()
            return with_cwd_notice(self.view(row),notice)

    def run(self,identifier):
        with self.lock:row=self.store.get('goal_long_command',identifier);runtime=self.runtime[identifier]
        result=None;status=None
        try:
            goal=self.manager.store.get('goal',row['goal_id']);ws=Workspace(goal['workspace'],self.store.root)
            argv=list(row['argv'])
            with self.lock:
                if runtime['cancel'].is_set():raise GoalLimit('Command cancelled before process creation.')
                assessment=command_assessment(str(ws.root),ws.state,argv,row['cwd'],goal['policy'])
                if row['automatic'] and not assessment['auto']:raise PermissionError('Automatic policy changed before process creation.')
            if argv[0].lower() in ('python','python3','python.exe'):argv[0]=sys.executable
            def started(pid,output):
                with self.lock:
                    runtime['output']=output
                    self.store.update('goal_long_command',identifier,status='running',started=time.time(),pid=pid)
            result=ws.run(argv,row['cwd'],row['timeout_s'],runtime['cancel'],
                extra_env={'MUSE_PROXY_BASE_URL':self.manager.proxy_base,'MUSE_PROXY_TOKEN':runtime['token']},
                full_output=True,timeout_limit=1800,on_process=started)
            status='interrupted' if runtime['reason']=='restart' else 'cancelled' if result['cancelled'] else 'timed_out' if result['timed_out'] else 'failed' if result['interrupted'] or result['exit_code']!=0 else 'succeeded'
        except Exception as error:
            status='interrupted' if runtime['reason']=='restart' else 'cancelled' if runtime['cancel'].is_set() else 'failed'
            result={'exit_code':None,'output':'','interrupted':True,'timed_out':False,'cancelled':status=='cancelled','timeout_s':row['timeout_s'],
                'duration_s':max(0,time.time()-row['created']),'error':redact(str(error)) if isinstance(error,(ValueError,PermissionError,GoalLimit)) else 'Long command execution failed; raw exception omitted.',
                'command_files':{'inputs':row['input_files'],'outputs':{},'method':'Execution did not produce a complete IO trace.'}}
        finally:
            with self.lock:
                try:self.finish(self.store.get('goal_long_command',identifier),result,status)
                finally:self.tokens.pop(runtime['token'],None);self.runtime.pop(identifier,None)

    def finish(self,row,result,status):
        with self.lock:
            current=self.store.get('goal_long_command',row['id'])
            if current.get('completion_event_index') is not None:return
            result={**result,'long_command_id':row['id'],'status':status,'proxy_cost':self.cost(row['id']),
                'proxy_rejections':current.get('proxy_rejections',[]),'budget_refused':bool(current.get('budget_refused',False))}
            self.store.update('goal_long_command',row['id'],status=status,finished=time.time(),result=result,output_tail=result.get('output','')[-8000:])
            try:
                # The existing collector reads these exact command events and IO hashes.
                with self.store.lock:
                    self.store.event(row['task_id'],'tool',name='run_command',arguments={'argv':row['argv'],'cwd':row['cwd'],'timeout':row['timeout_s']},long_command_id=row['id'])
                    task=self.store.event(row['task_id'],'tool_result',name='run_command',result=result,long_command_id=row['id'])
                    index=len(task['events'])-1
                    self.store.update('goal_long_command',row['id'],completion_event_index=index,evidence_id=task['id']+':'+str(index))
                item=self.store.get('plan_item',row['plan_item_id']);_,fresh=self.manager.collect_evidence(item,task)
                if fresh:
                    goal=self.store.get('goal',row['goal_id'])
                    if goal.get('no_progress'):self.manager.save(goal,no_progress=0)
                if item.get('wait_reason')=='long_command':self.manager.park_item(item,'continuation',resume='continue',continue_pending=True)
            except (ValueError,KeyError,OSError):pass # Orphan records still retain their terminal command result.

    def cancel(self,identifier,reason='cancel'):
        with self.lock:
            row=self.store.get('goal_long_command',identifier);runtime=self.runtime.get(identifier)
            if row['status'] in ACTIVE and runtime:
                runtime['reason']=reason;runtime['cancel'].set();self.store.update('goal_long_command',identifier,status='cancelling')

    def stop_goal(self,goal_id):
        for row in self.active(goal_id):self.cancel(row['id'])

    def shutdown(self):
        with self.lock:
            self.stopping=True;workers=[r['thread'] for r in self.runtime.values()]
            for row in self.active():self.cancel(row['id'],'restart')
        for worker in workers:worker.join(timeout=15)
        if any(w.is_alive() for w in workers):raise RuntimeError('Long command termination is still pending; server shutdown is not complete.')

    def token_goal(self,token):
        with self.lock:
            identifier=self.tokens.get(token)
            return self.store.get('goal_long_command',identifier)['goal_id'] if identifier else None

    @contextmanager
    def proxy_scope(self,token):
        previous=getattr(self.local,'identifier',None)
        with self.lock:identifier=self.tokens.get(token)
        self.local.identifier=identifier
        try:yield
        finally:
            self.local.identifier=previous
            if identifier:self.refresh_cost(identifier)

    def metadata(self,goal_id):
        identifier=getattr(self.local,'identifier',None)
        if not identifier:return {}
        row=self.owned(goal_id,identifier)
        return {'long_command_id':identifier,'task_id':row['task_id'],'plan_item_id':row['plan_item_id']}

    def proxy_denied(self,error):
        identifier=getattr(self.local,'identifier',None)
        if not identifier or not isinstance(error,GoalLimit):return
        budget=any(s in str(error).lower() for s in ('상한','budget','allowance','cap reached'))
        with self.lock:
            row=self.store.get('goal_long_command',identifier);events=row.get('proxy_rejections',[])
            event={'created':time.time(),'category':'budget_limit' if budget else 'goal_inactive_or_deadline','http_status':429}
            self.store.update('goal_long_command',identifier,proxy_rejections=[*events[-49:],event],
                proxy_rejection_count=row.get('proxy_rejection_count',0)+1,budget_refused=bool(budget or row.get('budget_refused')))

    def refresh_cost(self,identifier):
        with self.lock:
            row=self.store.get('goal_long_command',identifier)
            if not row.get('result'):return
            result={**row['result'],'proxy_cost':self.cost(identifier),'proxy_rejections':row.get('proxy_rejections',[]),'budget_refused':bool(row.get('budget_refused'))}
            self.store.update('goal_long_command',identifier,result=result)
            if row.get('completion_event_index') is not None:
                task=self.store.update_event(row['task_id'],row['completion_event_index'],result=result)
                self.manager.collect_evidence(self.store.get('plan_item',row['plan_item_id']),task)
