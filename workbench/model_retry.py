"""Retry classification for model requests only, never tool execution."""
from .response_data import field,usage_dict

DELAYS=(5,20)
CONNECTIONS={'APIConnectionError','APITimeoutError','ConnectionResetError','ConnectionAbortedError','ConnectionError','BrokenPipeError','ConnectError','ReadError','RemoteProtocolError'}

def retryable(error):
    if isinstance(error,(ValueError,TypeError,KeyError)):return False
    status=field(error,'status_code')
    if isinstance(status,int):return status==429 or 500<=status<=599
    seen=set()
    for _ in range(6):
        if error is None or id(error) in seen:break
        seen.add(id(error))
        if type(error).__name__ in CONNECTIONS or isinstance(error,(ConnectionError,BrokenPipeError)):return True
        error=error.__cause__ or error.__context__
    return False

def failure_label(error):
    status=field(error,'status_code')
    return {'exception_type':type(error).__name__,'http_status':status if isinstance(status,int) else None,
        'category':'validation' if isinstance(error,(ValueError,TypeError,KeyError)) else 'provider','retryable':retryable(error)}

def failure_usage(error):
    value=field(error,'usage');body=field(error,'body')
    if value is None and isinstance(body,dict):value=body.get('usage')
    try:return usage_dict(value)
    except (ValueError,TypeError,OverflowError):return None
