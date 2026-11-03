import logging
import os
from urllib.parse import urlparse

from .security import refresh_keys
from .generation import output_limit, complete_continuing, reasoning, reasoning_supported, validate_reasoning
from .response_data import usage_dict

logging.getLogger("openai").setLevel(logging.CRITICAL)
logging.getLogger("httpx").setLevel(logging.CRITICAL)
logging.getLogger("httpx2").setLevel(logging.CRITICAL)

DEFAULTS = {
    "meta": {"label": "Meta", "base_url": "https://api.meta.ai/v1", "model": "muse-spark-1.3", "key_env": "MODEL_API_KEY"},
    "baseten": {"label": "Baseten", "base_url": "https://inference.baseten.co/v1", "model": "meta-models/muse-spark-open-1026", "key_env": "BASETEN_API_KEY"},
}
BASETEN_MODEL = "meta-models/muse-spark-open-1026"


def selected_model(provider, model):
    if provider == "baseten":
        if model and model != BASETEN_MODEL:
            raise ValueError("Baseten은 Muse Spark Open 1026 고정입니다. 다른 모델로 변경할 수 없습니다.")
        return BASETEN_MODEL
    return model


class Providers:
    def __init__(self, store):
        self.store = store
        from .provider_registry import ProviderRegistry
        from .model_adapters import ModelAdapters
        self.registry=ProviderRegistry(store)
        self.adapters=ModelAdapters(self.registry,self.goal_client)

    def goal_client(self,provider):
        if provider in DEFAULTS or provider=='modal_llm':return self.client(provider)
        from openai import OpenAI, DefaultHttpxClient
        from .security import register_secret
        value=self.registry.get(provider);refresh_keys();key=os.environ.get(value['credential_ref'])
        if not key:raise ValueError('Provider key is not registered. Run scripts/set_provider_key.ps1 locally.')
        register_secret(key)
        return OpenAI(base_url=value['base_url'],api_key=key,max_retries=0,timeout=90,
            http_client=DefaultHttpxClient(follow_redirects=False,trust_env=False,timeout=90))

    def goal_complete(self,provider,messages,model,tools=None,max_tokens=8000,timeout_seconds=None,reasoning_effort=None):
        return self.adapters.complete(provider,messages,model,tools,max_tokens,reasoning_effort,timeout_seconds or 90)

    def goal_search(self,provider,query,model,max_tokens,reasoning_effort,timeout_seconds,instructions):
        return self.adapters.search(provider,query,model,max_tokens,reasoning_effort,timeout_seconds,instructions)

    def settings(self):
        try:
            saved = self.store.get("settings", "providers")
        except ValueError:
            saved = {}
        result = {}
        try:vision=self.store.get('settings','attachment_capabilities')
        except ValueError:vision={}
        refresh_keys()
        for key, default in DEFAULTS.items():
            result[key] = {**default, **saved.get(key, {})}
            if key == "baseten":
                result[key] = dict(default)
                result[key]["locked"] = True
            result[key]['reasoning_effort']=validate_reasoning(saved.get(key,{}).get('reasoning_effort','high'))
            result[key]['reasoning_supported']=reasoning_supported(key)
            result[key]["ready"] = bool(os.environ.get(default["key_env"]))
            result[key]['vision_supported']=vision.get(key,True)
        return result

    def image_error(self, provider, error, has_images):
        """Only explicit upstream modality rejection disables future image sends."""
        if provider!='baseten' or not has_images or getattr(error,'status_code',None) not in (400,422):return None
        text=str(error).lower()
        if not (any(word in text for word in ('image','vision','multimodal')) and
                any(word in text for word in ('not support','unsupported','does not accept','not allowed','text-only','text only'))):return None
        try:saved=self.store.get('settings','attachment_capabilities')
        except ValueError:saved={'id':'attachment_capabilities'}
        saved['baseten']=False;self.store.save('settings',saved)
        return '이 공급자는 이미지 미지원'

    def save(self, data):
        saved = {"id": "providers"}
        current=self.settings()
        for key in DEFAULTS:
            value = data.get(key, {})
            effort=validate_reasoning(value.get('reasoning_effort',current[key]['reasoning_effort']))
            if key == "baseten":
                selected_model(key,value.get("model"))
                if value.get("base_url",DEFAULTS[key]["base_url"]) != DEFAULTS[key]["base_url"]:
                    raise ValueError("Baseten 주소는 고정되어 있습니다.")
                saved[key]={'reasoning_effort':effort}
                continue
            url = str(value.get("base_url", current[key]["base_url"])).rstrip("/")
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            allowed = host == "api.meta.ai" if key == "meta" else (host == "baseten.co" or host.endswith(".baseten.co"))
            if parsed.scheme != "https" or not allowed or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("해당 공급자의 공식 HTTPS 주소만 설정할 수 있습니다.")
            model = str(value.get("model", current[key]["model"])).strip()
            if not model or len(model) > 200:
                raise ValueError("모델 ID를 입력하세요.")
            saved[key] = {"base_url": url, "model": model,'reasoning_effort':effort}
        self.store.save("settings", saved)
        return self.settings()

    def client(self, provider):
        if provider == 'modal_llm':
            refresh_keys()
            token = os.environ.get('MODAL_PROXY_TOKEN', '')
            import re
            if not re.fullmatch(r'wk-[A-Za-z0-9_-]+\.ws-[A-Za-z0-9_-]+', token):
                raise ValueError('MODAL_PROXY_TOKEN이 없거나 형식이 올바르지 않습니다. 보안 입력 스크립트로 설정하세요.')
            from openai import OpenAI
            return OpenAI(base_url='https://inference.us-west.modal.direct/v1', api_key=token, max_retries=0, timeout=90)
        if provider not in DEFAULTS:
            raise ValueError("지원하지 않는 공급자입니다.")
        settings = self.settings()[provider]
        if not settings["ready"]:
            raise ValueError(settings["key_env"] + "가 없습니다. 보안 입력 스크립트로 설정하세요.")
        from openai import OpenAI
        return OpenAI(base_url=settings["base_url"], api_key=os.environ[settings["key_env"]], max_retries=0, timeout=90)

    def models(self, provider):
        if provider == "baseten":
            return [BASETEN_MODEL]
        with self.client(provider) as client:
            return sorted({model.id for model in client.models.list().data})

    def modal_models(self):
        with self.client('modal_llm') as client:
            return [{'id':model.id} for model in client.models.list().data]

    def completion(self, provider, messages, tools, model=None):
        selected=selected_model(provider,model) or self.settings()[provider]['model']
        effort=reasoning(provider,self.settings()[provider]['reasoning_effort'])
        limit=output_limit(provider,'Work',selected,reasoning_effort=effort)
        result=complete_continuing(lambda current,available:self.complete(provider,current,selected,available,limit,reasoning_effort=effort),messages,tools,limit)
        result._muse_reasoning_effort=effort
        return result

    def stream_chat(self, provider, messages, model=None):
        with self.client(provider) as client:
            options = {'model':selected_model(provider,model) or self.settings()[provider]['model'], 'messages':messages, 'stream':True}
            effort=reasoning(provider,self.settings()[provider]['reasoning_effort'])
            if effort is not None:options['reasoning_effort']=effort
            if provider == 'baseten':
                # Baseten documents max_tokens; max_completion_tokens may leave the 4096 default in force.
                options.update(max_tokens=output_limit(provider,'Chat',options['model'],reasoning_effort=effort),stream_options={'include_usage':True})
            else:
                options['max_completion_tokens']=output_limit(provider,'Chat',options['model'],reasoning_effort=effort)
            yield {'delta':'','finish_reason':None,'reasoning_effort':effort}
            with client.chat.completions.create(**options) as stream:
                for chunk in stream:
                    if chunk.choices:
                        choice=chunk.choices[0]
                        yield {"delta":choice.delta.content or "", "finish_reason":choice.finish_reason}
                    if getattr(chunk,'usage',None):
                        usage=usage_dict(chunk.usage)
                        if usage is not None:yield {'delta':'','finish_reason':None,'usage':usage}

    def complete(self, provider, messages, model=None, tools=None, max_tokens=None, timeout_seconds=None, reasoning_effort=None, role=None, tool_options=None):
        """Bounded non-streaming call for Goal roles and the local product proxy."""
        with self.client(provider) as client:
            if provider == 'modal_llm':
                from .muse_routes import ENDPOINT
                if not model or not ENDPOINT.fullmatch(model):
                    raise ValueError('Modal 엔드포인트 호스트 이름이 필요합니다.')
            kwargs = {'model':selected_model(provider, model) or self.settings()[provider]['model'], 'messages':messages}
            effort=reasoning(provider,reasoning_effort or ('high' if provider=='modal_llm' else self.settings()[provider]['reasoning_effort']))
            kwargs['max_tokens' if provider in ('baseten','modal_llm') else 'max_completion_tokens']=output_limit(provider,role or ('Planner' if provider in ('baseten','modal_llm') else 'Builder'),kwargs['model'],max_tokens,effort)
            if tools:
                kwargs['tools']=tools
                if role!='Product':kwargs['parallel_tool_calls']=False
            if tool_options is not None:
                # Product proxy validates these options; ordinary Goal defaults stay unchanged.
                if role!='Product' or set(tool_options)-{'tool_choice','parallel_tool_calls'}:raise ValueError('Invalid product tool options.')
                kwargs.update(tool_options)
            if effort is not None:
                kwargs['reasoning_effort'] = effort
            if timeout_seconds is not None:
                kwargs['timeout']=max(.1,min(90,float(timeout_seconds)))
            return client.chat.completions.create(**kwargs)

    def reference_search(self,query,model,max_tokens,reasoning_effort,timeout_seconds,instructions):
        """One stateless Meta Responses request; ordinary Chat Completions is unchanged."""
        with self.client('meta') as client:
            return client.responses.create(model=selected_model('meta',model),input=query,instructions=instructions,
                tools=[{'type':'web_search','search_context_size':'low'}],include=['web_search_call.results'],
                max_tool_calls=1,store=False,max_output_tokens=max_tokens,reasoning={'effort':reasoning_effort},
                timeout=max(.1,min(90,float(timeout_seconds))))

    def sam_segment(self,image,prompt,timeout_seconds=90):
        """Single SAM Responses attempt; the Goal caller owns retries and accounting."""
        with self.client('meta') as client:
            return client.responses.create(model='sam-3.1',store=False,stream=False,
                input=[{'role':'user','content':[{'type':'input_text','text':prompt},{'type':'input_image','image_url':image}]}],
                metadata={'include_confidence':'true','mask_encoding':'one_bit'},timeout=max(.1,min(90,float(timeout_seconds))))


def error_message(error):
    if type(error).__name__ == "VaultError":
        return error.message
    status = getattr(error, "status_code", None)
    if status:
        return f"API HTTP {status}. 모델 ID, 권한 및 한도를 확인하세요. 자동 재시도는 하지 않습니다."
    if isinstance(error, (ValueError, PermissionError, FileNotFoundError)):
        return str(error)
    return f"{type(error).__name__}: 실행하지 못했습니다. 키 보호를 위해 원문 예외는 숨겼습니다."
