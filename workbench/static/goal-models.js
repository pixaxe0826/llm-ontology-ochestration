'use strict';
function supportedGoalBuilder(value){return (goalUI.config?.builder_models||['muse-spark-1.3']).includes(value);}
function populateGoalBuilderModels(models,selected){
  const select=$('goal-model');select.replaceChildren(new Option('모델 선택',''));
  const ids=[...new Set([goalUI.config?.builder_default||'muse-spark-1.3',...models,selected].filter(Boolean))];
  for(const id of ids){const option=new Option(id,id);option.disabled=!supportedGoalBuilder(id);select.append(option);}
  select.value=selected;validateGoalBuilderSelection();
}
function resetGoalBuilderModel(selected){
  goalUI.modelRequest=(goalUI.modelRequest||0)+1;goalUI.formSaving=false;
  populateGoalBuilderModels([],selected);
  $('goal-model-status').hidden=false;$('goal-model-status').textContent='모델 목록을 불러오는 중…';
}
async function loadGoalBuilderModels(request){
  try{
    const catalog=await api('/api/goals/models',{});
    if(request!==goalUI.modelRequest||!$('goal-create-dialog').open)return;
    if(!Array.isArray(catalog.models)||!catalog.models.length)throw Error('No usable models.');
    const selected=$('goal-model').value;populateGoalBuilderModels(catalog.models.filter(supportedGoalBuilder),selected);
    const absent=selected&&!catalog.models.includes(selected);
    $('goal-model-status').hidden=!absent;$('goal-model-status').textContent=absent?'현재 선택한 모델은 조회 목록에 없음 · 선택값 유지':'';
  }catch{
    if(request!==goalUI.modelRequest||!$('goal-create-dialog').open)return;
    $('goal-model-status').hidden=false;$('goal-model-status').textContent='목록을 불러오지 못함';
  }
}
function validateGoalBuilderSelection(report=false){
  const select=$('goal-model'),valid=supportedGoalBuilder(select.value);
  select.setCustomValidity(valid?'':'작업 모드에서 사용할 Builder 모델을 선택하세요.');
  $('goal-model-error').hidden=valid;$('goal-model-error').textContent=valid?'':'Builder 모델을 선택하세요.';
  $('goal-create-submit').disabled=!!goalUI.formSaving||!valid;
  if(!valid&&report)select.reportValidity();
  return valid;
}
$('goal-model').addEventListener('change',()=>validateGoalBuilderSelection());
