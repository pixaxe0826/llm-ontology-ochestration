"""One credential-free provider/model catalog. Registration never accepts a key value."""
import copy,ipaddress,json,os,re,threading,time
from pathlib import Path
from urllib.parse import urlsplit
from .security import contains_key,refresh_keys
from .generation import settings as generation_settings,REASONING_LEVELS
from .goal_costs import number

ROLES=('Planner','Builder','ImageAnalysis','ReferenceSearch')
CAPS=('tool_calling','image_input','reasoning_content','web_search','structured_json')
ID=re.compile(r'[a-z][a-z0-9_]{0,39}')
FALLBACK_PRICE={'input':20.0,'cached_input':20.0,'output':100.0}


def credential_ref(identifier):
    return 'OPENAI_API_KEY' if identifier=='openai' else 'MUSE_PROVIDER_'+identifier.upper()+'_API_KEY'


def endpoint(value,allow_lan=False):
    if not isinstance(value,str) or len(value)>1500 or contains_key(value) or any(ord(c)<=32 for c in value):raise ValueError('Use a non-secret API base URL.')
    parsed=urlsplit(value);host=(parsed.hostname or '').lower()
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment or '..' in parsed.path.split('/') or '\\' in value:raise ValueError('Base URL cannot contain credentials, query strings or traversal.')
    if parsed.scheme not in ('http','https'):raise ValueError('Provider URL must use HTTP(S).')
    try:address=ipaddress.ip_address(host)
    except ValueError:address=None
    local=host in ('localhost','127.0.0.1','::1') or bool(address and address.is_loopback)
    if address and not local and (address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved):raise ValueError('Metadata, link-local and special addresses are forbidden.')
    if host in ('metadata.google.internal','metadata','instance-data') or host.endswith('.internal'):raise ValueError('Metadata addresses are forbidden.')
    if address and not address.is_global and not local and not allow_lan:raise ValueError('Enable the explicit LAN option for a private provider address.')
    if parsed.scheme!='https' and not local and not (allow_lan and address and address.is_private):raise ValueError('Public provider URLs require HTTPS. HTTP is limited to loopback or an explicitly configured LAN IP.')
    try:parsed.port
    except ValueError:raise ValueError('Invalid provider port.') from None
    return value.rstrip('/')


def positive(value,label,maximum=10_000_000,optional=False):
    if optional and value in ('',None):return None
    if type(value) is not int or not 1<=value<=maximum:raise ValueError(label+' must be a positive bounded integer.')
    return value


def capabilities(value):
    if not isinstance(value,dict) or set(value)-set(CAPS)-{'reasoning_efforts','max_context','max_output'}:raise ValueError('Invalid model capability fields.')
    result={key:value.get(key,False) for key in CAPS}
    if any(type(v) is not bool for v in result.values()):raise ValueError('Capabilities must be booleans.')
    efforts=value.get('reasoning_efforts',[])
    if not isinstance(efforts,list) or len(set(efforts))!=len(efforts) or any(e not in REASONING_LEVELS+('none',) for e in efforts):raise ValueError('Invalid registered reasoning levels.')
    result.update(reasoning_efforts=efforts,max_context=positive(value.get('max_context'),'Max context',optional=True),max_output=positive(value.get('max_output',4096),'Max output'))
    if result['max_context'] is not None and result['max_output']>result['max_context']:raise ValueError('Max output cannot exceed max context.')
    return result


def prices(value):
    if value is None:return None
    if not isinstance(value,dict) or set(value)-{'input','cached_input','output','web_search'} or not {'input','output'}<=set(value):raise ValueError('Prices require input/output USD per million tokens.')
    return {key:number(value.get(key,value['input'] if key=='cached_input' else 0),'Model price') for key in ('input','cached_input','output','web_search')}


def eligible(model,role):
    caps=model['capabilities']
    if role in ('Planner','Builder'):return caps['tool_calling'] or caps['structured_json']
    return caps['image_input'] if role=='ImageAnalysis' else caps['web_search'] if role=='ReferenceSearch' else False


class ProviderRegistry:
    def __init__(self,store):self.store=store;self.lock=threading.RLock()

    def builtins(self):
        config=generation_settings();caps=config.get('capabilities',{})
        rates=json.loads(Path(__file__).with_name('goal_prices.json').read_text(encoding='utf-8'))
        def model(identifier,provider,**updates):
            c={'tool_calling':True,'image_input':True,'reasoning_content':True,'web_search':provider=='meta','structured_json':True,
               'reasoning_efforts':list(REASONING_LEVELS),'max_context':None,'max_output':config['model_caps'].get(identifier,config['unknown_model_cap'])}
            c.update(updates);rate=rates['tokens_per_million']['meta_contributor' if provider=='meta' and 'contributor' in identifier else provider]
            verified=identifier in ('muse-spark-1.3','meta-models/muse-spark-open-1026')
            return {'id':identifier,'capabilities':c,'prices':{**rate,'web_search':rates['web_search']['meta_usd_per_query'] if provider=='meta' else 0},
                'verification':{'source':'existing platform probes' if verified else 'legacy work-model configuration; not individually probed',
                                'tool_calling':'verified' if verified and c['tool_calling'] else 'NOT VERIFIED',
                                'image_input':'verified' if verified and c['image_input'] else 'NOT VERIFIED','max_context':'NOT VERIFIED','max_output':'configured request cap; not advertised maximum'}}
        meta_models=['muse-spark-1.3','muse-spark-1.3-contributor','muse-spark-1.2','muse-spark-1.2-contributor','muse-spark-1.1']
        try:vision=self.store.get('settings','attachment_capabilities')
        except ValueError:vision={}
        result={
            'meta':{'id':'meta','name':'Meta API','api_style':'chat_completions','base_url':'https://api.meta.ai/v1','credential_ref':'MODEL_API_KEY','output_parameter':'max_completion_tokens',
                'models':[model(i,'meta') for i in meta_models],'budget':rates['limits']['meta']},
            'baseten':{'id':'baseten','name':'Baseten','api_style':'chat_completions','base_url':'https://inference.baseten.co/v1','credential_ref':'BASETEN_API_KEY','output_parameter':'max_tokens',
                'models':[model('meta-models/muse-spark-open-1026','baseten',tool_calling=bool(caps.get('baseten_tools')),image_input=vision.get('baseten',True),web_search=False,
                    reasoning_efforts=list(REASONING_LEVELS) if caps.get('baseten_reasoning') else [])],'budget':rates['limits']['baseten']},
            'modal_llm':{'id':'modal_llm','name':'Modal LLM','api_style':'chat_completions','base_url':'https://inference.us-west.modal.direct/v1','credential_ref':'MODAL_PROXY_TOKEN','output_parameter':'max_tokens',
                'models':[],'budget':rates['limits']['modal'],'budget_provider':'modal'}}
        try:catalog=self.store.get('settings','muse_open_catalog')
        except ValueError:catalog={}
        for item in catalog.get('endpoints',[]):
            identifier=item.get('id')
            if identifier:
                row=model(identifier,'modal_llm',tool_calling=bool(caps.get('modal_llm_tools')),structured_json=False,image_input=False,web_search=False,
                    reasoning_efforts=list(REASONING_LEVELS) if caps.get('modal_llm_reasoning') else [])
                row['verification']={'source':'authenticated endpoint discovery','capabilities':'NOT VERIFIED; declare or probe before assigning a role'}
                result['modal_llm']['models'].append(row)
        for item in result.values():item.update(locked=True,allow_lan=False,limits={'concurrency':2,'requests_per_minute':None},search_include='web_search_call.results' if item['id']=='meta' else 'web_search_call.action.sources')
        return result

    def all(self):
        result=self.builtins()
        try:saved=self.store.get('settings','provider_registry').get('providers',{})
        except ValueError:saved={}
        for identifier,row in saved.items():result[identifier]=copy.deepcopy(row)
        return result

    def get(self,identifier):
        if identifier not in self.all():raise ValueError('Provider is not registered: '+str(identifier))
        return self.all()[identifier]

    def model(self,identifier,model):
        provider=self.get(identifier);match=next((m for m in provider['models'] if m['id']==model),None)
        if match is None:raise ValueError('Model is not registered for '+identifier+': '+str(model))
        return provider,match

    def validate_route(self,value,role,optional=False):
        if value is None and optional:return None
        if not isinstance(value,dict) or set(value)-{'provider','model','reasoning_effort'}:raise ValueError('A role route requires registered provider/model and optional reasoning_effort.')
        provider,model=self.model(value.get('provider'),value.get('model'))
        if not eligible(model,role):raise ValueError(role+' requires '+('image input' if role=='ImageAnalysis' else 'built-in web search' if role=='ReferenceSearch' else 'tool calling or structured JSON')+'; selected model does not support it.')
        levels=model['capabilities']['reasoning_efforts'];effort=value.get('reasoning_effort','high' if 'high' in levels else levels[0] if levels else None)
        if effort is not None and effort not in REASONING_LEVELS+('none',):raise ValueError('Invalid reasoning effort.')
        if levels and effort not in levels:raise ValueError('Unsupported reasoning effort for this model; allowed: '+', '.join(levels))
        return {'provider':provider['id'],'model':model['id'],'reasoning_effort':effort if levels else None}

    def public(self):
        # Catalog reads must not mutate the credential environment during a model/review call.
        rows=[]
        for row in self.all().values():
            value=copy.deepcopy(row);value['ready']=bool(os.environ.get(row['credential_ref']));value['budget_provider']=row.get('budget_provider',row['id'])
            for model in value['models']:
                model['roles']=[role for role in ROLES if eligible(model,role)]
                model['price_estimated']=model.get('prices') is None
                model['effective_prices']=model.get('prices') or {**FALLBACK_PRICE,'web_search':.05}
                if model['price_estimated']:model['warning']='Price not registered; conservative estimate is used.'
            rows.append(value)
        return {'providers':rows,'roles':list(ROLES),'templates':[self.openai_template()]}

    @staticmethod
    def openai_template():
        return {'id':'openai','name':'OpenAI','api_style':'responses','base_url':'https://api.openai.com/v1','credential_ref':'OPENAI_API_KEY',
            'models':[],'output_parameter':'max_completion_tokens','allow_lan':False,'budget':{'total':100,'daily':None},'limits':{'concurrency':2,'requests_per_minute':None}}

    def save(self,data):
        fields={'id','name','api_style','base_url','credential_ref','models','output_parameter','allow_lan','budget','limits','search_include'}
        if not isinstance(data,dict) or set(data)-fields or contains_key(json.dumps(data,ensure_ascii=False)):raise ValueError('Provider registration accepts metadata only. Enter keys with the secure local script.')
        identifier=data.get('id')
        if not isinstance(identifier,str) or not ID.fullmatch(identifier) or identifier in ('modal',):raise ValueError('Use a provider id with lowercase letters, digits and underscore.')
        base=self.builtins().get(identifier);current=self.all().get(identifier,{})
        if current:
            active=any(r.get('goal_id') and r.get('provider')==identifier and r.get('status') in ('running','queued','waiting_approval') for r in self.store.items('task',None))
            active=active or any(r.get('provider')==identifier and r.get('state')=='running' for r in self.store.items('goal_model_attempt',None))
            if active:raise ValueError('Provider is in use by an active chunk/request. Wait for that boundary before changing its registration.')
        name=data.get('name',current.get('name',identifier))
        if not isinstance(name,str) or not 1<=len(name.strip())<=80:raise ValueError('Provider name must be 1–80 characters.')
        style=data.get('api_style',current.get('api_style','chat_completions'))
        if style not in ('chat_completions','responses'):raise ValueError('Select a registered adapter: chat_completions or responses.')
        lan=data.get('allow_lan',False)
        if type(lan) is not bool:raise ValueError('LAN option must be a boolean.')
        url=endpoint(data.get('base_url',current.get('base_url')),lan)
        ref=data.get('credential_ref',base['credential_ref'] if base else credential_ref(identifier))
        if ref!=(base['credential_ref'] if base else credential_ref(identifier)):raise ValueError('Credential references are scoped to this provider; keys from other providers cannot be reused.')
        if base and (url!=base['base_url'] or style!=base['api_style']):raise ValueError('Built-in provider base URL and API style are fixed.')
        models=data.get('models',current.get('models',[]))
        if not isinstance(models,list) or not 1<=len(models)<=100:raise ValueError('Register 1–100 models.')
        parsed=[]
        for raw in models:
            if not isinstance(raw,dict) or set(raw)-{'id','capabilities','prices'}:raise ValueError('Model registration requires id, capabilities and prices.')
            mid=raw.get('id')
            if not isinstance(mid,str) or not 1<=len(mid)<=200 or any(ord(c)<=32 for c in mid) or mid in [r['id'] for r in parsed]:raise ValueError('Model IDs must be nonempty and unique.')
            if identifier=='baseten' and mid!='meta-models/muse-spark-open-1026':raise ValueError('Baseten model is fixed to meta-models/muse-spark-open-1026.')
            if identifier=='modal_llm':
                from .muse_routes import ENDPOINT
                if not ENDPOINT.fullmatch(mid):raise ValueError('Use a discovered Modal endpoint hostname.')
                try:known=self.store.get('settings','muse_open_catalog')['endpoints']
                except ValueError:known=[]
                if mid not in {r['id'] for r in known}:raise ValueError('Discover the Modal endpoint with the registered token first.')
            parsed.append({'id':mid,'capabilities':capabilities(raw.get('capabilities',{})),'prices':prices(raw.get('prices')),
                'verification':{'source':'user registration','capabilities':'declared; not proven by a text connection test'}})
        limits=data.get('limits',current.get('limits',{}))
        if not isinstance(limits,dict) or set(limits)-{'concurrency','requests_per_minute'}:raise ValueError('Invalid provider rate limits.')
        limits={'concurrency':positive(limits.get('concurrency',2),'Concurrency',32),'requests_per_minute':positive(limits.get('requests_per_minute'),'Requests per minute',100000,True)}
        budget=data.get('budget',current.get('budget',{'total':100,'daily':None}))
        if not isinstance(budget,dict) or set(budget)-{'total','daily','reserve','llm'}:raise ValueError('Invalid provider budget.')
        budget={key:(None if v in ('',None) and key=='daily' else number(v,'Provider budget')) for key,v in budget.items()}
        budget.setdefault('total',100);budget.setdefault('daily',None)
        if identifier=='baseten':
            budget.setdefault('reserve',45)
            if budget['reserve']>budget['total']:raise ValueError('Product reserve exceeds the total budget.')
        if identifier=='modal_llm':budget.setdefault('llm',30)
        output=data.get('output_parameter',current.get('output_parameter','max_completion_tokens'))
        if output not in ('max_completion_tokens','max_tokens'):raise ValueError('Invalid Chat Completions output parameter.')
        search_include=data.get('search_include',current.get('search_include','web_search_call.action.sources'))
        if search_include not in ('web_search_call.results','web_search_call.action.sources'):raise ValueError('Invalid web search result format.')
        row={'id':identifier,'name':name.strip(),'api_style':style,'base_url':url,'credential_ref':ref,'models':parsed,'allow_lan':lan,
             'limits':limits,'budget':budget,'output_parameter':output,'search_include':search_include,'locked':bool(base),'revision':time.time()}
        if identifier=='modal_llm':row['budget_provider']='modal'
        with self.lock:
            try:saved=self.store.get('settings','provider_registry')
            except ValueError:saved={'id':'provider_registry','providers':{}}
            saved['providers'][identifier]=row;self.store.save('settings',saved)
        return self.public()

    def rate(self,provider,model):
        try:_,value=self.model('modal_llm' if provider=='modal' else provider,model)
        except ValueError:return None
        return value.get('prices') or {**FALLBACK_PRICE,'web_search':.05}

    def budget_defaults(self):
        return {p.get('budget_provider',p['id']):copy.deepcopy(p['budget']) for p in self.all().values()}
