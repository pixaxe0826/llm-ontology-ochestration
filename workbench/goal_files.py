"""Goal-only bounded file tools and compact read history across Builder chunks."""
import copy
import json
import time
from .agent import tool,S
from .file_views import bounded_result,encoded
from .workspace import Workspace


def file_tools(tools):
    result=copy.deepcopy(tools)
    for index,entry in enumerate(result):
        if entry['function']['name']=='read_file':
            result[index]=tool('read_file','Read a UTF-8 workspace file with its full-file sha256. Optional 1-based inclusive start_line/end_line. Full-file model view is 8000 characters; a line-range view is 20000. Inspect truncated and next_start_line. Max source file 200 KB.',
                {'path':S,'start_line':{'type':'integer','minimum':1},'end_line':{'type':'integer','minimum':1}},['path'])
    result.append(tool('read_structured','Read only a JSON/YAML key_path, e.g. conveyors.CV1_infeed or items.0.name. Missing keys return top_level_keys. Oversized values report truncated; request a narrower key.',
                       {'path':S,'key_path':S},['path','key_path']))
    result.append(tool('edit_file','Prefer this for existing files. Replace old_str only when exactly one match exists and the file still matches its read_file/read_structured snapshot from THIS chunk. Hash verification, backup and diff are handled internally.',
        {'path':S,'old_str':S,'new_str':S},['path','old_str','new_str']))
    result.append(tool('code_map','Locate Python modules/classes/functions (signatures, first docstring lines, line numbers) using AST without execution; YAML/JSON top-level structure. Hash-cached, bounded 20000-character JSON. Optional paths are workspace-relative files/folders.',
        {'paths':{'type':'array','items':S,'maxItems':20}},[]))
    return result


class GoalFiles:
    def read_tool(self,task,name,args,chunk_id):
        ws=Workspace(task['workspace'],self.store.root)
        if name=='read_file':raw=ws.read(args['path'],args.get('start_line'),args.get('end_line'),include_lines=True)
        else:raw=ws.read_structured(args['path'],args['key_path'])
        record=self.store.save('goal_file_read',{'goal_id':task['goal_id'],'plan_item_id':task['plan_item_id'],'task_id':task['id'],
            'chunk_id':chunk_id,'name':name,'arguments':args,'created':time.time(),'result':raw})
        view=bounded_result({**raw,'read_id':record['id']})
        shown={k:view[k] for k in ('path','sha256','read_mode','ranged','total_lines','start_line','end_line','partial_line','next_start_line','key_path','found','truncated') if k in view}
        self.store.update('goal_file_read',record['id'],shown=shown)
        self.store.save('goal_file_read_index',{k:record[k] for k in ('id','goal_id','plan_item_id','task_id','chunk_id','name','created')}|{'shown':shown})
        return view

    def reading_context(self,item):
        if not item:return {'previous_chunk_summary':None,'already_read':[]}
        identifier=item['id'];entries=[]
        reads=sorted([r for r in self.store.items('goal_file_read_index',None) if r['plan_item_id']==identifier],key=lambda r:r['created'])
        for record in reads:
            if record.get('shown'):entries.append({k:v for k,v in record['shown'].items() if k not in ('ranged','truncated','next_start_line')})
        # Existing tasks predate range metadata. Do not claim their clipped output covered full files.
        for task_id in item.get('task_ids',[]):
            try:task=self.store.get('task',task_id)
            except ValueError:continue
            for event in task.get('events',[]):
                value=event.get('result')
                if event.get('kind')!='tool_result' or event.get('name')!='read_file' or not isinstance(value,dict) or not value.get('path') or not value.get('sha256'):continue
                if value.get('read_id'):continue
                if value.get('read_mode')=='lines':
                    entries.append({k:value[k] for k in ('path','sha256','read_mode','ranged','start_line','end_line','partial_line','truncated') if k in value})
                else:
                    content=value.get('content','');complete=isinstance(content,str) and len(encoded(value))<=8000
                    entries.append({'path':value['path'],'sha256':value['sha256'],'read_mode':'legacy',
                        'start_line':1 if complete and content else None,'end_line':len(content.splitlines()) if complete else None,
                        'legacy_range_unknown':not complete})
        merged=[]
        for row in entries:
            if row.get('start_line') is not None and row.get('end_line') is not None and not row.get('partial_line'):
                overlaps=[r for r in merged if r.get('path')==row['path'] and r.get('sha256')==row['sha256'] and r.get('start_line') is not None and r.get('end_line') is not None and not r.get('partial_line') and row['start_line']<=r['end_line']+1 and r['start_line']<=row['end_line']+1]
                for old in overlaps:
                    row={**row,'start_line':min(row['start_line'],old['start_line']),'end_line':max(row['end_line'],old['end_line'])};merged.remove(old)
                row['next_start_line']=row['end_line']+1 if row.get('total_lines') is None or row['end_line']<row['total_lines'] else None
            else:
                duplicate=next((r for r in merged if all(r.get(k)==row.get(k) for k in ('path','sha256','read_mode','start_line','end_line','key_path','found'))),None)
                if duplicate:merged.remove(duplicate)
            merged.append(row)
        omitted=max(0,len(merged)-80);merged=merged[-80:]
        while len(encoded(merged))>12000 and merged:merged.pop(0);omitted+=1
        summaries=[r for r in self.store.items('goal_chunk_summary',None) if r['plan_item_id']==identifier]
        previous=max(summaries,key=lambda r:r['created']) if summaries else None
        if previous:previous={k:previous[k] for k in ('task_id','chunk_id','status','summary','observed_tools','stalled') if k in previous}
        else:
            for task_id in reversed(item.get('task_ids',[])):
                try:task=self.store.get('task',task_id)
                except ValueError:continue
                events=task.get('events',[])
                starts=[index for index,e in enumerate(events) if e.get('kind')=='chunk_started']
                if starts:events=events[:starts[-1]]
                authored=[e.get('text','') for e in events if e.get('kind')=='assistant' and e.get('text')]
                if not authored:continue
                previous={'task_id':task['id'],'status':task['status'],'summary':authored[-1][:2000],
                          'note':'Legacy progress note; verification still requires observed evidence.'};break
            if previous is None and (item.get('report') or {}).get('summary'):
                previous={'summary':item['report']['summary'][:2000],'note':'Prior stored REPORT; verification still requires observed evidence.'}
        return {'previous_chunk_summary':previous,'already_read':merged,'omitted_read_entries':omitted,
                'note':'Ranges describe content already shown, grouped by full-file sha256. A changed sha256 requires a fresh read. Legacy unknown ranges must not be treated as complete coverage.'}

    def record_chunk_summary(self,task_id,chunk_id):
        task=self.store.get('task',task_id);events=task.get('events',[])
        start=next((index for index,e in enumerate(events) if e.get('kind')=='chunk_started' and e.get('chunk_id')==chunk_id),0)
        current=events[start:];authored=[e.get('text','') for e in current if e.get('kind')=='assistant' and e.get('text')]
        declared=task.get('builder_report') or {}
        summary=declared['summary'] if declared.get('chunk_id')==chunk_id else authored[-1] if authored else 'No final authored summary; consult the observed tool outcomes.'
        observed=[]
        for event in current:
            if event.get('kind')!='tool_result':continue
            result=event.get('result');row={'tool':event.get('name')}
            if isinstance(result,dict):
                for key in ('path','start_line','end_line','key_path','truncated','exit_code','error','denied'):
                    if key in result:row[key]=result[key][:300] if isinstance(result[key],str) else result[key]
            observed.append(row)
        from .builder_productivity import READ_TOOLS,real_action
        actions=sum(e.get('kind')=='change' or e.get('kind')=='gpu' or e.get('kind')=='tool_result' and real_action(e.get('name'),e.get('result')) for e in current)
        reads=sum(e.get('kind')=='tool' and e.get('name') in READ_TOOLS for e in current)
        stalled=bool(reads and not actions and task['status'] in ('paused','succeeded') and task.get('human_interrupt_chunk')!=chunk_id and not self.long_commands.active(task['goal_id'],task['plan_item_id']))
        self.store.update('task',task_id,chunk_outcome={'chunk_id':chunk_id,'status':'stalled' if stalled else task['status'],'stalled':stalled})
        return self.store.save('goal_chunk_summary',{'id':task_id+':'+chunk_id,'goal_id':task['goal_id'],'plan_item_id':task['plan_item_id'],'task_id':task_id,
            'chunk_id':chunk_id,'created':time.time(),'status':'stalled' if stalled else task['status'],'task_status':task['status'],
            'stalled':stalled,'execution_cycle':task.get('execution_cycle',0),'read_calls':reads,'action_observed':bool(actions),
            'summary':summary[:2000],
            'observed_tools':observed[-8:]})
