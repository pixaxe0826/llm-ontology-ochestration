"""Persist human decisions, schedule independent work and sleep without model polling."""
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from .goal_costs import GoalLimit
from .item_states import ItemSupersession,terminal,dependency_target,supersede_requested
from .plan_updates import plan_key,prepare_plan
from .security import contains_key,redact
from .workspace import Workspace
from .dependency_contract import dependency_instruction, explicit_reopen, classification_requested
from .plan_capacity import plan_limit,PlanCapacityError,DEFAULT as DEFAULT_PLAN_LIMIT

from .human_briefs import HUMAN_BRIEF_SCHEMA,HumanBriefError,make_brief,validate_brief

DEPENDENCY_FIELDS=('depends_on','blocked_by_decision','priority','provisional_scope')
DECISION_PARAMETERS={
    'question':{'type':'string'},'options':{'type':'array','items':{'type':'string'}},
    'recommendation':{'type':'string'},'reason':{'type':'string'},
    'affected_items':{'type':'array','items':{'type':'string'}},'reversible':{'type':'boolean'},'human_brief':HUMAN_BRIEF_SCHEMA}

def dependencies_known(item):
    return all(isinstance(item.get(k),list) for k in ('depends_on','blocked_by_decision'))


class GoalDecisions(ItemSupersession):
    def decisions(self,goal_id):
        return sorted([r for r in self.store.items('decision',None) if r['goal_id']==goal_id],key=lambda r:r['created'])

    def pending_decisions(self,goal_id):
        return [d for d in self.decisions(goal_id) if d['state']=='pending']

    def resolve_items(self,goal_id,refs):
        items=self.items(goal_id);lookup={k:i['id'] for i in items for k in (i['id'],plan_key(i))}
        if not isinstance(refs,list) or len(refs)>plan_limit(self.store.get('goal',goal_id).get('max_plan_items',DEFAULT_PLAN_LIMIT)) or any(not isinstance(v,str) or v not in lookup for v in refs):
            raise ValueError('Affected items must be existing IDs or plan keys in this Goal.')
        return list(dict.fromkeys(lookup[v] for v in refs))

    def create_decision(self,goal,data,*,item_id=None,role='Planner',source=None,approval_id=None):
        if not isinstance(data,dict) or contains_key(json.dumps(data,ensure_ascii=False)):raise ValueError('Decisions must not contain secrets.')
        question=data.get('question','');options=data.get('options',[])
        recommendation=data.get('recommendation','');reason=data.get('reason','')
        if not isinstance(question,str) or not 1<=len(question.strip())<=4000:raise ValueError('A decision question must contain 1–4000 characters.')
        if not isinstance(options,list) or len(options)>12 or any(not isinstance(v,str) or not 1<=len(v.strip())<=1000 for v in options) or len(set(options))!=len(options):raise ValueError('Invalid decision options.')
        if any(not isinstance(v,str) or len(v)>4000 for v in (recommendation,reason)):raise ValueError('Invalid recommendation or reason.')
        reversible=data.get('reversible',False)
        if type(reversible) is not bool:raise ValueError('Reversible must be a boolean.')
        if options and recommendation and recommendation not in options:raise ValueError('The recommendation must match an option.')
        affected=self.resolve_items(goal['id'],data.get('affected_items',[item_id] if item_id else []))
        provided=None
        if data.get('human_brief'):
            try:provided=validate_brief(data['human_brief'],goal,data)
            except (ValueError,TypeError,KeyError):pass # Deterministic fallback for automatic/server callers.
        human_brief=make_brief(self,goal,{**data,'affected_items':affected,'approval_id':approval_id,'source':source},item_id,provided)
        fingerprint=source or ('question:'+str(item_id)+':'+hashlib.sha256(question.encode()).hexdigest())
        with self.lock:
            old=next((d for d in self.decisions(goal['id']) if d.get('source')==fingerprint),None)
            if old:return old
            decision=self.store.save('decision',{'goal_id':goal['id'],'question':question.strip(),'options':options,
                'recommendation':recommendation,'reason':reason,'affected_items':affected,'reversible':reversible,
                'state':'pending','created':time.time(),'answered_at':None,'answer':None,'role':role,
                'source':fingerprint,'approval_id':approval_id,'human_brief':human_brief,**self.times(goal)})
            for identifier in affected:
                item=self.store.get('plan_item',identifier)
                self.store.update('plan_item',identifier,blocked_by_decision=list(dict.fromkeys([*item.get('blocked_by_decision',[]),decision['id']])))
            return decision

    def request_decision(self,goal,item,args,role):
        if role=='Planner' and getattr(self.review_scope,'validate_human_brief',False):
            key=hashlib.sha256(str(args.get('question','')).encode()).hexdigest()
            try:validate_brief(args.get('human_brief'),goal,args)
            except (ValueError,TypeError,KeyError) as error:
                retries=self.review_scope.human_brief_retries
                if retries.get(key,0)==0:
                    retries[key]=1
                    raise HumanBriefError('Correct human_brief once in the same request_decision call: '+str(error)+'. Keep original decision arguments unchanged.') from None
        decision=self.create_decision(goal,args,item_id=item['id'] if item else None,role=role)
        return {'decision_id':decision['id'],'state':decision['state'],'recommendation':decision['recommendation'],'answer':decision.get('answer'),
                'instruction':'Only independent work or server-approved PROVISIONAL local work may continue. Wait for the answer otherwise.'}

    def dependency_view(self,goal,item):
        known=dependencies_known(item)
        by_id={i['id']:i for i in self.items(goal['id'])}
        decisions={d['id']:d for d in self.decisions(goal['id'])}
        pending={k:d for k,d in decisions.items() if d['state']=='pending'}
        refs=set(item.get('blocked_by_decision') or [])|{d['id'] for d in pending.values() if item['id'] in d['affected_items']}
        blocked=[pending[v] for v in sorted(refs) if v in pending]
        targets=[dependency_target(v,by_id) for v in item.get('depends_on') or []]
        unresolved=list(dict.fromkeys(v for v,done in targets if not done))
        # A completed checkpoint is not an answered checkpoint. Carry pending decisions
        # through completed predecessors as well as explicitly provisional predecessors.
        inherited=[]
        ancestors=[v for v,_ in targets];seen=set()
        while ancestors:
            identifier=ancestors.pop()
            if identifier in seen:continue
            seen.add(identifier);ancestor=by_id.get(identifier,{})
            known=known and dependencies_known(ancestor)
            ancestor_refs=set(ancestor.get('blocked_by_decision') or [])|set(ancestor.get('provisional_decisions') or [])|{d['id'] for d in pending.values() if identifier in d['affected_items']}
            if ancestor_refs-set(decisions):known=False
            for did in ancestor_refs:
                if did in pending and did not in refs:inherited.append(pending[did]);refs.add(did)
            if ancestor.get('status')!='superseded':ancestors.extend(dependency_target(v,by_id)[0] for v in ancestor.get('depends_on') or [])
        blocked+=inherited
        provisional=bool(known and not (refs-set(decisions)) and not unresolved and blocked and goal.get('allow_provisional',True)
            and item.get('provisional_scope')=='local_files_and_tests' and all(d['reversible'] and d['recommendation'] and not d.get('approval_id') for d in blocked))
        ready=known and not unresolved and not (refs-set(decisions)) and (not blocked or provisional)
        return {'dependency_state':'known' if known else 'unconfirmed','ready':ready,'unresolved_dependencies':unresolved,
                'pending_decisions':[d['id'] for d in blocked], 'provisional':provisional,
                'provisional_decisions':[d['id'] for d in blocked] if provisional else []}

    def builder_item_guard(self,task):
        goal=self.guard(task['goal_id'],task['generation']);item=self.store.get('plan_item',task['plan_item_id'])
        if terminal(item):raise GoalLimit('Terminal item cannot execute Builder tools.')
        if task.get('decision_revision',0)!=item.get('decision_revision',0):raise GoalLimit('Decision changed; this Builder revision must stop.')
        view=self.dependency_view(goal,item)
        if not view['ready']:raise GoalLimit('Item is waiting for a decision or dependency.')
        if item.get('provisional_decisions',[])!=view['provisional_decisions']:
            item=self.store.update('plan_item',item['id'],provisional=view['provisional'],provisional_decisions=view['provisional_decisions'])
        return goal,item

    @staticmethod
    def core_spec_path(path):
        value=str(path).replace('\\','/').lower()
        return Path(value).suffix in ('.yaml','.yml','.json') or any(p in ('spec','specs','specification','specifications') for p in value.split('/'))

    def provisional_guard(self,task,name,args):
        goal,item=self.builder_item_guard(task)
        ids=item.get('provisional_decisions',[])
        if not ids:return
        if name in ('read_file','read_structured','list_files','search_files','code_map','memory_search','memory_read','request_decision','ask_planner','report_item','check_long_command','cancel_long_command'):return
        if name in ('write_file','edit_file'):
            if self.core_spec_path(args.get('path','')):raise PermissionError('PROVISIONAL work cannot change specification/core structured files.')
            return
        if name=='run_command':
            argv=args.get('argv',[])
            if len(argv)>=3 and argv[0] in ('python','python3','python.exe',sys_executable()) and argv[1]=='-m' and argv[2] in ('pytest','unittest'):
                return
            raise PermissionError('PROVISIONAL commands are limited to local pytest/unittest verification. No GPU, proxy credentials, git, or arbitrary programs.')
        raise PermissionError('PROVISIONAL work is restricted to local file edits and local tests; GPU and additional paid work are forbidden.')

    def provisional_file(self,task,path):
        item=self.store.get('plan_item',task['plan_item_id']);ids=item.get('provisional_decisions',[])
        if not ids:return
        identifier=hashlib.sha256((task['goal_id']+'\0'+path).encode()).hexdigest()[:24]
        try:old=self.store.get('goal_provisional_file',identifier)
        except ValueError:old={}
        self.store.save('goal_provisional_file',{'id':identifier,'goal_id':task['goal_id'],'path':path,'plan_item_id':item['id'],
            'status':'PROVISIONAL','decision_ids':list(dict.fromkeys([*old.get('decision_ids',[]),*ids])),'created':old.get('created',time.time())})
        self.provisional_manifest(self.store.get('goal',task['goal_id']))

    def provisional_manifest(self,goal):
        rows=[{k:r[k] for k in ('path','plan_item_id','status','decision_ids')} for r in self.store.items('goal_provisional_file',None) if r['goal_id']==goal['id']]
        if not rows:return
        ws=Workspace(goal['workspace'],self.store.root);path='.muse-provisional-'+goal['id']+'.json'
        # The dedicated manifest marks files without inserting comments into arbitrary formats.
        content=json.dumps({'goal_id':goal['id'],'files':rows},ensure_ascii=False,indent=2)+'\n'
        sha=ws.read(path)['sha256'] if ws.path(path).exists() else None
        ws.write(path,content,sha)

    def wake_decisions(self,goal_id):
        with self.lock:
            goal=self.store.get('goal',goal_id)
            if goal['status']=='waiting_decision':self.save(goal,status='running',reason='')
            self.decision_events.setdefault(goal_id,threading.Event()).set()

    def answer_decision(self,goal_id,data):
        if set(data)-{'goal_id','decision_id','answer','option','withdraw'}:raise ValueError('Unknown decision answer field.')
        with self.lock:
            goal=self.store.get('goal',goal_id);decision=self.store.get('decision',data['decision_id'])
            if decision['goal_id']!=goal_id or decision['state']!='pending':raise ValueError('The decision is no longer pending for this Goal.')
            option=data.get('option');free=data.get('answer','')
            if option is not None and (not isinstance(option,str) or option not in decision['options']):raise ValueError('Choose a listed option.')
            if not isinstance(free,str):raise ValueError('Direct answer must be text.')
            free=free.strip()
            if len(free)>4000:raise ValueError('Direct answer must contain at most 4000 characters.')
            answer=(option+'\n\n'+free) if option and free else option or free
            withdraw=data.get('withdraw',False)
            if type(withdraw) is not bool:raise ValueError('Withdraw must be a boolean.')
            if not isinstance(answer,str) or not withdraw and not answer.strip() or len(answer)>8000 or contains_key(answer):raise ValueError('Provide a listed choice and/or a non-secret direct answer (at most 4000 direct-answer characters).')
            answer=answer.strip();same=not withdraw and answer==decision['recommendation']
            self.store.update('decision',decision['id'],state='withdrawn' if withdraw else 'answered',answer=answer,
                selected_option=option,answer_text=free,answered_at=time.time(),matches_recommendation=same,answered_utc=self.times(goal)['time_utc'],answered_local=self.times(goal)['time_local'])
            changed=[];direct=[]
            for item in self.items(goal_id):
                if item['status']=='superseded':continue
                affected=item['id'] in decision['affected_items'] or (item.get('needs_decision_replan') and 'dependency JSON' in decision.get('question',''))
                if not withdraw and affected:
                    self.store.update('plan_item',item['id'],decision_action_source=decision['id'],human_instruction=answer)
                    if explicit_reopen(answer,item,allow_unscoped_retry=len(decision['affected_items'])==1) and item.get('task_id') not in self.builders.active:
                        self.apply_decision_action(goal,item,'rework',answer,decision['id']);direct.append(item['id'])
                        continue
                if decision['id'] not in item.get('provisional_decisions',[]):
                    if not decision.get('approval_id') and item['id'] in decision['affected_items'] and (
                            item['status']=='blocked' or item.get('wait_reason') in ('deferred','platform_error') or (item.get('review_error') or {}).get('needs_human')):
                        self.store.update('plan_item',item['id'],needs_decision_replan=True)
                        changed.append(item['id'])
                    continue
                remaining=[v for v in item['provisional_decisions'] if v!=decision['id']]
                values={'provisional_decisions':remaining,'provisional':bool(remaining)}
                if not same:
                    values.update(status='rework',accepted_criteria={},continue_pending=False,task_id=None,
                        decision_revision=item.get('decision_revision',0)+1,needs_decision_replan=True,
                        decision_rework_reason='Human answer differs from provisional recommendation: '+decision['id'])
                    changed.append(item['id'])
                    for tid in list(self.builders.active):
                        if self.store.get('task',tid).get('plan_item_id')==item['id']:self.builders.cancel(tid)
                self.store.update('plan_item',item['id'],**values)
            for row in self.store.items('goal_provisional_file',None):
                if row['goal_id']!=goal_id or decision['id'] not in row['decision_ids']:continue
                ids=[v for v in row['decision_ids'] if v!=decision['id']]
                self.store.update('goal_provisional_file',row['id'],decision_ids=ids,status='REWORK' if not same else 'PROVISIONAL' if ids else 'CONFIRMED')
            if not withdraw and classification_requested(answer):
                for item in self.items(goal_id):
                    if item['id'] in decision['affected_items']:
                        self.store.update('plan_item',item['id'],acceptance_replan_pending=True,needs_decision_replan=True)
                        changed.append(item['id'])
            self.provisional_manifest(goal)
            if changed or goal.get('dependency_refresh_error'):self.save(goal,decision_replan_pending=True)
            if direct and not changed and all(dependencies_known(i) for i in self.items(goal_id)):
                self.save(goal,decision_replan_pending=False,dependency_refresh_error=None)
            approval_id=decision.get('approval_id')
            if approval_id:
                request=self.store.get('goal_approval',approval_id);slot=self.builders.approvals.get(approval_id)
                approved=not withdraw and (option if option is not None else answer)==decision['options'][0]
                self.store.update('goal_approval',approval_id,state='approved' if approved else 'denied',answer=answer,**self.times(goal))
                if slot:slot['approved']=approved;slot['event'].set()
            self.message(goal,'Human','Planner','ANSWER',answer or 'Decision withdrawn.',evidence=[decision['id']])
            self.wake_decisions(goal_id)
        return self.detail(goal_id)

    def update_dependencies(self,goal_id,data):
        if set(data)-{'goal_id','item_id',*DEPENDENCY_FIELDS}:raise ValueError('Only dependency/scheduling fields may be changed.')
        with self.lock:
            goal=self.store.get('goal',goal_id);item=self.store.get('plan_item',data['item_id'])
            if item['goal_id']!=goal_id:raise ValueError('Wrong Goal item.')
            if item.get('task_id') in self.builders.active:raise ValueError('Wait for this item\'s current Builder step before editing dependencies.')
            row={'key':plan_key(item),**{k:data[k] for k in DEPENDENCY_FIELDS if k in data}}
            patch=prepare_plan(self.items(goal_id),[row],max_plan_items=goal.get('max_plan_items',DEFAULT_PLAN_LIMIT));self.validate_decision_refs(goal,patch)
            self.apply_plan_patch(goal,patch)
            if all(dependencies_known(i) for i in self.items(goal_id)):self.save(goal,dependency_refresh_error=None)
            if 'blocked_by_decision' in data:
                for decision in self.pending_decisions(goal_id):
                    affected=set(decision['affected_items'])
                    if decision['id'] in data['blocked_by_decision']:affected.add(item['id'])
                    else:affected.discard(item['id'])
                    if affected!=set(decision['affected_items']):self.store.update('decision',decision['id'],affected_items=sorted(affected))
            self.store.save('goal_dependency_change',{'goal_id':goal_id,'plan_item_id':item['id'],'before':{k:item.get(k) for k in DEPENDENCY_FIELDS},
                'after':row,'created':time.time(),**self.times(goal)})
            self.wake_decisions(goal_id)
        return self.detail(goal_id)

    def validate_decision_refs(self,goal,patch):
        ids={d['id'] for d in self.decisions(goal['id'])}
        for row in [*patch['updates'],*patch['creates']]:
            if any(ref not in ids for ref in row.get('blocked_by_decision',[])):raise ValueError('Unknown decision reference in this Goal.')

    def validate_item_action(self,goal,item,action,instruction,superseded_reason=None,superseded_by=None):
        if action not in ('rework','review','blocked','superseded'):raise ValueError('Resolution must be rework, review, blocked, or superseded.')
        if item['status']=='superseded' and action!='superseded':raise ValueError('Superseded is terminal; create a replacement item instead.')
        if action=='superseded':self.validate_supersession(goal,item,superseded_reason or instruction,superseded_by)
        if not isinstance(instruction,str) or len(instruction)>8000 or contains_key(instruction):raise ValueError('Invalid item instruction.')
        if item.get('task_id') in self.builders.active:raise ValueError('Wait for the active Builder before reopening this item.')
        if action=='review' and not self.valid_evidence(goal,item):raise ValueError('A review resolution requires observed current evidence.')

    def apply_decision_action(self,goal,item,action,instruction,decision_id,source_kind='decision',superseded_reason=None,superseded_by=None):
        if source_kind=='directive':
            source=self.store.get('goal_directive',decision_id)
            if source['goal_id']!=goal['id'] or source['state'] not in ('pending','processing') or source['kind']=='note':raise ValueError('An active human instruction is required for item actions.')
        else:
            source=self.store.get('decision',decision_id)
            if source['goal_id']!=goal['id'] or source['state']!='answered':raise ValueError('An answered human decision is required for item actions.')
        self.validate_item_action(goal,item,action,instruction,superseded_reason,superseded_by)
        if action=='superseded':
            if not supersede_requested(source.get('text',source.get('answer',''))):raise ValueError('Superseded requires a human closure/cancel/supersede request.')
            return self.supersede_item(goal,item,superseded_reason or instruction,superseded_by,actor='Human via Planner',source=decision_id)
        token=('directive:' if source_kind=='directive' else '')+decision_id+':'+action
        if token in item.get('applied_decision_actions',{}):return self.store.get('goal_item_action',item['applied_decision_actions'][token])
        audit=self.store.save('goal_item_action',{'goal_id':goal['id'],'plan_item_id':item['id'],'decision_id':decision_id if source_kind=='decision' else None,'directive_id':decision_id if source_kind=='directive' else None,
            'action':action,'instruction':instruction,'before':item,'created':time.time(),**self.times(goal)})
        values={'status':action,'instruction':instruction or item.get('instruction',''),'needs_decision_replan':False,
            'review_error':None,'review_error_count':0,'continue_pending':False,'task_id':None,'wait_reason':None,'waiting_resume':None,
            'human_instruction':instruction,'decision_revision':item.get('decision_revision',0)+1}
        values['inbox_action_source' if source_kind=='directive' else 'decision_action_source']=decision_id
        values['applied_decision_actions']={**item.get('applied_decision_actions',{}),token:audit['id']}
        if action=='rework':
            values.update(accepted_criteria={},bundles=0,bundle_reported=0,review_trigger=None,document_cycle_active=False,
                          execution_cycle=(item.get('execution_cycle') or 0)+1,
                          cycle_started_at=time.time(),
                          historical_bundles=item.get('historical_bundles',0)+item['bundles'])
        self.store.update('plan_item',item['id'],**values)
        self.store.update('goal_item_action',audit['id'],state='applied')
        return audit

    def dependency_actions(self,goal,items,value):
        lookup={ref:i for i in items for ref in (i['id'],plan_key(i))}
        rows=value.get('actions',[])
        if not isinstance(rows,list) or len(rows)>plan_limit(goal.get('max_plan_items',DEFAULT_PLAN_LIMIT)):raise ValueError('actions must be a bounded array within this Goal plan capacity.')
        rows=rows+[r for r in value.get('items',[]) if isinstance(r,dict) and 'resolution' in r]
        result={}
        for row in rows:
            if not isinstance(row,dict):raise ValueError('Action must be an object.')
            item=lookup.get(row.get('key') or row.get('id'))
            if not item:raise ValueError('Action must identify an existing item key or ID.')
            source=item.get('decision_action_source')
            if not source:
                source=next((d['id'] for d in reversed(self.decisions(goal['id'])) if d['state']=='answered' and item['id'] in d['affected_items']),None)
            if not source:raise ValueError('Action needs an answered human decision affecting this item.')
            action=row.get('resolution');instruction=row.get('instruction') or item.get('human_instruction') or self.store.get('decision',source).get('answer','')
            if action not in ('rework','review','blocked','superseded'):raise ValueError('Resolution must be rework, review, blocked, or superseded.')
            if not isinstance(instruction,str) or len(instruction)>8000 or contains_key(instruction):raise ValueError('Invalid action instruction.')
            if action=='review' and not self.valid_evidence(goal,item):raise ValueError('A review resolution requires observed current evidence.')
            if item['id'] in result:raise ValueError('Provide only one resolution per item.')
            reason=row.get('reason',row.get('superseded_reason'));replacement=row.get('superseded_by')
            self.validate_item_action(goal,item,action,instruction,reason,replacement)
            if action=='superseded' and not supersede_requested(self.store.get('decision',source).get('answer','')):raise ValueError('Human closure request required for superseded.')
            result[item['id']]=(item,action,instruction,source,reason,replacement)
        return list(result.values())

    def refresh_dependencies(self,goal):
        self.release_answered_waits(goal)
        items=self.items(goal['id']);unknown=[i for i in items if i['status']!='superseded' and not dependencies_known(i)]
        if not unknown and not goal.get('decision_replan_pending'):return
        if goal.get('dependency_refresh_error') and not goal.get('decision_replan_pending'):return
        instruction=dependency_instruction(items,self.decisions(goal['id']));safe_actions=[];attempts=[]
        for retry in range(2):
            raw='';attempt=None
            try:
                raw,human_queue=self.inbox_phase(goal,None,instruction);value=self.parse(raw)
                attempt=self.store.save('goal_dependency_attempt',{'goal_id':goal['id'],'retry':retry,'response':redact(raw),
                    'instruction':redact(instruction),'created':time.time(),**self.times(goal)})
                attempts.append(attempt['id'])
                if value.get('items') is None:value['items']=[]
                safe_actions=self.dependency_actions(goal,items,value)
                patch=prepare_plan(items,value['items'],max_plan_items=goal.get('max_plan_items',DEFAULT_PLAN_LIMIT));self.validate_decision_refs(goal,patch)
                for item in items:
                    if not item.get('acceptance_replan_pending'):continue
                    raw=next((r for r in value['items'] if r.get('key') in (item['id'],plan_key(item)) or r.get('id')==item['id']),None)
                    if not raw or not ('acceptance_rules' in raw or any(isinstance(c,dict) for c in raw.get('acceptance',[]))):
                        raise ValueError('Human requested acceptance-kind change for '+plan_key(item)+'. Include explicit acceptance_rules keyed by the exact criterion text; do not discard this instruction.')
                if patch['creates']:raise ValueError('Dependency refresh updates existing items only; do not create new keys.')
                updated={r['id']:r for r in patch['updates']}
                if any(not all(k in updated.get(i['id'],{}) for k in ('depends_on','blocked_by_decision')) for i in unknown):raise ValueError('Every unknown item needs explicit dependency lists.')
                self.apply_plan_patch(goal,patch)
                for item,action,text,source,reason,replacement in safe_actions:self.apply_decision_action(goal,self.store.get('plan_item',item['id']),action,text,source,superseded_reason=reason,superseded_by=replacement)
                for item in self.items(goal['id']):
                    if item.get('needs_decision_replan'):self.store.update('plan_item',item['id'],needs_decision_replan=False,acceptance_replan_pending=False)
                self.save(goal,decision_replan_pending=False,dependency_refresh_error=None)
                self.store.update('goal_dependency_attempt',attempt['id'],validation='accepted')
                self.message(goal,'Planner','Builder','NOTE',str(value.get('summary','Dependencies updated from observed human answers.')))
                self.finish_inbox(goal,human_queue,value)
                self.release_answered_waits(self.store.get('goal',goal['id']))
                return
            except GoalLimit:raise
            except (ValueError,TypeError,KeyError) as error:
                if attempt is None:
                    attempt=self.store.save('goal_dependency_attempt',{'goal_id':goal['id'],'retry':retry,'response':redact(raw),
                        'instruction':redact(instruction),'created':time.time(),**self.times(goal)})
                    attempts.append(attempt['id'])
                self.store.update('goal_dependency_attempt',attempt['id'],validation='rejected',error=redact(str(error)))
                instruction+='\nSpecific validation error: '+redact(str(error))+'. Return corrected JSON once; use items:[] for no dependency changes.'
        # A malformed graph is a platform warning, never a new human question.
        for item,action,text,source,reason,replacement in safe_actions:self.apply_decision_action(goal,self.store.get('plan_item',item['id']),action,text,source,superseded_reason=reason,superseded_by=replacement)
        for item in self.items(goal['id']):
            if item.get('needs_decision_replan') and dependencies_known(item):
                self.store.update('plan_item',item['id'],needs_decision_replan=False,instruction=item.get('human_instruction') or item.get('instruction',''))
        self.save(goal,decision_replan_pending=False,dependency_refresh_error='Planner dependency validation failed; previous dependencies retained.',dependency_warning_attempts=attempts)
        self.message(goal,'System','Builder','NOTE','Platform warning: dependency validation failed twice. Previous dependencies retained; validated human item actions applied. Unknown dependencies remain blocked.',evidence=attempts)
        self.release_answered_waits(self.store.get('goal',goal['id']))

    def checkpoint_decision(self,goal,item):
        if re.search(r'human checkpoint|사람 확인|사용자 확인',item['title'],re.I):
            self.create_decision(goal,{'question':item['title'],'options':['Continue','Revise'],'recommendation':'Continue',
                'reason':'Review the observed checkpoint evidence before dependent work.','reversible':False},
                item_id=item['id'],source='checkpoint:'+item['id'])
            # Existing/unknown dependencies must be classified by the Planner once.
            self.save(goal,decision_replan_pending=True)

    def wait_for_decisions(self,goal):
        with self.lock:
            if self.interrupt_pending(goal['id']):return
            event=self.decision_events.setdefault(goal['id'],threading.Event());event.clear()
            goal=self.store.get('goal',goal['id'])
            if goal['status'] not in ('running','planning','waiting_approval','waiting_decision'):return
            pending=self.pending_decisions(goal['id'])
            dependency_warning=bool(goal.get('dependency_refresh_error') and any(not dependencies_known(i) for i in self.items(goal['id'])))
            if not pending and not dependency_warning:return
            items=self.items(goal['id'])
            # An answer may have arrived between scheduler selection and acquiring this lock.
            # Recheck work under the same lock used by answer_decision before sleeping.
            # Completion can race the earlier scheduler tick. An unconsumed chunk is work,
            # even after its Builder leaves active and before finished_batch runs.
            if any(i['status']=='in_progress' and i.get('task_id') and i.get('bundle_reported',0)<i['bundles'] for i in items):return
            if goal.get('decision_replan_pending') or any(self.dependency_view(goal,i)['ready'] and not i.get('needs_decision_replan') and (
                    self.candidate_ready(goal,i) or self.review_ready(i)) for i in items):return
            if any(self.store.get('task',tid).get('goal_id')==goal['id'] and self.store.get('task',tid)['status']!='waiting_approval' for tid in list(self.builders.active)):return
            summary=[{'id':d['id'],'question':d['question'],'blocked_items':[
                i['id'] for i in self.items(goal['id']) if d['id'] in self.dependency_view(goal,i)['pending_decisions']]} for d in pending]
            self.save(goal,status='waiting_decision',reason='Waiting for an explicit dependency edit; no model polling.' if dependency_warning and not pending else 'Waiting for human decisions; no runnable work remains.',decision_wait_summary=summary)
        # The next wake is a human answer/dependency edit, pause/stop, or the UTC deadline.
        event.wait(max(0,goal['deadline_epoch']-time.time()))


def sys_executable():
    import sys
    return sys.executable
