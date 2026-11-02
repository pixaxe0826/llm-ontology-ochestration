'use strict';
const MarkdownExport=(()=>{
  const services={meta:'Meta Work',baseten:'Baseten Chat',modal:'Modal Compute'};
  function fence(text,language=''){
    const value=String(text??'');
    const size=Math.max(3,...[...value.matchAll(/`+/g)].map(m=>m[0].length+1));
    const ticks='`'.repeat(size);
    return `${ticks}${language}\n${value}\n${ticks}`;
  }
  function sources(items){
    if(!items?.length)return '';
    return '\n\n'+items.map((s,i)=>`- 기억${i+1}: ${s.title||''} (출처 ${s.chat_id||''}, 메시지 ${s.message_id||''})`).join('\n');
  }
  function response(event){return String(event.text||'')+sources(event.sources)+'\n';}
  function task(record){
    const result=[`# ${record.title||'대화'}`,`서비스: ${services[record.provider]||record.provider}\n모델: ${record.model||''}\n기록 ID: ${record.id}\n상태: ${record.status}${record.archived?' · 아카이브':''}`];
    for(const e of record.events||[]){
      if(e.kind==='user'||e.kind==='assistant')result.push(`## ${e.kind==='user'?'사용자':'응답'}\n\n${response(e)}`);
      else if(e.kind==='tool')result.push(`### 도구 · ${e.name}\n\n${fence(JSON.stringify(e.arguments,null,2),'json')}`);
      else if(e.kind==='tool_result')result.push(`### 실행 결과 · ${e.name}\n\n${fence(JSON.stringify(e.result,null,2),'json')}`);
      else if(e.kind==='change')result.push(`### 파일 변경 · ${e.change.path}\n\n${fence(e.change.diff,'diff')}`);
      else if(e.kind==='error')result.push(`### 오류\n\n${e.text||''}`);
    }
    return result.join('\n\n')+'\n';
  }
  function job(record){
    return [`# ${record.spec.entry||record.spec.program}`,'서비스: Modal Compute',`기록 ID: ${record.id}\nGPU: ${record.spec.gpu}\n프로그램: ${record.spec.program}\n상태: ${record.status}`,
      '## 실행 로그\n\n'+fence(record.logs||'','text'),
      ...(record.error?['## 오류\n\n'+record.error]:[]),
      ...(record.result?['## 실행 결과\n\n'+fence(JSON.stringify(record.result,null,2),'json')]:[]),
      '## 결과 파일\n\n'+(record.artifacts||[]).map(p=>'- '+p).join('\n')].join('\n\n')+'\n';
  }
  function filename(title){return (String(title||'Muse').replace(/[<>:"/\\|?*\x00-\x1f]/g,'_').replace(/[. ]+$/g,'').slice(0,90)||'Muse')+'.md';}
  function suggestGoalTitle(objective,allowFallback=true){
    let fallback='',fence=null;
    for(const line of String(objective||'').split(/\r?\n/)){
      const marker=line.match(/^ {0,3}(`{3,}|~{3,})/);
      if(marker){const token=marker[1];if(!fence)fence=[token[0],token.length];else if(token[0]===fence[0]&&token.length>=fence[1])fence=null;continue;}
      if(fence)continue;
      if(!fallback&&line.trim())fallback=line.trim();
      const heading=line.match(/^ {0,3}#{1,6}[ \t]+(.*)$/);
      if(heading){const title=heading[1].replace(/[ \t]+#+[ \t]*$/,'').trim();if(title)return Array.from(title).slice(0,120).join('');}
    }
    return allowFallback?Array.from(fallback||'Untitled Goal').slice(0,120).join(''):'';
  }
  function goalTitle(record){return record.title||suggestGoalTitle(record.objective);}
  function goal(detail){
    const record=detail.goal;
    let text='# '+goalTitle(record)+'\n\n'+String(record.objective??'');
    const summary=record.final_report||record.summary;
    if(summary)text+='\n\n## Progress / Final report\n\n'+summary;
    for(const message of detail.messages||[])text+=`\n\n## ${message.from} → ${message.to} · ${message.type}\n\n${message.body}`;
    return text;
  }
  return {task,job,response,filename,goal,goalTitle,suggestGoalTitle};
})();
