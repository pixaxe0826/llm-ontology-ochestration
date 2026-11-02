"""Fixed Muse Spark Open identities and safe errors for the local product API."""
import json

from .providers import BASETEN_MODEL, error_message
from .security import redact
from .generation import settings as generation_settings

MUSE_ALIASES = (BASETEN_MODEL, 'muse-spark-1026', 'muse-spark-open-1026', 'muse-spark-open')


def product_output(data,model):
    parameter='max_completion_tokens' if 'max_completion_tokens' in data else 'max_tokens' if 'max_tokens' in data else None
    if parameter is None:return None,{'proxy_adjusted':False},None
    requested=data[parameter]
    if isinstance(requested,bool) or not isinstance(requested,int) or requested<1:
        raise ValueError(parameter+' must be a positive integer.')
    config=generation_settings();cap=int(config['model_caps'].get(model,config['unknown_model_cap']))
    effective=min(cap,max(1024,requested) if parameter=='max_tokens' else requested)
    adjusted=parameter=='max_tokens' and effective!=requested
    metadata={'proxy_adjusted':adjusted,'proxy_requested_parameter':parameter,
              'proxy_requested_tokens':requested,'proxy_effective_tokens':effective}
    adjustment={'parameter':parameter,'requested':requested,'effective':effective} if adjusted else None
    return effective,metadata,adjustment


def resolve_model(requested, target):
    """Aliases identify the configured Muse route, never a different provider."""
    if requested is None or isinstance(requested, str) and not requested.strip():
        return target['model']
    accepted = MUSE_ALIASES + ((target['model'],) if target['route'] == 'modal' else ())
    if isinstance(requested, str) and requested.strip() in accepted:
        return target['model']
    # Do not echo an untrusted model ID, which could itself contain a credential.
    raise ValueError('허용 모델: ' + target['model'] + '. 허용 별칭: ' + ', '.join(MUSE_ALIASES[1:]))


def proxy_error_message(error):
    """Classify upstream failures without reflecting their body, URL or headers."""
    status = getattr(error, 'status_code', None)
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        body = getattr(error, 'body', None)
        try:
            text = redact(json.dumps(body, ensure_ascii=True) if isinstance(body, (dict, list)) else str(body or ''))[:8000].lower()
        except Exception:
            text = ''
        if status == 404:
            category = 'model not found' if 'model' in text else 'model or endpoint not found'
        elif status == 401:
            category = 'authentication failed'
        elif status == 403:
            category = 'permission denied'
        elif status == 429:
            category = 'quota exceeded' if any(s in text for s in ('quota', 'credit', 'insufficient')) else 'rate limit exceeded'
        elif status in (408, 504):
            category = 'request timed out'
        elif status >= 500:
            category = 'provider service error'
        elif status == 400:
            category = 'invalid request'
        else:
            category = 'request rejected'
        return f'upstream {status}: {category}'
    if type(error).__name__ in ('APIConnectionError', 'APITimeoutError'):
        return 'upstream: connection failed' if type(error).__name__ == 'APIConnectionError' else 'upstream: request timed out'
    return redact(error_message(error))
