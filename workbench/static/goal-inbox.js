'use strict';
const goalInboxDrafts=new Map(),goalInboxSending=new Set();
const inboxStatusLabels={pending:'대기',processing:'반영 중',applied:'반영됨',partial:'일부 반영',rejected:'거부'};
let inboxGoalId=null;
function inboxDraft(id){if(!goalInboxDrafts.has(id))goalInboxDrafts.set(id,{kind:'note',target:'',text:'',cancel_gpu:false,cancel_long:false,request_id:null});return goalInboxDrafts.get(id);}
function readInboxDraft(){
  if(!inboxGoalId)return;const d=inboxDraft(inboxGoalId);
  Object.assign(d,{kind:$('goal-inbox-kind').value,target:$('goal-inbox-target').value,text:$('goal-inbox-text').value,cancel_gpu:$('goal-inbox-cancel-gpu').checked,cancel_long:$('goal-inbox-cancel-long').checked,request_id:null});updateInboxComposer();
}
function updateInboxComposer(){
  if(!inboxGoalId)return;const d=inboxDraft(inboxGoalId),goal=goalUI.detail?.goal,busy=goalInboxSending.has(inboxGoalId);
  const disabled=!goal||goal.archived||['done','stopped'].includes(goal.status);
  $('goal-inbox-cancellations').hidden=d.kind!=='interrupt';
  const hints={note:'다음 계획·검토에 참고합니다. 현재 작업은 계속됩니다.',instruction:'다음 계획·검토에 반영합니다. 지정한 항목의 다음 Builder 입력에도 원문을 전달합니다.',interrupt:'현재 도구가 끝나는 경계에서 멈춘 뒤 Planner가 즉시 재계획합니다. GPU·장시간 명령은 취소 옵션을 선택한 경우에만 취소합니다.'};
  $('goal-inbox-help').textContent=disabled?'완료·중지·아카이브 Goal에는 새 지시를 보낼 수 없습니다.':hints[d.kind]+(['paused','interrupted','blocked'].includes(goal.status)?' 이 Goal은 재개 전까지 지시를 보관합니다.':goal.status==='waiting_decision'&&d.kind!=='interrupt'?' 판단에 대한 답변은 판단 카드에서 보내세요.':'');
  for(const id of ['goal-inbox-kind','goal-inbox-target','goal-inbox-text','goal-inbox-cancel-gpu','goal-inbox-cancel-long'])$(id).disabled=disabled||busy;
  $('goal-inbox-send').disabled=disabled||busy||!d.text.trim();$('goal-inbox-send').textContent=busy?'전송 중…':'지시 보내기';
}
function renderGoalInbox(detail){
  const {goal,items}=detail,composer=$('goal-human-composer');composer.hidden=false;
  const switched=inboxGoalId!==goal.id;inboxGoalId=goal.id;const draft=inboxDraft(goal.id);
  const signature=JSON.stringify(items.map(i=>[i.id,i.title]));const select=$('goal-inbox-target');
  if(select.dataset.signature!==signature||switched){select.dataset.signature=signature;select.replaceChildren(new Option('Goal 전체',''));for(const item of items)select.append(new Option(item.title,item.id));select.value=draft.target;if(select.value!==draft.target){draft.target='';select.value='';}}
  if(switched){$('goal-inbox-kind').value=draft.kind;$('goal-inbox-text').value=draft.text;$('goal-inbox-cancel-gpu').checked=draft.cancel_gpu;$('goal-inbox-cancel-long').checked=draft.cancel_long;}
  const pending=(detail.directives||[]).filter(d=>d.state==='pending'||d.state==='processing').length;
  $('goal-inbox-count').textContent=pending?'처리 대기 '+pending+'건':'';updateInboxComposer();
}
async function sendGoalInbox(){
  const id=inboxGoalId;if(!id||goalInboxSending.has(id))return;const d=inboxDraft(id);if(!d.text.trim())return;
  goalInboxSending.add(id);updateInboxComposer();$('goal-inbox-error').textContent='';
  if(!d.request_id)d.request_id=crypto.randomUUID();
  try{
    await api('/api/goals/message',{goal_id:id,kind:d.kind,text:d.text.trim(),plan_item_id:d.target||null,request_id:d.request_id,cancel_gpu:d.kind==='interrupt'&&d.cancel_gpu,cancel_long:d.kind==='interrupt'&&d.cancel_long});
    d.text='';d.request_id=null;d.cancel_gpu=false;d.cancel_long=false;
    if(inboxGoalId===id){$('goal-inbox-text').value='';$('goal-inbox-cancel-gpu').checked=false;$('goal-inbox-cancel-long').checked=false;}
    await loadGoal();
  }catch(error){$('goal-inbox-error').textContent=error.message;throw error;}
  finally{goalInboxSending.delete(id);updateInboxComposer();}
}
for(const id of ['goal-inbox-kind','goal-inbox-target','goal-inbox-cancel-gpu','goal-inbox-cancel-long'])$(id).addEventListener('change',readInboxDraft);
$('goal-inbox-text').addEventListener('input',readInboxDraft);
$('goal-inbox-send').addEventListener('click',handle(sendGoalInbox));
