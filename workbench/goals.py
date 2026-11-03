"""One coordinator per Goal. Existing Agents execute through one global Builder gate."""
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import threading
import time
import uuid

from .agent import Agents, tool, S
from .goal_costs import Ledger, GoalLimit
from .goal_policy import policy, command_decision
from .goal_prompts import system, brief, PLAN_SCHEMA, REVIEW_SCHEMA
from .goal_time import deadline, stamp, zone
from .generation import output_limit, complete_continuing, validate_reasoning, reasoning, settings as generation_settings
from .providers import BASETEN_MODEL, error_message, selected_model
from .muse_routes import MuseRoutes
from .product_proxy import resolve_model, product_output
from .product_tools import request_tools, assistant_message
from .product_images import validate_messages, VISION_INPUT_RESERVE
from .goal_history import assert_unarchived
from .execution_index import ExecutionIndex
from .plan_capacity import GoalPlanCapacity,plan_limit,PlanCapacityError,DEFAULT as DEFAULT_PLAN_LIMIT
from .human_briefs import HumanBriefs,display_language,note_summary,validate_brief
from .goal_inbox import GoalInbox
from .goal_models import validate_builder_model, DEFAULT_BUILDER_MODEL
from .diagnostics import log_exception, exception_summary
from .response_data import usage_dict
from .goal_titles import validate_title, suggested_title
from .goal_settings import GoalSettings
from .goal_reviews import GoalReviews
from .plan_updates import plan_key,prepare_plan,initial_items
from .goal_files import GoalFiles,file_tools
from .goal_decisions import GoalDecisions,DECISION_PARAMETERS
from .goal_scheduler import GoalScheduler
from .document_cycles import DocumentCycles
from .file_views import tool_text,observation_text,planner_tool_view
from .security import clean_data, contains_key, redact, register_secret
from .workspace import Workspace
from .command_cwd import MISSING,normalize_cwd,with_cwd_notice
from .goal_gpu import submission_status,check_submission,copy_artifact
from .builder_failures import recover_failed_chunk
from .builder_restarts import GoalBuilderRestarts,last_chunk,TERMINATED,paused_cancellation
from .builder_errors import escalate_failure
from .acceptance_rules import has_documents
from .document_evidence import goal_sources
from .review_evidence import packet
from .acceptance_rules import needs_execution
from .goal_references import GoalReferences,TOOLS as REFERENCE_TOOLS,NAMES as REFERENCE_NAMES,reference_limits
from .goal_operations import GoalOperations,unattended,gpu_cycle_limit
from .model_retry import failure_usage,retryable
from .reference_pages import GoalReferencePages,page_limits
from .sam_proxy import SAMProxy
from .long_commands import LongCommands,TOOLS as LONG_TOOLS,NAMES as LONG_NAMES
from .git_checkpoint import GoalCommits,enabled as auto_commit_enabled
from .builder_productivity import GoalProductivity,read_limits
from .code_map import CodeMap
from .item_states import terminal
from .goal_routing import GoalRouting
from .provider_registry import ProviderRegistry

LIVE = {'planning','running','waiting_approval','waiting_decision'}
TASK_LIVE = {'running','queued','waiting_approval'}
MEMORY_TOOLS = [
    tool('memory_search','Search source-grounded shared memory.',{'query':S},['query']),
    tool('memory_read','Read a shared memory source by source_id.',{'source_id':S},['source_id']),
    tool('memory_note','Record a decision, result or failure in output_language.',{'title':S,'text':S,'concepts':{'type':'array','items':S}},['title','text','concepts'])]
QUESTION_TOOL = tool('ask_planner','Ask the Planner about an ambiguity; escalate only when a human decision is needed.',{'question':S},['question'])
DECISION_TOOL = tool('request_decision','Persist a human checkpoint/question and its affected existing plan items. Independent work continues; this is not an approval bypass.',DECISION_PARAMETERS,['question','options','recommendation','reason','affected_items','reversible'])
REPORT_TOOL = tool('report_item','End this Builder chunk with progress or completed. completed means all assigned acceptance work is finished; only the Reviewer can accept evidence. Use progress when more implementation/testing remains.',
    {'status':{'type':'string','enum':['progress','completed']},'summary':S,'verification':S},['status','summary','verification'])
COPY_GPU_TOOL=tool('copy_gpu_artifact','Recover a downloaded GPU artifact from this item, its parent, children or siblings in the SAME Goal. Use for PNG/GLB. Equal destination bytes produce verified_present evidence without copying or requiring expected_sha256; replacing DIFFERENT bytes requires current expected_sha256. Proof includes submitting/copying item IDs and both hashes. No GPU execution, shell commands or provider credentials.',
    {'job_id':S,'path':S,'destination':S,'expected_sha256':S},['job_id','path','destination'])
PLANNER_TOOLS=MEMORY_TOOLS+[
    tool('model_routes','List registered provider/model capabilities and prices for role routing; no credentials or settings changes.',{'provider':S},[]),
    tool('read_file','Read workspace evidence only; optional 1-based inclusive start_line/end_line narrows large files. Legacy line_count is also accepted.',{'path':S,'start_line':{'type':'integer'},'end_line':{'type':'integer'},'line_count':{'type':'integer'}},['path']),
    tool('search_files','Search workspace evidence without modifying files.',{'query':S,'path':S},['query']),DECISION_TOOL]
PLANNER_ALLOWED={t['function']['name'] for t in PLANNER_TOOLS}


def dumped(value):
    return json.dumps(clean_data(value), ensure_ascii=False, sort_keys=True)


def usage_of(response):
    return usage_dict(getattr(response,'usage',None))


class GoalWorkspace(Workspace):
    def __init__(self, manager, task):
        super().__init__(task['workspace'],manager.store.root)
        self.manager, self.task = manager, task

    def write(self,path,content,expected_sha256=None,**kwargs):
        self.manager.provisional_guard(self.task,'write_file',{'path':path})
        result=super().write(path,content,expected_sha256,full_diff=True)
        self.manager.provisional_file(self.task,path)
        item=self.manager.store.get('plan_item',self.task['plan_item_id'])
        if item.get('provisional_decisions'):result.update(provisional=True,decision_ids=item['provisional_decisions'])
        return result

    def run(self,argv,cwd=MISSING,timeout=120,cancel=None,**kwargs):
        cwd,notice=normalize_cwd(cwd)
        self.manager.provisional_guard(self.task,'run_command',{'argv':argv})
        # Repeat the hard checks after human approval and immediately before execution.
        checked = list(argv)
        if checked and checked[0] == sys.executable:
            checked[0] = 'python'
        goal = self.manager.store.get('goal',self.task['goal_id'])
        auto=command_decision(str(self.root),self.state,checked,cwd,goal['policy'])
        if getattr(self.manager.builders.local,'command_auto',False) and not auto:
            raise PermissionError('Automatic policy changed before execution; request approval again.')
        item=self.manager.store.get('plan_item',self.task['plan_item_id'])
        return with_cwd_notice(super().run(argv,cwd,timeout,cancel,extra_env=None if item.get('provisional_decisions') else self.manager.proxy_environment(goal['id']),full_output=True),notice)


class BuilderProviders:
    def __init__(self,manager):
        self.manager = manager
        self.local = threading.local()

    def settings(self):
        result=dict(self.manager.providers.settings())
        for provider in self.manager.model_registry.public()['providers']:
            result.setdefault(provider['id'],{'ready':provider['ready'],'model':provider['models'][0]['id'] if provider['models'] else ''})
        return result

    def completion(self,provider,messages,tools,model=None):
        task = self.manager.store.get('task',self.local.task_id)
        goal,item = self.manager.builder_item_guard(task)
        prefix = self.manager.context(goal,item,'Builder')
        # Keep complete tool-call groups, never orphan a tool response when trimming history.
        recent = messages[1:]
        groups=[]
        for message in recent:
            if message['role']!='tool':
                groups.append([])
            if not groups:continue
            value=dict(message)
            if isinstance(value.get('content'),str):
                value['content']=tool_text(value['content']) if value['role']=='tool' else value['content'][-8000:]
            groups[-1].append(value)
        tail=[message for group in groups[-4:] for message in group]
        available=file_tools(tools)+MEMORY_TOOLS+[QUESTION_TOOL,DECISION_TOOL,REPORT_TOOL,COPY_GPU_TOOL]+REFERENCE_TOOLS+LONG_TOOLS
        if item.get('provisional_decisions'):
            available=[t for t in available if t['function']['name'] in ('read_file','read_structured','list_files','search_files','code_map','file_hash','edit_file','write_file','run_command','memory_search','memory_read','ask_planner','request_decision','report_item','check_long_command','cancel_long_command')]
        available=[t for t in available if not (t['function']['name']=='analyze_images' and self.manager.effective_routes(goal).get('ImageAnalysis') is None) and not (t['function']['name']=='reference_search' and self.manager.effective_routes(goal).get('ReferenceSearch') is None)]
        return self.manager.call(goal,task.get('provider','meta'),'development','Builder',prefix+tail,available,task.get('model',goal['builder_model']),task_id=task['id'],role_route=task.get('model_route'))


class GoalAgents(Agents):
    def __init__(self,manager):
        self.manager = manager
        super().__init__(manager.store,BuilderProviders(manager),manager.gpu,manager.memory)
        self.local = threading.local()

    def _start(self,root,prompt,provider='meta',model=None,allow_write=True,max_steps=16,task_id=None,mode='work',use_memory=True,share_memory=True,context=None,attachments=None,conversation_id=None):
        if task_id:
            previous=self.store.get('task',task_id)
            if context and (previous.get('goal_id')!=context.get('goal_id') or previous.get('plan_item_id')!=context.get('plan_item_id')):raise ValueError('Goal continuation must belong to the same Goal item.')
            if previous['status'] in TERMINATED or (self.cancels.get(task_id) and self.cancels[task_id].is_set()):task_id=None
            elif previous['status'] in ('succeeded','paused') and previous.get('error'):
                self.store.event(task_id,'builder_continuation',previous_status=previous['status'],previous_error=redact(previous['error']))
                self.store.update('task',task_id,error=None)
        return super()._start(root,prompt,provider,model,allow_write,max_steps,task_id,mode,use_memory,share_memory,context,attachments,conversation_id)

    def _run(self,task_id):
        self.local.held=False
        self.local.chunk_id=uuid.uuid4().hex[:16]
        self.local.summary_saved=False
        self.providers.local.task_id=task_id
        self.store.update('task',task_id,status='queued',human_interrupt_ids=[],human_interrupt_chunk=None)
        self.store.event(task_id,'chunk_started',chunk_id=self.local.chunk_id,attempt_charged=bool(self.store.get('task',task_id).get('attempt_charged')))
        retry=self.store.get('task',task_id).get('restart_retry_audit_id')
        if retry:
            audit=self.store.get('goal_restart_retry',retry)
            if not audit.get('retry_chunk_id'):self.store.update('goal_restart_retry',retry,retry_task_id=task_id,retry_chunk_id=self.local.chunk_id,state='dispatched')
        try:
            self.acquire(task_id)
            super()._run(task_id)
        except GoalLimit as error:
            self.store.update('task',task_id,status='cancelled',error=str(error),approval=None)
        finally:
            if self.local.held:
                self.manager.builder_gate.release()
                self.local.held=False
            if not self.local.summary_saved:self.task_finished(task_id)
            super().release_active(task_id)

    def release_active(self,task_id):
        # Base Agents has finished, but the Goal wrapper still owns the Builder gate.
        # Publish inactivity only after releasing that gate, before any same-task reuse.
        pass

    def task_finished(self,task_id):
        self.local.summary_saved=True
        try:self.manager.record_chunk_summary(task_id,self.local.chunk_id)
        except Exception as error:log_exception(self.store.root,error,component='Builder context',task_id=task_id)

    def boundary_stop(self,task_id):
        return self.manager.interrupt_boundary(task_id,self.local.chunk_id)

    def finish_batch(self,task_id):
        report=self.store.get('task',task_id).get('builder_report') or {}
        if report.get('chunk_id')!=self.local.chunk_id:return False
        self.store.update('task',task_id,status='succeeded',finished=time.time())
        return True

    def acquire(self,task_id):
        task=self.store.get('task',task_id)
        while True:
            if self.boundary_stop(task_id):return
            self.manager.guard(task['goal_id'],task['generation'])
            if self.cancels[task_id].is_set():raise GoalLimit('Builder was stopped.')
            with self.manager.lock:allowed=self.manager.builder_turn_allowed(task)
            if not allowed:
                self.cancels[task_id].wait(.05);continue
            if not self.manager.builder_gate.acquire(timeout=.2):continue
            self.local.held=True
            with self.manager.lock:
                if not self.manager.builder_turn_allowed(task):
                    self.manager.builder_gate.release();self.local.held=False;continue
                self.manager.mark_builder_running(task)
                self.store.update('task',task_id,status='running')
                return

    def workspace(self,task):
        return GoalWorkspace(self.manager,task)

    def model_tool_text(self,text):
        return tool_text(text)

    def approval(self,task_id,kind,payload):
        with self.manager.lock:
            task=self.store.get('task',task_id)
            goal=self.manager.guard(task['goal_id'],task['generation'])
            verdict=self.manager.approval_assessment(goal,kind,payload)
            auto=verdict['auto']
            if not auto:
                event=threading.Event();slot={'event':event,'approved':False}
                request_id=uuid.uuid4().hex[:16];self.approvals[request_id]=slot
                request=self.store.save('goal_approval',{'id':request_id,'goal_id':goal['id'],'plan_item_id':task['plan_item_id'],'task_id':task_id,
                    'kind':kind,'payload':payload,'state':'pending','auto_approval_reason':verdict['reason'],'created':time.time(),**self.manager.times(goal)})
                decision=self.manager.create_decision(goal,{'question':f'Approve {kind} for '+self.store.get('plan_item',task['plan_item_id'])['title']+'?',
                    'options':['Approve','Deny'],'recommendation':'','reason':verdict['reason'],'reversible':False},
                    item_id=task['plan_item_id'],source='approval:'+request_id,approval_id=request_id)
                self.store.update('goal_approval',request_id,decision_id=decision['id'])
                self.store.update('task',task_id,status='waiting_approval',approval={'id':request['id'],'kind':kind,'payload':payload})
                self.manager.park_item(self.store.get('plan_item',task['plan_item_id']),'approval',resume='active_task')
                self.manager.message(goal,'Builder','Human','ESCALATE',f'Approval required for {kind}.',task['plan_item_id'],[request['id']])
                self.manager.builder_gate.release();self.local.held=False
        if not auto:
            interrupted=False
            try:
                while not event.wait(.2):
                    if self.manager.interrupt_pending(goal['id']):
                        interrupted=True;self.store.update('goal_approval',request['id'],state='interrupted');break
                    self.manager.guard(goal['id'],task['generation'])
                    if self.cancels[task_id].is_set():raise GoalLimit('Builder was stopped.')
                approved=slot['approved']
            finally:
                self.approvals.pop(request['id'],None)
            if interrupted:return False
            self.store.update('task',task_id,status='queued',approval=None)
            with self.manager.lock:self.manager.park_item(self.store.get('plan_item',task['plan_item_id']),'builder_queue',resume='active_task')
            self.acquire(task_id)
            if not approved:return False
        goal=self.manager.guard(goal['id'],task['generation'])
        automatic=auto or self.store.get('goal_approval',request['id']).get('automatic',False)
        if kind=='command':self.local.command_auto=automatic
        if kind=='gpu':
            if automatic and not self.manager.approval_assessment(goal,kind,payload)['auto']:
                raise PermissionError('Automatic GPU policy changed before execution; request approval again.')
            self.local.gpu_reservation=self.manager.reserve(goal,'modal','development',self.manager.gpu_estimate(payload['spec']),role='Executor',gpu=payload['spec']['gpu'])
        return True

    def execute(self,task_id,name,args):
        task=self.store.get('task',task_id)
        self.manager.guard(task['goal_id'],task['generation'])
        self.manager.provisional_guard(task,name,args)
        chunk_id=getattr(self.local,'chunk_id',task_id)
        blocked=self.manager.before_builder_tool(task,name,chunk_id)
        if blocked is not None:return blocked
        result=self.execute_observed(task_id,name,args)
        return self.manager.after_builder_tool(task,name,result,chunk_id)

    def execute_observed(self,task_id,name,args):
        task=self.store.get('task',task_id)
        goal=self.manager.guard(task['goal_id'],task['generation'])
        self.manager.provisional_guard(task,name,args)
        self.manager.document_cycle_guard(task,name,args)
        if name in LONG_NAMES:return self.manager.long_commands.tool(task,name,args)
        if name in REFERENCE_NAMES:return self.manager.reference_tool(task,name,args)
        if name=='edit_file':return self.manager.edit_tool(task,args,getattr(self.local,'chunk_id',task_id))
        if name=='code_map':return self.manager.code_map_tool(task,args)
        if name=='file_hash':return self.manager.file_hash_tool(task,args)
        if name in ('read_file','read_structured'):
            return self.manager.read_tool(task,name,args,getattr(self.local,'chunk_id',task_id))
        if name.startswith('memory_'):
            return self.manager.memory_tool(goal,'Builder',name,args)
        if name=='ask_planner':
            return self.manager.question(goal,task['plan_item_id'],args['question'])
        if name=='request_decision':
            return self.manager.request_decision(goal,self.store.get('plan_item',task['plan_item_id']),args,'Builder')
        if name=='report_item':
            return self.manager.record_builder_report(task_id,args,self.local.chunk_id)
        if name=='copy_gpu_artifact':return copy_artifact(self.manager,task,args)
        if name=='read_gpu_artifact':
            from .goal_gpu import read_artifact
            return read_artifact(self.manager,task,args)
        if name=='submit_gpu_job':
            check_submission(self.manager,goal,self.store.get('plan_item',task['plan_item_id']))
            self.local.gpu_reservation=None
            try:
                result=super().execute(task_id,name,args)
                reservation=self.local.gpu_reservation
                if reservation:
                    actual=(result.get('result') or {}).get('summary',{}).get('duration_s')
                    rate=self.manager.ledger.prices['gpu_hourly'][reservation['gpu']]
                    self.manager.ledger.settle(reservation,actual_cost=float(actual)*rate/3600 if actual is not None else None,
                        gpu_job_id=result.get('id'),runtime_s=actual)
                    self.local.gpu_reservation=None
                return result
            finally:
                if self.local.gpu_reservation:
                    self.manager.ledger.settle(self.local.gpu_reservation,error='Execution outcome unavailable; reserved estimate charged.')
                    self.local.gpu_reservation=None
        return super().execute(task_id,name,args)


class GoalManager(GoalRouting,GoalBuilderRestarts,GoalSettings,GoalReviews,GoalFiles,GoalDecisions,GoalScheduler,DocumentCycles,GoalReferences,GoalOperations,GoalReferencePages,SAMProxy,GoalCommits,GoalProductivity,GoalPlanCapacity,HumanBriefs,GoalInbox):
    def __init__(self,store,providers,gpu,memory,prices=None):
        self.store,self.providers,self.gpu,self.memory=store,providers,gpu,memory
        self.model_registry=getattr(providers,'registry',None) or ProviderRegistry(store)
        self.ledger=Ledger(store,prices,self.model_registry)
        self.code_mapper=CodeMap(store)
        self.muse_routes=MuseRoutes(store,providers,self.ledger)
        self.bind_legacy_routes()
        self.builder_gate=threading.Lock()
        self.review_scope=threading.local()
        self.lock=threading.RLock()
        self.runtime_lock=threading.RLock();self.model_active={};self.retry_sleep=time.sleep;self.notifier=None
        self.operations_stopping=threading.Event()
        self.threads,self.signals,self.proxy_tokens={},{},{}
        self.decision_events={}
        self.proxy_base='http://127.0.0.1:8765/proxy/v1'
        self.builders=GoalAgents(self)
        for goal in self.store.items('goal',None):
            if not goal.get('title'):
                goal=self.store.save('goal',{**goal,'title':suggested_title(goal['objective'])},preserve_updated=True)
            if goal['status'] in LIVE:
                self.remember_restart(goal)
                self.save(goal,status='interrupted',reason='Server restarted. Resume explicitly to continue.')
        for row in self.store.items('ledger',None):
            if row['state']=='reserved':
                self.ledger.settle(row,error='Server restarted before usage was confirmed; conservative estimate retained.')
        for request in self.store.items('goal_approval',None):
            if request['state']=='pending':self.store.update('goal_approval',request['id'],state='interrupted')
        for request in self.store.items('goal_reference_page',None):
            if request['state']=='fetching':self.store.update('goal_reference_page',request['id'],state='interrupted')
        self.long_commands=LongCommands(self)
        self.executions=ExecutionIndex(self.store)
        for record in self.store.items('goal_commit',None):
            if record['state']=='running':
                warning='Server restarted during checkpoint; inspect Git before any manual retry.'
                self.store.update('goal_commit',record['id'],state='warning',warning=warning)
                try:
                    self.store.update('plan_item',record['plan_item_id'],last_commit_state='warning',commit_warning=warning)
                    self.message(self.store.get('goal',record['goal_id']),'System','Planner','NOTE',warning,record['plan_item_id'],[record['id']])
                except ValueError:pass

        self.backfill_human_briefs()
        self.recover_directives()

    def times(self,goal,epoch=None):
        value=stamp(time.time() if epoch is None else epoch,goal['timezone'])
        return {'time_utc':value['utc'],'time_local':value['local'],'timezone':value['timezone']}

    def save(self,goal,**values):
        if 'status' in values and values['status']!=self.store.get('goal',goal['id'])['status']:
            values.setdefault('status_since',time.time())
            if values['status']=='paused':values.setdefault('pause_origin','system')
        return self.store.update('goal',goal['id'],**{**values,**self.times({**goal,**values})})

    def message(self,goal,sender,recipient,kind,body,item_id=None,evidence=None):
        evidence=list(evidence or [])
        if recipient=='Human' and kind in ('ESCALATE','QUESTION') and not any(
                r['id'] in evidence for r in [*self.decisions(goal['id']),*[a for a in self.store.items('goal_approval',None) if a['goal_id']==goal['id']]]):
            decision=self.create_decision(goal,{'question':redact(body)[:4000] or 'Human decision required.','options':[],
                'recommendation':'','reason':'The agent requires a human decision.','reversible':False},item_id=item_id,role=sender,
                source='escalation:'+uuid.uuid4().hex)
            evidence.append(decision['id'])
        display={}
        if recipient=='Human':
            if kind=='NOTE':display={'human_summary':note_summary(self,goal),'human_display_language':goal.get('human_display_language','Korean')}
            elif kind in ('QUESTION','ESCALATE'):
                decision=next((d for d in self.decisions(goal['id']) if d['id'] in evidence or d.get('approval_id') in evidence),None)
                if decision:display={'human_brief':decision.get('human_brief')}
        return self.store.save('message',{'goal_id':goal['id'],'plan_item_id':item_id,'from':sender,'to':recipient,'type':kind,**display,
            'body':redact(body),'evidence_refs':evidence or [],'created':time.time(),**self.times(goal)})

    def defaults(self):
        try:value=self.store.get('settings','goal_defaults')
        except ValueError:value={}
        return {'human_display_language':'Korean','max_plan_items':DEFAULT_PLAN_LIMIT,'reasoning':{role:validate_reasoning(value.get('reasoning',{}).get(role,'high')) for role in ('Planner','Builder')}}

    def reasoning_values(self,value=None):
        value=value or {}
        if not isinstance(value,dict) or set(value)-{'Planner','Builder'}:raise ValueError('Goal reasoning roles must be Planner or Builder.')
        defaults=self.defaults()['reasoning']
        return {role:validate_reasoning(value.get(role,defaults[role])) for role in defaults}

    def set_reasoning(self,data):
        if data.get('goal_id'):
            return self.update(data['goal_id'],{'reasoning':data.get('reasoning')})
        values=self.reasoning_values(data.get('reasoning'))
        self.store.save('settings',{'id':'goal_defaults','reasoning':values})
        return self.defaults()

    def create(self,data,start=True):
        title=validate_title(data.get('title'))
        objective=str(data.get('objective',''))
        if not objective.strip() or len(objective)>20000 or contains_key(json.dumps(data,ensure_ascii=False)):
            raise ValueError('A goal must contain 1–20000 characters and no secrets.')
        ws=Workspace(data['workspace'],self.store.root)
        language=str(data.get('output_language','English')).strip()
        timezone=str(data.get('timezone','America/New_York'))
        if not language or len(language)>60 or any(c in language for c in '\n\r{}'):
            raise ValueError('Use a short output language name.')
        zone(timezone)
        model=validate_builder_model(data.get('builder_model',DEFAULT_BUILDER_MODEL))
        routes=self.validate_routes(data['role_routes']) if 'role_routes' in data else None
        role_efforts=self.reasoning_values(data.get('reasoning'))
        if routes:role_efforts.update({role:route['reasoning_effort'] for role,route in routes.items() if role in role_efforts and route and route.get('reasoning_effort') in ('minimal','low','medium','high','xhigh')})
        now=time.time()
        goal=self.store.save('goal',{'title':title,'objective':objective,'workspace':str(ws.root),'status':'planning','created':now,
            'builder_model':model,'output_language':language,'human_display_language':display_language(data.get('human_display_language','Korean')),'timezone':timezone,'budgets':self.ledger.budgets(data.get('budgets')),
            'reasoning':role_efforts,**({'role_routes':routes} if routes else {}),
            'allow_provisional':self.provisional_setting(data.get('allow_provisional',True)),
            'auto_commit':auto_commit_enabled(data.get('auto_commit',True)),
            'read_loop_limits':read_limits(data.get('read_loop_limits')),'max_plan_items':plan_limit(data.get('max_plan_items',DEFAULT_PLAN_LIMIT)),
            'reference_limits':reference_limits(data.get('reference_limits')),
            'reference_page_limits':page_limits(data.get('reference_page_limits')),
            'unattended':unattended(data.get('unattended',False)),'gpu_jobs_per_cycle':gpu_cycle_limit(data.get('gpu_jobs_per_cycle',3)),
            'policy':policy(data.get('policy')),'generation':1,'no_progress':0,'summary':'',
            'max_steps':max(2,min(32,int(data.get('max_steps',8)))),**deadline(data.get('deadline'),timezone,now),
            'created_utc':stamp(now,timezone)['utc'],'created_local':stamp(now,timezone)['local'],**self.times({'timezone':timezone},now)})
        self.store.save('settings',{'id':'goal_defaults','reasoning':goal['reasoning']})
        if start:self.launch(goal['id'])
        return self.detail(goal['id'])

    def items(self,goal_id):
        return sorted([r for r in self.store.items('plan_item',None) if r['goal_id']==goal_id],key=lambda r:r['created'])

    def messages(self,goal_id):
        return sorted([r for r in self.store.items('message',None) if r['goal_id']==goal_id],key=lambda r:r['created'])

    def approvals(self,goal_id):
        return [r for r in self.store.items('goal_approval',None) if r['goal_id']==goal_id and r['state']=='pending']

    def detail(self,goal_id):
        from .ui_metrics import locked
        with locked(self.lock),self.store.display_read():
            goal=self.store.get('goal',goal_id)
            directives=self.directives(goal_id);statuses={d['id']:d['state'] for d in directives}
            display_context={'decisions':self.decisions(goal_id),'summary':note_summary(self,goal)}
            return {'goal':goal,'effective_routes':self.effective_routes(goal),'items':[{**i,**self.dependency_view(goal,i),**self.execution_view(goal,i)} for i in self.items(goal_id)],'messages':[{**self.human_message_view(goal,m,display_context),**({'directive_status':statuses.get(m['directive_id'])} if m.get('directive_id') else {})} for m in self.messages(goal_id)],'directives':directives,
                    'decisions':self.decisions(goal_id),
                    'ledger':self.ledger.rows(goal_id),'costs':self.ledger.summary(goal),'approvals':self.approvals(goal_id),
                    'settings_history':self.settings_history(goal_id),'references':self.reference_usage(goal),'reference_pages':self.reference_page_usage(goal),
                    'long_commands':[self.long_commands.view(row) for row in self.long_commands.rows(goal_id)],
                    'commits':[row for row in self.store.items('goal_commit',None) if row['goal_id']==goal_id],
                    'execution':self.executions.goal(goal_id)}

    def rename(self,goal_id,data):
        if not isinstance(data,dict) or set(data)-{'goal_id','title'}:
            raise ValueError('제목 변경에는 title만 전달하세요.')
        title=validate_title(data.get('title'))
        with self.store.lock:
            goal=self.store.get('goal',goal_id)
            if goal.get('title')!=title:self.store.update('goal',goal_id,title=title)
        return self.detail(goal_id)

    def guard(self,goal_id,generation=None):
        goal=self.store.get('goal',goal_id)
        if goal.get('archived'):raise GoalLimit('Archived Goal cannot run; restore it first.')
        if generation is not None and generation!=goal['generation']:
            raise GoalLimit('Stale Builder generation; no further action is allowed.')
        if goal['status'] not in LIVE:
            raise GoalLimit('Goal is not running: '+goal['status'])
        if time.time()>=goal['deadline_epoch']:
            self.control(goal_id,'pause',reason='Deadline reached (UTC).')
            raise GoalLimit('Deadline reached (UTC).')
        return goal

    def reserve(self,goal,provider,purpose,amount,**metadata):
        goal=self.store.get('goal',goal['id'])
        if purpose=='product':metadata={**metadata,**self.long_commands.metadata(goal['id'])}
        try:
            row=self.ledger.reserve(goal,provider,purpose,amount,**metadata)
        except GoalLimit as error:
            self.control(goal['id'],'pause',reason=str(error))
            raise
        old=set(self.store.get('goal',goal['id']).get('budget_warnings',[]))
        fresh=set(row['warnings'])-old
        if fresh:
            self.save(goal,budget_warnings=sorted(old|fresh))
            self.message(goal,'System','Human','NOTE','Estimated spend warning: '+', '.join(sorted(fresh)))
        return row

    def call(self,goal,provider,purpose,role,messages,tools=None,model=None,max_tokens=None,reasoning_effort=None,task_id=None,muse_target=None,proxy_metadata=None,plan_item_id=None,tool_options=None,role_route=None):
        current_goal=self.store.get('goal',goal['id'])
        item_id=plan_item_id or (self.store.get('task',task_id).get('plan_item_id') if task_id else None)
        registered=None
        if purpose!='product':
            item=self.store.get('plan_item',item_id) if item_id else None
            registered=self.model_route(current_goal,role if role in ('Planner','Builder','ImageAnalysis','ReferenceSearch') else 'Builder',item,role_route)
            if not current_goal.get('role_routes') and current_goal['id'] not in self.legacy_routes_by_goal and role=='Builder' and not (item or {}).get('builder_route') and role_route is None and model:
                registered=self.model_route(current_goal,'Builder',override={'provider':provider,'model':model,'reasoning_effort':reasoning_effort or current_goal.get('reasoning',{}).get(role,'high')})
            if muse_target and not current_goal.get('role_routes') and current_goal['id'] not in self.legacy_routes_by_goal:
                registered=self.model_route(current_goal,role,override={'provider':muse_target['provider'],'model':muse_target['model'],'reasoning_effort':reasoning_effort or current_goal.get('reasoning',{}).get(role,'high')})
        model=(model or goal['builder_model']) if registered else selected_model(provider,model) or goal['builder_model']
        requested_effort=reasoning_effort or current_goal.get('reasoning',{}).get(role) or self.providers.settings().get(provider,{}).get('reasoning_effort','high')
        # Resolve once per logical request (including all length continuations).
        target=registered or ((muse_target or self.muse_routes.target()) if provider=='baseten' and (role=='Planner' or purpose=='product') else None)
        actual_provider=target['provider'] if target else provider
        actual_model=target['model'] if target else model
        budget_provider=target['budget_provider'] if target else provider
        route=target['route'] if target else provider
        selected_effort=registered['reasoning_effort'] if registered else reasoning(actual_provider,requested_effort)
        limit=self.route_limit(registered,role,max_tokens) if registered else output_limit(actual_provider,role,actual_model,max_tokens,selected_effort)
        def once(current,available):
            return self.model_retry(goal,actual_provider,lambda index,call_id:attempt(current,available,index,call_id),
                role=role,task_id=task_id,item_id=item_id,purpose=purpose)
        def attempt(current,available,retry_index,call_id):
            if role in ('Planner','Builder') and self.store.get('goal',goal['id'])['status']=='waiting_decision':raise GoalLimit('No model calls while waiting for a human decision.')
            if purpose!='product':self.guard(goal['id'],goal['generation'])
            else:self.product_guard(goal)
            if role=='Planner':current=self.limit_planner_evidence(current)
            vision_bound=VISION_INPUT_RESERVE if purpose=='product' and (proxy_metadata or {}).get('proxy_image_count') else None
            if vision_bound is not None and (available or tool_options):vision_bound+=len(json.dumps([available,tool_options],ensure_ascii=False).encode('utf-8'))
            reservation=self.reserve(goal,budget_provider,purpose,self.ledger.estimate_call(budget_provider,actual_model,current,available,limit,input_token_bound=vision_bound),model=actual_model,role=role,reasoning_effort=selected_effort,task_id=task_id,plan_item_id=item_id,route=route,workload='llm',logical_model=model,retry_index=retry_index,model_call_id=call_id,
                **({'provider_id':actual_provider,'api_style':self.model_registry.get(actual_provider)['api_style'],'price_estimated':registered['price_estimated']} if registered else {}),**(proxy_metadata or {}))
            try:
                response=(self.routed_complete(registered,current,available,limit,goal['deadline_epoch']-time.time()) if registered else
                    self.providers.complete(actual_provider,current,actual_model,available,limit,timeout_seconds=goal['deadline_epoch']-time.time(),reasoning_effort=selected_effort,**({'role':'Product'} if purpose=='product' else {}),**({'tool_options':tool_options} if tool_options else {})))
                raw_usage=getattr(response,'usage',None)
                if purpose=='product' and raw_usage is not None:
                    raw_usage=raw_usage.model_dump() if callable(getattr(raw_usage,'model_dump',None)) else raw_usage
                    raw_usage=clean_data(raw_usage) if isinstance(raw_usage,dict) else None
                self.ledger.settle(reservation,usage_of(response),response_model=getattr(response,'model',None) or actual_model,**({'provider_usage':raw_usage} if purpose=='product' else {}))
                if actual_provider=='modal_llm':
                    try:self.ledger.check_modal_llm(self.store.get('goal',goal['id']))
                    except GoalLimit as error:
                        self.control(goal['id'],'pause',reason=str(error))
                        raise
                if purpose!='product':self.guard(goal['id'],goal['generation'])
                else:self.product_guard(goal)
                return response
            except Exception as error:
                self.ledger.settle(reservation,failure_usage(error),error='Provider request failed; missing usage is conservatively estimated.')
                raise
        result=complete_continuing(once,messages,tools,limit)
        result._muse_reasoning_effort=selected_effort
        result._muse_route=route
        return result

    def context(self,goal,item,role):
        plan=[{**{k:i[k] for k in ('id','title','status','acceptance')},'key':plan_key(i),
            **{k:i.get(k) for k in ('superseded_by','superseded_reason') if k in i},
            'parent':i.get('parent'),
            'acceptance_rules':i.get('acceptance_rules',{}),
            **{k:i.get(k) for k in ('depends_on','blocked_by_decision','priority','provisional_scope','provisional_decisions','bundles','wait_reason')},
            'dependency_state':self.dependency_view(goal,i)['dependency_state']} for i in self.items(goal['id'])]
        recent=[{**{k:m[k] for k in ('from','to','type','body')},'evidence_refs':m.get('evidence_refs',m.get('evidence',[])) or []} for m in self.messages(goal['id']) if not m.get('directive_id')][-6:]
        for m in recent:m['body']=m['body'][-3000:]
        compact_item=None if item is None else {k:v for k,v in item.items() if k in ('id','title','parent','acceptance','acceptance_rules','status','role','attempts','reworks','bundles','defects','report','instruction','rerun_instruction','rerun_requirement','depends_on','blocked_by_decision','provisional','provisional_decisions','wait_reason','review_trigger','split_item_ids','files_to_read','files_to_write','commands_to_run','stalled_chunks','document_cycle_active','document_cycles_used','document_cycle_paths','document_cycle_execution_criteria','gpu_artifact_requirement','superseded_by','superseded_reason','builder_route','pending_builder_route')}
        if compact_item is not None:compact_item['bundle_limit']=self.bundle_limit(item)
        if compact_item is not None:compact_item['model_routes']=self.effective_routes(goal)
        if compact_item is not None and role=='Builder':compact_item['reference_search']=self.reference_usage(goal,item)
        if compact_item is not None and role=='Builder':compact_item['reference_pages']=self.reference_page_usage(goal,item)
        if compact_item is not None and role=='Builder':compact_item['long_commands']=[{k:v for k,v in self.long_commands.view(row).items() if k in ('id','label','status','elapsed_s','evidence_id','proxy_cost')} for row in self.long_commands.rows(goal['id'])[:8]]
        if compact_item is not None:compact_item['gpu_submission']=submission_status(self,goal,item)
        if compact_item and 'report' in compact_item:
            compact_item['report']={**compact_item['report'],'summary':compact_item['report']['summary'][-3000:]}
        evidence=(item or {}).get('evidence',[])[-6:]
        results=[]
        for ref in evidence:
            try:
                if ref.get('observation_id'):
                    observed=self.store.get('goal_evidence',ref['observation_id'])['result']
                else:
                    task=self.store.get('task',ref['task_id']);observed=task['events'][ref['event_index']]
                results.append({'id':ref['id'],'observed':observation_text(observed)})
            except (ValueError,IndexError):pass
        messages=[{'role':'system','content':system(goal,role,[p['key'] for p in plan] if role=='Planner' else None)},brief(goal),
            {'role':'user','content':'CURRENT PLAN\n'+dumped(plan)},
            {'role':'user','content':'CURRENT ITEM AND RECENT MESSAGES\n'+dumped({'item':compact_item,'messages':recent,**({'reading_context':self.reading_context(item),'productivity':self.productivity_context(goal,item),'human_directions':self.builder_directions(goal,item)} if role=='Builder' else {})})},
            {'role':'user','content':'HUMAN DECISIONS (pending assumptions are not authorization)\n'+dumped([{k:v for k,v in d.items() if k not in ('human_brief','human_summary','human_display_language')} for d in self.decisions(goal['id'])[-10:]])},
            {'role':'user','content':'RECENT OBSERVED TOOL RESULTS\n'+dumped(results)}]
        if role=='Builder' and item is not None and has_documents(item):
            refs=goal_sources(self,goal,item);accounting={}
            for identifier,ref in reversed(list(refs.items())):
                if ref.get('_ledger') and ref.get('supports_acceptance'):accounting.setdefault(ref['_ledger']['provider'],identifier)
            catalog=packet(refs,limit=8000,preferred=list(accounting.values()))
            messages.insert(-1,{'role':'user','content':'DOCUMENT SOURCE CATALOG (same Goal; cite IDs, not assistant claims)\n'+dumped(catalog)})
        if role=='Planner':
            catalog=self.route_catalog()
            messages.insert(-1,{'role':'user','content':'REGISTERED MODEL ROUTES (use model_routes to inspect a provider if omitted)\n'+dumped(catalog[:80])})
            active=[{k:r[k] for k in ('id','kind','text','plan_item_id')} for r in self.directives(goal['id']) if r['kind']!='note' and r['state'] in ('applied','partial')][-20:]
            if active:messages.insert(-1,{'role':'user','content':'ACKNOWLEDGED HUMAN DIRECTIONS (never policy bypass)\n'+dumped(active)})
        return messages

    @staticmethod
    def limit_planner_evidence(messages):
        import copy
        result=copy.deepcopy(messages);left=20000
        for message in result:
            content=message.get('content','')
            if isinstance(content,str) and '\nREVIEW EVIDENCE PACKET\n' in content:
                # The bounded packet is evidence too; retain the combined 20k budget.
                left-=len(content.split('\nREVIEW EVIDENCE PACKET\n',1)[1])
        left=max(0,left)
        for message in reversed(result):
            content=message.get('content')
            if not isinstance(content,str):continue
            if message.get('role')!='tool' and not content.startswith('RECENT OBSERVED TOOL RESULTS'):continue
            if len(content)>left:message['content']=content[:max(0,left-40)]+('[truncated; full evidence is in store]'[:left] if left else '')
            left-=len(message['content'])
        return result

    def planner_tool(self,goal,item,name,args):
        if name not in PLANNER_ALLOWED:raise PermissionError('Planner tool forbidden: '+name)
        if name=='model_routes':return self.route_catalog(args.get('provider'))
        if name=='request_decision' and getattr(self.review_scope,'read_only',False):raise PermissionError('Review-only cannot create human decisions. Return accept/rework from existing observations.')
        if name.startswith('memory_'):return self.memory_tool(goal,'Planner',name,args)
        if name=='request_decision':return self.request_decision(goal,item,args,'Planner')
        self.guard(goal['id'],goal['generation']);ws=Workspace(goal['workspace'],self.store.root)
        files={};path=None
        if name=='read_file':
            start=args.get('start_line');end=args.get('end_line')
            if 'line_count' in args:
                if end is not None:raise ValueError('Use end_line or legacy line_count, not both.')
                start=1 if start is None else start;end=start+max(1,min(500,int(args['line_count'])))-1
            result=ws.read(args['path'],start,end,include_lines=True);path=result['path'];files[path]=result['sha256']
        else:
            result=ws.search(args['query'],args.get('path','.'))
            for match in result:
                if match['path'] not in files:files[match['path']]=ws.read(match['path'])['sha256']
        observation=self.store.save('goal_evidence',{'goal_id':goal['id'],'plan_item_id':item['id'] if item else None,'role':'Planner',
            'tool':name,'result':result,'files':files,'created':time.time(),**self.times(goal)})
        evidence_id='observation:'+observation['id']
        if item:
            fresh=self.store.get('plan_item',item['id']);refs=list(fresh.get('evidence',[]))
            refs.append({'id':evidence_id,'observation_id':observation['id'],'kind':name,'sha256':hashlib.sha256(dumped(result).encode()).hexdigest(),
                'path':path,'file_sha256':files.get(path),'files':files})
            self.store.update('plan_item',item['id'],evidence=refs)
        return {'evidence_id':evidence_id,'result':result}

    def memory_tool(self,goal,role,name,args):
        self.guard(goal['id'],goal['generation'])
        if name=='memory_search':return self.memory.search(args['query'])
        if name=='memory_read':return self.memory.source(args['source_id'])
        if name=='memory_note':
            if any(contains_key(str(v)) for v in args.values()):raise ValueError('Secrets cannot be written to memory.')
            concepts=list(dict.fromkeys([goal['id'],role,*args.get('concepts',[])]))[:30]
            self.memory.add_note(args['title'],args['text'],concepts)
            return {'saved':True,'goal_id':goal['id'],'role':role}
        raise ValueError('Unknown memory tool.')

    def phase(self,goal,item,instruction):
        self.review_scope.validate_human_brief=True;self.review_scope.human_brief_retries={}
        try:return self._phase(goal,item,instruction)
        finally:self.review_scope.validate_human_brief=False;self.review_scope.human_brief_retries={}

    def _phase(self,goal,item,instruction):
        messages=self.context(goal,item,'Planner')+[{'role':'user','content':instruction}]
        registered=self.model_route(goal,'Planner',item)
        target=self.muse_routes.target(generation_settings().get('capabilities',{})) if not goal.get('role_routes') and goal['id'] not in self.legacy_routes_by_goal else None
        native=target['tools_supported'] if target else registered['capabilities']['tool_calling']
        if not native:messages.append({'role':'user','content':'Native tools unavailable. For evidence or a human decision, return {"tool_requests":[{"name":"read_file","arguments":{"path":"..."}}]} instead of the final JSON. Only the supplied orchestration/read-only/memory tool names are allowed.'})
        for round_index in range(4):
            # One format-correction round must not consume the existing evidence-tool budget.
            if round_index==3 and not self.review_scope.human_brief_retries:break
            response=self.call(goal,registered['provider'],'development','Planner',messages,PLANNER_TOOLS if native else None,registered['model'],muse_target=target,plan_item_id=item['id'] if item else None,role_route=None if target else registered)
            if not response.choices:raise ValueError('Planner returned no choice.')
            value=response.choices[0].message
            content=getattr(value,'content',None) or ''
            calls=getattr(value,'tool_calls',None) or []
            if not calls:
                if native:return content
                try:requests=self.parse(content).get('tool_requests')
                except ValueError:return content
                if not requests:return content
                if not isinstance(requests,list) or len(requests)>3:raise ValueError('Too many Planner JSON tool requests.')
                observed=[]
                for request in requests:
                    try:result=self.planner_tool(goal,item,request['name'],request.get('arguments',{}))
                    except (ValueError,PermissionError,OSError) as error:result={'error':redact(str(error))}
                    observed.append(planner_tool_view(result))
                messages.extend([{'role':'assistant','content':content},{'role':'tool','tool_call_id':'json-fallback','content':dumped(observed)}])
                # JSON fallback uses a regular user result, not an unmatched native tool message.
                messages[-1]={'role':'user','content':'RECENT OBSERVED TOOL RESULTS\n'+dumped(observed)}
                continue
            if len(calls)>3:raise ValueError('Planner requested too many memory tools.')
            extra=getattr(value,'model_extra',None) or {}
            messages.append({'role':'assistant','content':content, 'tool_calls':[{'id':c.id,'type':'function','function':{'name':c.function.name,'arguments':c.function.arguments}} for c in calls],
                **{k:extra[k] for k in ('reasoning_content','_muse_response_items') if extra.get(k) is not None}})
            for call in calls:
                try:result=self.planner_tool(goal,item,call.function.name,json.loads(call.function.arguments))
                except (ValueError,PermissionError,OSError) as error:result={'error':redact(str(error))}
                messages.append({'role':'tool','tool_call_id':call.id,'content':dumped(planner_tool_view(result))})
        raise ValueError('Planner evidence-tool limit reached.')

    @staticmethod
    def parse(text):
        text=text.strip()
        if text.startswith('```'):
            lines=text.splitlines();text='\n'.join(lines[1:-1])
        value=json.loads(text)
        if not isinstance(value,dict):raise ValueError('Expected a JSON object.')
        return value

    def validate_plan(self,value,goal=None):
        return initial_items(value.get('items'),(goal or {}).get('max_plan_items',DEFAULT_PLAN_LIMIT))

    def add_items(self,goal,items):
        try:self.apply_plan_patch(goal,prepare_plan(self.items(goal['id']),items,max_plan_items=goal.get('max_plan_items',DEFAULT_PLAN_LIMIT)))
        except PlanCapacityError as error:
            self.capacity_notice(goal,None,error,'plan_update');raise

    def plan(self,goal):
        instruction='Create the initial plan. Schema: '+dumped(PLAN_SCHEMA)
        capacity=None
        for attempt in range(2):
            try:
                raw,human_queue=self.inbox_phase(goal,None,instruction)
                self.store.save('goal_plan_attempt',{'goal_id':goal['id'],'created':time.time(),'response':redact(raw),'retry':attempt,**self.times(goal)})
                value=self.parse(raw)
                items=self.validate_plan(value,goal)
                self.add_items(goal,items)
                self.message(goal,'Planner','Builder','NOTE',str(value.get('summary','Plan created.')))
                self.finish_inbox(goal,human_queue,value)
                self.save(goal,status='running')
                return
            except GoalLimit:raise
            except PlanCapacityError as error:
                capacity=error;self.capacity_notice(goal,None,error,'initial_plan')
                instruction='The proposed plan exceeds configured capacity. '+str(error)+' Return a smaller plan within max_plan_items. Schema: '+dumped(PLAN_SCHEMA)
            except (ValueError,TypeError,KeyError,json.JSONDecodeError) as error:
                instruction='The previous plan JSON was invalid: '+redact(str(error))+'. Return a corrected object. Schema: '+dumped(PLAN_SCHEMA)
        if capacity:
            self.save(goal,status='paused',reason=str(capacity));return
        self.save(goal,status='blocked',reason='Planner JSON failed schema validation twice.')
        self.message(goal,'Planner','Human','ESCALATE','Planner JSON failed schema validation twice; a human decision is required.')

    def gpu_estimate(self,spec):
        return self.ledger.prices['gpu_hourly'][spec['gpu']]*spec['timeout']/3600

    def collect_evidence(self,item,task):
        from .review_evidence import collect
        with self.lock:
            try:current=self.store.get('task',task['id'])
            except ValueError:current=task # Keep legacy callers that supply an unsaved observed task.
            return collect(self,self.store.get('plan_item',item['id']),current)

    def observed_evidence(self,goal,item):
        from .review_evidence import observations
        return observations(self,goal,item)

    def valid_evidence(self,goal,item):
        return {key:ref for key,ref in self.observed_evidence(goal,item).items() if ref['supports_acceptance']}

    def assign(self,goal,item,continuation=False):
        with self.lock:
            goal=self.guard(goal['id'],goal['generation'])
            self.apply_deferred_directives(goal['id'])
            self.apply_item_routes(goal['id'])
            item=self.store.get('plan_item',item['id']);view=self.dependency_view(goal,item)
            if terminal(item):return
            if item.get('review_only_next'):return
            if self.child_items(goal,item):return
            if not view['ready'] or item.get('needs_decision_replan'):return
            if item['bundles']>=self.bundle_limit(item):self.prepare_limit_review(goal,item);return
            continuation=bool(item['bundles']>0 and (continuation or item.get('continue_pending',False) or item.get('waiting_resume')=='continue' or item['status']=='todo'))
            self.store.update('plan_item',item['id'],provisional=view['provisional'],provisional_decisions=view['provisional_decisions'],
                status='waiting',wait_reason='builder_queue',waiting_resume='active_task')
            self.save(goal,builder_focus_item_id=item['id'])
            text=dumped({'title':item['title'],'acceptance':item['acceptance'],'acceptance_rules':item.get('acceptance_rules',{}),'defects':item.get('defects',[]),
                **{k:item[k] for k in ('files_to_read','files_to_write','commands_to_run') if k in item},
                'instruction':item.get('instruction') or item.get('rerun_instruction') or ('Continue this same item using the prior chunk summary, read history and observed results. End with report_item(progress or completed).' if continuation else 'Implement and verify this item. End with report_item(progress or completed), including NOT RUN / NOT VERIFIED limits.')})
            self.message(goal,'Planner','Builder','ASSIGN',text,item['id'])
            previous=self.task_for_item(item)
            resolved=self.model_route(goal,'Builder',item)
            previous_id=previous['id'] if previous else self.store.get('goal_restart_retry',item['restart_retry_pending'])['source_task_id'] if item.get('restart_retry_pending') else item.get('previous_builder_task_id')
            # Across server generations use a fresh tool history plus preserved carryover.
            same_route=not previous or previous.get('provider','meta')==resolved['provider'] and previous.get('model',goal['builder_model'])==resolved['model']
            if previous and (previous.get('model_route') or {}).get('provider_revision',0)!=resolved['provider_revision']:same_route=False
            resume_id=previous['id'] if continuation and same_route and self.reusable_builder(goal,item,previous) and not item.get('restart_retry_pending') else None
            task=self.builders.start(goal['workspace'],text,provider=resolved['provider'],model=resolved['model'],max_steps=goal['max_steps'],
                task_id=resume_id,use_memory=False,share_memory=False,
                context={'goal_id':goal['id'],'plan_item_id':item['id'],'generation':goal['generation'],
                    'decision_revision':item.get('decision_revision',0),'execution_cycle':item.get('execution_cycle',0),
                    'attempt_charged':not continuation,'system_prompt':system(goal,'Builder'),
                    'previous_task_id':previous_id,'restart_retry_audit_id':item.get('restart_retry_pending'),'model_route':resolved})
            self.store.update('plan_item',item['id'],task_id=task['id'],task_ids=list(dict.fromkeys([*item['task_ids'],task['id']])),
                bundles=item['bundles']+1,attempts=item['attempts']+(0 if continuation else 1),continue_pending=False,restart_retry_pending=None)

    def finished_batch(self,goal,item,task):
        evidence,fresh=self.collect_evidence(item,task)
        declared=self.batch_report(task)
        report=self.observed_report(task,evidence,declared)
        self.message(goal,'Builder','Planner','REPORT',dumped(report),item['id'],report['evidence_paths'])
        latest=self.store.get('goal',goal['id'])
        if task.get('human_interrupt_ids'):
            self.park_item(item,'continuation',resume='continue',bundle_reported=item['bundles'],report=report,continue_pending=True)
            return
        if self.retry_restart_chunk(goal,item,task):return
        waiting_long=bool(self.long_commands.active(goal['id'],item['id']))
        self.save(latest,no_progress=0 if fresh else latest['no_progress'] if waiting_long else latest['no_progress']+1)
        if task['status'] in TERMINATED:
            recover_failed_chunk(self,goal,item,task);item=self.store.get('plan_item',item['id'])
            self.park_item(item,'platform_error',resume=None,bundle_reported=item['bundles'],report=report,continue_pending=False)
            escalate_failure(self,goal,item,task)
        elif self.long_commands.active(goal['id'],item['id']):
            self.park_item(item,'long_command',resume='continue',bundle_reported=item['bundles'],report=report,continue_pending=False)
        elif not item.get('document_cycle_active') and (declared['status']!='completed' or any(needs_execution(item,c) for c in item['acceptance']) and not any(e.get('supports_acceptance') and e['kind'] in ('run_command','submit_gpu_job','get_gpu_job') for e in evidence)) and self.handle_stalled_batch(goal,item,task):
            self.store.update('plan_item',item['id'],bundle_reported=item['bundles'],report=report)
            self.save(self.store.get('goal',goal['id']),no_progress=0)
        elif declared['status']=='completed' or item['bundles']>=self.bundle_limit(item):
            self.store.update('plan_item',item['id'],bundle_reported=item['bundles'],report=report,status='review',
                review_trigger='completed' if declared['status']=='completed' else 'batch_limit',continue_pending=False,wait_reason=None,waiting_resume=None)
        else:
            self.park_item(item,'continuation',resume='continue',bundle_reported=item['bundles'],report=report,continue_pending=True)

    def progress(self,goal,note=''):
        items=self.items(goal['id']);costs=self.ledger.summary(goal)
        titles=lambda states: '; '.join(i['title'] for i in items if i['status'] in states) or '—'
        times=self.times(goal)
        return '\n'.join([f"{times['time_local']} [{goal['timezone']}] | UTC {times['time_utc']}",
            'Done: '+titles({'done'}),'In progress: '+titles({'in_progress','review'}),
            'Next: '+titles({'todo','rework'}),'Blocked & needs decision: '+titles({'blocked'}),
            'Waiting (unfinished): '+('; '.join(r['title']+' ['+r['reason']+']' for r in self.scheduling_blockers(goal,items) if r['status'] in ('waiting','waiting_children')) or '—'),
            'Spend by provider (estimated USD): '+', '.join(f"{p} ${v['spent']:.6f} (+${v['reserved']:.6f} reserved)" for p,v in costs.items()),
            f"Modal routes (estimated USD): LLM ${costs['modal']['llm_spent']:.6f} / ${costs['modal']['llm']:.2f}; GPU ${costs['modal']['gpu_spent']:.6f}",
            'Pending approvals: '+str(len(self.approvals(goal['id']))),note])

    def question(self,goal,item_id,text):
        self.message(goal,'Builder','Planner','QUESTION',text,item_id)
        item=self.store.get('plan_item',item_id)
        base='Review this Builder question. Return {"answer":"...","needs_human":false}. If a human must decide, set needs_human=true and include options, recommendation (one option or empty), reason, affected_items (existing keys/IDs), reversible (boolean), and human_brief using the human display schema in your system prompt. Question: '+text
        instruction=base
        for attempt in range(2):
            try:
                value=self.parse(self.phase(goal,item,instruction));answer=str(value['answer'])
            except GoalLimit:raise
            except (ValueError,KeyError,TypeError):
                value={'needs_human':True};answer='The Planner could not resolve this question; a human decision is required.'
                break
            if not value.get('needs_human'):break
            args={'question':text,'options':value.get('options',[]),'recommendation':value.get('recommendation','')}
            try:validate_brief(value.get('human_brief'),goal,args);break
            except (ValueError,TypeError,KeyError) as error:
                if attempt==0:instruction=base+'\nCorrect human_brief once: '+redact(str(error))+'. Preserve the original question and decision options.'
        if value.get('needs_human'):
            return self.request_decision(goal,item,{'question':text,'options':value.get('options',[]),
                'recommendation':value.get('recommendation',''),'reason':value.get('reason',answer),
                'human_brief':value.get('human_brief'),
                'reversible':value.get('reversible',False),'affected_items':value.get('affected_items',[item_id])},'Planner')
        self.message(goal,'Planner','Builder','ANSWER',answer,item_id)
        return {'answer':answer}

    def decide(self,goal_id,approval_id,approve,answer=''):
        with self.lock:
            return self._decide_locked(goal_id,approval_id,approve,answer)

    def _decide_locked(self,goal_id,approval_id,approve,answer=''):
        goal=self.guard(goal_id)
        request=self.store.get('goal_approval',approval_id)
        if request['goal_id']!=goal_id or request['state']!='pending':raise ValueError('Approval is no longer pending for this goal.')
        slot=self.builders.approvals.get(approval_id)
        if not slot:raise ValueError('Approval expired; resume the interrupted goal.')
        if request['kind']=='question' and approve and (not answer.strip() or contains_key(answer)):raise ValueError('Provide a non-secret answer.')
        self.store.update('goal_approval',approval_id,state='approved' if approve else 'denied',answer=answer if request['kind']=='question' else '',**self.times(goal))
        if request.get('decision_id'):
            self.store.update('decision',request['decision_id'],state='answered',answer=answer or ('Approve' if approve else 'Deny'),answered_at=time.time(),**self.times(goal))
        self.wake_decisions(goal_id)
        slot['approved']=bool(approve);slot['event'].set()
        self.message(goal,'Human','Builder','ANSWER',answer or ('Approved.' if approve else 'Denied.'),request['plan_item_id'],[approval_id])
        return self.detail(goal_id)

    def launch(self,goal_id):
        with self.lock:
            assert_unarchived(self.store.get('goal',goal_id))
            old=self.threads.get(goal_id)
            if old and old.is_alive():raise ValueError('The previous coordinator is still stopping; retry resume shortly.')
            token=secrets.token_urlsafe(40);register_secret(token)
            self.proxy_tokens[goal_id]=token
            self.signals[goal_id]=threading.Event()
            self.decision_events[goal_id]=threading.Event()
            thread=threading.Thread(target=self._loop,args=(goal_id,),daemon=True,name='goal-'+goal_id)
            self.threads[goal_id]=thread;thread.start()

    def control(self,goal_id,action,reason=''):
        with self.lock:
            goal=assert_unarchived(self.store.get('goal',goal_id))
            if action=='resume':
                if goal['status'] not in ('paused','blocked','stopped','interrupted'):raise ValueError('Only an inactive goal can resume.')
                if any(self.store.get('task',tid).get('goal_id')==goal_id for tid in list(self.builders.active)):
                    raise ValueError('A previous Builder is still stopping; retry resume shortly.')
                self.apply_pending_settings(goal_id);goal=self.store.get('goal',goal_id)
                if time.time()>=goal['deadline_epoch']:raise ValueError('Update the UTC deadline before resuming.')
                for item in self.items(goal_id):
                    previous=self.task_for_item(item)
                    if not terminal(item) and previous and paused_cancellation(previous):
                        self.store.update('plan_item',item['id'],task_id=None,previous_builder_task_id=previous['id'],status='todo',wait_reason=None,waiting_resume=None,continue_pending=item['bundles']<self.bundle_limit(item),bundle_reported=item['bundles'])
                        continue
                    if item['status']=='in_progress' or item['status']=='waiting' and item.get('waiting_resume')=='active_task':
                        self.park_item(item,'interrupted',resume='continue' if item['bundles']<self.bundle_limit(item) else 'review',continue_pending=item['bundles']<self.bundle_limit(item))
                self.save(goal,status='running' if self.items(goal_id) else 'planning',generation=goal['generation']+1,reason='',no_progress=0,pause_origin=None,scheduling_blocked=False)
                self.launch(goal_id)
            elif action in ('pause','stop'):
                if action=='stop':self.long_commands.stop_goal(goal_id)
                for intent in self.store.items('goal_restart',None):
                    if intent['goal_id']==goal_id and intent['state']=='pending':self.store.update('goal_restart',intent['id'],state='cancelled')
                target='paused' if action=='pause' else 'stopped'
                self.save(goal,status=target,reason=reason or ('Paused by user.' if action=='pause' else 'Stopped by user.'),pause_origin='user' if not reason or reason=='Paused by user.' else 'system')
                if goal_id in self.signals:self.signals[goal_id].set()
                self.decision_events.setdefault(goal_id,threading.Event()).set()
                self.proxy_tokens.pop(goal_id,None)
                for task_id in list(self.builders.active):
                    if self.store.get('task',task_id).get('goal_id')==goal_id:self.builders.cancel(task_id)
                for request in self.approvals(goal_id):
                    self.store.update('goal_approval',request['id'],state='cancelled')
                    if request.get('decision_id'):self.store.update('decision',request['decision_id'],state='withdrawn',answered_at=time.time())
            else:raise ValueError('Unknown Goal control action.')
        return self.detail(goal_id)

    def update_item(self,goal_id,data):
        goal=self.store.get('goal',goal_id)
        if goal['status'] in LIVE:raise ValueError('Pause the goal before editing its settings.')
        if set(data)-{'goal_id','unblock_item','rereview_item','retry_review_item'}:raise ValueError('Change item state separately from Goal settings.')
        if 'retry_review_item' in data:
            item=self.store.get('plan_item',data['retry_review_item'])
            if item['goal_id']!=goal_id or item['status']!='review' or not item.get('review_error'):raise ValueError('Choose an item with a Planner review_error.')
            self.store.update('plan_item',item['id'],review_error=None,review_error_count=0,review_trigger='manual')
            self.store.save('goal_review_reset',{'goal_id':goal_id,'plan_item_id':item['id'],'created':time.time(),**self.times(goal)})
        if 'unblock_item' in data:
            item=self.store.get('plan_item',data['unblock_item'])
            if item['goal_id']!=goal_id:raise ValueError('Wrong plan item.')
            if item['status']=='superseded':raise ValueError('Superseded items are terminal.')
            if item['bundles']>=self.bundle_limit(item):raise ValueError('Builder batches exhausted. Review the evidence before opening another cycle.')
            self.store.update('plan_item',item['id'],status='rework',reworks=0)
        if 'rereview_item' in data:
            item=self.store.get('plan_item',data['rereview_item'])
            if item['goal_id']!=goal_id or item['status']!='blocked':raise ValueError('Choose a blocked item in this Goal.')
            valid=self.valid_evidence(goal,item)
            if not valid:raise ValueError('No current observed evidence is available for re-review.')
            self.store.update('plan_item',item['id'],status='review',review_trigger='manual')
            self.message(goal,'Human','Planner','NOTE','Re-review the existing observed evidence. Static file criteria require file evidence; execution criteria require successful command/GPU evidence. Do not rerun the Builder.',item['id'],list(valid))
        return self.detail(goal_id)

    def _loop(self,goal_id):
        try:
            while not self.signals[goal_id].wait(.15):
                self.apply_pending_settings(goal_id)
                self.apply_deferred_directives(goal_id)
                self.apply_item_routes(goal_id)
                goal=self.guard(goal_id)
                if self.process_interrupts(goal):continue
                if not self.items(goal_id):self.plan(goal);continue
                # One bounded Planner refresh, never a status polling call.
                if not any(self.store.get('task',tid).get('goal_id')==goal_id and self.store.get('task',tid)['status']!='waiting_approval' for tid in list(self.builders.active)):
                    self.refresh_dependencies(goal)
                    goal=self.guard(goal_id)
                items=self.items(goal_id)
                for item in items:
                    if item['status'] not in ('in_progress','waiting') or not item.get('task_id'):continue
                    task=self.store.get('task',item['task_id'])
                    if task['id'] not in self.builders.active and item.get('bundle_reported',0)<item['bundles']:
                        if not self.dependency_view(goal,item)['ready']:
                            self.park_finished_batch(goal,item,task)
                        else:self.finished_batch(goal,item,task)
                self.reconcile_waiting(self.guard(goal_id))
                self.refresh_split_reviews(self.guard(goal_id))
                self.repair_schedule_cycles(self.guard(goal_id))
                if not self.builder_busy(goal):
                    for stalled in self.items(goal_id):
                        self.recover_stall_request(goal,stalled)
                        if stalled.get('stall_replan_pending') and self.dependency_view(goal,stalled)['ready']:
                            self.shrink_stalled_item(self.guard(goal_id),stalled)
                # No intervening review/planning call for another item before a ready continuation.
                goal=self.guard(goal_id)
                if goal['no_progress']>=3:
                    self.control(goal_id,'pause',reason='Three consecutive cycles without new observed evidence.');return
                if not self.builder_busy(goal):
                    pinned=self.next_builder(goal)
                    if pinned and pinned['bundles']<self.bundle_limit(pinned) and (pinned.get('continue_pending') or pinned.get('waiting_resume')=='continue'):
                        self.assign(goal,pinned,True)
                        continue
                for item in self.items(goal_id):
                    if self.review_ready(item) and self.dependency_view(goal,item)['ready'] and not item.get('needs_decision_replan'):self.review(self.guard(goal_id),item,review_only=bool(item.get('review_only_next')))
                    if self.store.get('goal',goal_id)['status'] not in LIVE:return
                goal=self.guard(goal_id)
                if goal['no_progress']>=3:
                    self.control(goal_id,'pause',reason='Three consecutive cycles without new observed evidence.');return
                items=self.items(goal_id)
                busy=self.builder_busy(goal)
                candidate=None
                if not busy:
                    candidate=self.next_builder(goal)
                    if candidate:self.assign(goal,candidate)
                pending=self.approvals(goal_id)
                if self.store.get('goal',goal_id)['status'] not in LIVE:return
                items=self.items(goal_id)
                live_work=self.live_scheduler_work(goal,items)
                if not live_work and not busy and not candidate and not pending and any(i.get('wait_reason')=='plan_capacity' for i in items) and not self.pending_decisions(goal_id) and not any(self.review_ready(i) for i in items):
                    self.control(goal_id,'pause',reason='Plan capacity requires completed-item consolidation or a configured limit increase; no model polling.');return
                if not self.live_scheduler_work(goal,items,ignore_approvals=True) and not busy and not candidate and (self.pending_decisions(goal_id) or goal.get('dependency_refresh_error')) and not any(self.review_ready(i) and self.dependency_view(goal,i)['ready'] for i in items):
                    self.wait_for_decisions(goal)
                    continue
                if not live_work and items and all(terminal(i) for i in items) and goal.get('completion_verdict') and not self.pending_decisions(goal_id) and not self.pending_directions(goal_id):
                    self.close_pending_notes(goal)
                    summary=self.progress(goal)
                    self.save(goal,status='done',summary=summary,final_report=summary)
                    return
                if not busy and not candidate and not pending and self.block_if_no_runnable(goal,items):return
                if not live_work and not any(i['status'] in ('todo','rework','in_progress','review','waiting','waiting_children') for i in items):
                    if self.pending_directions(goal_id):
                        self.save(goal,status='paused',reason='No scheduled Planner point remains for pending instructions. Send an interrupt and resume to request an immediate plan; no extra call was made.');return
                    self.save(goal,status='blocked',reason='No runnable plan items remain; review blocked items or the final verdict.');return
                if not live_work and not busy and not candidate and not pending and all(terminal(i) or i['status']=='blocked' or i['status']=='review' and (i.get('review_error') or {}).get('needs_human') for i in items):
                    self.save(goal,status='blocked',reason='Platform review_error requires a decision; Builder retries were not charged.');return
                next_status='waiting_approval' if pending and not busy and not candidate else 'running'
                if self.store.get('goal',goal_id)['status']!=next_status:self.save(goal,status=next_status)
        except GoalLimit:
            pass
        except Exception as error:
            diagnostic=log_exception(self.store.root,error,component='Coordinator',goal_id=goal_id)
            goal=self.store.get('goal',goal_id)
            if goal['status'] in LIVE:
                from .builder_failures import failure_kind
                provider_failure=failure_kind(type(error).__name__)=='provider' or retryable(error)
                summary=('Provider request failed after bounded retries; inspect the provider error before resuming. ' if provider_failure else '')+exception_summary(diagnostic)
                self.save(goal,status='blocked',reason=summary,**({'provider_error':diagnostic,'platform_error':None} if provider_failure else {'platform_error':diagnostic}))
                self.message(goal,'System','Human','ESCALATE',summary)
        finally:
            self.apply_pending_settings(goal_id)

    def shutdown(self):
        with self.lock:self.operations_stopping.set()
        self.long_commands.shutdown()
        for goal in self.store.items('goal',None):
            with self.lock:
                goal=self.store.get('goal',goal['id'])
                if goal['status'] in LIVE:
                    for task_id in list(self.builders.active):
                        task=self.store.get('task',task_id)
                        if task.get('goal_id')==goal['id']:self.store.update('task',task_id,restart_interruption={'kind':'server_restart','created':time.time(),'generation':goal['generation']})
                    self.control(goal['id'],'pause',reason='Server is shutting down.')
                    self.save(goal,status='interrupted',reason='Server restarted. Resume explicitly to continue.')
                    self.remember_restart(goal)

    def proxy_environment(self,goal_id):
        token=self.proxy_tokens.get(goal_id)
        if not token:raise GoalLimit('Goal product proxy token is not active.')
        return {'MUSE_PROXY_BASE_URL':self.proxy_base,'MUSE_PROXY_TOKEN':token}

    def product_guard(self,goal):
        current=self.store.get('goal',goal['id'])
        if current.get('archived'):raise GoalLimit('The archived Goal proxy is not active.')
        if current['status'] not in LIVE|{'done'}:raise GoalLimit('The Goal proxy is not active.')
        if time.time()>=current['deadline_epoch']:
            self.control(goal['id'],'pause',reason='Deadline reached (UTC).')
            raise GoalLimit('Deadline reached (UTC).')

    def proxy_goal(self,token):
        if not isinstance(token,str) or not 20<=len(token)<=256:raise PermissionError('Invalid Goal proxy token.')
        goal_id=next((gid for gid,value in self.proxy_tokens.items() if secrets.compare_digest(token,value)),None)
        if not goal_id:goal_id=self.long_commands.token_goal(token)
        if not goal_id:raise PermissionError('Invalid Goal proxy token.')
        goal=self.store.get('goal',goal_id);self.product_guard(goal)
        return goal

    def proxy_models(self,token):
        self.proxy_goal(token)
        target=self.muse_routes.target()
        return clean_data({'object':'list','data':[{'id':target['model'],'object':'model','created':0,'owned_by':target['route']}]})

    def proxy(self,token,data):
        goal=self.proxy_goal(token)
        target=self.muse_routes.target()
        model=resolve_model(data.get('model'),target)
        tools,tool_options=request_tools(data)
        messages=data.get('messages')
        image_metadata=validate_messages(messages)
        requested_effort=data.get('reasoning_effort')
        if requested_effort is not None:validate_reasoning(requested_effort)
        limit,metadata,adjustment=product_output(data,model)
        metadata.update(image_metadata)
        response=self.call(goal,'baseten','product','Product',messages,tools=tools,model=BASETEN_MODEL,max_tokens=limit,reasoning_effort=requested_effort,muse_target=target,proxy_metadata=metadata,tool_options=tool_options)
        if not response.choices:raise ValueError('Provider returned no answer.')
        choice=response.choices[0]
        return clean_data({'id':'muse-proxy-'+uuid.uuid4().hex,'object':'chat.completion','created':int(time.time()),'model':model,
            'muse_route':response._muse_route,
            'choices':[{'index':0,'message':assistant_message(choice.message),'finish_reason':choice.finish_reason}],
            'usage':usage_of(response) or {},**({'muse_proxy_adjustment':adjustment} if adjustment else {})})

    def record_chat_usage(self,task,usage,attempt):
        if task.get('goal_id'):return
        cost=self.ledger.token_cost(task['provider'],task['model'],usage)
        self.store.save('ledger',{'goal_id':None,'task_id':task['id'],'provider':task['provider'],'purpose':'chat' if task.get('mode')=='chat' else 'work',
            'route':task['provider'],'workload':'llm',
            'role':'Chat' if task.get('mode')=='chat' else 'Work','model':task['model'],'created':time.time(),'state':'settled',
            'reserved_usd':0,'usage_estimated':bool(usage.get('usage_estimated')),'attempt':attempt,'reasoning_effort':task.get('reasoning_effort'),
            **task.get('attachment_usage',{}),'provider_usage':usage,**cost})
