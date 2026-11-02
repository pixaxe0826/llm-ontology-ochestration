// Behavioral composer tests with a DOM/network double; no browser/cloud dependencies.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
function node(){return {children:[],hidden:false,disabled:false,value:'baseten',files:[],listeners:{},classList:{add(){},remove(){}},replaceChildren(){this.children=[]},append(...v){this.children.push(...v)},setAttribute(){},addEventListener(k,v){this.listeners[k]=v},contains(){return false},click(){this.clicked=true}};}
const elements=new Map();const $=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id)};
const posts=[];let confirms=[],allowConfirm=false,uploads=0;
const sandbox={console,$,state:{current:null,task:null,token:'synthetic-session',providers:{baseten:{vision_supported:true}}},platform:{mode:'baseten'},running:new Set(['running']),bytes:n=>n+' B',el:()=>node(),document:{addEventListener(){}},handle:f=>(...a)=>f(...a),refreshControls(){},confirm:text=>{confirms.push(text);return allowConfirm},loadMemory:async()=>{},api:async(p,d)=>{posts.push([p,d]);return p.endsWith('conversation')?{conversation_id:'c'.repeat(16)}:{}},fetch:async()=>{uploads++;return {ok:true,json:async()=>({name:'red.png',size:100,sha256:'f'.repeat(64),image_count:1,kind:'image'})}}};
vm.createContext(sandbox);vm.runInContext(fs.readFileSync(path.join(__dirname,'../workbench/static/attachments.js'),'utf8'),sandbox);
const run=code=>vm.runInContext(code,sandbox);
(async()=>{
  let prevented=false;await $('composer-area').listeners.drop({dataTransfer:{files:[{name:'red.png',size:100}],types:['Files']},preventDefault(){prevented=true}});
  // The listener delegates through handle; finish that promise's microtasks.
  await new Promise(resolve=>setImmediate(resolve));
  assert(prevented);assert.equal(uploads,1);assert.equal(run('chatAttachments.items.length'),1);
  assert.equal($('attachment-queue').children.length,1);assert.equal($('attachment-status').hidden,false);
  $('attachment-queue').children[0].children[2].listeners.click();assert.equal(run('chatAttachments.items.length'),0);
  sandbox.state.providers.baseten.vision_supported=false;
  await assert.rejects(run('uploadAttachments([{name:"x.png",size:100}])'),/이미지 미지원/);assert.equal(uploads,1);
  sandbox.state.providers.baseten.vision_supported=true;
  await assert.rejects(run('uploadAttachments([{name:"large.txt",size:200001}])'),/크기/);
  run('chatAttachments.items=[{name:"x",image_count:1}];clearAttachments()');assert.equal(run('chatAttachments.items.length'),0);
  await run('toggleMemoryShare({id:"response-id",shared:false,attachment_related:true})');assert.match(confirms[0],/첨부 내용이 포함될 수 있음/);
  assert.equal(posts.filter(([p])=>p==='/api/memory/share').length,0);
  allowConfirm=true;await run('toggleMemoryShare({id:"response-id",shared:false,attachment_related:true})');
  const share=posts.find(([p])=>p==='/api/memory/share')[1];assert.equal(share.shared,true);assert.equal(share.attachment_confirmed,true);
  confirms=[];await run('toggleMemoryShare({id:"ordinary",shared:false})');assert.equal(confirms.length,0);
  const previousAPI=sandbox.api;let releaseConversation;
  sandbox.api=async()=>new Promise(resolve=>releaseConversation=resolve);
  const pending=run('uploadAttachments([{name:"stale.png",size:100}])');run('clearAttachments()');
  releaseConversation({conversation_id:'b'.repeat(16)});await pending;sandbox.api=previousAPI;
  assert.equal(run('chatAttachments.conversation'),null);assert.equal(run('chatAttachments.items.length'),0);assert.equal(uploads,1);
  console.log('PASS: drop/upload/remove, size/capability guards, reset/upload race, per-response confirmation/cancel, ordinary sharing.');
})().catch(error=>{console.error(error);process.exitCode=1});
