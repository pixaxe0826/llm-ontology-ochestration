"""Goal-only optimistic edits and bounded read loops; no security/approval bypass."""
import json
import time
from pathlib import Path

from .code_map import CodeMap
from .security import contains_key,redact
from .workspace import Workspace
from .plan_updates import prepare_plan
from .plan_capacity import PlanCapacityError,plan_limit,DEFAULT as DEFAULT_PLAN_LIMIT,active_count
from .item_states import terminal
from .goal_costs import GoalLimit

READ_TOOLS={'read_file','read_structured','search_files','list_files','code_map'}
ACTION_TOOLS={'write_file','edit_file','run_command','start_long_command','submit_gpu_job','copy_gpu_artifact'}
NOTICE='Your next action must write a file (prefer edit_file for existing files) or execute a permitted local command. Implement from what you have already read; do not repeat reading to postpone the deliverable. Safety, approvals and budget limits still apply. If an action is unsafe or genuinely blocked, report the concrete blocker instead of making a dummy change.'


def read_limits(value=None):
    value={'warn':8,'block':12} if value is None else value
    if not isinstance(value,dict) or set(value)!={'warn','block'} or any(type(v) is not int for v in value.values()) or not 1<=value['warn']<=value['block']<=100:
        raise ValueError('Read loop limits require integer 1 <= warn <= block <= 100.')
    return dict(value)


def real_action(name,result):
    if name not in ACTION_TOOLS or not isinstance(result,dict) or result.get('error') or result.get('denied'):return False
    if name in ('write_file','edit_file','copy_gpu_artifact'):return bool(result.get('sha256') and not result.get('unchanged'))
    if name=='run_command':return result.get('exit_code') is not None or bool(result.get('timed_out') or result.get('cancelled') and result.get('command_files'))
    if name=='start_long_command':return bool(result.get('id') and result.get('status') in ('queued','running','succeeded','failed'))
    return bool(result.get('id') or result.get('job_id'))


class GoalProductivity:
    def chunk_state(self,task,chunk_id):
        ident=task['id']+':'+chunk_id
        try:return self.store.get('goal_chunk_state',ident)
        except ValueError:return {'id':ident,'goal_id':task['goal_id'],'plan_item_id':task['plan_item_id'],'task_id':task['id'],'chunk_id':chunk_id,'consecutive_reads':0,'read_calls':0,'actions':0,'blocked_reads':0}

    def before_builder_tool(self,task,name,chunk_id):
        if name not in READ_TOOLS:return None
        state=self.chunk_state(task,chunk_id);limits=read_limits(self.store.get('goal',task['goal_id']).get('read_loop_limits'))
        state['read_calls']+=1
        if state['consecutive_reads']>=limits['block']:
            state['blocked_reads']+=1;self.store.save('goal_chunk_state',state)
            return {'error':'Reading blocked for the remainder of this chunk until a real write or command executes.','read_stall':True,'consecutive_reads':state['consecutive_reads'],'system_notice':NOTICE}
        state['consecutive_reads']+=1;self.store.save('goal_chunk_state',state)
        return None

    def after_builder_tool(self,task,name,result,chunk_id):
        state=self.chunk_state(task,chunk_id)
        if real_action(name,result):
            state.update(consecutive_reads=0,actions=state['actions']+1);self.store.save('goal_chunk_state',state)
        limits=read_limits(self.store.get('goal',task['goal_id']).get('read_loop_limits'))
        if name in READ_TOOLS and state['consecutive_reads']>=limits['warn']:
            result={**result} if isinstance(result,dict) else {'result':result}
            result.update(read_stall=True,consecutive_reads=state['consecutive_reads'],system_notice=NOTICE)
        return result

    def edit_tool(self,task,args,chunk_id):
        if not task.get('allow_write',True):raise PermissionError('This task is read-only.')
        old,new=args.get('old_str'),args.get('new_str')
        if not isinstance(old,str) or not old or not isinstance(new,str) or contains_key(old) or contains_key(new):raise ValueError('edit_file requires a nonempty old_str and non-secret string new_str.')
        ws=self.builders.workspace(task);path=ws.path(args['path'],True).relative_to(ws.root).as_posix()
        current=ws.read(path)
        reads=[]
        for row in self.store.items('goal_file_read_index',None):
            if row.get('task_id')!=task['id'] or row.get('chunk_id')!=chunk_id:continue
            shown=row.get('shown',{})
            try:read_path=ws.path(shown.get('path',''),True).relative_to(ws.root).as_posix()
            except (ValueError,OSError):continue
            if read_path==path and shown.get('sha256'):reads.append(row)
        if not reads:raise ValueError('Read this file with read_file/read_structured in the current chunk before edit_file.')
        snapshot=max(reads,key=lambda row:row['created'])['shown']['sha256']
        if current['sha256']!=snapshot:raise ValueError('File changed since this chunk read it. Read the latest version before editing.')
        count=0;positions=[];start=0
        while True:
            found=current['content'].find(old,start)
            if found<0:break
            count+=1
            if len(positions)<20:positions.append({'line':current['content'].count('\n',0,found)+1,'column':found-current['content'].rfind('\n',0,found)})
            start=found+1
        if count!=1:
            return {'error':'old_str must match exactly once; found '+str(count)+'. Use a larger unique context from the current file.','match_count':count,'match_locations':positions,'locations_truncated':count>20,'path':path}
        if old==new:return {'path':path,'sha256':current['sha256'],'unchanged':True,'match_count':1}
        result=ws.write(path,current['content'].replace(old,new,1),snapshot)
        result.update(previous_sha256=snapshot,tool='edit_file',match_count=1)
        self.store.event(task['id'],'change',change=result)
        return {k:v for k,v in result.items() if k!='diff'}

    def code_map_tool(self,task,args):
        return self.code_mapper.build(Workspace(task['workspace'],self.store.root),args.get('paths'))

    def productivity_context(self,goal,item):
        if not item:return {}
        ws=Workspace(goal['workspace'],self.store.root);paths=[]
        for hint in [*item.get('files_to_read',[]),*item.get('files_to_write',[])]:
            try:
                path=ws.path(hint)
                while not path.exists() and path!=ws.root:path=path.parent
                if path.is_file():path=path.parent
                relative=path.relative_to(ws.root).as_posix()
                if relative not in paths:paths.append(relative)
            except (ValueError,OSError):continue
        try:mapped=self.code_mapper.build(ws,paths[:5] or ['.'],limit=8000)
        except (ValueError,OSError):mapped={'error':'Automatic code map unavailable; use a scoped code_map request.'}
        task=self.task_for_item(item);chunk=getattr(self.builders.local,'chunk_id',None)
        state=self.chunk_state(task,chunk) if task and chunk else None
        summaries=[s for s in self.store.items('goal_chunk_summary',None) if s['plan_item_id']==item['id']]
        last=max(summaries,key=lambda s:s['created']) if summaries else {}
        return {'code_map':mapped,'read_loop_limits':read_limits(goal.get('read_loop_limits')),
            'read_loop':state and {k:state[k] for k in ('consecutive_reads','read_calls','actions','blocked_reads')},
            'previous_chunk_stalled':last.get('stalled',False),
            'instruction':('The previous chunk ended without a real write or execution. Implement immediately from the code map and prior reads. '+NOTICE) if last.get('stalled') else NOTICE if state and state['consecutive_reads']>=read_limits(goal.get('read_loop_limits'))['warn'] else 'Use code_map to locate code before opening only the necessary line range.'}

    def handle_stalled_batch(self,goal,item,task):
        records=[s for s in self.store.items('goal_chunk_summary',None) if s['plan_item_id']==item['id'] and s.get('execution_cycle',0)==item.get('execution_cycle',0) and s.get('stalled')]
        count=len({s.get('chunk_id',s['id']) for s in records})
        self.store.update('plan_item',item['id'],stalled_chunks=count)
        if count<2:return False
        item=self.store.get('plan_item',item['id'])
        if item.get('stall_replan_attempted'):
            self.park_item(item,'platform_error',resume=None,continue_pending=False,stall_reason='No implementation progress after the single automatic scope reduction.')
            self.message(goal,'Planner','Human','ESCALATE','Builder stalled again after the bounded automatic scope reduction. Decide a smaller scope or missing prerequisite.',item['id'])
        else:
            self.park_item(item,'stall_replan',resume=None,continue_pending=False,stalled_chunks=count,stall_replan_pending=True)
        return True

    def recover_stall_request(self,goal,item):
        if terminal(item):return
        if item.get('wait_reason')!='stall_replan' or item.get('stall_replan_pending') or not item.get('stall_replan_attempted'):return
        for row in self.store.items('goal_stall_replan',None):
            if row['plan_item_id']==item['id'] and row['state']=='requesting':self.store.update('goal_stall_replan',row['id'],state='interrupted')
        self.park_item(item,'platform_error',resume=None,continue_pending=False,stall_reason='The claimed-once scope reduction was interrupted; it will not be replayed.')
        self.message(goal,'System','Human','ESCALATE','The automatic read-stall scope reduction was interrupted. Inspect the recorded response before retrying; no automatic duplicate request was made.',item['id'])

    def shrink_stalled_item(self,goal,item):
        if terminal(item):return False
        if not item.get('stall_replan_pending'):return False
        limit=plan_limit(goal.get('max_plan_items',DEFAULT_PLAN_LIMIT));count=active_count(self.items(goal['id']))
        if count>=limit:
            self.store.update('plan_item',item['id'],stall_replan_pending=False)
            self.capacity_notice(goal,item,PlanCapacityError(limit,count+1),'stall_split')
            return False
        # Claim once before the paid Planner request; restart never silently replays it.
        self.store.update('plan_item',item['id'],stall_replan_pending=False,stall_replan_attempted=True)
        schema={'items':[{'key':'new-child-key','title':'Small implementation deliverable','acceptance':['Observable original-scope criterion'],
            'files_to_read':['existing.py'],'files_to_write':['existing.py'],'commands_to_run':[['python','-m','pytest','tests']], 'depends_on':[], 'blocked_by_decision':[]}], 'summary':'Why this scope will make progress'}
        instruction='AUTOMATIC READ-STALL SCOPE REDUCTION. The Builder ended two chunks with reads only. Before asking a human, split ONLY this item into 1–4 smaller implementation children. Preserve the parent acceptance criteria and all existing items/counters/evidence. Specify files_to_read (at most 5), files_to_write and commands_to_run for each child. At least one write path or command is required. Children must not depend on their parent; dependencies only where actual output is consumed. Do not create human decisions, claim success, or return review-only children. Return JSON: '+json.dumps(schema)
        attempt=self.store.save('goal_stall_replan',{'goal_id':goal['id'],'plan_item_id':item['id'],'state':'requesting','created':time.time()})
        try:
            outputs=[]
            for retry in range(2):
                previous=getattr(self.review_scope,'read_only',False);self.review_scope.read_only=True
                try:raw,human_queue=self.inbox_phase(goal,item,instruction)
                finally:self.review_scope.read_only=previous
                outputs.append({'retry':retry,'response':redact(raw)})
                self.store.update('goal_stall_replan',attempt['id'],response=redact(raw),attempts=outputs)
                try:
                    value=self.parse(raw);patch=self.validate_stall_split(goal,item,value)
                    break
                except GoalLimit:raise
                except PlanCapacityError:raise
                except (ValueError,TypeError,KeyError) as error:
                    outputs[-1]['validation_error']=redact(str(error))
                    self.store.update('goal_stall_replan',attempt['id'],attempts=outputs)
                    if retry:raise
                    instruction+='\nYour previous scope JSON failed validation: '+redact(str(error))+'. Correct this JSON once, preserving the original parent and all other items. This is one logical scope reduction, not another Builder attempt.'
            with self.lock:
                self.guard(goal['id'],goal['generation']);self.apply_plan_patch(goal,patch)
                for row in patch['creates']:self.store.update('plan_item',row['id'],stall_replan_attempted=True,stall_replan_root=item.get('stall_replan_root',item['id']))
                self.park_item(item,'split_children',resume='review',split_item_ids=[r['id'] for r in patch['creates']],continue_pending=False)
                self.store.update('goal_stall_replan',attempt['id'],state='applied',child_ids=[r['id'] for r in patch['creates']],summary=str(value.get('summary',''))[:2000])
                self.message(goal,'Planner','Builder','NOTE','Automatic read-stall scope reduction applied: '+str(value.get('summary',''))[:2000],item['id'],[attempt['id']])
            self.finish_inbox(goal,human_queue,value)
            return True
        except GoalLimit:raise
        except PlanCapacityError as error:
            self.store.update('goal_stall_replan',attempt['id'],state='capacity_wait',error=str(error))
            self.capacity_notice(goal,item,error,'stall_split')
            return False
        except (ValueError,TypeError,KeyError) as error:
            self.store.update('goal_stall_replan',attempt['id'],state='failed',error=redact(str(error)))
            self.park_item(item,'platform_error',resume=None,continue_pending=False,stall_reason='Automatic scope reduction failed validation; existing plan preserved.')
            self.message(goal,'System','Human','ESCALATE','The single automatic read-stall scope reduction could not produce a valid bounded plan. Existing acceptance criteria and evidence were preserved.',item['id'],[attempt['id']])
            return False

    def validate_stall_split(self,goal,item,value):
        if not isinstance(value,dict):raise ValueError('Scope reduction JSON must be an object.')
        rows=value.get('items')
        if not isinstance(rows,list) or not 1<=len(rows)<=4:raise ValueError('Scope reduction requires 1–4 new implementation children.')
        patch=prepare_plan(self.items(goal['id']),[{**r,'operation':'create'} for r in rows],max_plan_items=goal.get('max_plan_items',DEFAULT_PLAN_LIMIT))
        if patch['updates'] or len(patch['creates'])!=len(rows):raise ValueError('Scope reduction must create distinct children only.')
        ids={r['id'] for r in patch['creates']}
        for row in patch['creates']:
            if not row.get('files_to_write') and not row.get('commands_to_run'):raise ValueError('Each child needs explicit implementation/write or execution hints.')
            if 'files_to_read' not in row or 'files_to_write' not in row or 'commands_to_run' not in row:raise ValueError('Each child needs all three bounded implementation hints.')
            if row['parent_id'] not in (None,item['id']) and row['parent_id'] not in ids:raise ValueError('Child parent is outside the stalled item.')
            if row['parent_id'] is None:row['parent_id']=item['id']
            row['depends_on']=list(dict.fromkeys([*item.get('depends_on',[]),*row.get('depends_on',[])]))
            if item['id'] in row['depends_on']:raise ValueError('Children cannot depend on the stalled parent.')
            row['blocked_by_decision']=list(dict.fromkeys([*item.get('blocked_by_decision',[]),*row.get('blocked_by_decision',[])]))
        graph={i['id']:i.get('depends_on',[]) for i in self.items(goal['id'])};graph.update({r['id']:r['depends_on'] for r in patch['creates']})
        def check(key,trail):
            if key==item['id'] or key in trail:raise ValueError('Scope reduction creates a dependency cycle through the parent.')
            for ref in graph.get(key,[]):check(ref,trail|{key})
        for key in ids:check(key,set())
        self.validate_decision_refs(goal,patch)
        return patch
