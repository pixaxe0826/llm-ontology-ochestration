'use strict';
const $ = (id) => document.getElementById(id);
const state = {token:'', providers:{}, tasks:[], jobs:[], root:'', files:[], current:null, task:null, editor:null, review:null, viewingJob:null, rendered:'',goals:[],historyView:'recent',historyAction:null,historyRevision:0,selectedJob:null};
const running = new Set(['running','waiting_approval','queued']);
const statusLabels = {running:'실행 중',queued:'준비 중',succeeded:'완료',waiting_approval:'승인 대기',failed:'실패',cancelled:'중지됨',paused:'단계 제한',interrupted:'확인 필요'};
const toolLabels = {list_files:'폴더 살펴보기',read_file:'파일 읽기',search_files:'내용 검색',write_file:'파일 수정',run_command:'명령 실행',gpu_catalog:'GPU 확인',submit_gpu_job:'GPU 작업 실행',get_gpu_job:'작업 결과 확인',read_gpu_artifact:'결과 파일 읽기'};
const el = (tag, cls, text) => {const node=document.createElement(tag);if(cls)node.className=cls;if(text!==undefined)node.textContent=text;return node;};
function toast(text,error=false){$('toast').textContent=text;$('toast').className='toast'+(error?' error':'');$('toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$('toast').hidden=true,6500);}
const apiBodies=new Map(),apiRequests=new Map();state.dataRevision=0;
async function api(path,data){
  if(data!==undefined){state.dataRevision++;apiBodies.clear();apiRequests.clear();}
  else if(apiRequests.has(path))return apiRequests.get(path);
  const revision=state.dataRevision,cached=data===undefined?apiBodies.get(path):null;
  const operation=(async()=>{
    const headers=data===undefined?(cached?.etag?{'If-None-Match':cached.etag}:{}):{'Content-Type':'application/json','X-Local-Token':state.token};
    const response=await fetch(path,{method:data===undefined?'GET':'POST',headers,body:data===undefined?undefined:JSON.stringify(data)});
    if(response.status===304&&cached)return cached.data;
    const result=await response.json();if(!response.ok)throw Error(result.error||'요청을 처리하지 못했습니다.');
    if(data===undefined&&revision===state.dataRevision){apiBodies.delete(path);apiBodies.set(path,{data:result,etag:response.headers.get('ETag')});while(apiBodies.size>32)apiBodies.delete(apiBodies.keys().next().value);}
    return result;
  })();
  if(data===undefined)apiRequests.set(path,operation);
  try{return await operation;}finally{if(apiRequests.get(path)===operation)apiRequests.delete(path);}
}
function handle(fn){return async(...args)=>{try{await fn(...args);}catch(error){toast(error.message,true);}};}
function badge(status){return el('span','status '+status,statusLabels[status]||status);}
function bytes(n){return n>1e6?(n/1e6).toFixed(1)+' MB':n>1e3?(n/1e3).toFixed(1)+' KB':n+' B';}
function changeTab(name){document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('selected',b.dataset.tab===name));['files','changes','jobs','memory'].forEach(tab=>$('panel-'+tab).hidden=tab!==name);if(name==='memory')handle(loadMemory)();}
function refreshControls(){const attachmentBusy=typeof chatAttachments!=='undefined'&&(chatAttachments.busy||attachmentBlocked());$('attach-text').disabled=!!state.task?.archived||!!(state.task&&running.has(state.task.status))||!!attachmentBusy;const busy=state.task&&running.has(state.task.status);$('prompt').disabled=!!state.task?.archived;$('send-task').disabled=!!state.task?.archived||!!busy||!!attachmentBusy||(!state.root&&platform.mode==='meta')||!state.providers[$('provider').value]?.ready;$('send-task').firstChild.textContent=platform.mode==='baseten'?'보내기 ':state.current?'계속 작업 ':'작업 시작 ';$('stop-task').hidden=!busy;$('allow-write').disabled=!!busy;$('provider').disabled=!!busy;$('model').disabled=!!busy;updateReasoningUI();}
function updateConnections(data){state.providers=data.providers;for(const name of ['meta','baseten']){const ready=data.providers[name].ready;$(name+'-dot').classList.toggle('ready',ready);$(name+'-state').classList.toggle('ready',ready);$(name+'-state').textContent=ready?'키 설정됨':'키 필요';}const modal=data.modal_ready;$('modal-dot').classList.toggle('ready',modal);$('modal-state').classList.toggle('ready',modal);$('modal-state').textContent=modal?'토큰 설정됨':'토큰 필요';refreshControls();}
function providerChanged(){const settings=state.providers[$('provider').value];if(settings)$('model').value=settings.model;refreshControls();}
const serviceNames={meta:'Meta Work',baseten:'Baseten Chat',modal:'Modal Compute',goal:'Goal'};
function historyEntries(){
  return [...state.goals.map(g=>({kind:'goal',id:g.id,title:MarkdownExport.goalTitle(g),service:'goal',record:g})),...state.tasks.map(t=>({kind:'task',id:t.id,title:t.title,service:t.provider==='baseten'?'baseten':'meta',record:t})),
    ...state.jobs.map(j=>({kind:'gpu',id:j.id,title:j.spec.entry||j.spec.program,service:'modal',record:j}))]
    .sort((a,b)=>b.record.updated-a.record.updated);
}
function history(){
  const allEntries=historyEntries();
  const entries=ExecutionView.top(allEntries,state.goals,executionUI.showGoal);
  const archived=state.historyView==='archived';
  const visible=entries.filter(e=>!!e.record.archived===archived);
  const count=entries.filter(e=>e.record.archived).length;
  $('task-count').textContent=entries.filter(e=>!e.record.archived).length;
  $('archive-count').textContent=count;$('dialog-archive-count').textContent=count;
  document.querySelectorAll('[data-history-view]').forEach(b=>{const selected=b.dataset.historyView===state.historyView;b.classList.toggle('selected',selected);b.setAttribute('aria-pressed',String(selected));});
  for(const id of ['history','history-all']){
    const target=$(id);const signature=JSON.stringify([visible.map(e=>[e.kind,e.id,e.title,e.record.updated,e.record.history_busy]),allEntries.map(e=>[e.id,e.record.updated,e.record.goal_context]),executionUI.showGoal])+state.current+state.selectedJob;
    if(target.dataset.signature===signature)continue;
    target.dataset.signature=signature;target.replaceChildren();
    if(!visible.length){target.append(el('p','history-empty',archived?'아카이브한 기록이 없습니다.':'아직 기록이 없습니다.'));continue;}
    for(const entry of visible){
      const row=el('div','history-row'+((entry.kind==='task'?entry.id===state.current:entry.id===state.selectedJob)?' active':''));
      row.dataset.kind=entry.kind;row.dataset.id=entry.id;
      const button=el('button','history-select');button.title=entry.title;button.setAttribute('aria-label',serviceNames[entry.service]+' · '+entry.title);
      const service=el('span','history-service '+entry.service,serviceNames[entry.service]);
      button.append(service,el('strong','',entry.title),el('small','',(statusLabels[entry.record.status]||entry.record.status)+' · '+new Date(entry.record.updated*1000).toLocaleDateString(entry.kind==='goal'?'en-US':'ko-KR',entry.kind==='goal'?{timeZone:entry.record.timezone||'America/New_York'}:{})));
      button.addEventListener('click',handle(()=>selectHistory(entry)));
      const more=el('button','history-more','⋯');more.title='기록 관리';more.setAttribute('aria-label',entry.title+' 기록 관리');
      more.addEventListener('click',()=>entry.kind==='goal'?handle(()=>openGoalHistory(entry.id))():openHistoryActions(entry));row.append(button,more);target.append(row);appendGoalHistoryChildren(target,entry,allEntries);
    }
  }
}
async function selectHistory(entry){
  const row=document.querySelector('#history [data-kind="'+entry.kind+'"][data-id="'+entry.id+'"] .history-select');if(row){row.disabled=true;row.setAttribute('aria-busy','true');}
  try{
  if($('history-dialog').open)$('history-dialog').close();
  if(entry.kind==='goal'){setMode('goal');await selectGoal(entry.id);}
  else if(entry.kind==='task'){if(ExecutionView.owner(entry,state.goals)){await openGoalExecutionRecord({kind:'builder',id:entry.id,...ExecutionView.context(entry.record)});return;}state.selectedJob=null;await selectTask(entry.id);}
  else {setMode('modal');state.historyView=entry.record.archived?'archived':'recent';state.selectedJob=entry.id;showJob(entry.id);history();}
  }finally{if(row){row.disabled=false;row.removeAttribute('aria-busy');}}
}
function openHistoryActions(entry){
  state.historyAction={kind:entry.kind,id:entry.id};
  $('history-action-title').textContent=entry.title;
  $('history-action-service').textContent=serviceNames[entry.service];
  const busy=running.has(entry.record.status)||entry.record.history_busy;
  $('history-action-status').textContent=busy?'실행이 끝난 뒤 기록을 변경할 수 있습니다.':entry.record.archived?'아카이브됨':statusLabels[entry.record.status]||entry.record.status;
  $('history-archive').textContent=entry.record.archived?'복원':'아카이브';
  $('history-archive').dataset.action=entry.record.archived?'restore':'archive';
  $('history-archive').disabled=!!busy;$('history-delete').disabled=!!busy;
  $('history-delete-confirm').disabled=false;
  showHistoryDelete(false);$('history-action-dialog').showModal();
}
function showHistoryDelete(confirming){$('history-delete-notice').hidden=!confirming;$('history-confirm-actions').hidden=!confirming;$('history-choose-actions').hidden=confirming;}
async function manageHistory(action,entry=state.historyAction){
  state.historyRevision++;
  await api('/api/history',{...entry,action,confirmed:action==='delete'});
  state.historyRevision++;
  const [tasks,jobs]=await Promise.all([api('/api/tasks'),api('/api/jobs')]);state.tasks=tasks;state.jobs=jobs;
  if($('history-action-dialog').open)$('history-action-dialog').close();
  if(action==='delete'){
    if(entry.kind==='task'&&state.current===entry.id)newTask();
    if(entry.kind==='gpu'&&state.selectedJob===entry.id)state.selectedJob=null;
    if(entry.kind==='gpu'&&state.viewingJob===entry.id){$('job-dialog').close();state.viewingJob=null;}
    if($('source-dialog').open)$('source-dialog').close();
  }else if(entry.kind==='task'&&state.current===entry.id){renderTask(tasks.find(t=>t.id===entry.id));}
  if(action==='restore')state.historyView='recent';
  history();renderJobs();await loadMemory();
  toast(action==='delete'?'기록과 연결된 공유 메모리를 삭제했습니다.':action==='archive'?'아카이브했습니다.':'기록을 복원했습니다.');
}
function newTask(){if(typeof clearAttachments==='function')clearAttachments();if(platform.mode==='goal'){openGoalCreate();return;}state.selectedJob=null;state.historyView='recent';state.current=null;state.task=null;state.rendered='';$('task-heading').hidden=true;$('welcome').hidden=false;$('messages').replaceChildren();$('approval').hidden=true;$('prompt').value='';history();renderChanges();refreshControls();$('prompt').focus();}
async function connectWorkspace(root){if(state.task&&running.has(state.task.status))throw Error('진행 중인 작업을 먼저 중지하세요.');const result=await api('/api/workspace',{root});state.root=result.root;state.files=result.files;$('workspace-root').value=result.root;$('workspace-name').textContent=result.root.split(/[\\/]/).filter(Boolean).at(-1);renderFiles();refreshControls();return result;}
function renderFiles(){const target=$('file-list');target.replaceChildren();if(!state.files.length){const box=el('div','empty-panel');box.append(el('span','','▱'),el('p','',state.root?'표시할 파일이 없습니다.':'작업 폴더를 선택하세요.'));target.append(box);return;}state.files.forEach(file=>{const button=el('button','file-row'+(file.directory?' directory':''));const depth=file.path.split('/').length-1;button.style.paddingLeft=(8+Math.min(depth,6)*13)+'px';button.title=file.path;button.append(el('span','file-icon',file.directory?'▱':'▤'),el('span','',file.path.split('/').at(-1)));if(!file.directory)button.addEventListener('click',handle(()=>openFile(file.path)));target.append(button);});}
async function openFile(path){const file=await api('/api/file',{root:state.root,path});state.editor=file;$('editor-title').textContent=file.path;$('editor').value=file.content;$('editor-dialog').showModal();}
const markdownViews=new Map();let markdownViewBytes=0;
function richText(container,text){
  container.classList.add('markdown');
  if(!window.marked||!window.DOMPurify){container.textContent=text;return;}
  const cached=markdownViews.get(text);
  if(cached){container.innerHTML=cached.html;markdownViews.delete(text);markdownViews.set(text,cached);}
  else{
    container.innerHTML=DOMPurify.sanitize(marked.parse(text),{FORBID_TAGS:['img','iframe','svg','math','script','style','form','input','video','audio','object','embed'],FORBID_ATTR:['style']});
    container.querySelectorAll('a').forEach(a=>{if(!/^https?:/i.test(a.href))a.removeAttribute('href');a.target='_blank';a.rel='noopener noreferrer';});
    container.querySelectorAll('pre code').forEach(code=>{if(window.hljs)hljs.highlightElement(code);});
    const html=container.innerHTML,size=(text.length+html.length)*2;
    if(size<=2_000_000){markdownViews.set(text,{html,size});markdownViewBytes+=size;while(markdownViews.size>40||markdownViewBytes>2_000_000){const first=markdownViews.keys().next().value;markdownViewBytes-=markdownViews.get(first).size;markdownViews.delete(first);}}
  }
  container.querySelectorAll('pre code').forEach(code=>{const button=el('button','code-copy','복사');button.addEventListener('click',handle(async()=>{await navigator.clipboard.writeText(code.textContent);toast('코드를 복사했습니다.');}));code.parentNode.append(button);});
}

function renderTask(task){state.task=task;if(task.error?.startsWith('이 공급자는 이미지 미지원')&&state.providers.baseten)state.providers.baseten.vision_supported=false;$('task-cost').hidden=!task.cost?.known;$('task-cost').textContent=task.cost?.known?'추정 $'+task.cost.estimated_usd.toFixed(4)+' · 추론 '+(task.cost.reasoning_tokens_known?task.cost.reasoning_tokens.toLocaleString():'미보고')+' · 이미지 '+(task.cost.image_count||0)+'장':'';$('task-export').disabled=running.has(task.status);$('task-restore').hidden=!task.archived;$('task-status').title=task.archived?'아카이브됨':'';$('welcome').hidden=true;$('task-heading').hidden=false;$('task-title').textContent=task.title;$('task-status').className='status '+task.status;$('task-status').textContent=statusLabels[task.status]||task.status;const signature=task.id+':'+task.events.length+':'+task.status+':'+task.step+':'+task.updated;if(state.rendered!==signature){const scroll=$('conversation');const atBottom=scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<100;const feed=$('messages');feed.replaceChildren();task.events.forEach((event,eventIndex)=>{if(event.kind==='user'||event.kind==='assistant'){const item=el('article','message '+event.kind);const head=el('div','message-head');if(event.kind==='assistant')head.append(el('span','avatar','m'));head.append(el('span','',event.kind==='user'?'나':task.provider==='baseten'?'Baseten · '+task.model:'Meta · '+task.model));const body=el('div','message-body');richText(body,event.text||'');item.append(head,body);if(event.kind==='user')attachmentMetadata(item,event.attachments);memorySources(item,event.sources);if(event.kind==='assistant'&&event.text){const actions=el('div','response-actions');const download=el('button','text-button','↓ Markdown');download.setAttribute('aria-label','응답 Markdown 다운로드');download.disabled=running.has(task.status)||event.message_status==='pending';download.addEventListener('click',()=>downloadMarkdown(task.title+'-응답-'+(eventIndex+1),MarkdownExport.response(event)));actions.append(download);item.append(actions);}feed.append(item);}else if(event.kind==='tool'){const item=el('div','tool-event');item.append(el('strong','',toolLabels[event.name]||event.name));let info=event.arguments.path||event.arguments.query||event.arguments.entry||'';if(event.name==='run_command')info=(event.arguments.argv||[]).join(' ');item.append(document.createTextNode(info));feed.append(item);}else if(event.kind==='tool_result'){const details=el('details','tool-event');const result=event.result||{};details.append(el('summary','',result.error?'실행 오류 · '+result.error:result.denied?'실행 거절됨':(toolLabels[event.name]||event.name)+' 결과'));details.append(el('pre','',JSON.stringify(result,null,2)));feed.append(details);}else if(event.kind==='error'){feed.append(el('p','error-message',event.text));}});if(running.has(task.status)){const progress=el('div','activity');progress.append(el('i'),el('span','',task.status==='waiting_approval'?'승인 대기':task.mode==='chat'?'응답 생성 중':`작업 중 · ${task.step||1}/${task.max_steps}`));feed.append(progress);}if(task.status==='paused'||task.status==='interrupted')feed.append(el('p','error-message',task.error||'결과를 확인하고 계속 요청하세요.'));const initialView=state.rendered.split(':')[0]!==task.id;if(atBottom||initialView)scroll.scrollTo({top:scroll.scrollHeight,behavior:initialView?'instant':'auto'});state.rendered=signature;}renderApproval(task);renderChanges();refreshControls();}
function renderApproval(task){const target=$('approval');const approval=task.approval;if(task.status!=='waiting_approval'||!approval){target.hidden=true;target.replaceChildren();return;}if(target.dataset.request===approval.id&&!target.hidden)return;target.dataset.request=approval.id;target.replaceChildren();target.hidden=false;target.append(el('h3','',approval.kind==='gpu'?'GPU 전송·실행 승인':'로컬 명령 실행 승인'));target.append(el('p','',approval.kind==='gpu'?'선택한 파일이 Modal로 전송되며 GPU 사용 요금이 발생합니다. 실행 내용을 확인하세요.':approval.payload.notice));if(approval.kind==='gpu')target.append(reviewNode(approval.payload));else target.append(el('pre','',JSON.stringify(approval.payload,null,2)));const actions=el('div','approval-actions');for(const [approve,text] of [[false,'거절'],[true,'승인하고 실행']]){const button=el('button',approve?'approve':'',text);button.addEventListener('click',handle(async()=>{actions.querySelectorAll('button').forEach(b=>b.disabled=true);await api('/api/approve',{task_id:task.id,approval_id:approval.id,approve});target.hidden=true;}));actions.append(button);}target.append(actions);}
function renderChanges(){const signature=state.task?.id+':'+state.task?.updated+':'+state.task?.events?.length;const target=$('change-list');if(target.dataset.signature===signature)return;target.dataset.signature=signature;target.replaceChildren();const changes=(state.task?.events||[]).filter(e=>e.kind==='change');$('change-count').textContent=changes.length;if(!changes.length){const box=el('div','empty-panel');box.append(el('span','','±'),el('p','','변경한 파일과 diff가 여기에 표시됩니다.'));target.append(box);return;}changes.forEach(event=>{const change=event.change;const card=el('div','change-card');const details=el('details');details.open=true;details.append(el('summary','',change.path));const pre=el('pre');for(const line of change.diff.split('\n'))pre.append(el('span',line.startsWith('+')?'diff-add':line.startsWith('-')?'diff-remove':'',line+'\n'));details.append(pre);card.append(details);if(change.backup_id){const restore=el('button','','이 수정 직전 내용으로 복구');restore.addEventListener('click',handle(async()=>{if(!confirm('현재 파일이 이 수정 이후 바뀌지 않은 경우에만 복구합니다. 진행할까요?'))return;await api('/api/restore',{task_id:state.current,backup_id:change.backup_id});await poll();toast('이전 내용을 복구했습니다.');}));card.append(restore);}target.append(card);});}
async function selectTask(id){clearAttachments();const task=await api('/api/tasks/'+id);setMode(task.mode==='chat'&&task.provider==='baseten'?'baseten':'meta',true);state.historyView=task.archived?'archived':'recent';state.selectedJob=null;state.current=id;state.task=null;state.rendered='';if(task.workspace&&state.root!==task.workspace){state.root='';await connectWorkspace(task.workspace);}state.task=task;$('provider').value=task.provider;$('model').value=task.model;$('allow-write').checked=task.allow_write;renderTask(task);history();}
async function sendTask(){let prompt=$('prompt').value.trim();if(chatAttachments.busy||attachmentBlocked())return;if(!prompt&&!chatAttachments.items.length)return;if(!prompt)prompt='첨부 파일을 확인해 주세요.';const button=$('send-task');button.disabled=true;try{const task=await api('/api/task',{root:state.current&&state.task?.mode==='chat'?state.task.workspace:state.root,prompt,mode:platform.mode==='baseten'?'chat':'work',use_memory:$('use-memory').checked,share_memory:$('share-memory').checked,provider:$('provider').value,model:platform.mode==='baseten'?state.providers.baseten.model:$('model').value.trim(),allow_write:$('allow-write').checked,max_steps:Number($('max-steps').value),task_id:state.current,conversation_id:chatAttachments.conversation,attachments:chatAttachments.items.map(({name,size,sha256})=>({name,size,sha256}))});clearAttachments();state.current=task.id;state.rendered='';$('prompt').value='';renderTask(task);await poll();}finally{refreshControls();}}
function renderJobs(){const signature=JSON.stringify(state.jobs.map(j=>[j.id,j.updated,j.history_busy]));if(renderJobs.signature===signature)return;renderJobs.signature=signature;renderModalHome();$('job-count').textContent=state.jobs.length;const target=$('job-list');target.replaceChildren();if(!state.jobs.length){const box=el('div','empty-panel');box.append(el('span','','◇'),el('p','','아직 GPU 작업이 없습니다.\n직접 실행하거나 에이전트에 요청하세요.'));target.append(box);return;}state.jobs.forEach(job=>{const card=el('div','job-card');card.tabIndex=0;card.setAttribute('role','button');card.append(el('strong','',job.spec.entry||job.spec.program));const meta=el('div','job-meta');meta.append(el('span','',job.spec.gpu+' · '+job.spec.program),badge(job.status));card.append(meta);card.addEventListener('click',()=>showJob(job.id));card.addEventListener('keydown',e=>{if(e.key==='Enter')showJob(job.id);});target.append(card);});if(state.viewingJob&&$('job-dialog').open)showJob(state.viewingJob,false);}
function showJob(id,open=true){const job=state.jobs.find(j=>j.id===id);if(!job)return;const version=job.id+':'+job.updated;if(!open&&$('job-detail').dataset.version===version)return;$('job-detail').dataset.version=version;state.viewingJob=id;$('job-title').textContent=job.spec.gpu+' · '+job.spec.program+' · '+job.id;const target=$('job-detail');const previous=target.querySelector('pre.log');const oldScroll=previous?.scrollTop||0;target.replaceChildren();if(job.goal_context?.goal_id)target.append(executionBreadcrumb(job.goal_context));target.append(badge(job.status));const tools=el('div','job-export-actions');const download=el('button','outline-button','↓ Markdown');download.setAttribute('aria-label','GPU 작업 Markdown 다운로드');download.disabled=running.has(job.status);download.addEventListener('click',()=>downloadMarkdown(job.spec.entry||job.spec.program,MarkdownExport.job(job)));const manage=el('button','outline-button','⋯');manage.setAttribute('aria-label','현재 GPU 작업 관리');manage.addEventListener('click',()=>openHistoryActions(historyEntries().find(e=>e.kind==='gpu'&&e.id===job.id)));tools.append(download,manage);target.append(tools);if(job.error)target.append(el('p','error-message',job.error));target.append(el('p','muted',job.workspace));if(running.has(job.status)||job.status==='interrupted'){const stop=el('button','outline-button','GPU 작업 중지');stop.addEventListener('click',handle(async()=>{await api('/api/gpu/cancel',{job_id:job.id});await poll();}));target.append(stop);}const pre=el('pre','log',job.logs||'Modal 작업을 준비하고 있습니다.');target.append(pre);pre.scrollTop=oldScroll;if(job.result){const details=el('details');details.append(el('summary','muted','하드웨어·실행 요약'),el('pre','job-summary',JSON.stringify(job.result,null,2)));target.append(details);}const artifacts=el('div','artifact-list');for(const item of job.result?.retained_outputs||[]){if((job.artifacts||[]).includes(item.path))continue;const link=el('a','','↓ '+item.path+' · '+bytes(item.size)+' (Volume)');link.href='/api/remote-artifact?job_id='+encodeURIComponent(job.id)+'&path='+encodeURIComponent(item.path);link.download=item.path.split('/').at(-1);artifacts.append(link);}if(job.result?.volume)artifacts.append(el('p','muted small','클라우드 보관: '+job.result.volume+'/'+job.id+' · 필요 없는 결과는 Modal 대시보드에서 삭제하세요.'));for(const path of job.artifacts||[]){const link=el('a','','↓ '+path);link.href='/api/artifact?job_id='+encodeURIComponent(job.id)+'&path='+encodeURIComponent(path);link.download=path.split('/').at(-1);artifacts.append(link);}target.append(artifacts);if(open&&!$('job-dialog').open)$('job-dialog').showModal();}
function openGpu(){if(!state.root){toast('먼저 작업 폴더를 선택하세요.',true);return;}$('gpu-dialog').showModal();}
function gpuProgramChanged(){const p=$('gpu-program').value;$('custom-fields').hidden=p!=='custom';$('blender-frame-label').hidden=p!=='blender';$('gpu-entry').placeholder=p==='blender'?'scenes/scene.blend 또는 tools/render.py':'train.py';}
function gpuSpec(){return {program:$('gpu-program').value,gpu:$('gpu-type').value,entry:$('gpu-entry').value.trim(),files:$('gpu-files').value.split('\n').map(v=>v.trim()).filter(Boolean),packages:$('gpu-packages').value.split(',').map(v=>v.trim()).filter(Boolean),args:JSON.parse($('gpu-args').value||'[]'),timeout:Number($('gpu-timeout').value),min_vram_gb:Number($('gpu-vram').value),frame:Number($('blender-frame').value),volume:$('gpu-volume').value.trim(),share_memory:$('share-memory').checked,image:$('gpu-image').value.trim(),command:JSON.parse($('gpu-command').value||'[]')};}
function reviewNode(preview){const node=el('div');const stats=el('div','review-stat');for(const [label,value] of [['GPU',preview.spec.gpu],['프로그램',preview.spec.program],['전송 크기',bytes(preview.total_bytes)]]){const box=el('div','',label);box.append(el('strong','',value));stats.append(box);}node.append(stats,el('p','muted',`최대 실행 ${preview.spec.timeout}초 · 원격 Linux · 재시도 0회`),el('p','muted',preview.spec.volume?'결과 보관: '+preview.spec.volume+' (Modal Volume 저장 요금 적용)':'영구 저장 없음 · 결과 반환은 합계 32 MB까지'));const files=el('div','review-files');for(const file of preview.files){const row=el('div','',file.path);row.append(el('span','',bytes(file.size)));files.append(row);}node.append(files,el('pre','',JSON.stringify({entry:preview.spec.entry,args:preview.spec.args,packages:preview.spec.packages,image:preview.spec.image,command:preview.spec.command},null,2)));return node;}
async function settings(){const data=await api('/api/bootstrap');updateConnections(data);const target=$('provider-settings');target.replaceChildren();for(const [name,p] of Object.entries(data.providers)){const group=el('section','provider-setting');const title=el('h3','',p.label);title.append(el('span','',p.ready?'키 설정됨':'키 필요'));group.append(title);if(name==='baseten'){const locked=el('div','fixed-provider-model');locked.append(el('strong','','Muse Spark 1026'),el('span','locked-tag','고정'),el('code','',p.model));group.append(locked);}else{for(const [key,label] of [['base_url','API Base URL'],['model','기본 모델 ID']]){const field=el('label','',label);const input=el('input');input.id='setting-'+name+'-'+key;input.value=p[key];field.append(input);group.append(field);}const models=el('button','text-button','사용 가능한 모델 조회');models.addEventListener('click',handle(async()=>{models.disabled=true;try{const ids=await api('/api/models',{provider:name});const list=el('p','muted small',ids.join(' · ')||'반환된 모델이 없습니다.');group.append(list);}finally{models.disabled=false;}}));group.append(models);}target.append(group);}await renderMuseRouteSettings();$('settings-dialog').showModal();}

async function poll(){
  if(poll.active)return;
  if(document.visibilityState==='hidden'){if(browserPermission()==='granted'||attention.audio?.state==='running')handle(pollNotifications)();return;}
  poll.active=true;
  try{
    const revision=state.historyRevision,dataRevision=state.dataRevision;
    const [view,current]=await Promise.all([api('/api/ui/history'),state.current?api('/api/tasks/'+state.current):null]);
    if(revision!==state.historyRevision||dataRevision!==state.dataRevision)return;
    state.tasks=view.tasks;state.jobs=view.jobs;state.goals=view.goals;renderGoals();history();renderJobs();handle(pollNotifications)();
    if(state.current){if(current?.id===state.current)renderTask(current);else if(!view.tasks.some(t=>t.id===state.current))newTask();}
    if(platform.mode==='goal'&&goalUI.selected)await loadGoal();
    const memoryVisible=$('memory-dialog').open||!$('inspector').hidden&&!$('panel-memory').hidden;
    if(memoryVisible&&(!poll.lastMemory||Date.now()-poll.lastMemory>6000)){poll.lastMemory=Date.now();handle(loadMemory)();}
    if(!poll.lastRoute||Date.now()-poll.lastRoute>6000){poll.lastRoute=Date.now();handle(loadMuseRoute)();}
  }catch(error){console.warn('Local status unavailable');}finally{poll.active=false;}
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible')handle(poll)();});
async function init(){try{const data=await api('/api/bootstrap?view=compact');state.token=data.token;updateConnections(data);state.tasks=data.tasks;state.jobs=data.jobs;state.goals=await api('/api/goals');await loadMuseRoute();for(const gpu of Object.keys(data.gpus))$('gpu-type').append(new Option(gpu+' · '+data.gpus[gpu]+' GB',gpu));history();renderJobs();providerChanged();const last=await api('/api/workspace');if(last.root)try{await connectWorkspace(last.root);}catch{}const basetenCommand='powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "'+data.project_root+'\\scripts\\set_baseten_key.ps1"';$('baseten-command').textContent=basetenCommand;$('copy-baseten').addEventListener('click',handle(async()=>{await navigator.clipboard.writeText(basetenCommand);toast('명령을 복사했습니다.');}));state.pollTimer=setInterval(poll,Math.max(1000,Math.min(15000,data.ui_settings?.ui_poll_ms||3000)));}catch(error){toast('로컬 서버 연결 실패: '+error.message,true);}}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>changeTab(b.dataset.tab)));
document.querySelectorAll('[data-prompt]').forEach(b=>b.addEventListener('click',()=>{$('prompt').value=b.dataset.prompt;$('prompt').focus();}));
$('provider').addEventListener('change',providerChanged);
$('new-task').addEventListener('click',newTask);
$('workspace-shortcut').addEventListener('click',()=>$('workspace-root').focus());
$('workspace-connect').addEventListener('click',handle(async()=>{await connectWorkspace($('workspace-root').value.trim());if(state.current)newTask();toast('작업 폴더를 열었습니다.');}));
$('workspace-root').addEventListener('keydown',e=>{if(e.key==='Enter')$('workspace-connect').click();});
$('folder-picker').addEventListener('click',handle(async()=>{const button=$('folder-picker');button.disabled=true;button.textContent='선택 창 열림…';try{const result=await api('/api/pick-folder',{});if(result.root){await connectWorkspace(result.root);newTask();}}finally{button.disabled=false;button.textContent='폴더 찾기';}}));
$('refresh-files').addEventListener('click',handle(async()=>{if(state.root)await connectWorkspace(state.root);}));
$('send-task').addEventListener('click',handle(sendTask));
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();if(!$('send-task').disabled)handle(sendTask)();}});
$('stop-task').addEventListener('click',handle(async()=>{if(state.current){await api('/api/cancel',{task_id:state.current});await poll();}}));
$('editor-save').addEventListener('click',handle(async()=>{await api('/api/file',{action:'write',root:state.root,path:state.editor.path,content:$('editor').value,expected_sha256:state.editor.sha256});$('editor-dialog').close();await connectWorkspace(state.root);toast('파일을 저장했습니다.');}));
$('settings-open').addEventListener('click',handle(settings));
$('refresh-connections').addEventListener('click',handle(async()=>{const data=await api('/api/bootstrap');updateConnections(data);$('settings-dialog').close();await settings();toast('사용자 환경 변수에서 키 상태를 다시 확인했습니다.');}));
$('settings-save').addEventListener('click',handle(async()=>{const data={meta:{base_url:$('setting-meta-base_url').value,model:$('setting-meta-model').value}};state.providers=await api('/api/settings',data);providerChanged();$('settings-dialog').close();toast('모델 설정을 저장했습니다.');}));
$('gpu-open').addEventListener('click',()=>{changeTab('jobs');openGpu();});$('new-gpu').addEventListener('click',openGpu);
$('gpu-program').addEventListener('change',gpuProgramChanged);
$('gpu-preview').addEventListener('click',handle(async()=>{const button=$('gpu-preview');button.disabled=true;try{state.review=await api('/api/gpu/preview',{root:state.root,spec:gpuSpec()});$('gpu-review').replaceChildren(reviewNode(state.review));$('confirm-dialog').showModal();}finally{button.disabled=false;}}));
$('gpu-submit').addEventListener('click',handle(async()=>{const button=$('gpu-submit');button.disabled=true;try{const job=await api('/api/gpu/submit',{root:state.root,spec:state.review.spec,preview_digest:state.review.digest,confirmed:true});$('confirm-dialog').close();$('gpu-dialog').close();changeTab('jobs');await poll();showJob(job.id);toast('Modal 작업을 제출했습니다.');}finally{button.disabled=false;}}));
document.addEventListener('keydown',e=>{if(e.ctrlKey&&e.key==='Enter'){e.preventDefault();if(!state.task||!running.has(state.task.status))newTask();}});

document.querySelectorAll('[data-history-view]').forEach(b=>b.addEventListener('click',()=>{state.historyView=b.dataset.historyView;history();}));
$('history-open').addEventListener('click',()=>{history();$('history-dialog').showModal();});
$('history-archive').addEventListener('click',handle(()=>manageHistory($('history-archive').dataset.action)));
$('history-delete').addEventListener('click',()=>showHistoryDelete(true));
$('history-delete-back').addEventListener('click',()=>showHistoryDelete(false));
$('history-delete-confirm').addEventListener('click',handle(async()=>{const button=$('history-delete-confirm');button.disabled=true;try{await manageHistory('delete');}finally{button.disabled=false;}}));
$('task-manage-open').addEventListener('click',()=>{const entry=historyEntries().find(e=>e.kind==='task'&&e.id===state.current);if(entry)openHistoryActions(entry);});
$('task-restore').addEventListener('click',handle(()=>manageHistory('restore',{kind:'task',id:state.current})));

function downloadMarkdown(title,text){
  const url=URL.createObjectURL(new Blob([text],{type:'text/markdown;charset=utf-8'}));
  const link=document.createElement('a');link.href=url;link.download=MarkdownExport.filename(title);document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('task-export').addEventListener('click',()=>{if(state.task)downloadMarkdown(state.task.title,MarkdownExport.task(state.task));});
