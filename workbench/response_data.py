"""Optional OpenAI-compatible response fields may be absent, null or SDK objects."""
from collections.abc import Mapping


def field(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def usage_dict(usage):
    if usage is None:return None
    dump = getattr(usage, 'model_dump', None)
    if callable(dump):usage = dump()
    prompt = field(usage, 'prompt_tokens', field(usage, 'input_tokens'))
    output = field(usage, 'completion_tokens', field(usage, 'output_tokens'))
    # Unknown totals must use the caller's conservative reservation, never free usage.
    if prompt is None or output is None:return None
    prompt, output = max(0, int(prompt)), max(0, int(output))
    details = field(usage, 'prompt_tokens_details') or field(usage, 'input_tokens_details')
    cached = field(details, 'cached_tokens', field(usage, 'cached_input_tokens', 0))
    output_details = field(usage, 'completion_tokens_details') or field(usage, 'output_tokens_details')
    reasoning = field(output_details, 'reasoning_tokens', field(usage, 'reasoning_tokens'))
    return {'prompt_tokens':prompt, 'completion_tokens':output, 'total_tokens':prompt+output,
            'prompt_tokens_details':{'cached_tokens':min(prompt,max(0,int(cached or 0)))},
            'completion_tokens_details':{'reasoning_tokens':None if reasoning is None else min(output,max(0,int(reasoning)))},
            'usage_estimated':bool(field(usage, 'usage_estimated', False))}
