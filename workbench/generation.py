"""Provider/role output limits and at most three explicit length continuations."""
import copy
import json
from pathlib import Path
from .response_data import usage_dict

CONTINUE = 'Continue exactly where the previous output ended. Do not repeat the existing prefix or add a new introduction. If a tool call was truncated, return a complete replacement tool call; do not execute or assume the partial call.'
REASONING_LEVELS=('minimal','low','medium','high','xhigh')


def settings():
    return json.loads(Path(__file__).with_name('model_limits.json').read_text(encoding='utf-8'))


def validate_reasoning(value):
    if value not in REASONING_LEVELS:raise ValueError('Reasoning effort must be minimal, low, medium, high or xhigh.')
    return value


def reasoning_supported(provider):
    return provider not in ('baseten','modal_llm') or bool(settings().get('capabilities',{}).get(provider+'_reasoning',False))


def reasoning(provider,value=None):
    selected=validate_reasoning(value or settings().get('default_reasoning_effort','high'))
    return selected if reasoning_supported(provider) else None


def output_limit(provider,role,model,requested=None,reasoning_effort=None):
    config=settings()
    role_limit=int(config['roles'].get(provider,{}).get(role,8000))
    effort=reasoning(provider,reasoning_effort)
    if effort is not None:role_limit=int(config['reasoning_output_limits'][effort])
    cap=int(config['model_caps'].get(model,config['unknown_model_cap']))
    if role=='Product' and requested is not None:return max(1,min(cap,int(requested)))
    return max(1,min(role_limit,cap,int(requested) if requested is not None else role_limit))


class TotalUsage:
    def __init__(self,records):self.records=records
    def model_dump(self):
        records=[usage_dict(r) for r in self.records]
        records=[r for r in records if r is not None]
        prompt=sum(r['prompt_tokens'] for r in records)
        completion=sum(r['completion_tokens'] for r in records)
        reasoning_values=[r['completion_tokens_details']['reasoning_tokens'] for r in records]
        return {'prompt_tokens':prompt,'completion_tokens':completion,'total_tokens':prompt+completion,
                'completion_tokens_details':{'reasoning_tokens':sum(v or 0 for v in reasoning_values) if any(v is not None for v in reasoning_values) else None},
                'usage_estimated':len(records)!=len(self.records) or any(r.get('usage_estimated') for r in records),
                'prompt_tokens_details':{'cached_tokens':sum(r['prompt_tokens_details']['cached_tokens'] for r in records)}}


def concatenate(prefix,part):
    # Exact overlap only; never strip whitespace or guess at a semantic rewrite.
    if prefix and part.startswith(prefix):return part
    for size in range(min(len(prefix),len(part),2000),15,-1):
        if prefix[-size:]==part[:size]:return prefix+part[size:]
    return prefix+part


def complete_continuing(make_call,messages,tools=None,max_output_tokens=None):
    current=copy.deepcopy(messages)
    text='';usages=[]
    count=min(3,max(0,int(settings()['max_continuations'])))
    for attempt in range(count+1):
        response=make_call(current,tools)
        usage=usage_dict(getattr(response,'usage',None))
        if usage is not None:usages.append(usage)
        else:usages.append({'prompt_tokens':len(json.dumps([current,tools or []],ensure_ascii=False).encode('utf-8'))+1024,
                           'completion_tokens':int(max_output_tokens or 8000),'usage_estimated':True})
        if not response.choices:break
        choice=response.choices[0];message=choice.message
        text=concatenate(text,getattr(message,'content',None) or '')
        if choice.finish_reason!='length':break
        if attempt==count:break
        current.append({'role':'assistant','content':text or '[The previous tool call was truncated before it could be executed.]'})
        current.append({'role':'user','content':CONTINUE})
    if response.choices:response.choices[0].message.content=text
    response.usage=TotalUsage(usages) if usages else None
    response._muse_usage_records=usages
    response._muse_continuations=attempt
    return response
