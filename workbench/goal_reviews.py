"""Planner format failures never become Builder defects or spend Builder retries."""
import json
from pathlib import Path
import re
import time
from .goal_costs import GoalLimit
from .goal_prompts import REVIEW_SCHEMA
from .plan_updates import prepare_plan,plan_key
from .security import contains_key,redact
from .workspace import Workspace
from .command_cwd import MISSING,normalize_cwd
from .review_evidence import EXECUTION_KINDS,packet,review_scope_items
from .acceptance_rules import needs_execution,has_documents,rule_for
from .document_evidence import DocumentEvidenceError,goal_sources,validate_document,document_bundle,compact_bundle,preferred_documents
from .spec_citations import add_spec_sources
from .plan_capacity import PlanCapacityError,DEFAULT as DEFAULT_PLAN_LIMIT
from .document_cycles import defects
from .item_states import terminal
from .goal_gpu import GPUArtifactEvidenceError,require_recovery


def execution_criterion(text):
    return bool(re.search(r'\b(run|runs|running|passed|passing|executed|execution|renders?|rendered|retrieves?|retrieved|simulation)\b|실행|통과|렌더|시뮬레이션',text,re.I))


class GoalReviews:
    def apply_plan_patch(self,goal,patch,actor='Planner',source=None):
        with self.lock:return self._apply_plan_patch(goal,patch,actor,source)

    def _apply_plan_patch(self,goal,patch,actor,source):
        supersessions=[]
        for row in patch['updates']:
            if row.get('status')=='superseded':
                old=self.store.get('plan_item',row['id'])
                reason,replacement=self.validate_supersession(goal,old,row['superseded_reason'],row.get('superseded_by'),additional_items=patch['creates'])
                supersessions.append((old,reason,replacement))
        for row in patch['creates']:
            if row.get('split_from'):
                parent=self.store.get('plan_item',row['split_from'])
                row['blocked_by_decision']=list(dict.fromkeys([*row.get('blocked_by_decision',[]),*parent.get('blocked_by_decision',[])]))
        self.validate_decision_refs(goal,patch)
        for row in patch['updates']:
            old=self.store.get('plan_item',row['id'])
            changes={k:row[k] for k in ('key','title','acceptance','role')};changes['parent']=row['parent_id']
            if 'acceptance_rules' in row:changes['acceptance_rules']=row['acceptance_rules']
            if old.get('acceptance_rules',{})!=changes.get('acceptance_rules',old.get('acceptance_rules',{})):
                self.store.save('goal_acceptance_change',{'goal_id':goal['id'],'plan_item_id':old['id'],
                    'before':old.get('acceptance_rules',{}),'after':changes['acceptance_rules'],
                    'decision_id':old.get('decision_action_source'),'created':time.time(),**self.times(goal)})
            changes.update({k:row[k] for k in ('depends_on','blocked_by_decision','priority','provisional_scope') if k in row})
            changes.update({k:row[k] for k in ('files_to_read','files_to_write','commands_to_run') if k in row})
            if old['status']=='done' and (old['acceptance']!=row['acceptance'] or old.get('acceptance_rules',{})!=row.get('acceptance_rules',{})):
                changes.update(status='review',accepted_criteria={})
            self.store.update('plan_item',row['id'],**changes)
        for index,row in enumerate(patch['creates']):
            self.store.save('plan_item',{'id':row['id'],'key':row['key'],'goal_id':goal['id'],'parent':row['parent_id'],
                'title':row['title'],'acceptance':row['acceptance'],'status':'todo','role':'Builder','attempts':0,'reworks':0,
                'bundles':0,'evidence':[],'task_ids':[],'created':time.time()+index/100000,**self.times(goal)})
            self.store.update('plan_item',row['id'],**{k:row[k] for k in ('depends_on','blocked_by_decision','priority','provisional_scope','split_from','acceptance_rules','files_to_read','files_to_write','commands_to_run') if k in row})
        for decision in self.pending_decisions(goal['id']):
            affected=set(decision['affected_items'])
            for row in [*patch['updates'],*patch['creates']]:
                if 'blocked_by_decision' not in row:continue
                if decision['id'] in row['blocked_by_decision']:affected.add(row['id'])
                else:affected.discard(row['id'])
            if affected!=set(decision['affected_items']):self.store.update('decision',decision['id'],affected_items=sorted(affected))
        for old,reason,replacement in supersessions:self.supersede_item(goal,self.store.get('plan_item',old['id']),reason,replacement,actor=actor,source=source,before_snapshot=old)
        self.reconcile_waiting(goal)
        self.inspect_schedule(goal)  # record only; paid repair belongs to the coordinator

    def fresh_rerun_observed(self,goal,item,valid):
        requirement=item.get('rerun_requirement')
        if not requirement:return True
        ws=Workspace(goal['workspace'],self.store.root)
        if ws.read(requirement['script'])['sha256']!=requirement['script_sha256']:return False
        expected=requirement['argv']
        for ref in valid.values():
            if ref['kind']!='run_command' or ref.get('task_id') in requirement['stale_task_ids']:continue
            task=self.store.get('task',ref['task_id'])
            for event in reversed(task['events'][:ref['event_index']]):
                if event['kind']!='tool' or event.get('name')!='run_command':continue
                args=event.get('arguments',{});argv=args.get('argv',[])
                if len(argv)==len(expected) and Path(argv[0]).name.lower().removesuffix('.exe') in ('python','python3') and all(a.replace('\\','/')==b for a,b in zip(argv[1:],expected[1:])) and normalize_cwd(args.get('cwd',MISSING))[0]=='.':return True
                break
        return False

    def validate_review(self,goal,item,value,review_only=False):
        if contains_key(json.dumps(value,ensure_ascii=False)):raise ValueError('Review output contains protected content.')
        valid=self.valid_evidence(goal,item);observed=self.observed_evidence(goal,item)
        own_valid=dict(valid);doc_sources=goal_sources(self,goal,item) if has_documents(item) else {}
        doc_valid={key:ref for key,ref in doc_sources.items() if ref.get('supports_acceptance')}
        if value.get('decision') not in ('accept','rework'):raise ValueError('Invalid review decision.')
        if not isinstance(value.get('defects'),list):raise ValueError('Review defects must be an array.')
        defects(value)
        if value['decision']=='rework' and not value['defects']:raise ValueError('A rework decision requires concrete defects.')
        if not isinstance(value.get('criteria'),list):raise ValueError('Invalid review criteria schema.')
        if not isinstance(value.get('goal_complete',False),bool):raise ValueError('goal_complete must be a boolean.')
        accepted={};seen=set()
        for criterion in value['criteria']:
            if not isinstance(criterion,dict):raise ValueError('Invalid criterion review.')
            name=criterion.get('criterion');refs=criterion.get('evidence_ids')
            if name not in item['acceptance'] or name in seen:raise ValueError('Unknown or duplicate acceptance criterion.')
            seen.add(name)
            document=rule_for(item,name)['kind']=='document'
            if document and isinstance(refs,list):
                add_spec_sources(self,goal,doc_sources,[r for r in refs if isinstance(r,str)])
                doc_valid={key:ref for key,ref in doc_sources.items() if ref.get('supports_acceptance')}
            allowed=(doc_valid if document else own_valid) if value['decision']=='accept' else (doc_sources if document else observed)
            if document and value['decision']=='accept' and isinstance(refs,list) and all(isinstance(ref,str) and ref in doc_sources for ref in refs):
                invalid=[ref for ref in refs if ref not in doc_valid]
                if invalid:raise DocumentEvidenceError(['Document reference is not current/successful: '+ref+'; '+doc_sources[ref].get('stale_reason','') for ref in invalid])
            if not isinstance(refs,list) or any(not isinstance(ref,str) or ref not in allowed for ref in refs):raise ValueError('Acceptance requires valid observed evidence IDs. For unmet rework criteria use evidence_ids: [] or criteria: [].')
            if value['decision']=='accept' and not refs:
                if document:raise DocumentEvidenceError(['Document acceptance requires current file read/write evidence.'])
                raise ValueError('Acceptance requires valid observed evidence IDs. For unmet rework criteria use evidence_ids: [] or criteria: [].')
            if value['decision']=='accept' and needs_execution(item,name) and not any(own_valid[ref]['kind'] in EXECUTION_KINDS for ref in refs):
                raise ValueError('Execution criteria require successful command or GPU evidence.')
            if value['decision']=='accept' and document:
                checked=validate_document(self,goal,item,name,rule_for(item,name),refs,doc_sources)
                if not checked['passed']:raise DocumentEvidenceError(checked['defects'])
                valid.update(doc_valid)
            if value['decision']=='accept':accepted[name]=refs
        if value['decision']=='accept':
            require_recovery(self,goal,item,own_valid,value['criteria'])
            if set(accepted)!=set(item['acceptance']):raise ValueError('Every acceptance criterion requires evidence.')
            if not self.fresh_rerun_observed(goal,item,own_valid):raise ValueError('Recovery requires a fresh successful execution of the unchanged proxy script; historical evidence is insufficient.')
        patch=prepare_plan(self.items(goal['id']),value.get('next_items',[]),max_plan_items=goal.get('max_plan_items',DEFAULT_PLAN_LIMIT))
        if review_only and value.get('next_items'):raise ValueError('Review-only: preserve the plan; next_items must be []. Return rework defects on this existing item.')
        if value['decision']=='rework' and not review_only and self.child_items(goal,item):
            if not any(row['parent_id']==item['id'] for row in patch['creates']):
                raise ValueError('Parent aggregate rework must create bounded child deliverables with parent set to the existing parent key/ID. Never assign the parent or request a parent batch-limit decision.')
        if value['decision']=='rework' and not review_only and item.get('review_trigger') in ('batch_limit','batch_limit_children_completed'):
            action=value.get('batch_action')
            if action not in ('split','defer') and not self.document_cycle_assessment(goal,item,value):raise ValueError('Incomplete batch-limit rework requires batch_action=split or defer, unless a verified bounded document-only cycle is eligible.')
            if action=='split':
                if not patch['creates']:raise ValueError('Split requires new bounded child items; do not reset the exhausted parent counters.')
                created={r['id'] for r in patch['creates']}
                for row in patch['creates']:
                    if row['parent_id'] not in (None,item['id']) and row['parent_id'] not in created:raise ValueError('Split items must be descendants of the exhausted item.')
                    if row['parent_id'] is None:row['parent_id']=item['id']
                    row['split_from']=item['id']
                    row['depends_on']=list(dict.fromkeys([*item.get('depends_on',[]),*row.get('depends_on',[])]))
                    row['blocked_by_decision']=list(dict.fromkeys([*item.get('blocked_by_decision',[]),*row.get('blocked_by_decision',[])]))
                graph={i['id']:i.get('depends_on',[]) for i in self.items(goal['id'])}
                graph.update({r['id']:r.get('depends_on',[]) for r in [*patch['updates'],*patch['creates']]})
                for identifier in created:
                    pending=list(graph[identifier]);seen=set()
                    while pending:
                        ref=pending.pop()
                        if ref==item['id'] or ref==identifier:raise ValueError('Split children must not depend on their exhausted parent or themselves.')
                        if ref in seen:continue
                        seen.add(ref);pending.extend(graph.get(ref,[]))
        self.validate_decision_refs(goal,patch)
        return accepted,valid,patch

    def review_ready(self,item):
        if terminal(item):return False
        if any(not terminal(c) for c in self.child_items(self.store.get('goal',item['goal_id']),item)):return False
        if self.long_commands.active(item['goal_id'],item['id']):return False
        error=item.get('review_error') or {}
        return item['status']=='review' and not error.get('needs_human') and time.time()>=error.get('retry_at',0)

    def review(self,goal,item,review_only=False):
        if terminal(item) or any(not terminal(c) for c in self.child_items(goal,item)):return False
        if (self.store.get('plan_item',item['id']).get('review_error') or {}).get('needs_human'):return False
        # Reindex existing records only; do not run commands or submit new GPU work.
        for owner in review_scope_items(self,goal,item):
            for task_id in owner.get('task_ids',[]):
                task=self.store.get('task',task_id)
                if task.get('goal_id')==goal['id'] and task.get('plan_item_id')==owner['id']:
                    self.collect_evidence(self.store.get('plan_item',owner['id']),task)
        item=self.store.get('plan_item',item['id'])
        base='Review this REPORT against every acceptance criterion using only supplied observed evidence IDs. Review trigger: '+str(item.get('review_trigger','manual'))+'. Progress reports are not reviewed before the four-chunk limit. At an incomplete batch limit, accept only if all criteria already have valid evidence; otherwise return rework and batch_action=split with new bounded child items, or batch_action=defer so unrelated items proceed. Never reset parent counters or make split children depend on their exhausted parent. Update the remaining plan with next_items only when necessary. Existing keys update existing items; operation=create explicitly creates a new item. goal_complete may be true only if this acceptance completes the entire goal. Schema: '+json.dumps(REVIEW_SCHEMA)
        base+=' Rework does NOT claim that criteria were satisfied: use criteria: [] or list unmet criteria with evidence_ids: []. Failed/timeout observations may explain defects but NEVER prove acceptance. If execution or artifacts are missing, return rework with concrete paths, failed argv/exit/timeout and the next correction, not an unsupported accept. For new plan items OMIT id; the server generates IDs. Never invent existing IDs.'
        base+=' For document-kind criteria, do NOT require a new command or GPU. Cite the current document file observation in criteria.evidence_ids; its inline parenthesized source IDs may refer to other items in this same Goal. For prose only, the server checks inline citation validity/success and recognized numeric/hash values. YAML/JSON/TOML/CSV data/config files require current hash, valid syntax and required keys only; never add value citations. All execution-kind criteria still require successful execution evidence. Document validation defects require concrete rework. execution-kind criteria retain execution evidence requirements.'
        base+=' For each remaining defect return {"kind":"document_fix"|"execution"|"other","text":"specific correction"}; legacy strings remain valid but are not eligible for automatic document cycles. document_fix means ONLY prose format/citation/wording edits, never missing execution or artifacts. If ALL execution criteria already passed, include their exact criterion/evidence_ids in criteria even for rework, so the server can verify eligibility for at most two document-only follow-up cycles (two chunks each). Never request fresh execution for a document-only fix. Parent items with unfinished children are not Builder work or batch-limit reviews; once children finish review their combined evidence. If a parent still lacks deliverables, next_items must create bounded child work with parent explicitly set to this existing parent key/ID, instead of assigning the parent or escalating its batch count.'
        if review_only:base+=' REVIEW-ONLY: evaluate existing evidence only. Do not execute, assign, request a human decision, split or create items. next_items MUST be []; incomplete work must be rework on this item. Counters/history are preserved and no Builder is launched.'
        prior=item.get('review_error') or {}
        feedback=('\nPrevious review-cycle validation failure: '+prior['message']+'. It remains invalid to claim acceptance without successful execution. Allowed verdict for missing execution: rework, with criteria: [] and concrete defects.') if prior.get('message') else ''
        attempt_ids=[]
        for retry in range(2):
            current=self.store.get('plan_item',item['id']);valid=self.valid_evidence(goal,current);observed=self.observed_evidence(goal,current)
            doc_sources=goal_sources(self,goal,current) if has_documents(current) else None
            documents=document_bundle(self,goal,current,refs=doc_sources) if doc_sources is not None else None
            preferred=[]
            if documents:
                preferred=preferred_documents(documents,doc_sources)
                observed={**doc_sources,**observed};valid={key:ref for key,ref in observed.items() if ref.get('supports_acceptance')}
            catalog=packet(observed,limit=8000,preferred=preferred)
            catalog['superseded_items']={i['id']:i.get('superseded_by') for i in self.child_items(goal,current) if i['status']=='superseded'}
            exposed={r['id'] for r in catalog['observed']}
            exposed_valid=[identifier for identifier in valid if identifier in exposed]
            own_valid=self.valid_evidence(goal,current)
            blocked_execution=any(needs_execution(current,c) for c in current['acceptance']) and not any(e['kind'] in EXECUTION_KINDS for e in own_valid.values())
            restriction='\nACCEPT IS NOT AVAILABLE: required successful command/GPU evidence is absent. Allowed decision: rework, concrete defects, criteria: []. Read-only tools cannot manufacture execution evidence.' if blocked_execution else ''
            instruction=base+feedback+restriction+'\nREVIEW EVIDENCE PACKET\n'+json.dumps(catalog,ensure_ascii=False)+'\nVALID OBSERVED EVIDENCE IDS: '+json.dumps(exposed_valid)
            if documents:instruction+='\nDOCUMENT VALIDATION (no new runs required)\n'+json.dumps(compact_bundle(documents),ensure_ascii=False)
            attempt=self.store.save('goal_review_attempt',{'goal_id':goal['id'],'plan_item_id':item['id'],'created':time.time(),
                'retry':retry,'state':'requesting','platform_error':False,'review_only':review_only,
                'valid_evidence_ids':exposed_valid,'observed_evidence_ids':list(observed),'evidence_packet':catalog,'document_validation':documents,'instruction':instruction,**self.times(goal)})
            attempt_ids.append(attempt['id'])
            raw=None
            try:
                previous=getattr(self.review_scope,'read_only',False)
                self.review_scope.read_only=review_only
                try:raw,human_queue=self.inbox_phase(goal,current,instruction,enabled=not review_only)
                finally:self.review_scope.read_only=previous
                self.store.update('goal_review_attempt',attempt['id'],response=redact(raw))
                value=self.parse(raw);current=self.store.get('plan_item',item['id'])
                try:accepted,valid,patch=self.validate_review(goal,current,value,review_only=review_only)
                except (DocumentEvidenceError,GPUArtifactEvidenceError) as error:
                    # Bad document content is Builder rework, not a Planner JSON/platform failure.
                    value={**value,'decision':'rework','defects':[{'kind':'execution' if isinstance(error,GPUArtifactEvidenceError) else 'document_fix','text':d} for d in error.defects],
                        'criteria':[c for c in value.get('criteria',[]) if isinstance(c,dict) and c.get('criterion') in current['acceptance'] and rule_for(current,c['criterion'])['kind']!='document'],
                        'goal_complete':False,'next_items':[],
                        'summary':'Document acceptance blocked by server checks: '+'; '.join(error.defects[:5])}
                    if current.get('review_trigger') in ('batch_limit','batch_limit_children_completed'):value['batch_action']='defer'
                    accepted,valid,patch=self.validate_review(goal,current,value,review_only=review_only)
                    self.store.update('goal_review_attempt',attempt['id'],server_enforced_rework=True,effective_response=value)
                self.store.update('goal_review_attempt',attempt['id'],state='validated')
                break
            except GoalLimit:raise
            except PlanCapacityError as error:
                self.store.update('goal_review_attempt',attempt['id'],state='capacity_wait',platform_error=False,error=str(error))
                self.capacity_notice(goal,self.store.get('plan_item',item['id']),error,'review_split')
                return False
            except (ValueError,TypeError,KeyError) as error:
                reason=redact(str(error));self.store.update('goal_review_attempt',attempt['id'],state='review_error',platform_error=True,error=reason)
                feedback='\nThe previous Planner review output failed server validation: '+reason+'. Correct the review JSON; this is not a Builder defect. Rework may use criteria: [] and does not need successful execution. If successful execution is absent, accept is forbidden; return rework with concrete failure/missing-artifact defects. Use existing keys to update and omit id on new items. Return the complete corrected object.'
        else:
            current=self.store.get('plan_item',item['id']);count=current.get('review_error_count',0)+1
            error={'kind':'review_error','platform_error':True,'message':reason,'attempt_ids':attempt_ids,
                   'retry_at':time.time()+30,'needs_human':count>=3}
            self.store.update('plan_item',item['id'],review_error=error,review_error_count=count)
            if count==3:self.message(goal,'System','Human','ESCALATE',json.dumps({'kind':'review_error','platform_error':True,
                'summary':'Planner review output failed validation in three review cycles. Builder retry counters were not charged.',
                'item_key':plan_key(current),'error':reason}),item['id'],attempt_ids)
            return False
        with self.lock:
            self.guard(goal['id'],goal['generation']);item=self.store.get('plan_item',item['id'])
            cycle=self.document_cycle_assessment(goal,item,value) if not review_only else None
            detail=defects(value);value={**value,'defects':[d['text'] for d in detail],'defect_details':detail}
            review_refs=list(valid) if value['decision']=='accept' else list(self.observed_evidence(goal,item))
            self.message(goal,'Planner','Builder','REVIEW',json.dumps(value,ensure_ascii=False),item['id'],review_refs)
            if value['decision']=='accept':
                self.store.update('plan_item',item['id'],status='done',accepted_criteria=accepted,review_summary=value.get('summary',''),defects=[],review_error=None,review_error_count=0,wait_reason=None,waiting_resume=None,document_cycle_active=False)
                self.checkpoint_decision(goal,item)
            else:
                reworks=item['reworks']+1
                limit=item.get('review_trigger') in ('batch_limit','batch_limit_children_completed')
                if cycle:
                    self.open_document_cycle(goal,item,cycle,attempt_ids[-1])
                elif not review_only and self.child_items(goal,item):
                    self.store.update('plan_item',item['id'],status='waiting_children',wait_reason='children',waiting_resume='review',
                        reworks=reworks,defects=value['defects'],review_error=None,review_error_count=0,continue_pending=False,review_trigger=None)
                elif review_only:
                    self.store.update('plan_item',item['id'],status='blocked' if reworks>=3 else 'rework',reworks=reworks,
                        defects=value['defects'],review_error=None,review_error_count=0,continue_pending=False,wait_reason=None,waiting_resume=None)
                elif limit and value.get('batch_action')=='split' and reworks<3:
                    self.park_item(item,'split_children',resume='review',split_item_ids=[r['id'] for r in patch['creates']],
                        reworks=reworks,defects=value['defects'],review_error=None,review_error_count=0,continue_pending=False)
                elif limit and reworks<3:
                    self.park_item(item,'deferred',resume=None,reworks=reworks,defects=value['defects'],review_error=None,review_error_count=0,continue_pending=False)
                    self.message(goal,'Planner','Human','ESCALATE','The four-chunk limit was reached. The Planner deferred this item; decide a scope split or other next step. '+value.get('summary',''),item['id'])
                else:
                    blocked=reworks>=3 or item['bundles']>=self.bundle_limit(item)
                    self.store.update('plan_item',item['id'],status='blocked' if blocked else 'rework',reworks=reworks,defects=value['defects'],review_error=None,review_error_count=0,wait_reason=None,waiting_resume=None)
                    if blocked:self.message(goal,'Planner','Human','ESCALATE',json.dumps({'title':item['title'],'defects':value['defects'],'reason':'Rework or Builder batch limit reached.'}),item['id'])
            self.apply_plan_patch(goal,patch)
            items=self.items(goal['id'])
            complete=value.get('goal_complete') is True and items and not self.pending_decisions(goal['id']) and not self.pending_directions(goal['id']) and all(i['status']=='superseded' or i['status']=='done' and set(i.get('accepted_criteria',{}))==set(i['acceptance']) and self.valid_evidence(goal,i) for i in items)
            summary=self.progress(goal,str(value.get('summary','')))
            self.save(goal,summary=summary,status='done' if complete else goal['status'] if review_only else 'running',final_report=summary if complete else '',
                      completion_verdict=bool(value.get('goal_complete')) or goal.get('completion_verdict',False))
            self.message(goal,'Planner','Human','NOTE',summary,item['id'])
        self.finish_inbox(self.store.get('goal',goal['id']),human_queue,value)
        if self.store.get('goal',goal['id'])['status']=='done':self.close_pending_notes(self.store.get('goal',goal['id']))
        if value['decision']=='accept' and self.store.get('plan_item',item['id'])['status']=='done':
            try:self.auto_commit_item(goal['id'],item['id'],attempt_ids[-1])
            except Exception as error:
                from .diagnostics import log_exception
                log_exception(self.store.root,error,component='Accepted item checkpoint',goal_id=goal['id'],plan_item_id=item['id'])
        return True
