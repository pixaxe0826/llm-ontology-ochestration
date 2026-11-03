"""Normalize registered Chat Completions/Responses models to the existing tool loop."""
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace as NS
import copy,json,threading,time,uuid
from .response_data import field,usage_dict
from .security import contains_key


class AdapterError(Exception):
    def __init__(self,provider,category,status_code=None,*,not_sent=False):
        self.status_code=status_code;self.category=category
        self.usage={'prompt_tokens':0,'completion_tokens':0} if not_sent else None
        super().__init__(provider+(' upstream '+str(status_code) if status_code else '')+': '+category)


def error(provider,exc):
    status=getattr(exc,'status_code',None)
    category={400:'invalid request',401:'authentication failed',403:'access denied',404:'model or endpoint not found',422:'unsupported request',429:'rate limited'}.get(status,
        'upstream unavailable' if isinstance(status,int) and status>=500 else 'connection failed' if type(exc).__name__ in ('APIConnectionError','APITimeoutError','ConnectError','ReadError') else type(exc).__name__)
    result=AdapterError(provider,category,status)
    from .model_retry import failure_usage
    result.usage=failure_usage(exc)
    return result


def tools_normalized(raw):
    calls=[]
    for call in raw or []:
        function=field(call,'function') or {};arguments=field(function,'arguments','{}')
        if not isinstance(arguments,str):arguments=json.dumps(arguments,ensure_ascii=False)
        calls.append(NS(id=field(call,'id') or 'call_'+uuid.uuid4().hex[:16],type='function',function=NS(name=field(function,'name'),arguments=arguments)))
    return calls


def chat_messages(messages,cap):
    result=[]
    for source in messages:
        if source.get('role') not in ('system','developer','user','assistant','tool'):raise ValueError('Unsupported model message role.')
        row={key:copy.deepcopy(source[key]) for key in ('role','content','tool_calls','tool_call_id','name') if key in source}
        if cap['reasoning_content'] and source.get('reasoning_content') is not None:row['reasoning_content']=source['reasoning_content']
        content=row.get('content')
        if isinstance(content,list):
            for part in content:
                if part.get('type')=='image_url' and not cap['image_input']:raise ValueError('Selected model does not support image input.')
                if part.get('type') not in ('text','image_url'):raise ValueError('Unsupported model input content type.')
        result.append(row)
    return result


def response_input(messages,cap):
    result=[]
    chat_messages(messages,cap)  # Validate roles/modalities before preserving response items.
    for source in messages:
        role=source.get('role');content=source.get('content')
        if role=='assistant' and source.get('_muse_response_items'):
            items=source['_muse_response_items']
            if not isinstance(items,list) or len(json.dumps(items))>1_000_000 or any(not isinstance(i,dict) or i.get('type') not in ('reasoning','function_call','message') for i in items):raise ValueError('Invalid preserved Responses items.')
            result.extend(copy.deepcopy(items));continue
        if role=='tool':result.append({'type':'function_call_output','call_id':source['tool_call_id'],'output':content or ''});continue
        if role not in ('system','developer','user','assistant'):raise ValueError('Unsupported Responses role.')
        if isinstance(content,list):
            parts=[]
            for part in content:
                if part.get('type')=='text':parts.append({'type':'input_text','text':part.get('text','')})
                elif part.get('type')=='image_url':
                    if not cap['image_input']:raise ValueError('Selected model does not support image input.')
                    image=part['image_url'];parts.append({'type':'input_image','image_url':image if isinstance(image,str) else image['url'],
                        **({'detail':image['detail']} if isinstance(image,dict) and 'detail' in image else {})})
                else:raise ValueError('Unsupported Responses input content.')
            result.append({'role':role,'content':parts})
        elif content is not None and (content or not source.get('tool_calls')):result.append({'role':role,'content':content})
        for call in source.get('tool_calls') or []:
            function=call['function'];result.append({'type':'function_call','call_id':call['id'],'name':function['name'],'arguments':function['arguments']})
    return result


def json_tool_bridge(messages,tools):
    schema={'type':'object','properties':{'content':{'type':'string'},'tool_calls':{'type':'array','items':{'type':'object','properties':{
        'name':{'type':'string','enum':[t['function']['name'] for t in tools]},'arguments':{'type':'object'}},'required':['name','arguments']}}},'required':['content','tool_calls']}
    guidance='Native tools are unavailable. Return one JSON object {"content":"short message","tool_calls":[{"name":"allowed tool name","arguments":{}}]}. Use [] when finished. Never claim execution; only returned tool results prove actions. Allowed tool definitions: '+json.dumps(tools,ensure_ascii=False)
    converted=[]
    for row in messages:
        if row.get('role')=='tool':converted.append({'role':'user','content':'OBSERVED TOOL RESULT '+row['tool_call_id']+'\n'+str(row.get('content',''))})
        elif row.get('tool_calls'):converted.append({'role':'assistant','content':json.dumps({'content':row.get('content') or '',
            'tool_calls':[{'name':c['function']['name'],'arguments':json.loads(c['function']['arguments'])} for c in row['tool_calls']]},ensure_ascii=False)})
        else:converted.append(row)
    return [*converted,{'role':'user','content':guidance}],schema


def normalize_chat(response):
    choices=field(response,'choices') or []
    if not choices:raise ValueError('Provider returned no model choices.')
    choice=choices[0];source=field(choice,'message') or {};extras=field(source,'model_extra') or {}
    content=field(source,'content') or ''
    if isinstance(content,list):content=''.join(str(field(p,'text','')) for p in content)
    reasoning=field(source,'reasoning_content',field(extras,'reasoning_content'))
    message=NS(content=content,tool_calls=tools_normalized(field(source,'tool_calls')),model_extra={'reasoning_content':reasoning} if reasoning is not None else {})
    return NS(choices=[NS(message=message,finish_reason=field(choice,'finish_reason','stop'))],usage=usage_dict(field(response,'usage')),model=field(response,'model'))


def normalize_responses(response):
    output=field(response,'output') or [];text=[];calls=[];items=[]
    for raw in output:
        value=raw.model_dump(exclude_none=True) if callable(getattr(raw,'model_dump',None)) else copy.deepcopy(raw) if isinstance(raw,dict) else dict(vars(raw))
        kind=field(raw,'type')
        if kind in ('reasoning','function_call','message'):items.append(value)
        if kind=='function_call':calls.append({'id':field(raw,'call_id'),'function':{'name':field(raw,'name'),'arguments':field(raw,'arguments','{}')}})
        if kind=='message':
            for part in field(raw,'content') or []:
                if field(part,'type')=='output_text':text.append(str(field(part,'text','')))
    incomplete=field(response,'status')=='incomplete';why=field(field(response,'incomplete_details'),'reason')
    finish='length' if incomplete and why=='max_output_tokens' else 'content_filter' if incomplete else 'tool_calls' if calls else 'stop'
    message=NS(content=''.join(text) or field(response,'output_text','') or '',tool_calls=tools_normalized(calls),model_extra={'_muse_response_items':items})
    return NS(choices=[NS(message=message,finish_reason=finish)],usage=usage_dict(field(response,'usage')),model=field(response,'model'))


class ModelAdapters:
    def __init__(self,registry,client_factory):
        self.registry=registry;self.client_factory=client_factory;self.lock=threading.RLock();self.gates={}

    @contextmanager
    def slot(self,provider,timeout):
        key=(provider['id'],provider.get('revision',0));limits=provider['limits'];end=time.monotonic()+max(.1,timeout)
        with self.lock:
            gate,starts=self.gates.setdefault(key,(threading.BoundedSemaphore(limits['concurrency']),deque()))
        if not gate.acquire(timeout=max(0,end-time.monotonic())):raise AdapterError(provider['id'],'local concurrency limit',429,not_sent=True)
        try:
            while True:
                with self.lock:
                    now=time.monotonic()
                    while starts and now-starts[0]>=60:starts.popleft()
                    rpm=limits.get('requests_per_minute')
                    if not rpm or len(starts)<rpm:starts.append(now);break
                    wait=60-(now-starts[0])
                if now+wait>=end:raise AdapterError(provider['id'],'local rate limit',429,not_sent=True)
                time.sleep(min(wait,.25))
            yield
        finally:gate.release()

    def complete(self,provider_id,messages,model,tools=None,max_tokens=8000,reasoning_effort=None,timeout_seconds=90):
        return self.request(provider_id,messages,model,tools,max_tokens,reasoning_effort,timeout_seconds)

    def request(self,provider_id,messages,model,tools=None,max_tokens=8000,reasoning_effort=None,timeout_seconds=90,search=False,instructions=None):
        provider,record=self.registry.model(provider_id,model);cap=record['capabilities'];timeout=max(.1,min(90,float(timeout_seconds or 90)))
        if reasoning_effort is not None and reasoning_effort not in cap['reasoning_efforts']:raise ValueError('Unsupported model reasoning effort.')
        limit=min(int(max_tokens),cap['max_output']);bridge=bool(tools and not cap['tool_calling'])
        if cap.get('max_context') and len(json.dumps([messages,tools or []],ensure_ascii=False).encode('utf-8'))+limit>cap['max_context']:
            raise ValueError('Conservative context bound exceeds the registered max_context. Reduce input/output or register a verified context limit.')
        if bridge and not cap['structured_json']:raise ValueError('Model lacks tool calling and structured JSON.')
        from .generation import CONTINUE,concatenate
        prefix=messages[-2].get('content','') if bridge and len(messages)>=2 and messages[-1].get('content')==CONTINUE and messages[-2].get('role')=='assistant' else ''
        if bridge:messages,_=json_tool_bridge(messages,tools)
        kwargs={'model':model}
        style='responses' if search else provider['api_style']
        if style=='chat_completions':
            kwargs.update(messages=chat_messages(messages,cap),**{provider['output_parameter']:limit})
            if tools and not bridge:kwargs.update(tools=tools,parallel_tool_calls=False)
            if reasoning_effort is not None:kwargs['reasoning_effort']=reasoning_effort
            if bridge:kwargs['response_format']={'type':'json_object'}
        else:
            kwargs.update(input=response_input(messages,cap),store=False,max_output_tokens=limit)
            if instructions:kwargs['instructions']=instructions
            if reasoning_effort is not None:kwargs['reasoning']={'effort':reasoning_effort}
            if cap['reasoning_content']:kwargs['include']=['reasoning.encrypted_content']
            if tools and not bridge:
                kwargs['tools']=[{'type':'function','strict':False,**t['function']} for t in tools];kwargs['parallel_tool_calls']=False
            if bridge:kwargs['text']={'format':{'type':'json_object'}}
        if search:
            if not cap['web_search']:raise ValueError('Selected reference-search model has no built-in web search.')
            kwargs.update(tools=[{'type':'web_search','search_context_size':'low'}],max_tool_calls=1)
            kwargs['include']=[*kwargs.get('include',[]),provider.get('search_include','web_search_call.action.sources')]
        try:
            with self.slot(provider,timeout),self.client_factory(provider_id) as client:
                response=(client.chat.completions.create(**kwargs,timeout=timeout) if style=='chat_completions' else client.responses.create(**kwargs,timeout=timeout))
        except (ValueError,TypeError,KeyError,AdapterError):raise
        except Exception as exc:raise error(provider_id,exc) from None
        if search:return response
        normalized=normalize_chat(response) if style=='chat_completions' else normalize_responses(response)
        if bridge:
            normalized.choices[0].message.model_extra['_muse_bridge_raw']=normalized.choices[0].message.content
        if bridge and normalized.choices[0].finish_reason!='length':
            try:
                value=json.loads(concatenate(prefix,normalized.choices[0].message.content));calls=value.get('tool_calls',[]);allowed={t['function']['name'] for t in tools}
                if not isinstance(value,dict) or not isinstance(calls,list) or len(calls)>10:raise ValueError()
                if any(not isinstance(c,dict) or c.get('name') not in allowed or not isinstance(c.get('arguments'),dict) for c in calls):raise ValueError()
                normalized.choices[0].message.content=str(value.get('content',''));normalized.choices[0].message.tool_calls=tools_normalized([{'function':c} for c in calls])
                normalized.choices[0].finish_reason='tool_calls' if calls else 'stop'
                normalized.choices[0].message.model_extra={'_muse_bridge_raw':normalized.choices[0].message.model_extra['_muse_bridge_raw']}
            except (ValueError,TypeError,AttributeError):raise ValueError('Structured tool response failed validation; no tools were executed.') from None
        normalized._muse_provider=provider_id;normalized._muse_model=model
        return normalized

    def search(self,provider_id,query,model,max_tokens,reasoning_effort,timeout_seconds,instructions):
        return self.request(provider_id,[{'role':'user','content':query}],model,max_tokens=max_tokens,reasoning_effort=reasoning_effort,
            timeout_seconds=timeout_seconds,search=True,instructions=instructions)
