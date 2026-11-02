'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const app=fs.readFileSync(path.join(__dirname,'../workbench/static/app.js'),'utf8');
const code=app.slice(app.indexOf('const apiBodies='),app.indexOf('\nfunction handle('));
const calls=[],replies=[];
const ctx=vm.createContext({state:{token:'local-session'},fetch:async(url,options)=>{calls.push({url,options});const value=replies.shift();return typeof value==='function'?value():value;},Map,Error,JSON});
vm.runInContext(code,ctx);
const api=(url,data)=>ctx.api(url,data);
const response=(data,status=200,etag='"v1"')=>({status,ok:status<400,headers:{get:name=>name==='ETag'?etag:null},json:async()=>data});
(async()=>{
  replies.push(response({items:[1]}));let first=await api('/history');
  replies.push(response(null,304));assert.equal(await api('/history'),first);assert.equal(calls.at(-1).options.headers['If-None-Match'],'"v1"');
  let release;replies.push(()=>new Promise(resolve=>release=resolve));const a=api('/one'),b=api('/one');await Promise.resolve();release(response({n:2}));assert.equal(await a,await b);assert.equal(calls.filter(c=>c.url==='/one').length,1);
  replies.push(response({saved:true}));await api('/save',{enabled:true});replies.push(response({items:[2]}));await api('/history');assert.equal(calls.at(-1).options.headers['If-None-Match'],undefined);assert.equal(calls.find(c=>c.url==='/save').options.headers['X-Local-Token'],'local-session');
  let finish;replies.push(()=>new Promise(resolve=>finish=resolve));const stale=api('/slow');await Promise.resolve();replies.push(response({saved:true}));await api('/save',{enabled:false});finish(response({old:true}));await stale;replies.push(response({new:true}));await api('/slow');assert.equal(calls.at(-1).options.headers['If-None-Match'],undefined);
  replies.push(response({error:'Rejected'},403));await assert.rejects(api('/blocked'),/Rejected/);
  assert.match(app,/document\.visibilityState==='hidden'/);assert.match(app,/\/api\/ui\/history/);
  console.log('PASS: UI GET deduplication, 304 bodies, mutation/stale invalidation, errors and session header.');
})().catch(error=>{console.error(error);process.exitCode=1;});
