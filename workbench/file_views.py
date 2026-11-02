"""JSON-safe model views; originals stay in the local store, never silent tail slices."""
import copy
import json

FULL_LIMIT=8000
RANGE_LIMIT=20000


def encoded(value):
    return json.dumps(value,ensure_ascii=False)


def line_result(result,start_line=None,end_line=None):
    lines=result['content'].splitlines(keepends=True);total=len(lines)
    ranged=start_line is not None or end_line is not None
    for name,value in (('start_line',start_line),('end_line',end_line)):
        if value is not None and (isinstance(value,bool) or not isinstance(value,int) or value<1):raise ValueError(name+' must be a positive integer.')
    start=1 if start_line is None else start_line
    end=total if end_line is None else end_line
    if end_line is not None and end<start:raise ValueError('end_line must be at least start_line.')
    if total and start>total:raise ValueError(f'start_line exceeds total_lines ({total}).')
    if not total and start>1:raise ValueError('The file is empty (total_lines=0).')
    end=min(end,total);shown_start=start if total else 0
    return {**result,'content':''.join(lines[start-1:end]),'read_mode':'lines','ranged':ranged,'total_lines':total,
            'requested_start_line':start,'requested_end_line':end,'start_line':shown_start,'end_line':end,
            'truncated':False,'next_start_line':end+1 if end<total else None}


def result_limit(result):
    if isinstance(result,dict) and result.get('read_mode')=='code_map':return RANGE_LIMIT
    if isinstance(result,dict) and result.get('read_mode')=='reference_page':return 40000
    return RANGE_LIMIT if isinstance(result,dict) and (result.get('ranged') or result.get('read_mode')=='structured') else FULL_LIMIT


def bounded_lines(raw,limit):
    if 'total_lines' not in raw:raw=line_result(raw)
    if len(encoded(raw))<=limit:return copy.deepcopy(raw)
    lines=raw['content'].splitlines(keepends=True)
    def candidate(count,partial=None):
        value={**raw,'content':''.join(lines[:count]),'truncated':True,
               'end_line':raw['start_line']+count-1,'next_start_line':raw['start_line']+count,
               'guidance':'Continue with next_start_line and a line range. For YAML/JSON prefer read_structured with a narrower key_path.'}
        if partial is not None:
            value.update(content=lines[0][:partial],end_line=raw['start_line'],next_start_line=raw['start_line'],
                         partial_line=True,guidance='This single line exceeds the view budget. Use read_structured for YAML/JSON instead of repeating this range.')
        return value
    low,high=0,len(lines)
    while low<high:
        mid=(low+high+1)//2
        if len(encoded(candidate(mid)))<=limit:low=mid
        else:high=mid-1
    if low or not lines:return candidate(low)
    low,high=0,len(lines[0])
    while low<high:
        mid=(low+high+1)//2
        if len(encoded(candidate(0,mid)))<=limit:low=mid
        else:high=mid-1
    return candidate(0,low)


def bounded_structured(raw,limit):
    value=copy.deepcopy(raw)
    if len(encoded(value))<=limit:return value
    original=value.pop('value',None)
    value.update(value=None,truncated=True,guidance='The selected value exceeds the view budget. Choose a narrower key_path or a line range.')
    if isinstance(original,dict):value['child_keys']=list(original)[:80]
    elif isinstance(original,list):value['list_length']=len(original)
    for key in ('top_level_keys','child_keys','available_keys'):
        if key in value and len(value[key])>80:
            value[key+'_omitted']=len(value[key])-80;value[key]=value[key][:80]
    preview=encoded(original) if original is not None else ''
    low,high=0,len(preview)
    while low<high:
        mid=(low+high+1)//2
        if len(encoded({**value,'value_preview':preview[:mid]}))<=limit:low=mid
        else:high=mid-1
    value['value_preview']=preview[:low]
    # Even unusual, very long key names cannot overflow the model envelope.
    while len(encoded(value))>limit:
        keys=next((k for k in ('top_level_keys','child_keys','available_keys') if value.get(k)),None)
        if keys is None:raise ValueError('Structured metadata exceeds the view budget.')
        value[keys].pop();value[keys+'_omitted']=value.get(keys+'_omitted',0)+1
    return value


def bounded_result(result,limit=None):
    limit=result_limit(result) if limit is None else limit
    if isinstance(result,dict) and isinstance(result.get('argv'),list) and 'elapsed_s' in result and 'timeout_s' in result and 'label' in result:
        # Preserve actionable command state/cost/hashes even when its output is enormous.
        value={k:copy.deepcopy(result[k]) for k in ('id','label','status','elapsed_s','timeout_s','evidence_id','budget_refused','proxy_rejection_count') if k in result}
        cost=copy.deepcopy(result.get('proxy_cost',{}));ids=cost.get('ledger_ids',[])
        cost['ledger_ids']=ids[:20];cost['ledger_ids_omitted']=max(0,len(ids)-20);value['proxy_cost']=cost
        raw=result.get('result') or {}
        value['result']={k:raw[k] for k in ('exit_code','duration_s','interrupted','timed_out','cancelled','output_limit_exceeded','budget_refused','error') if k in raw}
        files=copy.deepcopy(raw.get('command_files',{}))
        for key in ('inputs','outputs'):
            entries=list(files.get(key,{}).items());files[key]=dict(entries[:20]);files[key+'_omitted']=max(0,len(entries)-20)
        value['result']['command_files']=files
        value['proxy_rejections']=result.get('proxy_rejections',[])[-3:]
        value['output_tail']=result.get('output_tail','')[-4000:]
        value['full_original_in_store']=True
        while len(encoded(value))>limit and (files['inputs'] or files['outputs']):
            key='inputs' if len(files['inputs'])>=len(files['outputs']) else 'outputs'
            files[key].pop(next(iter(files[key])));files[key+'_omitted']+=1
        if len(encoded(value))>limit:
            tail=value.pop('output_tail');low,high=0,len(tail)
            while low<high:
                mid=(low+high+1)//2
                if len(encoded({**value,'output_tail':tail[-mid:] if mid else ''}))<=limit:low=mid
                else:high=mid-1
            value['output_tail']=tail[-low:] if low else ''
        return value
    if isinstance(result,dict) and result.get('read_mode')=='reference_page':
        value=copy.deepcopy(result);value['content']=value.get('content','')[:20000]
        while len(encoded(value))>limit and value.get('images'):
            value['images'].pop();value['image_list_truncated']=True
        while len(encoded(value))>limit and value.get('text_pages'):
            value['text_pages'].pop();value['text_page_metadata_truncated']=True
        if len(encoded(value))>limit:
            original=value.pop('content','');value['text_truncated']=True
            low,high=0,len(original)
            while low<high:
                mid=(low+high+1)//2
                if len(encoded({**value,'content':original[:mid]}))<=limit:low=mid
                else:high=mid-1
            value['content']=original[:low]
        return value
    if isinstance(result,dict) and result.get('id') and 'status' in result and isinstance(result.get('spec'),dict) and 'program' in result['spec']:
        result=copy.deepcopy(result)
        if isinstance(result.get('result'),dict):
            inner=result['result'];logs=inner.pop('logs',None)
            if isinstance(logs,str):inner['logs_excerpt']=logs[-1600:];inner['full_logs_in_store']=True
        result.pop('logs',None)
        result['artifact_guidance']='Do not resubmit a succeeded job. Binary files: copy_gpu_artifact(job_id,path,destination). read_gpu_artifact is UTF-8 text only (200 KB).'
        if len(encoded(result))>limit:
            source=result;inner=source.get('result') or {};summary=inner.get('summary') or {}
            result={k:source[k] for k in ('id','status','call_id','failure','artifact_guidance') if k in source}
            result['spec']={k:source['spec'][k] for k in ('program','gpu','entry','timeout','volume') if k in source['spec']}
            result['artifacts']=source.get('artifacts',[])[:40];result['artifacts_omitted']=max(0,len(source.get('artifacts',[]))-40)
            result['result']={k:inner[k] for k in ('exit_code','volume','volume_path','full_logs_in_store') if k in inner}
            result['result']['summary']={k:summary[k] for k in ('duration_s','exit_code','sampling_interval_s') if k in summary}
            result['result']['logs_excerpt']=str(inner.get('logs_excerpt',''))[-800:]
            result['full_original_in_store']=True
    if isinstance(result,dict) and result.get('read_mode')=='structured':return bounded_structured(result,limit)
    if isinstance(result,dict) and isinstance(result.get('content'),str) and 'path' in result and 'sha256' in result:
        return bounded_lines(result,limit)
    text=encoded(result)
    if len(text)<=limit:return result
    view={'truncated':True,'guidance':'Full original tool output is retained in the local store.','preview':''}
    low,high=0,min(len(text),limit)
    while low<high:
        mid=(low+high+1)//2
        if len(encoded({**view,'preview':text[:mid]}))<=limit:low=mid
        else:high=mid-1
    return {**view,'preview':text[:low]}


def tool_text(text):
    try:result=json.loads(text)
    except (ValueError,TypeError):
        return encoded({'truncated':True,'legacy_result_unavailable':True,
                        'guidance':'Legacy tool JSON was clipped. Locate the section with search_files, then use a line range or read_structured.'})
    return encoded(bounded_result(result))


def observation_text(event):
    if isinstance(event,dict) and event.get('kind')=='tool_result' and event.get('name') in ('get_gpu_job','submit_gpu_job'):
        event={**event,'result':bounded_result(event.get('result'))}
    if isinstance(event,dict) and event.get('kind')=='tool_result' and event.get('name') in ('read_file','read_structured'):
        result=event['result'];limit=result_limit(result)
        view={**event,'result':bounded_result(result,max(1000,limit-len(encoded({**event,'result':{}}))-16))}
        return encoded(view)
    if isinstance(event,dict) and event.get('read_mode'):return encoded(bounded_result(event))
    return encoded(bounded_result(event,FULL_LIMIT))


def planner_tool_view(payload):
    if isinstance(payload,dict) and isinstance(payload.get('result'),dict) and payload['result'].get('read_mode'):
        result=payload['result']
        return {**payload,'result':bounded_result(result,result_limit(result)-128)}
    return payload
