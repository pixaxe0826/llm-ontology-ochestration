const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../workbench/static/model-routing.js'),'utf8');
class Option{constructor(text,value){this.text=text;this.value=value;this.disabled=false;}}
class Select{constructor(){this.options=[];this.current='';}replaceChildren(){this.options=[];this.current='';}append(option){this.options.push(option);}set value(value){this.current=this.options.some(o=>o.value===value)?value:'';}get value(){return this.current;}}
const noop=()=>{},context={Option,goalUI:{config:null},state:{},validateGoalBuilderSelection:noop,loadGoalBuilderModels:noop,openGoalCreate:noop,goalFormPayload:noop,renderGoalDetail:noop,updateReasoningUI:noop};
vm.createContext(context);vm.runInContext(source,context);const api=vm.runInContext('({modelFitsRole,reasoningOptions})',context);
const model={capabilities:{tool_calling:false,structured_json:true,image_input:false,web_search:false,reasoning_efforts:['low','high']}};
assert.equal(api.modelFitsRole(model,'Builder'),true);assert.equal(api.modelFitsRole(model,'Planner'),true);assert.equal(api.modelFitsRole(model,'ImageAnalysis'),false);assert.equal(api.modelFitsRole(model,'ReferenceSearch'),false);
const select=new Select();api.reasoningOptions(select,model,'high');assert.equal(select.value,'high');assert.equal(select.disabled,false);
api.reasoningOptions(select,model,'xhigh',true);assert.equal(select.value,'xhigh');assert.equal(select.options.at(-1).disabled,true);
api.reasoningOptions(select,{capabilities:{reasoning_efforts:[]}},'high');assert.equal(select.disabled,true);assert.equal(select.value,'');
assert(source.includes('routeFormReady'));assert(source.includes('pending_builder_route'));assert(source.includes('/api/provider-registry'));assert(!source.includes("type='password'"));
console.log('PASS: capability filters, reasoning preservation, disabled controls and metadata-only provider form.');
