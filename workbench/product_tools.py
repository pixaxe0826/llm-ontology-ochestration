"""Bounded OpenAI function-call transport; the proxy never executes tools."""
import json
from .response_data import field
from .security import contains_key, redact

REQUEST_FIELDS = frozenset(('model','messages','stream','max_tokens','max_completion_tokens',
                            'reasoning_effort','tools','tool_choice','parallel_tool_calls'))
MESSAGE_FIELDS = {
    'system': {'role','content','name'}, 'user': {'role','content','name'},
    'assistant': {'role','content','name','tool_calls','reasoning_content'},
    'tool': {'role','content','name','tool_call_id'},
}


def allowed_fields(value, allowed, location=''):
    unknown=sorted(set(value)-allowed)
    if unknown:
        names=', '.join(redact(str(k))[:120] for k in unknown[:20])
        raise ValueError('지원하지 않는 필드: '+location+names)


def bounded_string(value, label, maximum=200, empty=False):
    if not isinstance(value,str) or len(value)>maximum or (not empty and not value):
        raise ValueError(label+' must be a '+('possibly empty ' if empty else 'non-empty ')+f'string (maximum {maximum} characters).')


def request_tools(data):
    allowed_fields(data, REQUEST_FIELDS)
    if contains_key(json.dumps(data,ensure_ascii=False)):
        raise ValueError('Product proxy requests must not contain registered secrets.')
    if 'stream' in data and not isinstance(data['stream'],bool):raise ValueError('stream must be boolean.')
    tools=data.get('tools')
    names=set()
    if tools is not None:
        if not isinstance(tools,list) or len(tools)>128:raise ValueError('tools must be an array (maximum 128 functions).')
        if len(json.dumps(tools,ensure_ascii=False))>500000:raise ValueError('Tool definitions exceed 500000 characters.')
        for tool in tools:
            if not isinstance(tool,dict) or tool.get('type')!='function' or not isinstance(tool.get('function'),dict):
                raise ValueError('Only OpenAI function tool definitions are supported.')
            allowed_fields(tool,{'type','function'},'tools[].')
            function=tool['function'];allowed_fields(function,{'name','description','parameters','strict'},'tools[].function.')
            bounded_string(function.get('name'),'Tool name',64)
            if function['name'] in names:raise ValueError('Tool names must be unique.')
            names.add(function['name'])
            if 'parameters' in function and not isinstance(function['parameters'],dict):raise ValueError('Tool parameters must be a JSON schema object.')
            if 'description' in function:bounded_string(function['description'],'Tool description',100000,empty=True)
            if 'strict' in function and not isinstance(function['strict'],bool):raise ValueError('Tool strict must be boolean.')
    options={k:data[k] for k in ('tool_choice','parallel_tool_calls') if k in data}
    if 'parallel_tool_calls' in options and not isinstance(options['parallel_tool_calls'],bool):raise ValueError('parallel_tool_calls must be boolean.')
    if 'tool_choice' in options:
        choice=options['tool_choice']
        if isinstance(choice,str):
            if choice not in ('auto','none','required'):raise ValueError('tool_choice must be auto, none, required or a named function.')
            if choice!='none' and not names:raise ValueError('tool_choice requires tools.')
        elif isinstance(choice,dict) and choice.get('type')=='function' and isinstance(choice.get('function'),dict):
            allowed_fields(choice,{'type','function'},'tool_choice.')
            allowed_fields(choice['function'],{'name'},'tool_choice.function.')
            if choice['function'].get('name') not in names:raise ValueError('tool_choice must name a supplied function.')
        else:raise ValueError('Invalid tool_choice.')
    return tools,options


def message_tools(message):
    role=message.get('role')
    if role not in MESSAGE_FIELDS:raise ValueError('Invalid product proxy message role.')
    allowed_fields(message,MESSAGE_FIELDS[role],'messages[].')
    if 'name' in message:bounded_string(message['name'],'Message name',64)
    if role=='tool':bounded_string(message.get('tool_call_id'),'tool_call_id')
    if 'reasoning_content' in message and message['reasoning_content'] is not None:
        bounded_string(message['reasoning_content'],'reasoning_content',100000,empty=True)
    calls=message.get('tool_calls')
    if calls is not None:
        if not isinstance(calls,list) or len(calls)>128:raise ValueError('assistant tool_calls must contain at most 128 function calls.')
        ids=set()
        for call in calls:
            if not isinstance(call,dict) or call.get('type')!='function' or not isinstance(call.get('function'),dict):raise ValueError('Invalid assistant function call.')
            allowed_fields(call,{'id','type','function'},'messages[].tool_calls[].')
            bounded_string(call.get('id'),'Tool call ID')
            if call['id'] in ids:raise ValueError('Tool call IDs must be unique in a message.')
            ids.add(call['id'])
            function=call['function'];allowed_fields(function,{'name','arguments'},'messages[].tool_calls[].function.')
            bounded_string(function.get('name'),'Tool call name',64)
            bounded_string(function.get('arguments'),'Tool call arguments',100000,empty=True)
    return bool(calls)


def assistant_message(message):
    """SDK objects and optional fields use the same representation as Builder."""
    result={'role':'assistant','content':field(message,'content') or ''}
    calls=field(message,'tool_calls') or []
    if calls:
        result['tool_calls']=[{'id':field(call,'id'),'type':'function',
            'function':{'name':field(field(call,'function'),'name'),
                        'arguments':field(field(call,'function'),'arguments')}} for call in calls]
    reasoning=field(field(message,'model_extra') or {},'reasoning_content',field(message,'reasoning_content'))
    if reasoning is not None:result['reasoning_content']=reasoning
    return result
