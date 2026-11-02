'use strict';
const goalUI={selected:null,detail:null,busy:false,editing:null,config:null};
const goalLive=new Set(['planning','running','waiting_approval','waiting_decision']);
const goalLabels={planning:'계획 중',running:'진행 중',waiting_approval:'승인 대기',waiting_decision:'판단 대기',paused:'일시정지',blocked:'판단 필요',done:'완료',stopped:'중지',interrupted:'재개 필요',todo:'예정',waiting:'대기',in_progress:'구현 중',review:'검토 중',rework:'보완 중',waiting_children:'하위 항목 대기',superseded:'대체 종료'};
Object.assign(statusLabels,goalLabels);
const defaultGoalCommands=['python -m pytest','python -m unittest','python <workspace.py>','git status','git diff','git log','git add','git commit'];
function setGoalMode(){
  if(platform.mode!=='goal')newTask();
  platform.mode='goal';document.body.dataset.mode='goal';
  document.querySelectorAll('[data-mode]').forEach(b=>b.classList.toggle('active',b.dataset.mode==='goal'));
  $('new-task').querySelector('span').textContent='새 Goal';$('new-task').setAttribute('aria-label','새 Goal');
  $('service-title').textContent='Goal';
  for(const id of ['workspace-bar','conversation','composer-area','modal-home','task-heading','model','model-picker-open','locked-model'])$(id).hidden=true;
  $('goal-home').hidden=false;history();renderGoals();updateReasoningUI();
  if(!goalUI.config)handle(async()=>{goalUI.config=await api('/api/goals/config');goalUI.defaultReasoning=goalUI.config.defaults.reasoning;updateReasoningUI();})();
}
function openGoalFromSidebar(){
  // Enter the landing screen; only an explicit New Goal button opens the form.
  goalUI.selected=null;goalUI.detail=null;$('goal-human-composer').hidden=true;
  $('goal-detail').hidden=true;$('goal-empty').hidden=false;
  setGoalMode();
}
function goalStatus(status){const node=badge(status);node.textContent=goalLabels[status]||status;return node;}
function renderGoals(){
  renderGoalHistoryFilter();
}
async function selectGoal(id){const chosen=state.goals.find(g=>g.id===id);if(chosen){goalUI.archiveView=!!chosen.archived;state.historyView=chosen.archived?'archived':'recent';}renderGoalHistoryFilter();$('goal-objective-view').open=false;goalUI.selected=id;goalUI.detail=null;await loadGoal();}
async function loadGoal(){
  const id=goalUI.selected;if(!id)return;
  if(!goalUI.loading)goalUI.loading=new Map();if(goalUI.loading.has(id))return goalUI.loading.get(id);
  const revision=state.dataRevision;
  const request=(async()=>{const detail=await api('/api/goals/'+id);if(id!==goalUI.selected||revision!==state.dataRevision)return;if(goalUI.detail===detail)return;goalUI.detail=detail;renderGoalDetail(detail);})();
  goalUI.loading.set(id,request);try{return await request;}finally{if(goalUI.loading.get(id)===request)goalUI.loading.delete(id);}
}
function renderGoalDetail(detail){
  const {goal,items,messages,costs,approvals}=detail;
  $('goal-empty').hidden=true;$('goal-detail').hidden=false;
  $('goal-title').textContent=MarkdownExport.goalTitle(goal);const objectiveView=$('goal-objective-view');if(objectiveView.dataset.goal!==goal.id){objectiveView.open=false;objectiveView.dataset.goal=goal.id;const body=$('goal-objective-markdown');body.replaceChildren();richText(body,goal.objective);}$('goal-status').className='status '+goal.status;$('goal-status').textContent=goalLabels[goal.status]||goal.status;
  $('goal-meta').textContent=goal.builder_model+' · '+goal.output_language+'\n'+goal.workspace+'\n'+goal.deadline_local+' ['+goal.timezone+'] · UTC '+goal.deadline_utc;
  const live=goalLive.has(goal.status);$('goal-pause').hidden=!live;$('goal-stop').hidden=!live;$('goal-resume').hidden=live||goal.status==='done';$('goal-settings-open').disabled=false;
  $('goal-pending-settings').hidden=!goal.pending_settings;$('goal-pending-settings').textContent=goal.pending_settings?'설정 변경 대기 · 현재 Builder 묶음 또는 Planner 단계가 끝나면 적용됩니다.':'';
  renderGoalSettingsHistory(detail.settings_history||[],goal.timezone);renderGoalDecisions(detail);
  $('goal-reason').hidden=!goal.reason;$('goal-reason').textContent=goal.reason||'';$('goal-summary').textContent=goal.final_report||goal.summary||'';updateReasoningUI();
  const meters=$('goal-costs');meters.replaceChildren();
  for(const [provider,cost] of Object.entries(costs)){const meter=el('div','goal-cost'+(cost.ratio>=.8||(provider==='modal'&&(cost.llm_spent+cost.llm_reserved>=cost.llm*.8))?' warning':''));const header=el('div');header.append(el('strong','',provider),el('span','',`$${cost.spent.toFixed(4)} / $${cost.total.toFixed(2)}`));const bar=el('progress');bar.max=1;bar.value=Math.min(1,cost.ratio);meter.append(header,bar);const notes=[cost.reserved?`예약 $${cost.reserved.toFixed(4)}`:'',provider==='baseten'?`제품 예비분 $${cost.reserve.toFixed(2)}`:'',cost.daily!==null?`일일 $${cost.daily}`:'',`추론 토큰 ${cost.reasoning_tokens_known?cost.reasoning_tokens.toLocaleString():'미보고'}`,provider==='modal'?`LLM $${cost.llm_spent.toFixed(4)} / $${cost.llm.toFixed(2)} · GPU $${cost.gpu_spent.toFixed(4)}`:''].filter(Boolean);meter.append(el('small','',notes.join(' · ')||'추정 USD'));meters.append(meter);}
  if(detail.references){const ref=detail.references;meters.append(el('small','',`참고 검색 ${ref.goal_used}/${ref.limits.goal} · 항목당 ${ref.limits.item} · 검색료 약 $${(costs.meta?.reference_search_estimated_usd||0).toFixed(4)} (토큰 비용 별도)`));}
  if(detail.reference_pages){const ref=detail.reference_pages;meters.append(el('small','',`참고 페이지 ${ref.goal_used}/${ref.limits.goal} · 항목당 ${ref.limits.item}`));}
  const queue=$('goal-approvals');const queueSignature=JSON.stringify([approvals,goal.archived]);
  if(queue.dataset.signature!==queueSignature){queue.dataset.signature=queueSignature;queue.replaceChildren();for(const request of approvals){const box=el('section','goal-approval');box.append(el('strong','',request.kind==='question'?'판단 요청':request.kind==='gpu'?'GPU 전송·실행 승인':'로컬 명령 승인'));const pre=el('pre','',JSON.stringify(request.payload,null,2));box.append(pre,el('p','goal-approval-reason','자동 승인되지 않은 이유: '+(request.auto_approval_reason||(goal.policy.mode==='manual'?'Manual policy requires individual approval.':'Existing request requires policy reassessment or an active approval worker.'))));let answer;if(request.kind==='question'){answer=el('textarea');answer.placeholder='답변';answer.setAttribute('aria-label','Goal 판단 답변');box.append(answer);}const buttons=el('div','goal-actions');for(const [approve,label] of [[false,'거절'],[true,request.kind==='question'?'답변 보내기':'승인']]){const button=el('button',approve?'primary-button':'outline-button',label);button.addEventListener('click',handle(async()=>{button.disabled=true;try{await api('/api/goals/approve',{goal_id:goal.id,approval_id:request.id,approve,answer:answer?.value||''});await loadGoal();}finally{button.disabled=false;}}));buttons.append(button);}box.append(buttons);queue.append(box);}}
  $('goal-plan-count').textContent=goalPlanCapacity(items,goal);
  const tree=$('goal-plan-tree');const planSignature=JSON.stringify([items,goal.archived]);
  if(tree.dataset.signature!==planSignature){tree.dataset.signature=planSignature;tree.replaceChildren();const byId=new Map(items.map(i=>[i.id,i]));for(const item of items){let depth=0,parent=item.parent;while(parent&&byId.has(parent)&&depth<12){depth++;parent=byId.get(parent).parent;}const row=el('article','goal-plan-item');row.dataset.planItem=item.id;row.style.marginLeft=Math.min(depth,4)*14+'px';const head=el('div');head.append(goalStatus(item.execution_status||item.status),el('strong','',item.title));row.append(head,el('small','',`Builder · 시도 ${item.attempts} · 묶음 ${item.bundles}/${item.bundle_limit||4} · 보완 ${item.reworks}/3`));const criteria=el('ul');for(const criterion of item.acceptance){const kind=item.acceptance_rules?.[criterion]?.kind;criteria.append(el('li','',(kind==='document'?'[문서] ':kind==='execution'?'[실행] ':'')+criterion));}row.append(criteria);if(item.review_error){row.append(el('p','error-message','Planner 검토 출력 오류 · '+item.review_error_count+'/3 · '+(item.review_error.needs_human?'사용자 확인 필요': '재검토 대기')+'\n'+item.review_error.message));if(!live){const retry=el('button','outline-button','검토 다시 시도 허용');retry.addEventListener('click',handle(async()=>{await api('/api/goals/settings',{goal_id:goal.id,retry_review_item:item.id});await loadGoal();}));row.append(retry);}}if(item.defects?.length)row.append(el('p','error-message',item.defects.join('\n')));if(item.evidence?.length){const button=el('button','text-button',`근거 ${item.evidence.length}개 ↗`);button.addEventListener('click',handle(()=>openGoalEvidence(item)));row.append(button);}if(item.status==='blocked'&&!live&&item.evidence?.length){const rereview=el('button','outline-button','기존 근거 재검토');rereview.addEventListener('click',handle(async()=>{await api('/api/goals/settings',{goal_id:goal.id,rereview_item:item.id});await loadGoal();}));row.append(rereview);}if(item.status==='blocked'&&!live&&item.bundles<4){const unblock=el('button','outline-button','검토 후 재작업 허용');unblock.addEventListener('click',handle(async()=>{await api('/api/goals/settings',{goal_id:goal.id,unblock_item:item.id});await loadGoal();}));row.append(unblock);}renderGoalDependencyBadge(row,item,detail);tree.append(row);}}
  const timeline=$('goal-messages');const messageSignature=JSON.stringify(messages.map(m=>[m.id,m.updated,m.directive_status,m.human_display_language]));
  if(timeline.dataset.signature!==messageSignature){const atBottom=timeline.scrollHeight-timeline.clientHeight-timeline.scrollTop<80;const initial=timeline.dataset.goal!==goal.id;timeline.dataset.signature=messageSignature;timeline.dataset.goal=goal.id;timeline.replaceChildren();const messageDateFormatter=new Intl.DateTimeFormat('en-US',{timeZone:goal.timezone,month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'});for(const message of messages){const card=el('article','goal-message '+message.from.toLowerCase());const head=el('header');const date=messageDateFormatter.format(new Date(message.created*1000));head.append(el('strong','',message.from+' → '+message.to),el('span','',message.type+' · '+date));if(message.directive_status)head.append(el('span','inbox-status '+message.directive_status,inboxStatusLabels[message.directive_status]||message.directive_status));card.append(head);renderGoalHumanMessage(card,message);timeline.append(card);}if(initial||atBottom)timeline.scrollTo({top:timeline.scrollHeight,behavior:'instant'});}
  renderGoalExecutions(detail);renderGoalHistoryControls(goal);renderGoalInbox(detail);
}
async function openGoalEvidence(item){
  const body=$('goal-evidence-body');body.replaceChildren();
  for(const ref of item.evidence){const event=ref.observation_id?await api('/api/goals/evidence/'+ref.observation_id):(await api('/api/tasks/'+ref.task_id)).events[ref.event_index];const section=el('section');section.append(el('h3','',ref.kind+' · '+(ref.path||ref.gpu_job_id||ref.id)),el('pre','',JSON.stringify(event,null,2)));body.append(section);}
  $('goal-evidence-dialog').showModal();
}
async function openGoalCreate(edit=false){
  goalUI.config=await api('/api/goals/config');renderMuseRoute(goalUI.config.muse_open);
  const stored=edit?goalUI.detail?.goal:null;const goal=stored?{...stored,...(stored.pending_settings?.values||{})}:null;goalUI.editing=goal?.id||null;
  showGoalFormMessage('');$('goal-settings-timing').textContent=goal&&goalLive.has(goal.status)?'진행 중인 설정 변경은 현재 Builder 묶음 또는 Planner 단계 종료 후 적용됩니다. 승인 대기 중에는 새 정책을 즉시 판정합니다.':'';
  $('goal-form-title').textContent=goal?'Goal 설정':'새 Goal';$('goal-create-submit').textContent=goal?'설정 저장':'Goal 시작';
  $('goal-title-input').value=goal?MarkdownExport.goalTitle(goal):'';
  $('goal-objective').value=goal?.objective||'';$('goal-workspace').value=goal?.workspace||state.root||'';
  $('goal-objective').disabled=!!goal;$('goal-workspace').disabled=!!goal;$('goal-pick-folder').disabled=!!goal;
  resetGoalBuilderModel(goal?.builder_model??goalUI.config.builder_default??'muse-spark-1.3');$('goal-language').value=goal?.output_language||'English';$('goal-human-language').value=goal?.human_display_language||'Korean';$('goal-timezone').value=goal?.timezone||'America/New_York';
  $('goal-deadline').value=goal?.deadline_local||'';$('goal-policy').value=goal?.policy.mode||'manual';$('goal-allow-provisional').checked=goal?.allow_provisional??true;$('goal-max-steps').value=goal?.max_steps||8;$('goal-max-steps').disabled=!!goal;
  for(const provider of ['baseten','meta','modal']){const budget=goal?.budgets[provider]||goalUI.config.prices.limits[provider];$(`goal-${provider}-total`).value=budget.total;$(`goal-${provider}-daily`).value=budget.daily??'';if(provider==='baseten')$('goal-baseten-reserve').value=budget.reserve;}
  $('goal-modal-llm').value=goal?.budgets.modal.llm??30;
  $('goal-reference-limit').value=goal?.reference_limits?.goal??20;$('goal-reference-item-limit').value=goal?.reference_limits?.item??8;
  $('goal-page-limit').value=goal?.reference_page_limits?.goal??30;$('goal-page-item-limit').value=goal?.reference_page_limits?.item??12;
  $('goal-unattended').checked=goal?.unattended??false;$('goal-gpu-cycle-limit').value=goal?.gpu_jobs_per_cycle??3;
  $('goal-read-warn').value=goal?.read_loop_limits?.warn??8;$('goal-read-block').value=goal?.read_loop_limits?.block??12;
  $('goal-auto-commit').checked=goal?.auto_commit??true;$('goal-plan-limit').value=goal?.max_plan_items??goalUI.config.defaults?.max_plan_items??40;
  $('goal-gpu-max').value=goal?.policy.gpu_per_job??1;for(const role of ['Planner','Builder'])$('goal-'+role.toLowerCase()+'-reasoning').value=goal?.reasoning?.[role]||goalUI.config.defaults?.reasoning?.[role]||'high';const supported=goalUI.config.muse_open.reasoning_supported;$('goal-planner-reasoning').disabled=!supported;$('goal-planner-reasoning-help').hidden=supported;$('goal-planner-reasoning-help').textContent=goalUI.config.muse_open.route==='modal'?'추론 강도 미확인 · 파라미터 미전달':reasoningUnsupported;
  const commands=$('goal-command-list');commands.replaceChildren();for(const command of defaultGoalCommands){const label=el('label');const input=el('input');input.type='checkbox';input.value=command;input.checked=(goal?.policy.commands||defaultGoalCommands).includes(command);label.append(input,document.createTextNode(command));commands.append(label);}
  const gpus=$('goal-gpu-list');gpus.replaceChildren();for(const [gpu,rate] of Object.entries(goalUI.config.prices.gpu_hourly)){const label=el('label');const input=el('input');input.type='checkbox';input.value=gpu;input.checked=(goal?.policy.gpus||['T4','L4']).includes(gpu);label.append(input,document.createTextNode(`${gpu} · ~$${rate.toFixed(2)}/h`));gpus.append(label);}
  goalUI.formBaseline=JSON.stringify(goalFormPayload());updateGoalTitleSuggestion();
  $('goal-create-dialog').showModal();
  void loadGoalBuilderModels(goalUI.modelRequest);
}
function goalFormPayload(){
  const budgets={};for(const provider of ['baseten','meta','modal']){budgets[provider]={total:Number($(`goal-${provider}-total`).value),daily:$(`goal-${provider}-daily`).value===''?null:Number($(`goal-${provider}-daily`).value)};}budgets.baseten.reserve=Number($('goal-baseten-reserve').value);budgets.modal.llm=Number($('goal-modal-llm').value);
  const data={allow_provisional:$('goal-allow-provisional').checked,objective:$('goal-objective').value,workspace:$('goal-workspace').value,builder_model:$('goal-model').value,output_language:$('goal-language').value,human_display_language:$('goal-human-language').value,timezone:$('goal-timezone').value,deadline:$('goal-deadline').value||null,max_steps:Number($('goal-max-steps').value),reasoning:{Planner:$('goal-planner-reasoning').value,Builder:$('goal-builder-reasoning').value},budgets,
    reference_limits:{goal:Number($('goal-reference-limit').value),item:Number($('goal-reference-item-limit').value)},
    reference_page_limits:{goal:Number($('goal-page-limit').value),item:Number($('goal-page-item-limit').value)},
    unattended:$('goal-unattended').checked,gpu_jobs_per_cycle:Number($('goal-gpu-cycle-limit').value),
    auto_commit:$('goal-auto-commit').checked,max_plan_items:Number($('goal-plan-limit').value),
    read_loop_limits:{warn:Number($('goal-read-warn').value),block:Number($('goal-read-block').value)},
    policy:{mode:$('goal-policy').value,commands:[...$('goal-command-list').querySelectorAll('input:checked')].map(n=>n.value),gpus:[...$('goal-gpu-list').querySelectorAll('input:checked')].map(n=>n.value),gpu_per_job:Number($('goal-gpu-max').value)}};
  if(goalUI.editing)for(const key of ['objective','workspace','max_steps'])delete data[key];
  return data;
}
function showGoalFormMessage(text){const node=$('goal-form-error');node.hidden=!text;node.textContent=text;}
function renderGoalSettingsHistory(changes,timezone){
  const container=$('goal-settings-history');const signature=JSON.stringify(changes);if(container.dataset.signature===signature)return;container.dataset.signature=signature;container.replaceChildren();
  for(const change of [...changes].reverse()){const row=el('article','goal-setting-change');const date=new Intl.DateTimeFormat('en-US',{timeZone:timezone,month:'short',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(change.created*1000));row.append(el('strong','',`${change.state==='pending'?'적용 대기':change.state==='superseded'?'새 변경으로 대체':'적용됨'} · ${date}`),el('pre','',JSON.stringify({before:change.before,after:change.after},null,2)));container.append(row);}
}
async function saveGoal(){
  showGoalFormMessage('');if(!validateGoalBuilderSelection(true))return;const input=$('goal-title-input');input.value=input.value.trim();if(!input.reportValidity()){showGoalFormMessage('제목을 1~120자로 입력하세요.');return;}
  const title=input.value;const data=goalFormPayload();
  const button=$('goal-create-submit');goalUI.formSaving=true;button.disabled=true;
  try{let result;
    if(goalUI.editing){
      if(JSON.stringify(data)!==goalUI.formBaseline)result=await api('/api/goals/settings',{...data,goal_id:goalUI.editing});
      if(title!==goalUI.detail?.goal.title)result=await api('/api/goals/title',{goal_id:goalUI.editing,title});
      if(!result)result=goalUI.detail;
    }else result=await api('/api/goals',{...data,title});
    $('goal-create-dialog').close();setMode('goal');goalUI.selected=result.goal.id;goalUI.detail=result;renderGoalDetail(result);await poll();toast(result.goal.pending_settings?'설정 변경을 저장했습니다. 현재 단계 종료 후 적용됩니다.':'Goal 설정을 저장했습니다.');
  }catch(error){showGoalFormMessage('설정 저장 실패: '+error.message);}
  finally{goalUI.formSaving=false;validateGoalBuilderSelection();}
}
async function controlGoal(action){if(!goalUI.selected)return;await api('/api/goals/control',{goal_id:goalUI.selected,action});await loadGoal();await poll();}
$('goal-gpu-all').addEventListener('click',openGoalGpuList);
$('goal-create-open').addEventListener('click',handle(()=>openGoalCreate()));
$('goal-settings-open').addEventListener('click',handle(()=>openGoalCreate(true)));
$('goal-create-submit').addEventListener('click',handle(saveGoal));
$('goal-pick-folder').addEventListener('click',handle(async()=>{const result=await api('/api/pick-folder',{});if(result.root)$('goal-workspace').value=result.root;}));
for(const [id,action] of [['goal-pause','pause'],['goal-resume','resume'],['goal-stop','stop']])$(id).addEventListener('click',handle(()=>controlGoal(action)));
$('goal-export').addEventListener('click',()=>{const detail=goalUI.detail;if(detail)downloadMarkdown('Goal-'+detail.goal.id,MarkdownExport.goal(detail));});

function updateGoalTitleSuggestion(){
  const candidate=MarkdownExport.suggestGoalTitle($('goal-objective').value,false);const button=$('goal-title-suggest');
  button.hidden=!!$('goal-title-input').value.trim()||!candidate;button.textContent='제안 적용: '+candidate;
  $('goal-title-input').placeholder=candidate||'Goal 제목';
}
$('goal-objective').addEventListener('input',updateGoalTitleSuggestion);
$('goal-title-input').addEventListener('input',updateGoalTitleSuggestion);
$('goal-title-suggest').addEventListener('click',()=>{$('goal-title-input').value=MarkdownExport.suggestGoalTitle($('goal-objective').value,false);updateGoalTitleSuggestion();});
