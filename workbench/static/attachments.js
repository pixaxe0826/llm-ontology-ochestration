'use strict';
const chatAttachments={conversation:null,items:[],busy:false,revision:0};
function clearAttachments(){chatAttachments.revision++;chatAttachments.conversation=null;chatAttachments.items=[];renderAttachments();}
function attachmentBlocked(){return $('provider').value==='baseten'&&state.providers.baseten?.vision_supported===false&&(chatAttachments.items.some(a=>a.image_count>0)||state.task?.attachment_usage?.image_count>0);}
function renderAttachments(){
  const target=$('attachment-queue');if(!target)return;target.replaceChildren();
  for(const [index,item] of chatAttachments.items.entries()){
    const chip=el('div','attachment-chip');const text=el('span','',item.name);text.title=item.name;
    const info=item.kind==='pdf'?` · ${item.image_count}쪽 이미지${item.text_truncated?' · 텍스트 일부':''}`:item.gif_first_frame?' · 첫 프레임':'';
    const remove=el('button','attachment-remove','×');remove.type='button';remove.setAttribute('aria-label',item.name+' 첨부 제거');remove.disabled=chatAttachments.busy;
    remove.addEventListener('click',()=>{chatAttachments.items.splice(index,1);renderAttachments();refreshControls();});
    chip.append(text,el('small','',bytes(item.size)+info),remove);target.append(chip);
  }
  target.hidden=!chatAttachments.items.length;
  const hint=$('attachment-status');hint.hidden=!chatAttachments.items.length&&!chatAttachments.busy;
  hint.textContent=attachmentBlocked()?'이 공급자는 이미지 미지원':chatAttachments.busy?'첨부 처리 중…':'첨부 사용 이후 응답은 기본 공유 제외 · 기억 화면에서 응답별 변경';
}
async function uploadAttachments(files){
  if(chatAttachments.busy||!['meta','baseten'].includes(platform.mode))return;
  if(state.task?.archived||state.task&&running.has(state.task.status))throw Error('현재 응답이 끝난 뒤 첨부하세요.');
  if(chatAttachments.items.length+files.length>10)throw Error('메시지당 첨부는 10개까지 가능합니다.');
  chatAttachments.busy=true;renderAttachments();refreshControls();
  const revision=chatAttachments.revision;
  try{
    if(!chatAttachments.conversation){const value=await api('/api/attachments/conversation',{task_id:state.current});if(revision!==chatAttachments.revision)return;chatAttachments.conversation=value.conversation_id;}
    const ident=chatAttachments.conversation;
    for(const file of files){
      if(revision!==chatAttachments.revision)return;
      const suffix=file.name.split('.').pop().toLowerCase();const image=['png','jpg','jpeg','webp','gif'].includes(suffix);
      if(image&&$('provider').value==='baseten'&&state.providers.baseten?.vision_supported===false||suffix==='pdf'&&$('provider').value==='baseten'&&state.providers.baseten?.vision_supported===false)throw Error('이 공급자는 이미지 미지원');
      if(file.size>(suffix==='pdf'?15*1024*1024:image?5*1024*1024:200000))throw Error(file.name+' · 파일 크기 한도를 초과했습니다.');
      const response=await fetch('/api/attachments/upload',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Local-Token':state.token,'X-Conversation-Id':ident,'X-File-Name':encodeURIComponent(file.name)},body:file});
      const value=await response.json();if(!response.ok)throw Error(value.error||'첨부를 처리하지 못했습니다.');
      if(revision!==chatAttachments.revision)return;
      if(chatAttachments.items.reduce((sum,a)=>sum+a.image_count,0)+value.image_count>4)throw Error('이미지와 PDF 변환 페이지는 메시지당 합계 4장까지 가능합니다.');
      chatAttachments.items.push(value);renderAttachments();
    }
  }finally{chatAttachments.busy=false;$('attachment-input').value='';renderAttachments();refreshControls();}
}
async function toggleMemoryShare(source){
  const shared=!source.shared;
  if(shared&&source.attachment_related&&!confirm('첨부 내용이 포함될 수 있음\n이 응답을 공유 메모리 검색에 포함할까요?'))return;
  await api('/api/memory/share',{id:source.id,shared,attachment_confirmed:shared&&!!source.attachment_related});await loadMemory();
}
function attachmentMetadata(container,items){
  if(!items?.length)return;
  const list=el('div','sent-attachments');
  for(const item of items){const chip=el('div','attachment-chip');const name=el('span','',item.name);name.title=item.name;
    chip.append(name,el('small','',bytes(item.size)));chip.title='SHA-256: '+item.sha256;list.append(chip);}
  container.append(list);
}
$('attach-text').addEventListener('click',()=>$('attachment-input').click());
$('attachment-input').addEventListener('change',handle(()=>uploadAttachments(Array.from($('attachment-input').files))));
const dropComposer=$('composer-area');
dropComposer.addEventListener('dragover',event=>{if(event.dataTransfer.types.includes('Files')){event.preventDefault();dropComposer.classList.add('attachment-drag');}});
dropComposer.addEventListener('dragleave',event=>{if(!dropComposer.contains(event.relatedTarget))dropComposer.classList.remove('attachment-drag');});
dropComposer.addEventListener('drop',event=>{if(!event.dataTransfer.files.length)return;event.preventDefault();dropComposer.classList.remove('attachment-drag');handle(()=>uploadAttachments(Array.from(event.dataTransfer.files)))();});
document.addEventListener('dragover',event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault();});
document.addEventListener('drop',event=>{if(event.dataTransfer.types.includes('Files'))event.preventDefault();});
