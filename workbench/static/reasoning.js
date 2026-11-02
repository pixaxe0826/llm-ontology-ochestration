'use strict';
const reasoningUnsupported='이 공급자는 추론 강도 설정을 지원하지 않음';
function updateReasoningUI(){
  const isGoal=typeof platform!=='undefined'&&platform.mode==='goal';
  $('reasoning-control').hidden=isGoal;$('goal-reasoning-controls').hidden=!isGoal;
  const provider=$('provider').value||'meta';const settings=state.providers[provider]||{};
  const supported=settings.reasoning_supported!==false;
  $('reasoning-effort').value=settings.reasoning_effort||'high';
  $('reasoning-effort').disabled=!supported||!!(state.task&&running.has(state.task.status));
  $('reasoning-effort').title=supported?'추론 강도':reasoningUnsupported;
  const plannerSupported=state.museOpen?.reasoning_supported??(state.providers.baseten?.reasoning_supported!==false);
  $('reasoning-notice').hidden=isGoal?plannerSupported:supported;
  $('reasoning-notice').textContent=isGoal&&state.museOpen?.route==='modal'?'Modal 공유 엔드포인트 추론 강도 미확인 · 파라미터 미전달':reasoningUnsupported;
  if(isGoal){const chosen=goalUI.detail?.goal.pending_settings?.values?.reasoning||goalUI.detail?.goal.reasoning||goalUI.defaultReasoning||goalUI.config?.defaults?.reasoning||{Planner:'high',Builder:'high'};for(const role of ['Planner','Builder'])$('goal-'+role.toLowerCase()+'-effort').value=chosen[role]||'high';$('goal-planner-effort').disabled=!plannerSupported;$('goal-planner-effort').title=plannerSupported?'Planner 추론 강도':reasoningUnsupported;}
}
$('reasoning-effort').addEventListener('change',handle(async()=>{const provider=$('provider').value;state.providers=await api('/api/settings',{[provider]:{reasoning_effort:$('reasoning-effort').value}});updateReasoningUI();toast('추론 강도를 저장했습니다.');}));
for(const role of ['planner','builder'])$('goal-'+role+'-effort').addEventListener('change',handle(async()=>{
  const chosen={Planner:$('goal-planner-effort').value,Builder:$('goal-builder-effort').value};
  const result=await api('/api/goals/reasoning',{reasoning:chosen,...(goalUI.selected?{goal_id:goalUI.selected}:{})});
  goalUI.defaultReasoning=chosen;
  if(goalUI.config)goalUI.config.defaults={reasoning:chosen};
  if(result.goal){goalUI.detail=result;renderGoalDetail(result);}
  updateReasoningUI();toast(result.goal?.pending_settings?'추론 강도를 저장했습니다. 현재 단계 종료 후 적용됩니다.':'Goal 역할별 추론 강도를 저장했습니다.');
}));
