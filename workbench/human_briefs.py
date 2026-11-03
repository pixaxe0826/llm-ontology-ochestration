"""Human display fields only. No LLM, network, execution, decision or policy mutation."""
import json,re
from .security import contains_key,redact

CAUSES=('work','scope','platform','external')
ANSWERS=('yes','no','uncertain')
STR={'type':'string'}
HUMAN_BRIEF_SCHEMA={'type':'object','properties':{
    'language':STR,'summary':STR,'what_happened':{'type':'array','items':STR,'minItems':2,'maxItems':4},
    'cause':{'type':'string','enum':list(CAUSES)},
    'answer_helps':{'type':'object','properties':{'value':{'type':'string','enum':list(ANSWERS)},'reason':STR},'required':['value','reason']},
    'options':{'type':'array','items':{'type':'object','properties':{'value':STR,'label':STR,'effect':STR},'required':['value','label','effect']}},
    'recommendation':{'type':'object','properties':{'value':STR,'reason':STR},'required':['value','reason']}},
    'required':['language','summary','what_happened','cause','answer_helps','options','recommendation']}


class HumanBriefError(ValueError):pass


def display_language(value='Korean'):
    aliases={'korean':'Korean','ko':'Korean','ko-kr':'Korean','한국어':'Korean','english':'English','en':'English','en-us':'English'}
    if not isinstance(value,str) or value.strip().lower() not in aliases:raise ValueError('Human display language must be Korean or English.')
    return aliases[value.strip().lower()]


def ko(goal):return display_language(goal.get('human_display_language','Korean'))=='Korean'


def plain(text,limit=220):
    value=redact(str(text or ''))
    value=re.sub(r'\b(?:observation:|ledger:)?(?=[a-f0-9]{16,64}(?:\b|:))(?=[a-f0-9]*[a-f])[a-f0-9]{16,64}(?::\d+)?\b','[record]',value,flags=re.I)
    value=re.sub(r'```.*?```','',value,flags=re.S)
    return re.sub(r'\s+',' ',value).strip()[:limit]


def defect_groups(question,item=None,korean=True):
    try:raw=json.loads(question)
    except (ValueError,TypeError):raw={}
    defects=[]
    if isinstance(raw,dict):defects.extend(raw.get('defects',[]) if isinstance(raw.get('defects'),list) else [])
    if not defects:defects.extend((item or {}).get('defects',[]))
    if not defects:defects=[str(question)]
    tests=[('citation',r'Missing parenthesized|numeric.*(?:citation|evidence)|숫자.*근거',('숫자 근거 누락','Missing numeric citations')),
        ('section',r'Missing required (?:section|key)|필수.*(?:섹션|항목)',('문서 필수 항목 누락','Missing document sections/keys')),
        ('stale',r'Stale citation|Input changed|Unknown evidence|invalid.*evidence|valid observed evidence',('유효하지 않은 근거','Invalid or outdated evidence')),
        ('schema',r'JSON|schema|Unique plan keys|AttributeError|KeyError|NameError|TypeError|Traceback|Coordinator',('플랫폼 형식·코드 오류','Platform schema/code errors')),
        ('execution',r'exit|timeout|NOT RUN|not executed|GPU|command|실행',('실행·결과 확인 필요','Execution/results need checking'))]
    counts={};seen=set()
    for defect in defects:
        if not isinstance(defect,str) or defect in seen:continue
        seen.add(defect);matched=next(((key,labels) for key,pattern,labels in tests if re.search(pattern,defect,re.I)),('other',('기타 확인 사항','Other issues')))
        key,labels=matched;entry=counts.setdefault(key,{'kind':key,'label':labels[0 if korean else 1],'count':0});entry['count']+=1
    return list(counts.values())


def classify(question,item=None):
    text=str(question)+' '+json.dumps((item or {}).get('review_error') or {})+' '+' '.join((item or {}).get('defects',[]))
    if re.search(r'review_error|document validation|document evidence|Missing parenthesized|Missing required section|numeric/hash mismatch|invalid.*evidence|valid observed evidence|dependency JSON|Unique plan keys|schema|AttributeError|KeyError|NameError|TypeError|Coordinator|Traceback|플랫폼|검증.*(?:규칙|형식|오류)',text,re.I):return 'platform'
    if re.search(r'provider|APIConnection|HTTP [45]\d\d|429|quota|budget|deadline|access denied|authentication|접근.*차단|예산|공급자',text,re.I):return 'external'
    if re.search(r'chunk|batch|rework|not finish|did not|execution failed|NOT RUN|timeout|묶음|미완료|실행.*실패',text,re.I):return 'work'
    return 'scope'


def validate_brief(value,goal,data):
    def text(v,maximum=400):
        if not isinstance(v,str) or not v.strip() or len(v)>maximum or contains_key(v) or '```' in v or re.search(r'\b(?:observation:|ledger:)[\w-]+|\b[a-f0-9]{16}(?::\d+)?\b',v):raise HumanBriefError('Human display text must be concise, non-secret and free of internal evidence IDs/code.')
        return v.strip()
    required=set(HUMAN_BRIEF_SCHEMA['required'])
    if not isinstance(value,dict) or set(value)!=required:raise HumanBriefError('human_brief requires exactly: '+', '.join(sorted(required))+'.')
    language=display_language(value['language'])
    if language!=display_language(goal.get('human_display_language','Korean')):raise HumanBriefError('human_brief language must match human_display_language.')
    summary=text(value['summary'],180);happened=value['what_happened']
    if not isinstance(happened,list) or not 2<=len(happened)<=4:raise HumanBriefError('what_happened must contain 2–4 plain-language sentences.')
    happened=[text(v) for v in happened]
    if language=='Korean' and not re.search(r'[가-힣]',summary+' '.join(happened)):raise HumanBriefError('Write the human summary in Korean; preserve filenames/commands/numbers.')
    cause=value['cause'];answer=value['answer_helps']
    if cause not in CAUSES or not isinstance(answer,dict) or set(answer)!={'value','reason'} or answer['value'] not in ANSWERS:raise HumanBriefError('Invalid cause or answer_helps value.')
    answer={'value':answer['value'],'reason':text(answer['reason'],300)}
    options=value['options'];actual=data.get('options',[])
    if not isinstance(options,list) or len(options)!=len(actual):raise HumanBriefError('Explain every original option exactly once.')
    parsed=[]
    for option in options:
        if not isinstance(option,dict) or set(option)!={'value','label','effect'} or option['value'] not in actual or option['value'] in [p['value'] for p in parsed]:raise HumanBriefError('Option values must match the original decision options.')
        parsed.append({'value':option['value'],'label':text(option['label'],120),'effect':text(option['effect'],300)})
    recommended=value['recommendation']
    if not isinstance(recommended,dict) or set(recommended)!={'value','reason'} or recommended['value']!=data.get('recommendation',''):raise HumanBriefError('Recommendation must match the original decision.')
    return {'language':language,'summary':summary,'what_happened':happened,'cause':cause,'answer_helps':answer,'options':parsed,
        'recommendation':{'value':recommended['value'],'reason':text(recommended['reason'],300)}}


def context(manager,goal,data,item_id=None):
    items=manager.items(goal['id']);by_id={i['id']:i for i in items}
    affected=set(data.get('affected_items',[]));affected.add(item_id) if item_id else None
    blocked={i for i in affected if i in by_id and by_id[i].get('status') not in ('done','superseded')}
    while True:
        extra={i['id'] for i in items if i.get('status') not in ('done','superseded') and set(i.get('depends_on',[])).intersection(blocked)}-blocked
        if not extra:break
        blocked.update(extra)
    own=by_id.get(item_id) or next((by_id[i] for i in affected if i in by_id),{})
    active=[i for i in items if i['id'] not in blocked and i.get('status')=='in_progress']
    task_ids=set(own.get('task_ids',[]));ledger=[r for r in manager.ledger.rows(goal['id']) if r.get('state')!='released' and (own.get('id') and r.get('plan_item_id')==own['id'] or r.get('task_id') in task_ids)]
    impact={'blocked_count':len(blocked),'item_titles':[plain(by_id[i]['title'],140) for i in sorted(blocked)[:3]],
        'continuing':bool(active),'continuing_titles':[plain(i['title'],140) for i in active[:3]]}
    cost={'estimated_usd':sum(r.get('estimated_usd',0) for r in ledger),'known':bool(ledger),'bundles':own.get('bundles',0),'historical_bundles':own.get('historical_bundles',0)}
    return own,impact,cost


def make_brief(manager,goal,data,item_id=None,provided=None,source='server_fallback'):
    korean=ko(goal);item,impact,cost=context(manager,goal,data,item_id);category=classify(data.get('question',''),item)
    approval=None
    if data.get('approval_id'):
        try:approval=manager.store.get('goal_approval',data['approval_id'])
        except ValueError:pass
    if approval:category='scope'
    groups=defect_groups(data.get('question',''),item,korean);name=plain(item.get('title') or goal.get('title') or ('작업' if korean else 'Work'),100)
    labels={'work':('작업을 끝내기 위한 방향 확인이 필요합니다.','Work needs a completion strategy.'),
        'scope':('요구사항이나 진행 범위를 정해 주세요.','Choose the requirements or scope.'),
        'platform':('검증 규칙 또는 플랫폼 코드 확인이 필요합니다.','Validation rules or platform code need checking.'),
        'external':('외부 연결·권한·예산 조건을 확인해야 합니다.','External service, access or budget needs checking.')}
    summary=(name+' — '+labels[category][0 if korean else 1])[:180]
    reason_groups=' · '.join(g['label']+' '+str(g['count'])+('건' if korean else '') for g in groups[:4])
    events=[(f'이 작업은 현재 {item.get("attempts",0)}회 시도와 {cost["bundles"]}개 묶음을 사용했습니다.' if korean else f'This item has used {item.get("attempts",0)} attempts and {cost["bundles"]} batches.'),
        (f'진행을 막은 확인 사항은 {reason_groups}입니다.' if korean else 'The recorded issues are: '+reason_groups+'.')]
    explanations={'citation':('문서에 적힌 숫자와 주장의 출처를 검증 규칙이 확인하지 못했습니다.','The validator could not verify sources for numeric claims in the document.'),
        'schema':('에이전트가 반환한 데이터 형식 또는 플랫폼 코드 처리에서 오류가 기록됐습니다.','Agent response formatting or platform code processing recorded an error.'),
        'section':('문서에 필요한 항목이 빠졌거나 서버가 해당 구성을 인식하지 못했습니다.','Required document content is missing or was not recognized by the validator.'),
        'stale':('검토에 필요한 근거가 현재 파일 또는 실행 결과와 연결되지 않았습니다.','Review evidence could not be linked to the current files or execution results.'),
        'execution':('실행 결과가 없거나 실패해서 완료 여부를 확인해야 합니다.','Execution results are missing or failed, so completion still needs verification.')}
    if groups and groups[0]['kind'] in explanations:events.append(explanations[groups[0]['kind']][0 if korean else 1])
    helps='yes' if category=='scope' else 'no' if category=='external' else 'uncertain'
    reasons={'scope':('답변을 계획에 반영해 진행 방향을 정할 수 있습니다.','The answer can guide the next plan.'),
        'work':('범위를 조정할 수 있지만 구현·실행 성공은 별도로 확인해야 합니다.','Scope can be adjusted, but implementation and successful execution still need verification.'),
        'platform':('답변만으로는 내부 검증이나 코드 문제가 해결되지 않을 수 있습니다.','An answer alone may not fix the validation rule or platform code.'),
        'external':('실제 연결·접근 권한·예산 조건을 고쳐야 하며 답변만으로는 해결되지 않습니다.','Service, access or budget conditions must actually be corrected; an answer alone is insufficient.')}
    option_labels={'approve':('승인','Approve'),'reject':('거절','Reject'),'deny':('거절','Deny'),'retry':('다시 시도','Retry'),'split':('범위 나누기','Split scope'),'defer':('나중에 진행','Defer'),'continue':('계속 진행','Continue')}
    options=[]
    for number,raw in enumerate(data.get('options',[]),1):
        action=next((action for action in ('reject','deny','approve','defer','split','retry','continue') if re.search(r'\b'+action+r'\b',raw,re.I)),raw.lower())
        label=option_labels.get(action,(plain(raw,100),plain(raw,100)))[0 if korean else 1]
        if not label or label.startswith(('{','[')):label=('선택 ' if korean else 'Option ')+str(number)
        effect=('이 답변을 계획에 반영합니다. 실행은 기존 승인·예산 규칙을 따릅니다.' if korean else 'Record this choice for planning. Existing approval and budget rules still govern execution.')
        effects={'approve':('요청한 작업의 승인으로 기록합니다. 기존 실행·비용 상한은 유지됩니다.','Record approval for the requested action; execution and spending limits remain.'),
            'reject':('이 요청은 승인하지 않습니다. 이 결과에 의존하는 작업은 계속 대기할 수 있습니다.','Decline this request; dependent work may remain waiting.'),
            'retry':('재시도 지시를 계획에 반영합니다. 성공은 새 실행 근거로 확인해야 합니다.','Plan a retry; success must be verified with new execution evidence.'),
            'split':('범위를 더 작은 작업으로 나누도록 Planner에 전달합니다.','Ask the Planner to split this scope into smaller tasks.'),
            'defer':('해당 범위는 보류하고 가능한 독립 작업을 우선하도록 전달합니다.','Defer this scope and prioritize independent work.'),
            'continue':('현재 방향으로 진행하도록 전달합니다. 미해결 조건은 그대로 적용됩니다.','Continue in the current direction; unresolved constraints still apply.')}
        effect=effects.get(action,(effect,effect))[0 if korean else 1]
        if category=='platform':effect+=(' 플랫폼 수정은 별도로 필요할 수 있습니다.' if korean else ' A platform fix may still be required separately.')
        options.append({'value':raw,'label':label,'effect':effect})
    result={'language':'Korean' if korean else 'English','cause':category,'summary':summary,'what_happened':events,
        'answer_helps':{'value':helps,'reason':reasons[category][0 if korean else 1]},'options':options,
        'recommendation':{'value':data.get('recommendation',''),'reason':('저장된 추천 방향입니다. 위 해결 가능성과 영향 범위를 함께 확인하세요.' if korean else 'This is the recorded recommendation; consider resolution limits and impact above.')}}
    if approval:
        kind=approval['kind'];pending=approval.get('state')=='pending';payload=approval.get('payload',{})
        action=('로컬 명령' if kind=='command' else 'GPU 작업') if korean else ('local command' if kind=='command' else 'GPU job')
        result['summary']=name+' — '+(action+' 승인이 필요합니다.' if korean else 'Approval is needed for a '+action+'.')
        description=plain(' '.join(str(v) for v in payload.get('argv',[])),220) if kind=='command' else plain((payload.get('spec') or {}).get('gpu','GPU'),80)
        result['what_happened']=[('승인 요청 대상: ' if korean else 'Requested action: ')+description,
            ('자동 승인 조건에 맞지 않아 이 요청은 아직 실행되지 않았습니다.' if korean else 'This request did not meet automatic approval conditions and has not been executed.')] if pending else [
            ('이전 실행에서 승인을 요청한 기록입니다.' if korean else 'This is an approval request from an earlier execution.'),
            ('현재 실행 대기는 끝났으므로 재개 후 요청 상태를 다시 확인해야 합니다.' if korean else 'The execution wait has ended; inspect its request state after resuming.')]
        result['answer_helps']={'value':'yes' if pending else 'uncertain','reason':('승인 여부를 결정하면 대기 요청을 처리합니다. 기존 실행·예산 상한은 유지됩니다.' if korean and pending else '현재 활성 요청인지 확인해야 하며 답변만으로 재실행되지는 않습니다.' if korean else 'An answer resolves the waiting request; execution and budget limits remain.' if pending else 'Check whether a request is active; an answer alone does not replay execution.')}
        groups=[]
    if provided and category!='platform':result={**provided};source='model'
    failure=data.get('failure')
    if failure:
        # Server-observed task ID/error is deliberately visible here; notifications stay generic.
        from .builder_errors import safe_error
        failure={k:(safe_error(failure[k],1800 if k=='error' else 240) if failure.get(k) else '') for k in ('task_id','stage','error','exception_type','category','diagnostic_id')}
        category='external' if failure['category']=='provider' else 'platform'
        result.update(failure=failure,cause=category,summary=(('Builder 실패: ' if korean else 'Builder failed: ')+failure['error'][:80]+' · '+failure['stage'][-40:]+' · Task '+failure['task_id'])[:180],
            what_happened=[('실패 단계: ' if korean else 'Failing stage: ')+failure['stage'],('관찰된 오류: ' if korean else 'Observed error: ')+failure['error']],
            answer_helps={'value':'uncertain','reason':reasons[category][0 if korean else 1]})
        groups=[{'kind':'schema' if category=='platform' else 'external','label':'플랫폼 실행 오류' if korean and category=='platform' else '공급자 오류' if korean else 'Platform execution error' if category=='platform' else 'Provider error','count':1}]
    result.update(impact=impact,cost_time=cost,defect_groups=groups,source=source,version=1)
    return result


def note_summary(manager,goal):
    items=manager.items(goal['id']);done=sum(i['status']=='done' for i in items);active=sum(i['status']=='in_progress' for i in items)
    blocked=sum(i['status'] in ('blocked','waiting') for i in items);pending=len(manager.pending_decisions(goal['id']))
    rows=manager.ledger.rows(goal['id'])
    amounts={p:sum(r.get('estimated_usd',0) for r in rows if r.get('provider')==p and r.get('state')!='released') for p in ('baseten','meta','modal')}
    spend=' · '.join(p+' $'+format(v,'.4f') for p,v in amounts.items())
    return (f'완료 {done}개, 진행 중 {active}개입니다. 대기·막힘 {blocked}개와 답변 대기 {pending}건이 있습니다. 추정 지출: {spend}.' if ko(goal) else f'{done} items completed; {active} in progress. {blocked} waiting/blocked, with {pending} pending decisions. Estimated spend: {spend}.')


class HumanBriefs:
    def human_message_view(self,goal,message,display_context=None):
        display_context=display_context or {}
        if message.get('to')!='Human':return message
        if message.get('type') in ('ESCALATE','QUESTION'):
            refs=message.get('evidence_refs',[])
            linked=next((d for d in (display_context.get('decisions') if 'decisions' in display_context else self.decisions(goal['id'])) if d['id'] in refs or d.get('approval_id') in refs),None)
            if linked and linked.get('human_brief'):return {**message,'human_brief':linked['human_brief']}
            return {**message,'human_brief':make_brief(self,goal,{'question':message.get('body',''),'affected_items':[message['plan_item_id']] if message.get('plan_item_id') else []},message.get('plan_item_id'),source='message_template')}
        if message.get('type')=='NOTE' and (not message.get('human_summary') or message.get('human_display_language','Korean')!=goal.get('human_display_language','Korean')):
            # Explicitly label live context; never claim current totals were historical totals.
            prefix='현재 상태 요약 (원래 진행 기록은 아래 상세): ' if ko(goal) else 'Current context (original progress record below): '
            return {**message,'human_summary':prefix+(display_context['summary'] if 'summary' in display_context else note_summary(self,goal)),'human_display_language':goal.get('human_display_language','Korean')}
        return message

    def backfill_human_briefs(self,goal_id=None,language_changed=False):
        count=0
        for decision in self.store.items('decision',None):
            if decision.get('state')!='pending' or goal_id and decision['goal_id']!=goal_id:continue
            if decision.get('human_brief') and not language_changed and not (decision.get('failure') and not decision['human_brief'].get('failure')):continue
            try:goal=self.store.get('goal',decision['goal_id'])
            except ValueError:continue
            brief=make_brief(self,goal,decision,(decision.get('affected_items') or [None])[0],source='legacy_template')
            self.store.save('decision',{**decision,'human_brief':brief},preserve_updated=True);count+=1
        return count
