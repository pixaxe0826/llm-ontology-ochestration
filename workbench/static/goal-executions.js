'use strict';
const ExecutionView=(()=>{
  function context(record){const c=record.goal_context;if(c?.status==='conflict')return null;return c?.goal_id?c:record.goal_id?{goal_id:record.goal_id,plan_item_id:record.plan_item_id,task_id:record.task_id||record.id}:null;}
  function owner(entry,goals){const c=context(entry.record);return c&&goals.some(g=>g.id===c.goal_id)?c.goal_id:null;}
  function top(entries,goals,show){return entries.filter(e=>e.kind==='goal'||show||!owner(e,goals));}
  function children(entries,goalId){return entries.filter(e=>e.kind!=='goal'&&context(e.record)?.goal_id===goalId);}
  function runs(detail,itemId){return (detail.execution?.entries||[]).filter(r=>!itemId||r.plan_item_id===itemId).sort((a,b)=>(a.time||0)-(b.time||0)||a.id.localeCompare(b.id));}
  function duration(value){return Number.isFinite(value)?(value<60?value.toFixed(1)+'초':Math.floor(value/60)+'분 '+Math.round(value%60)+'초'):'시간 미확인';}
  function breadcrumb(c){return c?.goal_id?[c.goal_title||'Goal',c.item_title||'항목 연결 미확인']:[];}
  return {context,owner,top,children,runs,duration,breadcrumb};
})();
if(typeof module!=='undefined')module.exports=ExecutionView;

if(typeof document!=='undefined'){
  var executionUI={showGoal:false};
  try{executionUI.showGoal=localStorage.getItem('muse.history.goal-tasks')==='true';}catch{}
  for(const input of document.querySelectorAll('[data-show-goal-tasks]')){input.checked=executionUI.showGoal;input.addEventListener('change',()=>{executionUI.showGoal=input.checked;try{localStorage.setItem('muse.history.goal-tasks',String(input.checked));}catch{}for(const other of document.querySelectorAll('[data-show-goal-tasks]'))other.checked=input.checked;history();});}
}
function executionExpanded(key,fallback=false){try{return JSON.parse(localStorage.getItem('muse.executions.'+key)??String(fallback));}catch{return fallback;}}
function rememberExecutionDetails(details,key){details.addEventListener('toggle',()=>{try{localStorage.setItem('muse.executions.'+key,JSON.stringify(details.open));}catch{}});}
function appendGoalHistoryChildren(target,entry,entries){
  if(entry.kind!=='goal')return;const children=ExecutionView.children(entries,entry.id);if(!children.length)return;
  const details=el('details','goal-history-children');details.dataset.goal=entry.id;details.open=executionExpanded('history.'+entry.id);
  rememberExecutionDetails(details,'history.'+entry.id);const summary=el('summary');summary.append(el('span','','실행 기록'),el('b','execution-count',String(children.length)));details.append(summary);
  const list=el('div','goal-history-child-list');for(const child of children){const c=ExecutionView.context(child.record);const button=el('button','goal-history-child');button.dataset.kind=child.kind;button.dataset.id=child.id;button.append(el('span','',child.kind==='gpu'?'GPU · '+child.record.spec.gpu:'Builder'),el('strong','',child.kind==='gpu'?child.title:c.item_title||child.title),el('small','',(c.item_title||'항목 연결 미확인')+' · '+(statusLabels[child.record.status]||child.record.status)));button.addEventListener('click',handle(async()=>{if($('history-dialog').open)$('history-dialog').close();if(child.kind==='gpu')showJob(child.id);else await openGoalExecutionRecord({kind:'builder',id:child.id,...c});}));list.append(button);}details.append(list);target.append(details);
}
function executionTime(value,goal){return value?new Intl.DateTimeFormat('ko-KR',{timeZone:goal.timezone||'America/New_York',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(value*1000)):'';}
function gpuArtifactUrl(id,path,preview=false){return '/api/artifact?job_id='+encodeURIComponent(id)+'&path='+encodeURIComponent(path)+(preview?'&preview=1':'');}
function executionNode(run,goal){
  const node=el('article','goal-execution-row');node.dataset.run=run.id;node.dataset.kind=run.kind;
  const head=el('div','execution-head'),button=el('button','execution-view-action execution-title',run.kind==='gpu'?`${run.gpu} · ${run.label}`:run.kind==='builder'?'Builder · '+run.label:run.kind==='long_command'?'장시간 · '+run.label:run.label);
  button.addEventListener('click',handle(()=>openGoalExecutionRecord(run)));head.append(button,badge(run.status));node.append(head);
  const notes=[executionTime(run.time,goal)];if(run.kind==='gpu'){notes.push((run.runtime_s!==null?'GPU 실행 ':'전체 경과 ')+ExecutionView.duration(run.runtime_s??run.elapsed_s),run.cost_known?'추정 $'+run.estimated_usd.toFixed(5):'비용 연결 미확인');if(run.reserved_usd)notes.push('예약 $'+run.reserved_usd.toFixed(4));}
  else if(run.kind!=='builder'){notes.push(ExecutionView.duration(run.elapsed_s),'exit '+(run.exit_code??'—'));if(run.timed_out)notes.push('시간 초과');}
  node.append(el('small','execution-meta',notes.filter(Boolean).join(' · ')));
  if(run.kind==='gpu'){
    const files=el('div','execution-files');for(const file of run.files||[]){const link=el('a','',file.path+(file.location==='volume'?' · Volume':''));link.href=file.location==='local'?gpuArtifactUrl(run.id,file.path):'/api/remote-artifact?job_id='+encodeURIComponent(run.id)+'&path='+encodeURIComponent(file.path);link.download=file.path.split('/').at(-1);files.append(link);}
    for(const copy of run.copies||[])files.append(el('small','','작업 폴더 회수: '+copy.path));node.append(files);
    const previews=el('div','execution-previews');for(const file of (run.files||[]).filter(f=>f.location==='local'&&/\.png$/i.test(f.path)).slice(0,4)){const image=el('img');image.alt=file.path;image.loading='lazy';image.src=gpuArtifactUrl(run.id,file.path,true);const open=el('button','execution-view-action');open.title=file.path;open.append(image);open.addEventListener('click',()=>showJob(run.id));image.addEventListener('error',()=>{open.replaceChildren(el('small','','미리보기 불가 · '+file.path));},{once:true});previews.append(open);}node.append(previews);
  }
  return node;
}
function renderGoalExecutions(detail){
  const summary=detail.execution?.gpu_summary||{total:0,succeeded:0,failed:0,cancelled:0,estimated_usd:0,reserved_usd:0};
  $('goal-gpu-totals').textContent=`${summary.total}개 · 성공 ${summary.succeeded} · 실패 ${summary.failed} · 취소 ${summary.cancelled} · 추정 $${summary.estimated_usd.toFixed(5)}`+(summary.reserved_usd?` · 예약 $${summary.reserved_usd.toFixed(4)}`:'');
  const note=$('goal-gpu-cost-note');note.textContent=[summary.unattributed_usd?`개별 작업 미연결 비용 $${summary.unattributed_usd.toFixed(5)} 포함`:'',summary.unknown_cost_jobs?`비용 연결 미확인 ${summary.unknown_cost_jobs}개`: ''].filter(Boolean).join(' · ');note.hidden=!note.textContent;
  for(const row of $('goal-plan-tree').querySelectorAll('[data-plan-item]')){
    const runs=ExecutionView.runs(detail,row.dataset.planItem),signature=JSON.stringify(runs);let details=row.querySelector('.goal-item-executions');
    if(details?.dataset.signature===signature)continue;const wasOpen=details?.open,scroll=details?.querySelector('.goal-execution-list')?.scrollTop||0;
    if(!details){details=el('details','goal-item-executions');rememberExecutionDetails(details,detail.goal.id+'.'+row.dataset.planItem);row.append(details);}
    details.dataset.signature=signature;details.open=wasOpen??executionExpanded(detail.goal.id+'.'+row.dataset.planItem);details.replaceChildren(el('summary','','실행 기록 ('+runs.length+')'));
    const list=el('div','goal-execution-list');for(const run of runs)list.append(executionNode(run,detail.goal));if(!runs.length)list.append(el('p','muted','실행 기록 없음'));details.append(list);list.scrollTop=scroll;
  }
}
function openGoalGpuList(){const detail=goalUI.detail;if(!detail)return;const dialog=$('goal-runs-dialog'),body=$('goal-runs-body');$('goal-runs-title').textContent='GPU 작업 · '+MarkdownExport.goalTitle(detail.goal);body.replaceChildren();for(const run of ExecutionView.runs(detail).filter(r=>r.kind==='gpu')){const label=el('p','execution-item-label',run.context.item_title);body.append(label,executionNode(run,detail.goal));}if(!body.children.length)body.append(el('p','muted','GPU 작업 없음'));if(!dialog.open)dialog.showModal();}
async function openGoalExecutionRecord(run){
  if(run.kind==='gpu'){if($('goal-runs-dialog').open)$('goal-runs-dialog').close();showJob(run.id);return;}
  const dialog=$('goal-runs-dialog'),body=$('goal-runs-body');body.replaceChildren();$('goal-runs-title').textContent=run.kind==='builder'?'Builder 실행 기록':'명령 실행 기록';
  const context=run.context||state.tasks.find(t=>t.id===run.task_id)?.goal_context||state.tasks.find(t=>t.id===run.id)?.goal_context;
  if(context)body.append(executionBreadcrumb(context));
  let content=run;if(run.kind==='builder'){const task=await api('/api/tasks/'+run.id);content={id:task.id,status:task.status,events:task.events};}
  body.append(el('pre','execution-raw',JSON.stringify(content,null,2)));if(!dialog.open)dialog.showModal();
}
function executionBreadcrumb(context){
  const nav=el('nav','execution-breadcrumb');nav.setAttribute('aria-label','Goal 실행 경로');const labels=ExecutionView.breadcrumb(context);
  labels.forEach((label,i)=>{if(i)nav.append(el('span','','›'));const button=el('button','execution-view-action text-button',label);button.disabled=context.goal_available===false;button.addEventListener('click',handle(()=>openExecutionGoal(context,i===1)));nav.append(button);});return nav;
}
async function openExecutionGoal(context,item=true){
  for(const id of ['job-dialog','goal-runs-dialog'])if($(id).open)$(id).close();setMode('goal');await selectGoal(context.goal_id);
  if(item&&context.plan_item_id){const row=Array.from($('goal-plan-tree').querySelectorAll('[data-plan-item]')).find(r=>r.dataset.planItem===context.plan_item_id);if(row){row.querySelector('.goal-item-executions').open=true;row.scrollIntoView({block:'center',behavior:'instant'});row.classList.add('execution-highlight');setTimeout(()=>row.classList.remove('execution-highlight'),1800);}}
}
