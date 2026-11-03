'use strict';
const decisionDrafts=new Map(),decisionSelections=new Map(),decisionSending=new Set();
let dependencyEditing=null;
const decisionViews=new Map();
function decisionView(goalId){
  if(!decisionViews.has(goalId)){
    let saved;try{saved=JSON.parse(localStorage.getItem('muse.goal.decisions.'+goalId)||'null');}catch{}
    decisionViews.set(goalId,{groupOpen:saved?.groupOpen===true,expanded:new Set(Array.isArray(saved?.expanded)?saved.expanded.filter(id=>typeof id==='string'):[])});
  }
  return decisionViews.get(goalId);
}
function saveDecisionView(goalId){
  const view=decisionView(goalId);try{localStorage.setItem('muse.goal.decisions.'+goalId,JSON.stringify({groupOpen:view.groupOpen,expanded:[...view.expanded]}));}catch{}
}
function decisionQuestionObject(question){try{const value=JSON.parse(question);return value&&typeof value==='object'&&!Array.isArray(value)?value:null;}catch{return null;}}
function decisionShortText(value,max=100){const text=String(value||'').trim().split(/\r?\n/)[0].replace(/\s+/g,' ');return [...text].length>max?[...text].slice(0,max-1).join('')+'…':text;}
function goalDecisionTitle(decision,items=[]){
  if(decision.human_brief?.summary)return decisionShortText(decision.human_brief.summary,180);
  const raw=String(decision.question||''),value=decisionQuestionObject(raw);
  const item=items.find(i=>(decision.affected_items||[]).includes(i.id)||value&&(value.item_key===i.id||value.item_key===i.key||value.item_id===i.id));
  const itemTitle=decisionShortText(item?.title||value?.title,90);
  if(value?.kind==='review_error')return 'Planner 검토 오류'+(itemTitle?' — '+itemTitle:'');
  if(/four[- ]chunk limit|batch limit|묶음.*한도/i.test(value?.reason||value?.summary||raw))return '묶음 한도 도달'+(itemTitle?' — '+itemTitle:'');
  if(value)return decisionShortText(value.question||value.summary||value.title||'사용자 판단');
  return decisionShortText(raw)||'사용자 판단';
}
function goalDecisionTime(decision,goal){
  const epoch=decision.state==='withdrawn'?(decision.withdrawn_at??decision.answered_at):decision.answered_at;
  const date=epoch!=null?new Date(Number(epoch)*1000):new Date(decision.answered_utc||decision.answered_local||NaN);
  if(!Number.isFinite(date.getTime()))return {label:'처리 시각 없음',compact:'시각 없음',iso:null,zone:goal.timezone||'UTC'};
  let zone=goal.timezone||'UTC',format;
  try{format=new Intl.DateTimeFormat('ko-KR',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}catch{zone='UTC';format=new Intl.DateTimeFormat('ko-KR',{timeZone:zone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});}
  const parts=Object.fromEntries(format.formatToParts(date).map(p=>[p.type,p.value]));
  return {label:format.format(date)+' · '+zone,compact:`${parts.month}/${parts.day} ${parts.hour}:${parts.minute}`,iso:date.toISOString(),zone};
}
function setAllGoalDecisionDetails(open){
  const list=$('goal-decision-list'),goalId=list.dataset.goal;if(!goalId)return;
  const view=decisionView(goalId);view.groupOpen=open;view.expanded.clear();
  for(const card of list.querySelectorAll('details.goal-decision.resolved')){card.open=open;if(open)view.expanded.add(card.dataset.decisionId);}
  $('goal-decisions-processed').open=open;saveDecisionView(goalId);
}
function revealGoalDecision(decisionId){
  const card=$('decision-'+decisionId);if(!card)return null;
  if(card.matches('details.resolved')){
    const goalId=$('goal-decision-list').dataset.goal,view=decisionView(goalId);
    view.groupOpen=true;view.expanded.add(decisionId);$('goal-decisions-processed').open=true;card.open=true;saveDecisionView(goalId);
  }
  card.querySelector('summary')?.focus({preventScroll:true});return card;
}
function renderHumanBrief(card,brief){
  const english=brief.language==='English',word=(ko,en)=>english?en:ko;
  const causes={work:word('작업 진행 문제','Work progress'),scope:word('요구사항·범위 판단 필요','Requirements / scope'),platform:word('플랫폼·검증 규칙 문제','Platform / validation'),external:word('외부 문제','External conditions')};
  const box=el('section','human-brief'),badges=el('div','human-brief-badges');
  badges.append(el('span','human-cause '+brief.cause,causes[brief.cause]||causes.scope),el('strong','human-answer-helps',word('답하면 해결되는가: ','Will answering resolve this? ')+({yes:word('예','Yes'),no:word('아니오','No'),uncertain:word('불확실','Uncertain')}[brief.answer_helps?.value]||word('불확실','Uncertain'))));
  box.append(badges,el('h4','human-brief-summary',brief.summary));
  if(brief.failure){const f=brief.failure;const failed=el('section','human-failure');failed.append(el('strong','',word('실행 오류','Execution error')),el('p','','Task '+f.task_id+' · '+f.stage),el('pre','',f.error));box.append(failed);}
  box.append(el('p','human-resolution',brief.answer_helps?.reason||''));
  if(brief.cause==='platform')box.append(el('p','human-platform-notice',word('Codex 등에서 플랫폼 수정이 필요할 수 있음','A platform fix in Codex or a similar development tool may be needed.')));
  box.append(el('strong','',word('무슨 일이 있었나','What happened')));
  for(const sentence of brief.what_happened||[])box.append(el('p','human-happened',sentence));
  if(brief.defect_groups?.length){const groups=el('div','human-defect-groups');for(const group of brief.defect_groups)groups.append(el('span','',group.label+' '+group.count+word('건','')));box.append(groups);}
  if(brief.options?.length){
    box.append(el('strong','',word('선택하면 달라지는 점','What each choice changes')));const options=el('ul','human-options');
    for(const option of brief.options){const li=el('li');li.append(el('strong','',option.label+' — '),document.createTextNode(option.effect));options.append(li);}box.append(options);
  }
  const recommendation=brief.recommendation||{},label=brief.options?.find(o=>o.value===recommendation.value)?.label;
  if(label)box.append(el('p','human-recommendation',word('추천: ','Recommended: ')+label+' · '+recommendation.reason));
  if(brief.impact){const impact=brief.impact;box.append(el('p','human-impact',word('영향: ','Impact: ')+impact.blocked_count+word('개 작업 대기',' items blocked')+' · '+(impact.item_titles?.join(' · ')||word('없음','None'))+'. '+(impact.continuing?word('계속 진행 중: ','Continuing: ')+(impact.continuing_titles?.join(' · ')||''):word('현재 독립적으로 실행 중인 작업 없음','No independent item is currently running.'))));}
  if(brief.cost_time){const cost=brief.cost_time;box.append(el('p','human-spend',word('요청 시점 비용·묶음: ','Spend / batches at request: ')+(cost.known?'~$'+Number(cost.estimated_usd||0).toFixed(4):word('비용 미확인','Spend not attributed'))+' · '+cost.bundles+word('묶음',' batches')+(cost.historical_bundles?' · '+word('이전 주기 ','prior cycles ')+cost.historical_bundles:'')));}
  if(brief.applied_items?.length)box.append(el('p','human-applied-items',word('서버 반영 항목: ','Server-applied items: ')+brief.applied_items.join(' · ')));
  if(brief.unapplied?.length)box.append(el('p','human-unapplied',word('반영하지 못한 부분: ','Unapplied: ')+brief.unapplied.join(' · ')));
  if(brief.cancellation_requests)box.append(el('p','human-cancellations',word('GPU·장시간 명령 취소 요청: ','GPU / long-command cancellation requests: ')+brief.cancellation_requests+word('건. 실제 종료 상태는 실행 기록에서 확인하세요.','. Check execution records for terminal status.')));
  card.append(box);
}
function renderGoalHumanMessage(card,message){
  if(message.to==='Human'&&(message.human_brief||message.human_summary)){
    if(message.human_brief)renderHumanBrief(card,message.human_brief);
    else card.append(el('p','human-progress-summary',message.human_summary));
    const raw=el('details','goal-decision-raw');raw.append(el('summary','',message.human_display_language==='English'||message.human_brief?.language==='English'?'Original agent message':'에이전트 원문'),el('pre','',message.body));card.append(raw);
  }else card.append(el('pre','',message.body));
}
function renderDecisionBody(card,d,detail){
  const {goal,items}=detail;
  const brief=d.human_brief;
  if(brief)renderHumanBrief(card,brief);
  if(d.approval_files){const files=el('section','approval-files');files.append(el('strong','','승인 대상 파일 · SHA-256'));for(const [path,sha] of Object.entries(d.approval_files)){files.append(el('p','',path),el('code','',sha));}if(d.state==='pending')files.append(el('small','','파일이 바뀌면 새 승인이 필요합니다. 승인하려면 '+d.approval_option+' 선택 후 답변을 보내세요.'));card.append(files);}
  const raw=el('details','goal-decision-raw');raw.append(el('summary','',brief?.language==='English'?'Original details':'상세 원문'));card.append(raw);
  const question=el(decisionQuestionObject(d.question)?'pre':'p','goal-decision-question',d.question);raw.append(question);
  if(d.recommendation)raw.append(el('p','','추천: '+d.recommendation));
  if(d.reason)raw.append(el('p','goal-decision-reason',d.reason));
  if(d.options?.length){const options=el('ul','goal-decision-options');for(const option of d.options)options.append(el('li','',option));raw.append(el('strong','','원래 선택지'),options);}
  const blocked=items.filter(i=>(i.pending_decisions||[]).includes(d.id)&&!i.provisional);
  const affected=items.filter(i=>(d.affected_items||[]).includes(i.id)||(i.blocked_by_decision||[]).includes(d.id));
  const provisional=items.filter(i=>(i.provisional_decisions||[]).includes(d.id));
  raw.append(el('small','',`${d.reversible?'되돌릴 수 있음':'잠정 진행 불가'} · ${d.id}`));
  for(const [label,rows] of [[d.state==='pending'?'막힌 작업':'영향받은 작업',d.state==='pending'?blocked:affected],['PROVISIONAL',provisional]]){
    const line=el('p','goal-decision-impact');line.append(el('strong','',label+' '),document.createTextNode(rows.map(i=>i.title).join(' · ')||'없음'));raw.append(line);
  }
  if(d.state==='pending'){
    card.dataset.pendingDecision=d.id;
    const choices=el('div','goal-decision-choices');
    for(const option of d.options||[]){const label=brief?.options?.find(o=>o.value===option)?.label||option;const button=el('button','outline-button decision-option',label);button.type='button';button.dataset.option=option;button.setAttribute('aria-pressed',String(decisionSelections.get(d.id)===option));button.title=brief?.options?.find(o=>o.value===option)?.effect||'';button.addEventListener('click',()=>{if(decisionSending.has(d.id))return;decisionSelections.set(d.id,decisionSelections.get(d.id)===option?null:option);updateDecisionSubmission(card,d);});choices.append(button);}card.append(choices);
    const input=el('textarea');input.setAttribute('aria-label','판단 답변 '+d.id);input.placeholder='직접 답변';input.value=decisionDrafts.get(d.id)||'';input.maxLength=4000;
    input.dataset.decisionInput=d.id;input.addEventListener('input',()=>{decisionDrafts.set(d.id,input.value);updateDecisionSubmission(card,d);});card.append(input);
    const preview=el('pre','decision-send-preview');preview.setAttribute('aria-live','polite');card.append(preview);
    const send=el('button','primary-button decision-send','답변 보내기');send.addEventListener('click',handle(()=>sendGoalDecision(goal.id,d.id)));card.append(send);updateDecisionSubmission(card,d);
  }else{
    card.append(el('strong','','답변 전문'),el('pre','goal-decision-answer',d.answer||'답변 없음'));
  }
}
function renderGoalDecisions(detail){
  const {goal,items}=detail,decisions=detail.decisions||[],pending=decisions.filter(d=>d.state==='pending');
  const processed=decisions.filter(d=>d.state==='answered'||d.state==='withdrawn'),view=decisionView(goal.id);
  const panel=$('goal-decisions');panel.hidden=false;
  const header=panel.querySelector('.goal-section-label');
  if(!$('goal-decisions-expand')){
    header.querySelector('h3').replaceChildren(document.createTextNode('판단 '),$('goal-decision-count'));
    const controls=el('div','goal-decision-controls');
    for(const [id,label,open] of [['goal-decisions-expand','모두 펼치기',true],['goal-decisions-collapse','모두 접기',false]]){
      const button=el('button','text-button',label);button.id=id;button.type='button';button.addEventListener('click',()=>setAllGoalDecisionDetails(open));controls.append(button);
    }header.append(controls);
  }
  $('goal-decisions-expand').disabled=$('goal-decisions-collapse').disabled=!processed.length;
  $('goal-decision-count').textContent=String(pending.length);
  $('goal-decision-wait').hidden=goal.status!=='waiting_decision';
  const list=$('goal-decision-list'),signature=JSON.stringify([goal.id,goal.timezone,goal.archived,decisions,items.map(i=>[i.id,i.key,i.title,i.pending_decisions,i.provisional_decisions,i.blocked_by_decision])]);
  if(list.dataset.signature===signature)return;
  const focused=document.activeElement,editing=focused?.dataset?.decisionInput?{id:focused.dataset.decisionInput,start:focused.selectionStart,end:focused.selectionEnd}:null;
  list.dataset.signature=signature;list.dataset.goal=goal.id;list.replaceChildren();
  const waiting=el('section','goal-pending-decisions');waiting.setAttribute('aria-label','대기 중인 판단');waiting.append(el('h4','','대기 중인 판단'));
  if(!pending.length)waiting.append(el('p','goal-decision-empty','대기 중인 판단 없음'));
  for(const d of pending){
    const card=el('article','goal-decision pending');card.id='decision-'+d.id;
    const head=el('div','goal-decision-head');if(!d.human_brief)head.append(el('strong','',goalDecisionTitle(d,items)));head.append(el('span','status waiting_decision','판단 대기'));card.append(head);
    renderDecisionBody(card,d,detail);waiting.append(card);
  }
  list.append(waiting);
  const completed=el('details','goal-processed-decisions');completed.id='goal-decisions-processed';completed.open=view.groupOpen;
  const groupSummary=el('summary','goal-processed-summary');groupSummary.append(el('strong','',`처리된 판단 (${processed.length})`),el('small','','시간: '+(goal.timezone||'UTC')));completed.append(groupSummary);
  completed.addEventListener('toggle',()=>{if(completed.isConnected&&list.dataset.goal===goal.id){view.groupOpen=completed.open;saveDecisionView(goal.id);}});
  const rows=el('div','goal-processed-list');
  for(const d of [...processed].reverse()){
    const card=el('details','goal-decision resolved '+d.state);card.id='decision-'+d.id;card.dataset.decisionId=d.id;card.open=view.expanded.has(d.id);
    const summary=el('summary','goal-decision-summary'),title=goalDecisionTitle(d,items),answer=decisionShortText(d.answer)||'답변 없음',stamp=goalDecisionTime(d,goal);
    const badge=el('span','status decision-status '+d.state,d.state==='answered'?'답변됨':'철회됨');
    const label=el('strong','decision-short-title',title);label.title=title;
    const chosen=el('span','decision-short-answer',answer);chosen.title=String(d.answer||'답변 없음');
    const time=el('time','decision-processed-time');if(stamp.iso)time.dateTime=stamp.iso;time.title=stamp.label;time.setAttribute('aria-label',stamp.label);
    const full=el('span','decision-time-full',stamp.label),compact=el('span','decision-time-compact',stamp.compact);full.setAttribute('aria-hidden','true');compact.setAttribute('aria-hidden','true');time.append(full,compact);
    summary.append(badge,label,chosen,time);card.append(summary);
    const body=el('div','goal-decision-body');renderDecisionBody(body,d,detail);card.append(body);
    card.addEventListener('toggle',()=>{if(!card.isConnected||list.dataset.goal!==goal.id)return;if(card.open)view.expanded.add(d.id);else view.expanded.delete(d.id);saveDecisionView(goal.id);});rows.append(card);
  }
  if(!processed.length)rows.append(el('p','goal-decision-empty','처리된 판단 없음'));
  completed.append(rows);list.append(completed);
  if(editing){const restored=$('decision-'+editing.id)?.querySelector('[data-decision-input]');if(restored&&!restored.disabled){restored.focus({preventScroll:true});restored.setSelectionRange(editing.start,editing.end);}}
}
function decisionPayload(decisionId){
  const option=decisionSelections.get(decisionId),answer=(decisionDrafts.get(decisionId)||'').trim();
  return {...(option?{option}:{}),...(answer?{answer}:{})};
}
function updateDecisionSubmission(card,decision){
  const busy=decisionSending.has(decision.id),payload=decisionPayload(decision.id);
  for(const button of card.querySelectorAll('.decision-option')){const chosen=payload.option===button.dataset.option;button.setAttribute('aria-pressed',String(chosen));button.classList.toggle('selected',chosen);button.disabled=busy;}
  const input=card.querySelector('[data-decision-input]');if(input)input.disabled=busy;
  const preview=card.querySelector('.decision-send-preview');if(preview){const label=decision.human_brief?.options?.find(o=>o.value===payload.option)?.label||payload.option;preview.textContent='전송할 답변\n선택: '+(label||'선택 안 함')+'\n직접 답변: '+(payload.answer||'없음');}
  const send=card.querySelector('.decision-send');if(send){send.disabled=busy||!Object.keys(payload).length;send.textContent=busy?'전송 중…':'답변 보내기';}
}
async function sendGoalDecision(goalId,decisionId){
  if(decisionSending.has(decisionId))return;const value=decisionPayload(decisionId);if(!Object.keys(value).length)return;
  decisionSending.add(decisionId);const card=$('decision-'+decisionId),decision=goalUI.detail?.decisions?.find(d=>d.id===decisionId);if(card&&decision)updateDecisionSubmission(card,decision);
  try{const detail=await api('/api/goals/decision',{goal_id:goalId,decision_id:decisionId,...value});if(goalUI.selected===goalId&&detail?.goal){goalUI.detail=detail;renderGoalDetail(detail);}decisionDrafts.delete(decisionId);decisionSelections.delete(decisionId);if(!detail?.goal)await loadGoal();await poll();}
  finally{decisionSending.delete(decisionId);const current=$('decision-'+decisionId);if(current&&decision)updateDecisionSubmission(current,decision);}
}
function renderGoalDependencyBadge(row,item,detail){
  if(item.status==='superseded'){
    const replacement=detail.items.find(i=>i.id===item.superseded_by);
    row.append(el('p','goal-wait-reason','대체 종료 · '+(item.superseded_reason||'')+(replacement?' → '+replacement.title:'')));
    row.append(el('small','',(item.superseded_actor||'')+' · '+(item.superseded_local||'')));
    return;
  }
  if((item.execution_status||item.status)==='waiting_children')row.append(el('p','goal-wait-reason',`하위 항목 대기 (완료 ${item.children_done||0}/${item.children_total||0})`));
  if(item.document_cycle_active)row.append(el('p','goal-wait-reason',`문서 수정 전용 · 자동 주기 ${item.document_cycles_used}/2 · 새 실행 금지`));
  if(item.commit_hash){const checkpoint=el('p','goal-commit','로컬 커밋 · '+item.commit_hash.slice(0,12));checkpoint.title=item.commit_hash;row.append(checkpoint);}
  if(item.commit_warning)row.append(el('p','goal-commit-warning','커밋 경고 · '+item.commit_warning));
  if((item.execution_status||item.status)==='waiting'){
    const reasons={approval:'승인',decision:'사람 판단',dependency:'선행 작업·의존성 확인',builder_queue:'Builder 실행 순서',continuation:'같은 항목의 다음 묶음',long_command:'장시간 명령 실행 중',interrupted:'이어 실행',split_children:'분할된 하위 작업',deferred:'Planner 보류',platform_error:'플랫폼·도구 오류 확인',plan_capacity:'계획 항목 상한'};
    row.append(el('p','goal-wait-reason','대기 · '+(reasons[item.wait_reason]||'실행 준비')));
  }
  if(item.dependency_state==='unconfirmed')row.append(el('p','dependency-unknown','의존 여부 미확인 · Planner 갱신 전 배정 보류'));
  else if(item.provisional){row.classList.add('provisional');row.append(el('p','provisional-label','PROVISIONAL · '+(item.provisional_decisions||[]).join(', ')));}
  else if(item.pending_decisions?.length||item.unresolved_dependencies?.length){row.classList.add('decision-blocked');row.append(el('p','dependency-blocked','선행 작업 또는 판단 대기'));}
  const edit=el('button','text-button','의존성 수정');edit.addEventListener('click',()=>openGoalDependencies(detail,item));row.append(edit);
}
function goalPlanCapacity(items,goal){return `활성 ${items.filter(i=>!['done','superseded'].includes(i.status)).length} / ${goal.max_plan_items||40} · 전체 ${items.length} (완료 포함)`;}
function openGoalDependencies(detail,item){
  dependencyEditing={goalId:detail.goal.id,itemId:item.id};$('goal-dependency-title').textContent=item.title;$('goal-dependency-error').textContent='';
  for(const [id,rows,selected,label] of [
    ['goal-depends-items',detail.items.filter(i=>i.id!==item.id),item.depends_on||[],i=>i.title],
    ['goal-depends-decisions',(detail.decisions||[]).filter(d=>d.state==='pending'),item.blocked_by_decision||[],d=>goalDecisionTitle(d,detail.items)]]){
    const container=$(id);container.replaceChildren();for(const value of rows){const field=el('label');const input=el('input');input.type='checkbox';input.value=value.id;input.checked=selected.includes(value.id);field.append(input,document.createTextNode(label(value)));container.append(field);}
  }
  $('goal-depends-priority').value=item.priority??100;$('goal-depends-provisional').value=item.provisional_scope||'none';$('goal-dependencies-dialog').showModal();
}
$('goal-dependencies-save').addEventListener('click',handle(async()=>{
  const button=$('goal-dependencies-save');button.disabled=true;
  try{const checked=id=>[...$(id).querySelectorAll('input:checked')].map(i=>i.value);
    await api('/api/goals/dependencies',{goal_id:dependencyEditing.goalId,item_id:dependencyEditing.itemId,
      depends_on:checked('goal-depends-items'),blocked_by_decision:checked('goal-depends-decisions'),priority:Number($('goal-depends-priority').value),provisional_scope:$('goal-depends-provisional').value});
    $('goal-dependencies-dialog').close();await loadGoal();
  }catch(error){$('goal-dependency-error').textContent=error.message;}finally{button.disabled=false;}
}));
