'use strict';
goalUI.archiveView=false;
const goalHistoryUI={goal:null,preview:null,busy:false};
function clearGoalSelection(){$('goal-human-composer').hidden=true;goalUI.selected=null;goalUI.detail=null;$('goal-detail').hidden=true;$('goal-empty').hidden=false;}
function renderGoalHistoryFilter(){
  const count=state.goals.filter(g=>g.archived).length;$('goal-archive-count').textContent=count;
  for(const button of document.querySelectorAll('[data-goal-view]')){const chosen=(button.dataset.goalView==='archived')===goalUI.archiveView;button.classList.toggle('selected',chosen);button.setAttribute('aria-pressed',String(chosen));}
}
function renderGoalHistoryControls(goal){
  const archived=!!goal.archived;$('goal-archived-label').hidden=!archived;
  if(archived){$('goal-pause').hidden=true;$('goal-stop').hidden=true;$('goal-resume').hidden=true;$('goal-settings-open').disabled=true;}
  else $('goal-stop').hidden=['done','stopped'].includes(goal.status);
  if(archived)for(const section of ['goal-approvals','goal-decisions','goal-plan-tree'])for(const button of $(section).querySelectorAll('button,input,textarea,select')){if(!button.textContent.startsWith('근거 ')&&!button.classList.contains('execution-view-action'))button.disabled=true;}
}
async function openGoalHistory(id=goalUI.selected){
  if(!id)return;const preview=await api('/api/goals/history?goal_id='+encodeURIComponent(id));
  goalHistoryUI.goal=preview.goal;goalHistoryUI.preview=preview;goalHistoryUI.busy=false;
  $('goal-history-title').textContent=MarkdownExport.goalTitle(preview.goal);
  $('goal-history-status').textContent=(preview.goal.archived?'아카이브됨 · ':'')+(goalLabels[preview.goal.status]||preview.goal.status);
  $('goal-history-reason').textContent=preview.reason;$('goal-history-reason').hidden=!preview.reason;
  $('goal-history-error').textContent='';$('goal-history-delete-panel').hidden=true;
  $('goal-history-archive').textContent=preview.goal.archived?'복원':'아카이브';
  $('goal-history-archive').disabled=preview.goal.archived?!!preview.reason:!preview.can_archive;
  $('goal-history-delete-open').hidden=!preview.goal.archived;$('goal-history-delete-open').disabled=!preview.can_delete;
  $('goal-delete-title').value='';$('goal-delete-exclude-memory').checked=false;$('goal-delete-confirm').disabled=true;
  $('goal-delete-memory-count').textContent=preview.memory_records+'개';
  $('goal-delete-gpu-notice').textContent='Modal Volume 결과는 유지됩니다.'+(preview.gpu_results?' 확인된 결과 작업 '+preview.gpu_results+'개.':'')+' 남아 있는 결과는 Modal 대시보드에서 직접 삭제하세요.';
  if(!$('goal-history-dialog').open)$('goal-history-dialog').showModal();
}
function updateGoalDeleteConfirmation(){
  $('goal-delete-confirm').disabled=goalHistoryUI.busy||$('goal-delete-title').value!==goalHistoryUI.goal?.title;
}
async function manageGoalHistory(action){
  const goal=goalHistoryUI.goal;if(!goal||goalHistoryUI.busy)return;
  goalHistoryUI.busy=true;updateGoalDeleteConfirmation();$('goal-history-archive').disabled=true;$('goal-history-delete-open').disabled=true;$('goal-history-error').textContent='';
  try{
    state.historyRevision++;
    const result=await api('/api/goals/history',{goal_id:goal.id,action,...(action==='delete'?{confirmed:true,confirmation_title:$('goal-delete-title').value,exclude_memory:$('goal-delete-exclude-memory').checked}:{})});
    state.historyRevision++;state.goals=await api('/api/goals');
    $('goal-history-dialog').close();
    if(action==='archive'||action==='delete'){if(goalUI.selected===goal.id)clearGoalSelection();}
    if(action==='restore'){goalUI.archiveView=false;state.historyView='recent';if(goalUI.selected===goal.id)await loadGoal();}
    renderGoals();history();await pollNotifications();if(action==='delete')await loadMemory();
    toast(action==='delete'?'Goal을 삭제했습니다. 비용 장부·파일·공유 메모리·GPU 결과는 유지됩니다.':action==='archive'?'Goal을 아카이브했습니다.':'Goal을 복원했습니다. 실행 상태는 유지됩니다.');
  }catch(error){$('goal-history-error').textContent='저장 실패: '+error.message;}
  finally{goalHistoryUI.busy=false;updateGoalDeleteConfirmation();$('goal-history-archive').disabled=goal.archived?!!goalHistoryUI.preview.reason:!goalHistoryUI.preview.can_archive;$('goal-history-delete-open').disabled=!goalHistoryUI.preview.can_delete;}
}
for(const button of document.querySelectorAll('[data-goal-view]'))button.addEventListener('click',()=>{goalUI.archiveView=button.dataset.goalView==='archived';clearGoalSelection();renderGoals();});
$('goal-manage-open').addEventListener('click',handle(()=>openGoalHistory()));
$('goal-history-archive').addEventListener('click',()=>manageGoalHistory(goalHistoryUI.goal?.archived?'restore':'archive'));
$('goal-history-delete-open').addEventListener('click',()=>{$('goal-history-delete-panel').hidden=false;$('goal-delete-title').focus();});
$('goal-delete-title').addEventListener('input',updateGoalDeleteConfirmation);
$('goal-delete-confirm').addEventListener('click',()=>manageGoalHistory('delete'));
