'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../workbench/static/goal-decisions.js'),'utf8');
const saved=new Map();
function context(){const ctx={console,localStorage:{getItem:k=>saved.get(k)||null,setItem:(k,v)=>saved.set(k,v)},$:()=>({addEventListener(){}}),handle:f=>f};vm.createContext(ctx);vm.runInContext(source,ctx);return ctx;}
let ctx=context();const items=[{id:'item-p4',key:'P4',title:'P4 Belt model'}];
const review={state:'answered',question:JSON.stringify({kind:'review_error',item_key:'P4',summary:'Raw source summary'}),affected_items:[],answer:'Use existing modules.\nThen test.'};
const original=JSON.stringify(review);assert.equal(ctx.goalDecisionTitle(review,items),'Planner 검토 오류 — P4 Belt model');assert.equal(JSON.stringify(review),original);
assert.equal(ctx.goalDecisionTitle({question:JSON.stringify({title:'P4 Belt model',reason:'Rework or Builder batch limit reached.'})},items),'묶음 한도 도달 — P4 Belt model');
assert.equal(ctx.goalDecisionTitle({question:'The four-chunk limit was reached.',affected_items:['item-p4']},items),'묶음 한도 도달 — P4 Belt model');
assert.equal(ctx.decisionShortText(review.answer),'Use existing modules.');
const moment=Date.parse('2026-11-01T13:00:00Z')/1000;
assert.equal(ctx.goalDecisionTime({state:'answered',answered_at:moment},{timezone:'America/New_York'}).compact,'11/01 08:00');
assert.equal(ctx.goalDecisionTime({state:'withdrawn',answered_at:moment,withdrawn_at:moment+3600},{timezone:'UTC'}).compact,'11/01 14:00');
assert.equal(ctx.goalDecisionTime({state:'withdrawn'},{timezone:'UTC'}).label,'처리 시각 없음');
assert.equal(ctx.goalDecisionTitle({question:'{"kind":"review_error"}',human_brief:{summary:'문서 숫자 출처를 확인해야 합니다.'}},items),'문서 숫자 출처를 확인해야 합니다.');
let view=ctx.decisionView('goal-a');assert.equal(view.groupOpen,false);view.groupOpen=true;view.expanded.add('decision-1');ctx.saveDecisionView('goal-a');
ctx=context();assert.equal(ctx.decisionView('goal-a').groupOpen,true);assert.deepEqual([...ctx.decisionView('goal-a').expanded],['decision-1']);assert.equal(ctx.decisionView('goal-b').groupOpen,false);
assert.deepEqual(Object.keys(JSON.parse(saved.get('muse.goal.decisions.goal-a'))),['groupOpen','expanded']);
saved.set('muse.goal.decisions.broken','{bad');assert.equal(ctx.decisionView('broken').groupOpen,false);
ctx.localStorage.setItem=()=>{throw new Error('Storage unavailable');};assert.doesNotThrow(()=>ctx.saveDecisionView('goal-a'));
console.log('PASS: decision titles, original preservation, timezone/DST, first-line answer, per-Goal persistence and unavailable storage');

(async()=>{
  let calls=[],finish,fail;
  const send={disabled:false},input={disabled:false},preview={textContent:''};
  const option={dataset:{option:'A'},setAttribute(){},classList:{toggle(){}},disabled:false};
  const card={querySelector:s=>s==='.decision-send'?send:s==='[data-decision-input]'?input:preview,querySelectorAll:()=>[option]};
  const ctx={console,localStorage:{getItem:()=>null,setItem(){}},handle:f=>f,
    $:id=>id==='decision-d'?card:{addEventListener(){}},goalUI:{detail:{decisions:[{id:'d',options:['A','B']}] }},
    api:async(url,body)=>{calls.push({url,body});return new Promise((resolve,reject)=>{finish=resolve;fail=reject;});},loadGoal:async()=>{},poll:async()=>{}};
  vm.createContext(ctx);vm.runInContext(source,ctx);
  vm.runInContext("decisionSelections.set('d','A');decisionDrafts.set('d','Keep existing modules.');",ctx);
  const first=ctx.sendGoalDecision('g','d');assert.equal(send.disabled,true);assert.equal(input.disabled,true);assert.equal(option.disabled,true);
  await ctx.sendGoalDecision('g','d');assert.equal(calls.length,1);assert.equal(calls[0].body.option,'A');assert.equal(calls[0].body.answer,'Keep existing modules.');assert.match(preview.textContent,/Keep existing modules/);
  finish({});await first;assert.equal(vm.runInContext("decisionSelections.has('d')||decisionDrafts.has('d')",ctx),false);
  await ctx.sendGoalDecision('g','d');assert.equal(calls.length,1);
  vm.runInContext("decisionDrafts.set('d','Text only');",ctx);const failed=ctx.sendGoalDecision('g','d');fail(new Error('offline'));await assert.rejects(failed,/offline/);
  assert.equal(vm.runInContext("decisionDrafts.get('d')",ctx),'Text only');assert.equal(vm.runInContext("decisionSending.has('d')",ctx),false);
  const widgets=new Map();const node=id=>{if(!widgets.has(id))widgets.set(id,{addEventListener(){},value:'',checked:false,disabled:false,textContent:'',hidden:false});return widgets.get(id);};
  const inbox={console,handle:f=>f,$:node,crypto:{randomUUID:()=> 'test-request-123'},goalUI:{detail:{goal:{id:'g',status:'running'}}},loadGoal:async()=>{},
    api:async(url,body)=>{calls.push({url,body});return new Promise(resolve=>{finish=resolve;});}};
  vm.createContext(inbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../workbench/static/goal-inbox.js'),'utf8'),inbox);
  vm.runInContext("inboxGoalId='g';Object.assign(inboxDraft('g'),{kind:'instruction',target:'item',text:'Test each module.',cancel_gpu:true,cancel_long:true});",inbox);
  const pending=inbox.sendGoalInbox();assert.equal(node('goal-inbox-send').disabled,true);await inbox.sendGoalInbox();assert.equal(calls.length,3);
  assert.equal(calls[2].body.kind,'instruction');assert.equal(calls[2].body.plan_item_id,'item');assert.equal(calls[2].body.cancel_gpu,false);assert.equal(calls[2].body.cancel_long,false);assert.equal(calls[2].body.request_id,'test-request-123');
  finish({});await pending;assert.equal(inbox.inboxDraft('g').text,'');
  console.log('PASS: combined decision payload, immediate disable, duplicate suppression, retained failure draft, typed inbox payload and cancellation isolation');
})().catch(error=>{console.error(error);process.exitCode=1;});

assert.equal(ctx.goalPlanCapacity([{status:"done"},{status:"superseded"},{status:"review"},{status:"blocked"}],{max_plan_items:40}),"활성 2 / 40 · 전체 4 (완료 포함)");

// Observed failure details stay visible as text, never executable HTML.
{
 const made=[];const make=(tag,cls,text)=>{const node={tag,cls,textContent:text||'',children:[],append(...children){this.children.push(...children);}};made.push(node);return node;};
 ctx.el=make;ctx.document={createTextNode:text=>({textContent:text})};
 const card=make('article');const failure={task_id:'0123456789abcdef',stage:'workbench.goals.GoalManager.context',error:"KeyError: 'evidence_refs' <script>bad()</script>"};
 const summary="KeyError: 'evidence_refs' · context · Task 0123456789abcdef";
 ctx.renderHumanBrief(card,{language:'English',cause:'platform',summary,answer_helps:{value:'uncertain',reason:'Platform fix needed'},what_happened:[],failure});
 assert(made.some(n=>n.tag==='pre'&&n.textContent===failure.error));assert(made.some(n=>n.textContent.includes(failure.task_id)&&n.textContent.includes(failure.stage)));
 assert.equal(ctx.goalDecisionTitle({human_brief:{summary}}),summary);assert(!source.includes('failed.innerHTML'));
}
