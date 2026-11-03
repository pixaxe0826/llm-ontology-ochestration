'use strict';
function renderMuseRoute(value){
  state.museOpen=value;
  const modal=value.route==='modal';
  $('goal-route-name').textContent=modal?'Modal Shared Endpoint':'Baseten';
  $('goal-route-endpoint').textContent=modal?value.endpoint_id:'Muse Spark Open 1026';
  $('goal-route-budget').textContent=`Modal LLM $${value.modal_llm_committed.toFixed(4)} / $${value.modal_llm_cap.toFixed(2)}`;
  $('muse-budget-notice').hidden=!value.suggest_modal;
  $('muse-budget-text').textContent=`Baseten 예산 80% 도달 · $${value.baseten_committed.toFixed(2)} / $${value.baseten_total.toFixed(2)}`;
  const role=$('goal-planner-provider');if(role)role.textContent='Planner / Reviewer · '+(modal?'Modal':'Baseten');
  updateReasoningUI();
}
async function loadMuseRoute(){renderMuseRoute(await api('/api/muse-open'));}
async function renderMuseRouteSettings(){
  const value=await api('/api/muse-open');renderMuseRoute(value);
  const section=$('muse-route-settings');section.replaceChildren();
  const heading=el('h3','','Muse Spark Open');heading.append(el('span','','제품 프록시 기본 경로 · Goal 역할은 별도 설정'));section.append(heading);
  const form=el('div','muse-route-form');
  const routeLabel=el('label','','공급 경로');const route=el('select');route.id='muse-route-choice';route.append(new Option('Baseten','baseten'),new Option('Modal · 공유 엔드포인트','modal'));route.value=value.route;routeLabel.append(route);
  const capLabel=el('label','','플랫폼 Modal LLM 상한 · USD');const cap=el('input');cap.id='muse-modal-cap';cap.type='number';cap.min=0;cap.step='.01';cap.value=value.modal_llm_cap;capLabel.append(cap);
  const endpointLabel=el('label','span-2','Muse Spark Open 엔드포인트');const endpoint=el('select');endpoint.id='muse-endpoint-choice';endpoint.append(new Option(value.catalog.endpoints.length?'엔드포인트 선택':'Muse Spark Open 엔드포인트 없음',''));for(const item of value.catalog.endpoints)endpoint.append(new Option(item.id,item.id));endpoint.value=value.endpoint_id;endpointLabel.append(endpoint);
  form.append(routeLabel,capLabel,endpointLabel);section.append(form);
  const info=el('small','muse-route-info',`Modal LLM $${value.modal_llm_committed.toFixed(4)} · GPU $${value.modal_gpu_committed.toFixed(4)} · 추정 USD`);section.append(info);
  const status=el('small','muse-route-info',value.modal_ready?'Proxy Token 설정됨':'MODAL_PROXY_TOKEN 필요');section.append(status);
  const actions=el('div','muse-route-actions');const discover=el('button','outline-button','엔드포인트 조회');discover.id='muse-endpoint-refresh';
  discover.addEventListener('click',handle(async()=>{discover.disabled=true;try{await api('/api/muse-open/models',{});await renderMuseRouteSettings();toast('엔드포인트 목록을 조회했습니다.');}finally{discover.disabled=false;}}));
  const save=el('button','primary-button','경로 적용');save.id='muse-route-save';save.addEventListener('click',handle(async()=>{save.disabled=true;try{const result=await api('/api/muse-open',{route:route.value,endpoint_id:endpoint.value,modal_llm_cap:Number(cap.value)});renderMuseRoute(result);await renderMuseRouteSettings();toast('제품 프록시 기본 경로를 적용했습니다.');}finally{save.disabled=false;}}));
  actions.append(discover,save);section.append(actions);
  const toggle=()=>{endpoint.disabled=route.value!=='modal';};route.addEventListener('change',toggle);toggle();
}
for(const id of ['goal-route-open','muse-budget-open'])$(id).addEventListener('click',handle(settings));
