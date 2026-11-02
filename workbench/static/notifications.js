'use strict';
const attention={data:null,busy:false,audio:null,initialized:false};
const attentionLevels={urgent:'긴급',warning:'주의',info:'정보'};
function primeNotificationAudio(){
  try{if(!attention.audio)attention.audio=new (window.AudioContext||window.webkitAudioContext)();if(attention.audio.state==='suspended')attention.audio.resume().catch(()=>{});}catch{}
}
function browserPermission(){return !('Notification' in window)?'unavailable':Notification.permission;}
async function requestNotificationPermission(force=false){
  if(browserPermission()!=='default')return;
  try{if(!force&&localStorage.getItem('muse.notification.permissionAsked'))return;localStorage.setItem('muse.notification.permissionAsked','1');}catch{}
  try{await Notification.requestPermission();}catch{}
  refreshNotificationPermission();
}
function refreshNotificationPermission(){
  const label={granted:'허용됨',denied:'차단됨 · 브라우저 사이트 설정에서 변경',default:'허용 필요',unavailable:'이 브라우저에서 지원되지 않음'}[browserPermission()];
  $('notification-permission-state').textContent=label;$('notification-permission').disabled=browserPermission()!=='default';
}
function renderNotifications(data){
  attention.data=data;const count=data.unread_count;
  $('notification-count').textContent=count>99?'99+':String(count);$('notification-count').hidden=count===0;
  $('notification-open').setAttribute('aria-label',`알림 ${count}개 미확인`);document.title=(count?`(${count}) `:'')+'Muse Platform';
  const urgent=$('urgent-notifications');urgent.hidden=!data.urgent.length;
  const urgentSignature=JSON.stringify(data.urgent.map(n=>[n.id,n.resolved]));
  if(urgent.dataset.signature!==urgentSignature){urgent.dataset.signature=urgentSignature;urgent.replaceChildren();for(const row of data.urgent){const banner=el('div','urgent-notification');const text=el('div');text.append(el('strong','',row.event_label+' · '+row.title),el('span','',row.summary));const open=el('button','outline-button','확인하기');open.addEventListener('click',handle(()=>openNotification(row)));banner.append(text,open);urgent.append(banner);}}
  const list=$('notification-list');const signature=JSON.stringify(data.items.map(n=>[n.id,n.unread,n.resolved,n.delivery_seq]));
  if(list.dataset.signature!==signature){list.dataset.signature=signature;list.replaceChildren();for(const row of data.items){const card=el('article','notification-item '+row.severity+(row.unread?' unread':''));const button=el('button','notification-link');const top=el('span','notification-item-top');top.append(el('b','',attentionLevels[row.severity]+' · '+row.event_label),el('small','',row.resolved?'처리됨':row.severity==='urgent'?'처리 대기':''));button.append(top,el('strong','',row.title),el('span','',row.summary),el('time','',new Date(row.created*1000).toLocaleString()));button.addEventListener('click',handle(()=>openNotification(row)));card.append(button);if(!row.resolved&&!['command_approval','gpu_approval','question'].includes(row.event)){const done=el('button','text-button','확인 완료');done.addEventListener('click',handle(async()=>{await api('/api/notifications/resolve',{id:row.id});await pollNotifications();}));card.append(done);}list.append(card);}if(!data.items.length)list.append(el('p','empty-memory','새 알림이 없습니다.'));}
  refreshNotificationPermission();
}
async function openNotification(row){
  $('notification-panel').hidden=true;await api('/api/notifications/read',{id:row.id});
  if(row.goal_id){setMode('goal');await selectGoal(row.goal_id);const decision=row.decision_id?(typeof revealGoalDecision==='function'?revealGoalDecision(row.decision_id):$('decision-'+row.decision_id)):null;const target=decision||((row.event==='waiting_decision')?$('goal-decisions'):$('goal-approvals').children.length?$('goal-approvals'):$('goal-detail'));target.scrollIntoView({block:'start',behavior:'instant'});}
  else if(row.task_id)await selectTask(row.task_id);
  await pollNotifications();
}
async function deliverBrowserNotifications(data){
  if(browserPermission()==='granted')for(const row of data.browser){
    const claim=await api('/api/notifications/claim',{id:row.id,seq:row.delivery_seq,channel:'browser'});if(!claim.claimed)continue;
    try{const native=new Notification(row.title,{body:row.event_label+' · '+row.summary,tag:'muse-'+row.id,silent:true});native.onclick=()=>{window.focus();native.close();handle(()=>openNotification(row))();};}catch{}
  }
  if(attention.audio?.state==='running')for(const row of data.sounds){
    const claim=await api('/api/notifications/claim',{id:row.id,seq:row.delivery_seq,channel:'sound'});if(!claim.claimed)continue;
    try{const audio=attention.audio,osc=audio.createOscillator(),gain=audio.createGain();osc.frequency.value=880;gain.gain.setValueAtTime(.035,audio.currentTime);gain.gain.exponentialRampToValueAtTime(.001,audio.currentTime+.18);osc.connect(gain);gain.connect(audio.destination);osc.start();osc.stop(audio.currentTime+.18);}catch{}
  }
}
async function pollNotifications(){
  if(attention.busy||!state.token)return;attention.busy=true;
  try{const data=await api('/api/notifications');renderNotifications(data);await deliverBrowserNotifications(data);}finally{attention.busy=false;}
}
function notificationSettings(){
  const config=attention.data?.settings;if(!config)return;
  const controls=$('notification-settings-fields');controls.replaceChildren();
  const channels=el('div','notification-channel-settings');for(const [name,label] of [['app','앱 내부'],['browser','브라우저'],['windows','Windows']]){const field=el('label');const input=el('input');input.type='checkbox';input.id='notify-channel-'+name;input.checked=config.channels[name];field.append(input,document.createTextNode(label));channels.append(field);}controls.append(channels);
  const table=el('table','notification-setting-table');const head=el('tr');for(const text of ['종류','사용','앱','브라우저','Windows'])head.append(el('th','',text));table.append(head);
  for(const [level,label] of Object.entries(attentionLevels)){const row=el('tr');row.append(el('th','',label));for(const channel of ['enabled','app','browser','windows']){const cell=el('td');const input=el('input');input.type='checkbox';input.id=`notify-${level}-${channel}`;input.setAttribute('aria-label',label+' '+(channel==='enabled'?'사용':channel));input.checked=channel==='enabled'?config.enabled[level]:config.routes[level][channel];cell.append(input);row.append(cell);}table.append(row);}controls.append(table);
  const sound=el('label','notification-sound-field');const soundInput=el('input');soundInput.id='notify-sound';soundInput.type='checkbox';soundInput.checked=config.sound;sound.append(soundInput,document.createTextNode('긴급 알림만 짧은 소리'));controls.append(sound);
  const repeat=el('label','notification-repeat-field','미처리 긴급 알림 재알림 · 분 (0 = 해제)');const repeatInput=el('input');repeatInput.id='notify-repeat';repeatInput.type='number';repeatInput.min=0;repeatInput.max=1440;repeatInput.value=config.repeat_minutes;repeat.append(repeatInput);controls.append(repeat);
  $('notification-settings-dialog').showModal();
}
async function initNotifications(){
  if(attention.initialized)return;attention.initialized=true;await pollNotifications();
  const params=new URLSearchParams(location.search);const notification=params.get('notification');
  if(notification&&/^[a-f0-9]{24}$/.test(notification))await api('/api/notifications/read',{id:notification});
  const goal=params.get('goal'),task=params.get('task');
  if(goal&&/^[\w-]{1,128}$/.test(goal)){setMode('goal');await selectGoal(goal);const decisionId=params.get('decision')||attention.data?.items.find(row=>row.id===notification)?.decision_id;const target=decisionId&&typeof revealGoalDecision==='function'?revealGoalDecision(decisionId):null;if(target)target.scrollIntoView({block:'start',behavior:'instant'});else if($('goal-approvals').children.length)$('goal-approvals').scrollIntoView({block:'start'});}
  else if(task&&/^[\w-]{1,128}$/.test(task))await selectTask(task);
}
$('notification-open').addEventListener('click',handle(async()=>{primeNotificationAudio();requestNotificationPermission();$('notification-panel').hidden=!$('notification-panel').hidden;await pollNotifications();}));
$('notification-close').addEventListener('click',()=>{$('notification-panel').hidden=true;});
$('notification-read-all').addEventListener('click',handle(async()=>{await api('/api/notifications/read',{});await pollNotifications();}));
$('notification-settings-open').addEventListener('click',notificationSettings);
$('notification-permission').addEventListener('click',()=>{primeNotificationAudio();requestNotificationPermission(true);});
$('notification-settings-save').addEventListener('click',handle(async()=>{
  const data={enabled:{},channels:{},routes:{},sound:$('notify-sound').checked,repeat_minutes:Number($('notify-repeat').value)};
  for(const name of ['app','browser','windows'])data.channels[name]=$('notify-channel-'+name).checked;
  for(const level of Object.keys(attentionLevels)){data.enabled[level]=$(`notify-${level}-enabled`).checked;data.routes[level]={};for(const channel of ['app','browser','windows'])data.routes[level][channel]=$(`notify-${level}-${channel}`).checked;}
  await api('/api/notifications/settings',data);$('notification-settings-dialog').close();await pollNotifications();toast('알림 설정을 저장했습니다.');
}));
