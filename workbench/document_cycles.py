"""Bounded document-only follow-ups. No execution permission is granted by a verdict."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import time

from .acceptance_rules import rule_for
from .review_evidence import EXECUTION_KINDS
from .human_briefs import ko
from .security import redact


def defects(value):
    result=[]
    for row in value.get('defects',[]):
        if isinstance(row,str) and row.strip():result.append({'kind':'unspecified','text':row})
        elif isinstance(row,dict) and set(row)<= {'kind','text'} and row.get('kind') in ('document_fix','execution','other') and isinstance(row.get('text'),str) and row['text'].strip():result.append(dict(row))
        else:raise ValueError('Review defects must be concrete strings or {kind: document_fix|execution|other, text: actionable defect}.')
    return result


def signature(rows):
    # Evidence IDs and whitespace do not turn the same substantive defect into new work.
    texts=[re.sub(r'\b(?:observation:|ledger:)?[a-f0-9]{16}(?::\d+)?\b','<evidence>',r['text'].casefold()) for r in rows]
    return hashlib.sha256(json.dumps(sorted(' '.join(s.split()) for s in texts)).encode()).hexdigest()


class DocumentCycles:
    @staticmethod
    def bundle_limit(item):
        return 2 if item.get('document_cycle_active') else 4

    def execution_proof(self,goal,item,value):
        valid=self.valid_evidence(goal,item);proof={}
        for row in value.get('criteria',[]):
            if not isinstance(row,dict):continue
            name=row.get('criterion');ids=row.get('evidence_ids',[])
            if name in item['acceptance'] and rule_for(item,name)['kind']!='document' and isinstance(ids,list) and ids and all(isinstance(i,str) and i in valid for i in ids) and any(valid[i]['kind'] in EXECUTION_KINDS for i in ids):proof[name]=ids
        required={c for c in item['acceptance'] if rule_for(item,c)['kind']!='document'}
        return proof if required<=proof.keys() and self.fresh_rerun_observed(goal,item,valid) else None

    def document_cycle_assessment(self,goal,item,value):
        details=defects(value)
        if value.get('decision')!='rework' or not details or any(r['kind']!='document_fix' for r in details):return None
        if value.get('next_items'):return None  # Scope changes are reviewed through the normal plan path.
        if item['bundles']<self.bundle_limit(item) or self.child_items(goal,item):return None
        if item.get('document_cycles_used',0)>=2:return None
        fingerprint=signature(details)
        if fingerprint==item.get('document_cycle_defect_signature'):return None
        proof=self.execution_proof(goal,item,value)
        if proof is None:return None
        paths=sorted({p for c in item['acceptance'] if rule_for(item,c)['kind']=='document' for p in rule_for(item,c).get('document_paths',[])})
        if not paths:
            from .document_evidence import document_bundle
            bundle=document_bundle(self,goal,item)
            paths=sorted({f['path'] for row in (bundle or {}).get('criteria',[]) for f in row.get('files',[])})
        # Never infer write permission from arbitrary Planner prose or a non-document path.
        if not paths or any(PurePosixPath(p).suffix.lower() not in ('.md','.markdown','.txt','.rst','.html') and PurePosixPath(p).name.upper() not in ('LICENSE','LICENCE','COPYING','NOTICE') for p in paths):return None
        return {'defects':details,'signature':fingerprint,'execution_criteria':proof,'paths':paths}

    def open_document_cycle(self,goal,item,assessment,source):
        history={k:item.get(k) for k in ('bundles','bundle_reported','attempts','reworks','execution_cycle','task_id','status')}
        audit=self.store.save('goal_document_cycle',{'goal_id':goal['id'],'plan_item_id':item['id'],'source':source,'before':history,
            **assessment,'created':time.time(),**self.times(goal)})
        instruction='DOCUMENT-ONLY FOLLOW-UP. Fix ONLY the listed defects in '+', '.join(assessment['paths'])+'. No new commands, tests, evaluations, GPU jobs, external research or other file changes. Preserve all human instructions. Maximum two chunks.\n'+json.dumps(assessment['defects'],ensure_ascii=False)
        if item.get('human_instruction'):instruction+='\nHUMAN INSTRUCTION (still binding):\n'+item['human_instruction']
        self.store.update('plan_item',item['id'],status='rework',bundles=0,bundle_reported=0,task_id=None,continue_pending=False,wait_reason=None,waiting_resume=None,
            execution_cycle=(item.get('execution_cycle') or 0)+1,historical_bundles=item.get('historical_bundles',0)+item['bundles'],
            document_cycle_active=True,document_cycle_paths=assessment['paths'],document_cycle_execution_criteria=assessment['execution_criteria'],
            document_cycle_defect_signature=assessment['signature'],document_cycles_used=item.get('document_cycles_used',0)+1,
            document_cycle_audit=audit['id'],instruction=instruction,defects=[r['text'] for r in assessment['defects']],defect_details=assessment['defects'],
            review_trigger=None,review_error=None,review_error_count=0,needs_decision_replan=False)
        note=self.message(goal,'System','Human','NOTE','Opened bounded document-only follow-up (no new execution; at most two chunks): '+ '; '.join(r['text'] for r in assessment['defects']),item['id'],[audit['id']])
        summary=(f"{item['title']}: 실행 기준은 충족했고 문서 형식·인용·문구 수정만 남았습니다. 사람 판단 없이 문서 전용 자동 주기를 열었습니다. 새 실행 없이 최대 2묶음으로 지정 결함만 고칩니다." if ko(goal) else f"{item['title']}: execution criteria have evidence; only document wording/format/citation fixes remain. Opened an automatic document-only cycle, at most two chunks, without new execution.")
        self.store.update('message',note['id'],human_summary=redact(summary))
        return audit

    def document_cycle_guard(self,task,name,args):
        item=self.store.get('plan_item',task['plan_item_id'])
        if not item.get('document_cycle_active'):return
        reads={'read_file','read_structured','search_files','list_files','code_map','memory_search','memory_read','report_item','ask_planner','request_decision','get_gpu_job','read_gpu_artifact','check_long_command'}
        if name in reads:return
        if name in ('write_file','edit_file'):
            # Workspace.path still applies the existing traversal/absolute/secret checks.
            path=str(args.get('path','')).replace('\\','/')
            if path.startswith('./'):path=path[2:]
            if path in item.get('document_cycle_paths',[]):return
        raise PermissionError('Document-only follow-up: only specified document edits and read-only evidence inspection are allowed; no new execution.')
