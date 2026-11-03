'use strict';
const routeRoles=['Planner','Builder','ImageAnalysis','ReferenceSearch'];
const routeLabels={Planner:'Planner / Reviewer',Builder:'Builder',ImageAnalysis:'Image analysis',ReferenceSearch:'Reference search'};
let registryView=null,routeFormReady=false;
function registryProviders(){return (goalUI.config?.registry||registryView)?.providers||[];}
function routeModel(route){return registryProviders().find(p=>p.id===route?.provider)?.models.find(m=>m.id===route?.model);}
function modelFitsRole(model,role){return model.roles?.includes(role)||((role==='Planner'||role==='Builder')?(model.capabilities.tool_calling||model.capabilities.structured_json):model.capabilities[role==='ImageAnalysis'?'image_input':'web_search']);}
function defaultRoleRoutes(){return {Planner:{provider:'baseten',model:'meta-models/muse-spark-open-1026',reasoning_effort:'high'},Builder:{provider:'meta',model:'muse-spark-1.3',reasoning_effort:'high'},ImageAnalysis:{provider:'meta',model:'muse-spark-1.3',reasoning_effort:'high'},ReferenceSearch:{provider:'meta',model:'muse-spark-1.3',reasoning_effort:'high'}};}
function roleNode(role,key){return $('route-'+role+'-'+key);}
function reasoningOptions(select,model,selected,preserve=false){
  const levels=model?.capabilities.reasoning_efforts||[];select.replaceChildren();
  for(const level of levels)select.append(new Option(level,level));
  if(!levels.length)select.append(new Option('지원하지 않음',''));
  if(preserve&&levels.length&&selected&&!levels.includes(selected)){const old=new Option(selected+' · 현재 미지원',selected);old.disabled=true;select.append(old);}
  select.disabled=!levels.length;select.value=preserve&&selected&&levels.length?selected:levels.includes(selected)?selected:levels.includes('high')?'high':levels[0]||'';
}
function routeBadge(node,model){
  node.replaceChildren();if(!model){node.append(el('small','muted','역할을 사용하지 않음'));return;}
  const cap=model.capabilities;for(const [flag,label] of [['tool_calling','Tools'],['structured_json','JSON'],['image_input','Vision'],['web_search','Web search']])if(cap[flag])node.append(el('span','route-badge',label));
  node.append(el('span','route-badge','Output ≤ '+cap.max_output.toLocaleString()));
  const p=model.effective_prices||model.prices;node.append(el('small','',p?`$${p.input} in / $${p.output} out · 1M tokens`: '가격 미등록 · 보수적 추정'));
  if(model.price_estimated)node.append(el('small','error-message','가격 미등록 · 보수적 추정 사용'));
  if(model.verification?.capabilities)node.append(el('small','muted',model.verification.capabilities));
}
function buildRoleCard(role){
  let card=role==='Builder'?$('goal-builder-card'):role==='Planner'?$('goal-planner-card'):$('route-card-'+role);
  if(!card){card=el('section','goal-fixed-role goal-role-card');card.id='route-card-'+role;$('goal-planner-card').after(card);}
  let provider=roleNode(role,'provider');if(provider)return card;
  card.querySelector('h3')?.remove();card.prepend(el('h3','',routeLabels[role]));
  const label=el('label','','공급자');provider=el('select');provider.id='route-'+role+'-provider';label.append(provider);card.querySelector('h3').after(label);
  let model=role==='Builder'?$('goal-model'):role==='Planner'?$('goal-planner-model'):null;
  if(!model){model=el('select');model.id=role==='Planner'?'goal-planner-model':'route-'+role+'-model';const modelLabel=el('label','','모델');modelLabel.append(model);if(role==='Planner'){card.querySelector('.goal-role-model')?.closest('label')?.replaceWith(modelLabel);}else card.append(modelLabel);}
  model.dataset.routeRole=role;
  let effort=role==='Planner'?$('goal-planner-reasoning'):role==='Builder'?$('goal-builder-reasoning'):null;
  if(!effort){const field=el('label','','추론 강도');effort=el('select');effort.id='route-'+role+'-effort';field.append(effort);card.append(field);}
  const badges=el('div','route-capabilities');badges.id='route-'+role+'-badges';card.append(badges);
  provider.addEventListener('change',()=>fillRoleModels(role,null));model.addEventListener('change',()=>{reasoningOptions(effort,routeModel(readRoleRoute(role)),effort.value);routeBadge(badges,routeModel(readRoleRoute(role)));validateGoalBuilderSelection();});
  return card;
}
function roleModelSelect(role){return role==='Builder'?$('goal-model'):role==='Planner'?$('goal-planner-model'):roleNode(role,'model');}
function roleEffortSelect(role){return role==='Builder'?$('goal-builder-reasoning'):role==='Planner'?$('goal-planner-reasoning'):roleNode(role,'effort');}
function readRoleRoute(role){const provider=roleNode(role,'provider')?.value;if(!provider)return null;return {provider,model:roleModelSelect(role).value,reasoning_effort:roleEffortSelect(role).value||null};}
function fillRoleModels(role,selected){
  const provider=registryProviders().find(p=>p.id===roleNode(role,'provider').value),select=roleModelSelect(role),effort=roleEffortSelect(role);
  select.replaceChildren();for(const model of provider?.models.filter(m=>modelFitsRole(m,role))||[])select.append(new Option(model.id,model.id));
  if(selected?.model){if(![...select.options].some(o=>o.value===selected.model)){const old=new Option(selected.model+' · 등록 확인 필요',selected.model);old.disabled=true;select.append(old);}select.value=selected.model;}
  select.disabled=!provider;reasoningOptions(effort,routeModel(readRoleRoute(role)),selected?.reasoning_effort||effort.value||'high',!!selected);routeBadge(roleNode(role,'badges'),routeModel(readRoleRoute(role)));validateGoalBuilderSelection();
}
function renderRoleRoutes(goal){
  if(!goalUI.config?.registry)return;routeFormReady=false;const routes=goal?.role_routes||goal?.effective_routes||defaultRoleRoutes();
  for(const role of routeRoles){buildRoleCard(role);const select=roleNode(role,'provider');select.replaceChildren();
    if(['ImageAnalysis','ReferenceSearch'].includes(role))select.append(new Option('사용하지 않음',''));
    for(const provider of registryProviders()){const option=new Option(provider.name+(provider.ready?'':' · 키 필요'),provider.id);option.disabled=!provider.models.some(m=>modelFitsRole(m,role));select.append(option);}
    if(routes[role]?.provider&&![...select.options].some(o=>o.value===routes[role].provider)){const old=new Option(routes[role].provider+' · 등록 확인 필요',routes[role].provider);old.disabled=true;select.append(old);}select.value=routes[role]?.provider||'';fillRoleModels(role,routes[role]);
  }
  let extra=$('route-extra-budgets');if(!extra){extra=el('section','span-2 route-extra-budgets');extra.id='route-extra-budgets';$('goal-create-dialog').querySelector('.goal-form').append(extra);}extra.replaceChildren();
  for(const provider of registryProviders().filter(p=>!['meta','baseten','modal_llm'].includes(p.id))){const budget=goal?.budgets[provider.id]||provider.budget;const field=el('label','',provider.name+' · Goal 예산 USD'),input=el('input');input.type='number';input.min='0';input.step='.01';input.value=budget.total;input.dataset.routeBudget=provider.id;field.append(input);const daily=el('label','','일일 상한 · 빈칸은 없음'),day=el('input');day.type='number';day.min='0';day.step='.01';day.value=budget.daily??'';day.dataset.routeDaily=provider.id;daily.append(day);extra.append(field,daily);}
  routeFormReady=true;$('goal-model-status').hidden=true;$('goal-planner-reasoning-help').hidden=true;validateGoalBuilderSelection();
}
const legacyGoalValidation=validateGoalBuilderSelection;
validateGoalBuilderSelection=function(report=false){
  if(!goalUI.config?.registry)return legacyGoalValidation(report);
  const valid=routeRoles.every(role=>{const route=readRoleRoute(role),model=routeModel(route);if(!route)return !['Planner','Builder'].includes(role);return !!model&&modelFitsRole(model,role)&&(!model.capabilities.reasoning_efforts.length||model.capabilities.reasoning_efforts.includes(route.reasoning_effort));});
  $('goal-create-submit').disabled=!!goalUI.formSaving||!routeFormReady||!valid;$('goal-model-error').hidden=valid;$('goal-model-error').textContent=valid?'':'필수 역할의 공급자와 모델을 선택하세요.';return valid;
};
const legacyModelLoad=loadGoalBuilderModels;
loadGoalBuilderModels=async function(request){if(goalUI.config?.registry)return;return legacyModelLoad(request);};
const legacyOpenGoalForm=openGoalCreate;
openGoalCreate=async function(edit=false){await legacyOpenGoalForm(edit);if(goalUI.config?.registry){const stored=edit?goalUI.detail?.goal:null;renderRoleRoutes(stored?{...stored,effective_routes:goalUI.detail.effective_routes,...(stored.pending_settings?.values||{})}:null);goalUI.formBaseline=JSON.stringify(goalFormPayload());}};
const legacyGoalPayload=goalFormPayload;
goalFormPayload=function(){const data=legacyGoalPayload();if(!goalUI.config?.registry||!routeFormReady)return data;data.role_routes=Object.fromEntries(routeRoles.map(role=>[role,readRoleRoute(role)]));
  if(data.role_routes.Builder?.provider!=='meta')delete data.builder_model;
  delete data.reasoning;
  for(const input of document.querySelectorAll('[data-route-budget]')){const daily=document.querySelector('[data-route-daily="'+input.dataset.routeBudget+'"]');data.budgets[input.dataset.routeBudget]={total:Number(input.value),daily:daily.value===''?null:Number(daily.value)};}
  return data;
};
const legacyGoalRender=renderGoalDetail;
renderGoalDetail=function(detail){legacyGoalRender(detail);const routes=detail.goal.role_routes||detail.effective_routes;if(routes){$('goal-meta').textContent=`Planner · ${routes.Planner.provider} / ${routes.Planner.model}\nBuilder · ${routes.Builder.provider} / ${routes.Builder.model}\n${detail.goal.workspace}\n${detail.goal.deadline_local} [${detail.goal.timezone}]`;}for(const row of document.querySelectorAll('[data-plan-item]')){if(row.querySelector('.item-route-open'))continue;const item=detail.items.find(i=>i.id===row.dataset.planItem);if(!item)continue;const button=el('button','text-button item-route-open',item.pending_builder_route?'Builder 경로 · 다음 묶음 대기':item.builder_route?item.builder_route.provider+' / '+item.builder_route.model:'Builder 경로 · Goal 기본값');button.disabled=item.status==='superseded'||detail.goal.archived;button.addEventListener('click',handle(()=>openItemRoute(detail.goal,item)));row.append(button);}};
function openItemRoute(goal,item){
  let dialog=$('item-route-dialog');if(!dialog){dialog=el('dialog','dialog');dialog.id='item-route-dialog';document.body.append(dialog);}dialog.replaceChildren(el('h2','',item.title));
  const select=el('select');select.setAttribute('aria-label','항목 Builder 경로');select.append(new Option('Goal Builder 설정 따르기',''));
  for(const provider of registryProviders())for(const model of provider.models.filter(m=>modelFitsRole(m,'Builder')))select.append(new Option(provider.name+' / '+model.id,JSON.stringify({provider:provider.id,model:model.id})));
  const route=item.pending_builder_route?.route||item.builder_route;const value=route?JSON.stringify({provider:route.provider,model:route.model}):'';if(value&&![...select.options].some(o=>o.value===value)){const old=new Option(route.provider+' / '+route.model+' · 등록 확인 필요',value);old.disabled=true;select.append(old);}select.value=value;
  const effort=el('select'),badges=el('div','route-capabilities');const update=()=>{const selected=select.value?JSON.parse(select.value):null;reasoningOptions(effort,routeModel(selected),route?.reasoning_effort||'high');routeBadge(badges,routeModel(selected));};select.addEventListener('change',update);update();
  const error=el('p','error-message'),actions=el('div','dialog-actions'),close=el('button','outline-button','닫기'),save=el('button','primary-button','저장');close.addEventListener('click',()=>dialog.close());save.addEventListener('click',async()=>{save.disabled=true;try{const selected=select.value?{...JSON.parse(select.value),reasoning_effort:effort.value||null}:null;await api('/api/goals/item-route',{goal_id:goal.id,plan_item_id:item.id,builder_route:selected});dialog.close();await loadGoal();}catch(e){error.textContent=e.message;}finally{save.disabled=false;}});actions.append(close,save);dialog.append(select,effort,badges,el('p','muted','실행 중 변경은 다음 Builder 묶음부터 적용됩니다.'),error,actions);dialog.showModal();
}
const legacyReasoningUpdate=updateReasoningUI;
updateReasoningUI=function(){legacyReasoningUpdate();if(typeof platform==='undefined'||platform.mode!=='goal')return;const goal=goalUI.detail?.goal,routes=goal?.pending_settings?.values?.role_routes||goal?.role_routes||goalUI.detail?.effective_routes;if(!routes)return;
  for(const role of ['Planner','Builder'])reasoningOptions($('goal-'+role.toLowerCase()+'-effort'),routeModel(routes[role]),routes[role]?.reasoning_effort);
  $('reasoning-notice').hidden=true;
};

async function renderProviderRegistry(){
  registryView=await api('/api/provider-registry');if(goalUI.config)goalUI.config.registry=registryView;
  let section=$('registered-provider-settings');if(!section){section=el('section','registered-provider-settings');section.id='registered-provider-settings';$('provider-settings').after(section);}section.replaceChildren(el('h3','','Goal 공급자 · 모델'));
  const add=el('button','outline-button','OpenAI 호환 공급자 등록'),template=el('button','outline-button','OpenAI 템플릿');add.addEventListener('click',()=>openProviderRegistration());template.addEventListener('click',()=>openProviderRegistration(registryView.templates[0]));section.append(add,template);
  for(const provider of registryView.providers){const card=el('article','provider-registry-card');card.append(el('h4','',provider.name+' · '+(provider.ready?'키 등록됨':'키 필요')),el('code','',provider.base_url),el('p','muted',provider.api_style+' · 사용 $'+provider.spend.estimated_usd.toFixed(4)));
    for(const model of provider.models){const row=el('div'),badges=el('div','route-capabilities');row.append(el('strong','',model.id));routeBadge(badges,model);row.append(badges);const configure=el('button','text-button','이 모델 설정');configure.addEventListener('click',()=>openProviderRegistration(provider,model.id));row.append(configure);card.append(row);}
    if(!provider.models.length)card.append(el('p','muted','등록 모델 없음 · 기능 확인 후 추가하세요.'));
    const edit=el('button','text-button','모델·가격·기능 설정');edit.addEventListener('click',()=>openProviderRegistration(provider));card.append(edit);
    const model=el('select');for(const row of provider.models)model.append(new Option(row.id,row.id));const test=el('button','outline-button','연결 시험 · 최대 $0.05');test.disabled=!provider.ready||!provider.models.length;test.addEventListener('click',async()=>{test.disabled=true;try{const result=await api('/api/provider-registry/test',{provider:provider.id,model:model.value});toast('연결 확인 · $'+result.estimated_usd.toFixed(6)+' · 기능 검증은 별도');await renderProviderRegistry();}catch(e){toast(e.message);}finally{test.disabled=false;}});card.append(model,test);
    const command=el('code','provider-key-command',`powershell.exe -NoProfile -ExecutionPolicy RemoteSigned -File "${state.projectRoot||state.project_root||'.'}\\scripts\\set_provider_key.ps1" -Provider ${provider.id}`);if(!provider.locked)card.append(command);section.append(card);
  }
}
function openProviderRegistration(existing,selectedModel){
  let dialog=$('provider-registration-dialog');if(!dialog){dialog=el('dialog','dialog provider-registration-dialog');dialog.id='provider-registration-dialog';document.body.append(dialog);}dialog.replaceChildren(el('h2','','공급자 · 모델 등록'));
  const form=el('div','provider-registration-fields'),fields={};const input=(key,label,value='',type='text')=>{const field=el('label','',label),node=el(type==='textarea'?'textarea':type==='select'?'select':'input');if(type!=='textarea'&&type!=='select')node.type=type;node.value=value??'';fields[key]=node;field.append(node);form.append(field);return node;};
  input('id','공급자 ID',existing?.id||'');input('name','표시 이름',existing?.name||'');input('base_url','API Base URL',existing?.base_url||'https://');
  const style=input('api_style','API 형식','','select');for(const value of ['chat_completions','responses'])style.append(new Option(value,value));style.value=existing?.api_style||'chat_completions';
  const output=input('output_parameter','Chat 출력 한도 필드','','select');for(const value of ['max_completion_tokens','max_tokens'])output.append(new Option(value,value));output.value=existing?.output_parameter||'max_completion_tokens';
  const first=existing?.models?.find(m=>m.id===selectedModel)||existing?.models?.[0];input('models','모델 ID · 한 줄에 하나',(existing?.models||[]).map(m=>m.id).join('\n'),'textarea');
  if(first)form.append(el('p','muted','아래 기능·가격 설정 대상: '+first.id+' · 다른 등록 모델의 설정은 보존됩니다.'));
  const cap=first?.capabilities||{};for(const [key,label] of [['tool_calling','Tool calling'],['structured_json','Structured JSON'],['image_input','Image input'],['reasoning_content','Reasoning round-trip'],['web_search','Built-in web search']]){const box=input(key,label,'','checkbox');box.checked=cap[key]??false;}
  input('reasoning_efforts','추론 강도 · 쉼표로 구분',(cap.reasoning_efforts||[]).join(', '));input('max_context','최대 컨텍스트 · 미확인이면 빈칸',cap.max_context,'number');input('max_output','최대 출력',cap.max_output||4096,'number');
  for(const [key,label] of [['input','입력 USD / 1M'],['cached_input','캐시 입력 USD / 1M'],['output','출력 USD / 1M'],['web_search','검색 USD / 회']])input('price_'+key,label,first?.prices?.[key],'number');
  input('concurrency','동시 요청 상한',existing?.limits?.concurrency||2,'number');input('requests_per_minute','분당 요청 상한 · 선택',existing?.limits?.requests_per_minute,'number');input('total','공급자 전체 예산 USD',existing?.budget?.total??100,'number');input('daily','일일 예산 USD · 선택',existing?.budget?.daily,'number');const lan=input('allow_lan','지정 LAN IP 허용','','checkbox');lan.checked=existing?.allow_lan||false;
  if(existing?.locked)for(const key of ['id','base_url','api_style'])fields[key].disabled=true;
  const keyHelp=el('p','muted','키는 이 화면에 입력하지 않습니다. 저장 후 표시되는 PowerShell 보안 입력 명령으로 등록하세요. 가격이 비어 있으면 보수적 추정을 사용합니다. 기능 선택은 사용자 선언이며 연결 시험만으로 검증되지 않습니다.');
  const error=el('p','error-message'),actions=el('div','dialog-actions'),close=el('button','outline-button','닫기'),save=el('button','primary-button','등록 저장');close.addEventListener('click',()=>dialog.close());save.addEventListener('click',async()=>{save.disabled=true;try{
    const ids=fields.models.value.split(/\r?\n/).map(v=>v.trim()).filter(Boolean),capabilities={};for(const key of ['tool_calling','structured_json','image_input','reasoning_content','web_search'])capabilities[key]=fields[key].checked;
    Object.assign(capabilities,{reasoning_efforts:fields.reasoning_efforts.value.split(',').map(v=>v.trim()).filter(Boolean),max_context:fields.max_context.value===''?null:Number(fields.max_context.value),max_output:Number(fields.max_output.value)});
    const prices=fields.price_input.value===''||fields.price_output.value===''?null:{input:Number(fields.price_input.value),cached_input:fields.price_cached_input.value===''?Number(fields.price_input.value):Number(fields.price_cached_input.value),output:Number(fields.price_output.value),web_search:Number(fields.price_web_search.value||0)};
    const provider={id:fields.id.value.trim(),name:fields.name.value.trim(),base_url:fields.base_url.value.trim(),api_style:style.value,output_parameter:output.value,allow_lan:lan.checked,
      models:ids.map(id=>{const previous=existing?.models?.find(m=>m.id===id);return {id,capabilities:previous&&id!==first?.id?previous.capabilities:capabilities,prices:previous&&id!==first?.id?previous.prices:prices};}),limits:{concurrency:Number(fields.concurrency.value),requests_per_minute:fields.requests_per_minute.value===''?null:Number(fields.requests_per_minute.value)},budget:{...(existing?.budget||{}),total:Number(fields.total.value),daily:fields.daily.value===''?null:Number(fields.daily.value)}};
    await api('/api/provider-registry',provider);dialog.close();await renderProviderRegistry();toast('등록했습니다. 키는 로컬 보안 입력 스크립트로 설정하세요.');
  }catch(e){error.textContent=e.message;}finally{save.disabled=false;}});actions.append(close,save);dialog.append(form,keyHelp,error,actions);dialog.showModal();
}
