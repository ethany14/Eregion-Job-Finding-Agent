import {createBrowserControl} from './browser-control.js';
import {careerRoute,renderCareerResults,placeCareerResults} from './career-chat.js';
import {renderMarkdown} from './chat-markdown.js';
import {usageText,renderUsage} from './model-usage.js';
import {api,organise,createGroup,renameGroup,deleteGroup,sidebarSections,collapsedGroups,toggleGroup,
  openMenu,closeMenu,toast,confirmDialog,nameDialog} from './conversations.js';
import {renderPage} from './pages.js';
import {startAlertNotifications} from './job-alerts.js';
import {initTheme} from './theme.js';
const careerMode=new URLSearchParams(location.search).get('mode')!=='application';
import {AnswerDrafts,renderAnswerBatch,renderPendingQuestion} from './apply-answers.js';
const answerDrafts=new AnswerDrafts();
const $ = s => document.querySelector(s);
const el = (tag,text='',cls='') => {const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const terminal=new Set(['submitted','submission_unknown','cancelled','failed']);
const labels={first_name:'First name',last_name:'Last name',email:'Email',phone:'Phone',linkedin:'LinkedIn',github:'GitHub',website:'Website',school:'School',degree:'Degree',country:'Country',city:'City'};
const statuses={waiting:'Working with your application',completed:'Ready for your next request',queued:'Getting started',running:'Working on your request',needs_input:'Waiting for your input',awaiting_review:'Ready for your review',submitting:'Submitting once — waiting for confirmation',submitted:'Application received',submission_unknown:'Check the employer confirmation',cancelled:'Application cancelled',failed:'Application stopped'};
const toolNames={browser_open:'Open page',browser_read:'Read page',browser_click:'Click control',browser_type:'Type text',browser_select:'Select option',browser_check:'Set checkbox',browser_key:'Press key',browser_back:'Go back',browser_tabs:'Manage tabs',browser_frame:'Inspect frame',browser_scroll:'Scroll page',browser_wait:'Wait for loading',browser_screenshot:'Look at page',browser_click_at:'Click visible target',ask_user:'Ask you a question',wait_for_user:'Wait for your reply',draft_answer:'Draft an answer from your resume',view_browser:'Look at browser',computer_click:'Click visible target',computer_move:'Move pointer',computer_scroll:'Scroll browser',computer_drag:'Drag visible control',computer_type:'Type confirmed answer',computer_key:'Press key',computer_wait:'Wait for page',resolve_field:'Adapt answer to this form',explore_option:'Explore option category',click_page_action:'Navigate application',autofill:'Fill known details',inspect_field:'Read options',search_options:'Search options',choose_option:'Choose option',type_from_source:'Type answer',use_chat_answer:'Apply your answer',click_field:'Open control',view_field:'Inspect control',ready_for_review:'Check completion',request_input:'Ask for missing information',say:'Update',observe:'Inspect page',update_plan:'Update plan'};
const busyStatuses=new Set(['running','queued','waiting','submitting']);
const starterPrompts=[['Find roles','Find roles that match my resume.'],['Check my fit','Check my fit for this job: '],
  ['Tailor my resume','Tailor my resume and cover letter for a job I want to apply to.'],['Practise an interview','Start interview practice for my target role.']];
const icon=name=>{const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'),use=document.createElementNS('http://www.w3.org/2000/svg','use');svg.setAttribute('class','i');svg.setAttribute('aria-hidden','true');use.setAttribute('href',`#i-${name}`);svg.append(use);return svg;};
// "Needs you" means the agent cannot continue without the person.
const needsYou=item=>Boolean(item)&&!terminal.has(item.status)&&!item.pause_requested&&item.status!=='submitting'&&
  (['needs_input','awaiting_review'].includes(item.status)||Boolean(item.account_request)||Boolean(item.input_request));
const newDraft=()=>({id:crypto.randomUUID(),url:'',messages:[],field:null,lastText:'',payload:null});
let draft=newDraft(),profile={},resume=null,selected=null,current=null,attempts=[],groups=[],epoch=0,serial=0,poll=null,view='chat';
let chatBusy=false,actionBusy=false,extracting=false,starting=false,extractionVersion=0,previewUrl=null,logSignature='',actionSignature='',failedMessage=null;
const optimistic=new Map();
// Review approvals survive polling: run_id -> {remember:{key:bool}, scopes:{key:'global'|'employer'}}.
const memoryChoices=new Map();
let careerSignature='',careerCards=[],renderedScope=null,visualPreferenceRun=null,restoring=true,candidateWrites=Promise.resolve();
let defaultVisualFallback=false;
try{defaultVisualFallback=localStorage.getItem('northstar.visualFallback')==='true';}catch{/* Storage is optional. */}
function saveVisualPreference(enabled){
  defaultVisualFallback=enabled;
  try{localStorage.setItem('northstar.visualFallback',String(enabled));}catch{/* Keep the choice for this page. */}
}
function persistCandidate(extra={}){
  const payload={profile:structuredClone(profile),...extra};
  const pending=candidateWrites.catch(()=>{}).then(()=>request('/candidate',payload));
  candidateWrites=pending;return pending;
}
async function resourceChanged(text,taskId=current?.career?current.run_id:null,options={}){
  if(!taskId)return;await request(`/${taskId}/chat`,{text,message_id:crypto.randomUUID(),...options});await refresh();
}
async function removeResume(){
  const taskId=current?.career?current.run_id:null;
  extractionVersion++;resume=null;profile={};draft.id=crypto.randomUUID();draft.payload=null;$('#resume-file').value='';render();
  candidateWrites=candidateWrites.catch(()=>{}).then(()=>request('/candidate',undefined,'DELETE'));
  try{await candidateWrites;await resourceChanged('I removed my saved resume and details.',taskId);}catch(e){error(e.message);}
}

async function request(path='',body,method=body===undefined?'GET':'POST') {
  const routed=careerRoute(path,body,{enabled:careerMode,current,attempts});body=routed.body;
  const response=await fetch(routed.url,{method,headers:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(path==='/resume-profile'?120000:30000)});
  const data=await response.json();if(!response.ok){const failure=new Error(typeof data.detail==='string'?data.detail:data.detail?.message||'Please check the job link and profile details.');failure.existingRunId=data.detail?.existing_run_id;throw failure;}return data;
}
function error(text=''){$('#apply-error').textContent=text;$('#apply-error').hidden=!text;}
function company(item){if(item.career)return item.title||'Career task';try{const u=new URL(item.url);return /greenhouse\.io$|lever\.co$/.test(u.hostname)?u.pathname.split('/').filter(Boolean)[0]:u.hostname.endsWith('.myworkdayjobs.com')?u.hostname.split('.')[0]:u.hostname;}catch{return 'Application';}}
function browserHost(item){try{return item.browser?.url&&item.preview_url?.includes('/browser/preview')?new URL(item.browser.url).hostname:'';}catch{return '';}}
// A conversation about one job is named after that job; anything broader keeps its task title.
function displayTitle(value,limit=90){
  const text=(value||'Career task').trim();
  if(text.length<=limit)return text;
  const prefix=text.slice(0,limit-1),boundary=prefix.lastIndexOf(' ');
  return (boundary>0?prefix.slice(0,boundary):'').trimEnd()+'…';
}
function heading(item){
  if(item.custom_title)return {title:displayTitle(item.title),subtitle:''};
  const jobs=item.career?item.jobs||[]:[];
  if(jobs.length===1&&jobs[0].company)return {title:displayTitle(jobs[0].company),subtitle:jobs[0].title||''};
  // Older automatic titles were sliced mid-word in persistence. Recover the
  // source goal only for automatic prefix titles; never overwrite a custom name.
  if(item.career)return {title:displayTitle(item.goal?.startsWith(item.title)?item.goal:company(item)),subtitle:''};
  return {title:displayTitle(`Applying at ${browserHost(item)||company(item)}`),subtitle:''};
}
function statusText(item){
  if(item.status==='cancelled'&&item.career&&!item.application_run_id)return 'Cancelled';
  if(item.reopening)return 'Reopening your saved application';
  if(item.pause_requested&&!terminal.has(item.status))return 'Paused — send “continue” when ready';
  if(item.task_error)return 'Needs attention';
  if(item.model_activity?.phase==='retrying')return 'Retrying model connection';
  return statuses[item.status]||item.status;
}
function button(text,action,cls='secondary-button'){const b=el('button',text,cls);b.type='button';b.addEventListener('click',action);return b;}
function bottom(){const n=$('#chat-scroll');n.scrollTo({top:n.scrollHeight,behavior:'instant'});}
let panelByUser=false;
function workspace(open=true,{remember=false}={}){
  if(remember||!open)panelByUser=open&&remember;
  $('#browser-panel').hidden=!open;$('#browser-toggle').setAttribute('aria-expanded',String(open));
  if(remember){try{localStorage.setItem('northstar.activity',open?'open':'closed');}catch{/* Storage is optional. */}}
  if(open&&current)refreshPreview(current);
}
// The panel opens once per application run on wide screens, unless the person closed it before.
const autoOpened=new Set();
// Below 1150px the panel covers the chat, so one the app opened by itself steps aside.
matchMedia('(max-width: 1150px)').addEventListener('change',event=>{if(event.matches&&!panelByUser&&!$('#browser-panel').hidden)workspace(false);});
function autoOpenWorkspace(item){
  if(!item||autoOpened.has(item.run_id)||terminal.has(item.status)||window.innerWidth<1280)return;
  if(!(item.application_run_id||!item.career))return;
  autoOpened.add(item.run_id);
  let preference=null;try{preference=localStorage.getItem('northstar.activity');}catch{/* Storage is optional. */}
  if(preference!=='closed'&&$('#browser-panel').hidden)workspace(true);
}
function select(id){showView('chat');selected=id;current=attempts.find(a=>a.run_id===id)||null;epoch++;logSignature='';actionSignature='';$('#chat-text').value='';$('#history-panel').classList.remove('open');$('#browser-preview').hidden=true;$('#preview-placeholder').hidden=false;error();render();refresh();}
async function openExisting(id){const item=await request(`/${id}`);attempts=[item,...attempts.filter(a=>a.run_id!==id)];select(id);}
function reset(url=''){showView('chat');selected=null;current=null;epoch++;draft=newDraft();draft.url=url;logSignature='';actionSignature='';failedMessage=null;$('#chat-text').value='';$('#history-panel').classList.remove('open');$('#browser-preview').hidden=true;$('#preview-placeholder').hidden=false;error();restoring=true;render();if(url)addDraft('user',url);restoreCandidate();}
let historyRefreshedAt=0;
async function refresh({background=false}={}){
  clearTimeout(poll);const ticket=++serial;
  try{
    // Refresh the active conversation promptly without rebuilding every saved
    // document/interview on each tick. Explicit actions always refresh the list.
    const full=!background||!careerMode||!selected||Date.now()-historyRefreshedAt>=10000;
    if(full){
      const result=await request();if(ticket!==serial)return;
      attempts=result.attempts;groups=result.groups||[];historyRefreshedAt=Date.now();
    }else{
      const item=await request(`/${selected}`);if(ticket!==serial)return;
      attempts=attempts.map(a=>a.run_id===item.run_id?item:a);
    }
    current=selected?attempts.find(a=>a.run_id===selected)||null:null;render();
    // A slow screenshot must not postpone chat updates.
    if(current&&!$('#browser-panel').hidden)void refreshPreview(current);
  }catch(e){if(ticket===serial)error(e.message);}
  finally{if(ticket===serial){
    const active=current&&(busyStatuses.has(current.status)||busyStatuses.has(current.task_status));
    poll=setTimeout(()=>refresh({background:true}),active?750:2000);
  }}
}
const PAGE_VIEWS=new Set(['applications','materials','archived','trash','alerts']);
function showView(name,{push=true}={}){
  if(!careerMode||!PAGE_VIEWS.has(name))name='chat';
  const changed=name!==view;view=name;closeMenu();
  $('#conversation').hidden=name!=='chat';$('#page-view').hidden=name==='chat';
  if(name!=='chat'&&!$('#browser-panel').hidden)workspace(false);
  for(const link of document.querySelectorAll('[data-view]'))link.setAttribute('aria-current',String(link.dataset.view===name||link.dataset.also===name));
  markChatNav();
  $('#history-panel').classList.remove('open');
  if(push&&changed)history.pushState({view:name},'',name==='chat'?location.pathname:`${location.pathname}?view=${name}`);
  if(name!=='chat'&&(changed||!push))renderPage(name,$('#page-view'),pageContext);
  renderSidebar();
}
const pageContext={el,button,icon,error:text=>error(text),groups:()=>groups,refresh:()=>refresh(),
  counts:()=>refreshCounts(),open:id=>select(id),show:name=>showView(name),
  needsYou:()=>new Set(attempts.filter(needsYou).flatMap(a=>(a.jobs||[]).map(j=>j.application_id))),
  // Open the conversation that owns this application, or start one about it.
  openApplication:(applicationId,text='')=>{
    const owner=attempts.find(a=>a.career&&(a.jobs||[]).some(j=>j.application_id===applicationId));
    if(!owner){location.href=`/apply?application=${encodeURIComponent(applicationId)}`;return;}
    select(owner.run_id);if(text){$('#chat-text').value=text;growComposer();$('#chat-text').focus();}
  },send:text=>{showView('chat');reset();$('#chat-text').value=text;$('#chat-text').focus();},
  menuToggle:()=>{const b=button('',()=>$('#history-panel').classList.toggle('open'),'icon-button mobile-only');b.setAttribute('aria-label','Show conversation history');b.append(icon('menu'));return b;}};
window.addEventListener('popstate',event=>showView(event.state?.view||new URLSearchParams(location.search).get('view')||'chat',{push:false}));

let counts={archived:0,trash:0};
async function refreshCounts(){
  if(!careerMode)return;
  try{
    const [archived,trash]=await Promise.all(['archived','trash'].map(v=>api(`/api/career/tasks?view=${v}`)));
    counts={archived:archived.attempts.length,trash:trash.attempts.length};
    $('#count-archived').textContent=counts.archived+counts.trash||'';
  }catch{/* Counts are decoration; the pages load their own data. */}
}

// ---- Conversation organisation ----
async function changeConversation(item,changes,message,undo){
  try{
    await organise(item.run_id,changes);
    if((changes.archived||changes.trashed)&&selected===item.run_id)reset();
    await refresh();refreshCounts();
    if(message)toast(message,undo?{undo:async()=>{try{await organise(item.run_id,undo);await refresh();refreshCounts();}catch(e){error(e.message);}}}:{});
  }catch(e){error(e.message);}
}
async function trashConversation(item){
  const busy=busyStatuses.has(item.status)||busyStatuses.has(item.task_status);
  try{if(busy)await api(`/api/career/tasks/${encodeURIComponent(item.run_id)}/cancel`,{method:'POST'});}
  catch(e){error(e.message);return;}
  await changeConversation(item,{trashed:true},'Moved to Trash',{trashed:false});
}
async function newGroupFor(item){
  const name=await nameDialog({title:'New group',label:'Group name',confirmLabel:'Create'});
  if(!name)return;
  try{const group=await createGroup(name);groups=[...groups,group];if(item)await changeConversation(item,{group_id:group.group_id},`Moved to ${name}`);else await refresh();}
  catch(e){error(e.message);}
}
function conversationMenu(item,anchor,position){
  const busy=busyStatuses.has(item.status)||busyStatuses.has(item.task_status);
  openMenu(anchor,[
    {label:'Rename',hint:'F2',action:()=>startRename(item)},
    {label:item.pinned_at?'Unpin':'Pin to top',action:()=>changeConversation(item,{pinned:!item.pinned_at})},
    {label:'Move to group',submenu:[
      ...groups.map(g=>({label:g.name,checked:item.group_id===g.group_id,action:()=>changeConversation(item,{group_id:g.group_id},`Moved to ${g.name}`,{group_id:item.group_id||null})})),
      {label:'No group',checked:!item.group_id,action:()=>changeConversation(item,{group_id:null})},
      {separator:true},{label:'New group…',action:()=>newGroupFor(item)}]},
    {label:'Archive',action:()=>changeConversation(item,{archived:true},'Archived',{archived:false})},
    {separator:true},
    {label:busy?'Stop and move to Trash':'Move to Trash',hint:'Del',danger:true,action:()=>trashConversation(item)},
  ],position);
}
function groupMenu(group,anchor){
  openMenu(anchor,[
    {label:'Rename group',action:async()=>{const name=await nameDialog({title:'Rename group',label:'Group name',value:group.name});
      if(!name)return;try{await renameGroup(group.group_id,name);await refresh();}catch(e){error(e.message);}}},
    {label:'New group…',action:()=>newGroupFor(null)},
    {separator:true},
    {label:'Delete group',danger:true,action:async()=>{
      if(!await confirmDialog({title:`Delete “${group.name}”?`,text:'The group is removed. Its conversations move back to Recent; nothing else is deleted.',confirmLabel:'Delete group',danger:true}))return;
      try{await deleteGroup(group.group_id);await refresh();toast('Group deleted');}catch(e){error(e.message);}}},
  ]);
}
let renaming=null;
function startRename(item){
  renaming=item.run_id;renderSidebar(true);
  const input=document.querySelector('#attempts input.rename');if(!input)return;
  input.focus();input.select();
  let done=false;
  const finish=async save=>{
    if(done)return;done=true;renaming=null;
    const title=input.value.trim();
    if(save&&title!==heading(item).title){try{await organise(item.run_id,{title});await refresh();}catch(e){error(e.message);}}
    renderSidebar(true);document.querySelector(`#attempts [data-id="${CSS.escape(item.run_id)}"] .attempt`)?.focus();
  };
  input.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();finish(true);}else if(e.key==='Escape'){e.preventDefault();e.stopPropagation();finish(false);}});
  input.addEventListener('blur',()=>finish(true));
}
function dropTarget(node,groupId){
  if(!careerMode)return;
  node.addEventListener('dragover',e=>{if(e.dataTransfer.types.includes('text/x-northstar-task')){e.preventDefault();node.classList.add('drop');}});
  node.addEventListener('dragleave',()=>node.classList.remove('drop'));
  node.addEventListener('drop',e=>{
    e.preventDefault();node.classList.remove('drop');
    const item=attempts.find(a=>a.run_id===e.dataTransfer.getData('text/x-northstar-task'));
    if(item&&(item.group_id||null)!==groupId)changeConversation(item,{group_id:groupId},groupId?`Moved to ${groups.find(g=>g.group_id===groupId)?.name}`:'Removed from group');
  });
}
function attemptRow(item){
  const name=heading(item).title.replace(/^Applying at /,'');
  const b=button('',()=>select(item.run_id),'attempt');b.setAttribute('aria-current',String(view==='chat'&&selected===item.run_id));b.title=statusText(item);
  if(needsYou(item))b.append(el('span','','dot'));else if(busyStatuses.has(item.status))b.append(el('span','','busy'));
  b.append(el('span',name,'name'),el('span',`, ${statusText(item)}`,'sr-only'));
  // Browser attempts started outside a conversation have no conversation record to organise.
  if(!item.career)return b;
  const row=el('div','','attempt-row');row.dataset.id=item.run_id;
  if(renaming===item.run_id){const input=el('input','','rename');input.value=name;input.maxLength=120;input.setAttribute('aria-label','Conversation name');row.append(input);return row;}
  const more=button('',()=>conversationMenu(item,more),'more');more.append(icon('dots'));
  more.setAttribute('aria-label',`Options for ${name}`);more.setAttribute('aria-haspopup','menu');more.setAttribute('aria-expanded','false');
  b.addEventListener('contextmenu',e=>{e.preventDefault();conversationMenu(item,more,{x:e.clientX,y:e.clientY});});
  b.addEventListener('keydown',e=>{if(e.key==='F2'){e.preventDefault();startRename(item);}else if(e.key==='Delete'){e.preventDefault();trashConversation(item);}});
  row.draggable=true;row.addEventListener('dragstart',e=>{e.dataTransfer.setData('text/x-northstar-task',item.run_id);e.dataTransfer.effectAllowed='move';});
  row.append(b,more);return row;
}
function markChatNav(){
  if(!careerMode)return;
  $('#new-chat').setAttribute('aria-current',String(view==='chat'&&!selected));
  $('#nav-council').setAttribute('aria-current',String(view==='chat'&&Boolean(selected)));
}
function renderSidebar(force=false){
  markChatNav();
  const nav=$('#attempts');
  // Polling must not interrupt a rename in progress.
  if(!force&&renaming&&nav.querySelector('input.rename'))return;
  nav.replaceChildren();
  const collapsed=collapsedGroups();
  for(const section of sidebarSections(attempts,careerMode?groups:[],needsYou,careerMode?'Recent':'Applications')){
    if(section.group){
      const id=section.group.group_id,head=el('div','','side-group'),open=!collapsed.has(id);
      const toggle=button('',()=>{toggleGroup(id);renderSidebar(true);},'side-group-toggle');
      toggle.setAttribute('aria-expanded',String(open));toggle.append(icon(open?'chevd':'chev'),el('span',section.label));
      const more=button('',()=>groupMenu(section.group,more),'more');more.append(icon('dots'));
      more.setAttribute('aria-label',`Options for group ${section.label}`);more.setAttribute('aria-haspopup','menu');
      head.append(toggle,more);dropTarget(head,id);nav.append(head);
      if(!open)continue;
      if(!section.items.length)nav.append(el('p','Drag conversations here','side-empty'));
    }else{const label=el('p',section.label,'side-label');if(section.key==='recent')dropTarget(label,null);nav.append(label);}
    for(const item of section.items){const row=attemptRow(item);if(section.group)row.classList.add('in-group');nav.append(row);}
  }
  const name=[profile.first_name,profile.last_name].filter(Boolean).join(' ');
  $('#profile-name').textContent=name||'Your details';
  $('#profile-initials').textContent=(profile.first_name?.[0]||'')+(profile.last_name?.[0]||'')||'·';
}
// Match › Materials › Apply › Interview, derived from what this conversation has produced so far.
function journeyStages(item){
  if(!item?.career)return null;
  const jobs=item.jobs||[],docs=(item.materials||[]).flatMap(p=>(p.items||[]).map(i=>({...i,stale:p.status==='stale'})));
  const applied=jobs.some(j=>['applied','interviewing','offer'].includes(j.status));
  const stages=[
    ['Weighed',jobs.some(j=>j.match_score!=null)?'done':jobs.length?'active':''],
    ['Forged',docs.length&&docs.every(d=>d.status==='approved'&&!d.stale)?'done':docs.length?'active':''],
    ['Applying',applied?'done':item.application_run_id?'active':''],
    ['Interview',(item.interviews||[]).length?'active':''],
  ];
  if(!stages.some(([,state])=>state))return null;
  const now=stages.map(([,state])=>state).lastIndexOf('active');
  return stages.map(([label,state],i)=>({label,state:i===now?'now':state==='done'?'done':''}));
}
function renderHeader(messages){
  const view=current?heading(current):{title:careerMode?'New conversation':'Your next opportunity',subtitle:''};
  $('#chat-title').textContent=view.title;$('#chat-subtitle').textContent=view.subtitle;
  const status=$('#agent-status');
  // A finished career chat needs no status; everything else says what the agent is doing.
  status.textContent=extracting?'Reading your resume…':current&&!(current.career&&current.status==='completed'&&!current.task_error)?statusText(current):'';
  status.hidden=!status.textContent;status.classList.toggle('attention',needsYou(current));
  const stages=journeyStages(current),list=$('#journey');list.replaceChildren();list.hidden=!stages;
  for(const stage of stages||[]){
    const waiting=stage.state==='now'&&needsYou(current);
    const li=el('li','',`st ${waiting?'wait':stage.state}`);li.append(el('i'),el('span',stage.label));
    if(stage.state==='now')li.setAttribute('aria-current','step');
    list.append(li);
  }
  const empty=!current&&!messages.length;
  $('#conversation').classList.toggle('is-empty',empty);
  $('#conversation').classList.toggle('is-chronicle',empty&&careerMode);
  placeComposer(empty&&careerMode);
  if(empty&&careerMode)renderChronicle();
  $('#starters').hidden=!(empty&&careerMode);
  const notice=$('#needs-you');notice.hidden=!needsYou(current);
  if(!notice.hidden)notice.textContent=current.account_request?'Waiting for you to sign in':current.status==='awaiting_review'?'Ready for your review':'Waiting for your answer';
}
let traceSignature='';
// The way: the last few distinct steps the agent took, then where it stands now.
function renderTrace(){
  const actions=current?.actions||[],trace=$('#agent-trace'),sig=JSON.stringify([current?.run_id,actions.length,actions.at(-1),current?.status,needsYou(current)]);
  if(sig===traceSignature)return;traceSignature=sig;
  trace.hidden=!actions.length;if(!actions.length)return;
  const names=[];
  for(const a of actions){const name=toolNames[a.tool]||a.tool;if(names.at(-1)!==name)names.push(name);}
  const busy=busyStatuses.has(current.status)||busyStatuses.has(current.task_status);
  const stations=names.slice(-5).map(label=>({label,state:'done'}));
  if(needsYou(current))stations.push({label:current.status==='awaiting_review'?'Your review':'Your answer',state:'wait'});
  else if(busy)stations[stations.length-1].state='now';
  trace.querySelector('.trail').replaceChildren(...stations.map(({label,state})=>{const li=el('li','',`st ${state}`);li.append(el('i'),el('span',label));return li;}));
  trace.querySelector('.log').replaceChildren(...actions.slice(-12).map(a=>el('li',`${toolNames[a.tool]||a.tool}${a.label?` · ${a.label}`:''}${a.outcome?` · ${a.outcome}`:''}`)));
}
// ---- Chronicle: the home page when no conversation is open ----
const composerWrap=document.querySelector('.composer-wrap');
function placeComposer(home){
  const target=home?$('#composer-home'):$('#conversation');
  if(composerWrap.parentElement!==target)target.append(composerWrap);
}
const numberWords=['No','One','Two','Three','Four','Five','Six','Seven','Eight','Nine'];
const greeting=()=>{const h=new Date().getHours();return h<5?'Good evening':h<12?'Good morning':h<18?'Good afternoon':'Good evening';};
const sameDay=(iso,day=new Date())=>{const d=new Date(iso);return d.getFullYear()===day.getFullYear()&&d.getMonth()===day.getMonth()&&d.getDate()===day.getDate();};
const clock=iso=>new Date(iso).toLocaleTimeString('en',{hour:'2-digit',minute:'2-digit',hour12:false});
let roadData=null,roadLoadedAt=0,chronicleSignature='';
async function loadRoad(){
  if(Date.now()-roadLoadedAt<30000)return;roadLoadedAt=Date.now();
  try{roadData=await api('/api/applications/overview?weeks=4');chronicleSignature='';render();}catch{/* The Chronicle works without it. */}
}
const ROAD_STAGE={saved:0,analyzing:0,analysis_failed:0,needs_evidence:1,materials_ready:1,ready_to_apply:1,applied:2,interviewing:3,offer:4};
function stageTrail(status,waiting,count=5){
  const at=ROAD_STAGE[status]??0,ol=el('ol','','trail compact');
  for(let i=0;i<count;i++){const li=el('li','',`st ${i<at?'done':i===at?(waiting?'wait':status==='offer'?'gold':'now'):''}`);li.append(el('i'));ol.append(li);}
  return ol;
}
function renderChronicle(){
  const waiting=attempts.filter(needsYou),careers=attempts.filter(a=>a.career);
  const sig=JSON.stringify([profile.first_name,waiting.map(a=>[a.run_id,a.status]),careers.map(a=>[a.run_id,a.status,a.updated_at]),roadData?.applications?.length,roadData?.totals]);
  if(sig===chronicleSignature)return;chronicleSignature=sig;
  loadRoad();
  const first=profile.first_name?.trim();
  $('#chron-date').hidden=false;
  $('#chron-date').textContent=new Date().toLocaleDateString('en',{weekday:'long',day:'numeric',month:'long'});
  $('#welcome h2').textContent=`${greeting()}${first?`, ${first}`:''}.`;
  const lit=waiting.length;
  $('#welcome h2 + p').textContent=lit?`${numberWords[lit]||lit} ${lit===1?'beacon is':'beacons are'} lit.`:'All quiet. Where does the road lead next?';
  // Today's chronicle: what moved today, newest first.
  const today=careers.filter(a=>a.updated_at&&sameDay(a.updated_at)).sort((a,b)=>b.updated_at.localeCompare(a.updated_at)).slice(0,6);
  const list=$('#chron-today .vtrail');list.replaceChildren();
  $('#chron-today').hidden=!careers.length;
  for(const item of today){
    const name=heading(item).title,busy=busyStatuses.has(item.status);
    const state=needsYou(item)?'wait':busy?'now':'done';
    const li=el('li','',state),text=el('button','','chron-entry');text.type='button';text.addEventListener('click',()=>select(item.run_id));
    text.append(el('time',clock(item.updated_at)),el('b',name),el('small',statusText(item)));
    li.append(el('i'),text);list.append(li);
  }
  if(!today.length)list.append(el('li','Quiet so far today. Write to Eregion to begin.','empty'));
  // Beacons lit.
  $('#chron-side').hidden=!careers.length&&!roadData?.applications?.length;
  const beacons=$('#chron-beacons .rows');beacons.replaceChildren();
  $('#chron-beacons .tag').textContent=lit?`${lit} need you`:'';$('#chron-beacons .tag').hidden=!lit;
  for(const item of waiting.slice(0,4)){
    const row=el('div','','row'),info=el('div');info.append(el('b',heading(item).title),el('span',statusText(item)));
    row.append(info,button(item.status==='awaiting_review'?'Review':'Answer',()=>select(item.run_id),'secondary-button'));beacons.append(row);
  }
  if(!lit)beacons.append(el('p','No beacons lit. Nothing is waiting on you.','empty'));
  // On the road: active applications as trails.
  const needs=pageContext.needsYou(),road=$('#chron-road .rows');road.replaceChildren();
  const active=(roadData?.applications||[]).filter(a=>!['rejected','withdrawn','archived'].includes(a.status)).sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
  for(const app of active.slice(0,5)){
    const row=el('button','','road-row');row.type='button';row.addEventListener('click',()=>pageContext.openApplication(app.application_id));
    row.append(el('span',app.company||app.title||'Application'),stageTrail(app.status,needs.has(app.application_id)));road.append(row);
  }
  if(!active.length)road.append(el('p','No applications on the road yet.','empty'));
  $('#count-road').textContent=active.length||'';
}
function render(){
  const preferenceScope=current?.run_id||draft.id;
  const scroll=$('#chat-scroll'),follow=renderedScope!==preferenceScope||scroll.scrollHeight-scroll.scrollTop-scroll.clientHeight<120;
  renderedScope=preferenceScope;
  if(preferenceScope!==visualPreferenceRun){
    $('#visual-fallback').checked=current?Boolean(current.visual_fallback):defaultVisualFallback;
    visualPreferenceRun=preferenceScope;
  }
  renderSidebar();
  const local=selected?optimistic.get(selected)||[]:[];
  const messages=current?[...(current.messages||[]),...local.filter(m=>!current.messages?.some(s=>s.id===m.id))]:draft.messages;
  $('#welcome').hidden=Boolean(current||messages.length);
  const signature=JSON.stringify([current?.run_id||draft.id,messages]);
  const messagesChanged=signature!==logSignature;
  if(messagesChanged){
    logSignature=signature;
    const log=$('#agent-messages'),existing=new Map([...log.querySelectorAll(':scope > .message')].map(n=>[n.dataset.messageId,n]));
    for(const m of messages){
      const previous=existing.get(m.id);
      if(previous?.dataset.text===m.text&&previous.dataset.role===m.role)continue;
      const bubble=el('div','',`message ${m.role}`);bubble.dataset.messageId=m.id;bubble.dataset.text=m.text;bubble.dataset.role=m.role;
      bubble.append(el('span',m.role==='user'?'You':'Eregion','speaker'));
      const body=m.role==='assistant'?renderMarkdown(m.text):el('p',m.text);bubble.append(body);
      if(m.role==='assistant'){
        const copy=button('Copy',async()=>{try{await navigator.clipboard.writeText(body.innerText);copy.textContent='Copied';}catch{copy.textContent='Select text to copy';}setTimeout(()=>{copy.textContent='Copy';},1800);},'message-copy');
        copy.setAttribute('aria-label','Copy reply');bubble.append(copy);
      }
      if(previous)previous.replaceWith(bubble);else log.append(bubble);
    }
  }
  renderHeader(messages);
  $('#pause-agent').hidden=!current||terminal.has(current.status)||current.status==='submitting';$('#pause-agent').textContent=current?.pause_requested?'Resume':'Pause';$('#pause-agent').disabled=actionBusy||current?.reopening;
  $('#send-message').disabled=chatBusy||starting||restoring||current?.reopening||current?.status==='submitting';$('#attach-resume').disabled=extracting||restoring;$('#profile-toggle').disabled=extracting||restoring;
  const attachment=$('#attachment');attachment.replaceChildren();attachment.hidden=!resume&&!extracting;
  if(extracting)attachment.append(icon('file'),el('span','Reading your resume…'));
  else if(resume){const remove=button('×',removeResume,'text-button');remove.setAttribute('aria-label','Remove resume');attachment.append(icon('file'),el('span',resume.name),remove);}
  decisions();renderWorkspace();renderTrace();autoOpenWorkspace(current);
  const careerSig=JSON.stringify([current?.run_id,current?.jobs,current?.recommendations,current?.materials,current?.interviews,chatBusy,actionBusy]);
  const resultsChanged=careerSig!==careerSignature;
  if(resultsChanged){
    const previous=new Map(careerCards.map(card=>[card.dataset.resultKey,card]));
    careerSignature=careerSig;const results=el('div');
    renderCareerResults({container:results,item:current,el,button,send:(text,context,id)=>send(text,null,id,context),busy:chatBusy||actionBusy,review:reviewMaterial,renderText:renderMarkdown});
    careerCards=[...results.children];
    for(const card of careerCards){
      const old=previous.get(card.dataset.resultKey);
      if(!old)continue;
      if(card.tagName==='DETAILS'&&old.tagName==='DETAILS')card.open=old.open;
      [...card.querySelectorAll('details')].forEach((detail,i)=>{detail.open=old.querySelectorAll('details')[i]?.open||false;});
    }
  }
  placeCareerResults($('#agent-messages'),messages,careerCards);
  if(follow&&(messagesChanged||resultsChanged))requestAnimationFrame(bottom);
}
function addDraft(role,text){draft.messages.push({id:crypto.randomUUID(),role,text});render();requestAnimationFrame(bottom);}
function ask(text){if(draft.messages.at(-1)?.text!==text)addDraft('assistant',text);}
function decisions(){
  const container=$('#conversation-actions'),pending=current?.input_request;
  // Background fills must not replace a focused answer input on every poll.
  const sig=JSON.stringify([current?.run_id,current?.status,current?.pause_requested,current?.can_reopen,current?.reopening,pending?null:current?.message,current?.account_request,pending,pending?null:current?.pending_fields,pending?pending.fields:current?.fields,current?.answer_drafts,current?.task_error,chatBusy,actionBusy,starting]);
  if(sig===actionSignature)return;actionSignature=sig;container.replaceChildren();if(!current)return;
  const item=current,id=item.run_id;
  if(item.career&&item.task_error&&!item.pause_requested){
    const box=el('div','','decision model-error');box.setAttribute('role','alert');
    box.append(el('p',item.task_error.message));
    const retry=button('Try again',()=>send('continue'));retry.disabled=chatBusy||actionBusy;box.append(retry);container.append(box);
    if(!item.application_run_id)return;
  }
  if(item.career&&item.status==='completed'){container.append(el('p','You can keep working on these jobs or send a new request.','status-line'));return;}
  // The agent's decisions to leave optional questions empty are shown as soon as they are made.
  const leftEmpty=Object.entries(item.field_decisions||{}).filter(([,d])=>d?.mode==='leave_empty');
  if(leftEmpty.length&&!terminal.has(item.status)){const note=el('div','','decision left-empty');note.append(el('p',`Left empty by the agent (${leftEmpty.length})`,'eyebrow'));for(const [key,d] of leftEmpty){const field=(item.fields||[]).find(f=>f.key===key);note.append(el('p',`${field?.label||d.field?.label||key}: ${d.reason}`));}container.append(note);}
  if(!terminal.has(item.status)&&renderPendingQuestion({item,container,drafts:answerDrafts,busy:chatBusy||actionBusy,reply:body=>act('reply',body,id),el,button,renderText:renderMarkdown}))return;
  if(['running','queued','waiting','submitting'].includes(item.status)){container.append(el('p',item.message,'status-line'));return;}
  if(terminal.has(item.status)){const box=el('div','','decision');box.append(el('h3',statuses[item.status]),el('p',item.confirmation?.text||item.message));if(item.can_reopen){const resumeButton=button('Resume application',()=>act('resume',{},id),'primary-button');resumeButton.disabled=chatBusy||actionBusy;box.append(resumeButton);}box.append(button('Next application',()=>reset()));container.append(box);return;}
  if(item.status==='awaiting_review'){
    const answers=new Map((item.review_fields||[]).map(f=>[f.key,f]));for(const f of item.fields||[])answers.set(f.key,f);
    const all=[...answers.values()].filter(f=>f.kind!=='password'),isDefault=f=>item.field_decisions?.[f.key]?.mode==='default';
    const defaults=all.filter(isDefault),shown=[...defaults,...all.filter(f=>!isDefault(f))].slice(0,5);
    const box=el('div','','decision review-card');
    box.append(el('p',`${all.length} ${all.length===1?'answer':'answers'}${defaults.length?` · ${defaults.length} to check`:''}`,'eyebrow'),
      el('h3','Your application is ready'),
      el('p',defaults.length?`I used a default for ${defaults.length===1?'one answer':`${defaults.length} answers`}. Check ${defaults.length===1?'it':'them'}, then I’ll submit once when you confirm.`:`Review the answers for ${company(item)}. I’ll submit once when you confirm.`));
    if(shown.length){
      const list=el('div','','review-list');
      for(const f of shown){
        const row=el('div','','review-row'),value=el('span',typeof f.value==='boolean'?(f.value?'Yes':'No'):String(f.value||'Not filled'),'v');
        if(isDefault(f))value.append(el('span','Agent default','flag'));
        row.append(el('span',f.label,'k'),value);list.append(row);
      }
      box.append(list);
    }
    const approvals=item.pending_approvals||[],choices=memoryChoices.get(id)||{remember:{},scopes:{}};memoryChoices.set(id,choices);
    const reused=all.filter(f=>item.answer_sources?.[f.key]?.type==='memory');
    if(approvals.length)box.append(memoryApprovals(approvals,choices));
    if(reused.length){const saved=el('details','','saved-answers');saved.append(el('summary',`Filled from your saved answers (${reused.length})`));const list=el('div','','review-list');for(const f of reused){const row=el('div','','review-row');row.append(el('span',f.label,'k'),el('span',typeof f.value==='boolean'?(f.value?'Yes':'No'):String(f.value||'Not filled'),'v'));list.append(row);}saved.append(list);box.append(saved);}
    if(item.unfilled_optional?.length){const empty=el('details','','saved-answers');empty.append(el('summary',`Optional questions left empty (${item.unfilled_optional.length})`));const list=el('div','','review-list');for(const f of item.unfilled_optional){const row=el('div','','review-row');row.append(el('span',f.label,'k'),el('span',f.set_aside?'No matching option; check it':'Left empty','v'));list.append(row);}empty.append(list);box.append(empty);}
    const actions=el('div','','actions'),submit=button('Submit application',()=>act('continue',{confirm_submit:true,
      remember:Object.fromEntries(approvals.map(a=>[a.key,choices.remember[a.key]??true])),
      memory_scopes:Object.fromEntries(approvals.map(a=>[a.key,choices.scopes[a.key]||a.scope]))},id),'primary-button');submit.disabled=chatBusy||actionBusy;
    actions.append(submit,button('Review answers',()=>{workspace();$('#answer-list').parentElement.open=true;}),el('span','Submits once','lock-note'));
    box.append(actions);container.append(box);return;
  }
  if(item.pause_requested){const box=el('div','','decision');box.append(el('p','Paused. Use Control browser in Agent activity if you need to interact with the page, then resume when ready.'),button('Resume',()=>send('continue')));container.append(box);return;}
  if(item.status==='needs_input'&&item.agent_reason==='application_budget'){
    // A grant of more model calls is always an explicit user action (W4).
    const box=el('div','','decision');box.append(el('p',item.message));
    const more=button('Allow 100 more model calls',()=>act('continue',{extend_budget:true},id),'primary-button');more.disabled=chatBusy||actionBusy;
    box.append(more);container.append(box);return;
  }
  if(item.status==='needs_input'){
    if(item.account_request){const box=el('div','','decision');box.append(el('p',item.account_request.origin,'eyebrow'),el('p',item.message));const actions=el('div','','actions');actions.append(button(item.account_request.mode==='register'?'Create candidate account':'Sign in securely',()=>openAccount(item),'primary-button'));const other=item.account_request.mode==='register'?'login':'register';actions.append(button(other==='register'?'Create an account instead':'Use an existing account',()=>act('account-mode',{origin:item.account_request.origin,mode:other},id)));box.append(actions);container.append(box);return;}
    const last=[...(item.messages||[])].reverse().find(m=>m.role==='assistant'&&Array.isArray(m.question_keys));const keys=(last?.question_keys||[]).filter(key=>item.pending_fields?.includes(key)??true);
    renderAnswerBatch({container,id,fields:keys.map(key=>item.fields.find(f=>f.key===key)).filter(Boolean),drafts:answerDrafts,suggestions:item.answer_drafts,busy:chatBusy||actionBusy,submit:answers=>act('continue',{answers},id),el,button});
    if(!keys.length){const box=el('div','','decision');box.append(el('p',item.message));const actions=el('div','','actions');actions.append(button('Try again',()=>send('continue')));if(!item.career||item.application_run_id||item.browser)actions.append(button('Open browser',()=>workspace()));box.append(actions);container.append(box);}
  }
}
function memoryApprovals(approvals,choices){
  // Approve once: checked answers are reused automatically on later applications.
  const section=el('div','','memory-approvals');
  section.append(el('p',`New ${approvals.length===1?'answer':'answers'} from our chat`,'eyebrow'),el('p','Checked answers are reused automatically next time, so you only approve them once.','hint'));
  const list=el('div','','review-list');
  for(const a of approvals){
    const row=el('div','','review-row approval-row'),value=el('span',typeof a.value==='boolean'?(a.value?'Yes':'No'):String(a.value),'v');
    const keep=el('label','','remember-toggle'),box=el('input');box.type='checkbox';box.checked=choices.remember[a.key]??true;
    box.addEventListener('change',()=>{choices.remember[a.key]=box.checked;scope.disabled=!box.checked;});
    keep.append(box,el('span','Remember'));
    const scope=el('select','','memory-scope');scope.setAttribute('aria-label',`Where to reuse ${a.label}`);
    for(const [v,t] of [['global','All jobs'],['employer','This employer only']]){const o=el('option',t);o.value=v;scope.append(o);}
    scope.value=choices.scopes[a.key]||a.scope;scope.disabled=!box.checked;scope.addEventListener('change',()=>{choices.scopes[a.key]=scope.value;});
    const controls=el('span','','approval-controls');controls.append(keep,scope);value.append(controls);
    row.append(el('span',a.label,'k'),value);list.append(row);
  }
  section.append(list);return section;
}
function renderWorkspace(){
  $('#task-plan').replaceChildren();$('#browser-actions').replaceChildren();$('#answer-list').replaceChildren();
  const canControl=current&&(!current.career||current.application_run_id||current.browser)&&!terminal.has(current.status)&&current.status!=='submitting';
  // The agent works in a hidden Chrome shown live in this panel. Control browser pauses it and
  // pops up that real (local) Chrome window; it hides again when the agent resumes.
  $('#take-over').hidden=!canControl;$('#background-browser').hidden=true;$('#browser-mode-note').hidden=!canControl;
  $('#take-over').disabled=actionBusy||chatBusy;$('#background-browser').disabled=actionBusy||chatBusy;
  $('#cancel-attempt').hidden=!current||terminal.has(current.status)||current.status==='submitting';
  $('#browser-caption').textContent=current?`${browserHost(current)||company(current)} · live view`:'The browser will appear here.';
  const now=$('#workspace-now');now.hidden=!current;if(current)now.textContent=current.message||statusText(current);
  const perf=current?.perf_summary,usage=current?.usage_summary,summary=$('#perf-summary');summary.hidden=!perf&&!usage?.calls;
  summary.textContent=usageText(usage)||(perf?`${perf.model_calls} model calls · ${Math.round(perf.model_seconds)} s model · ${Math.round(perf.browser_seconds)} s browser`:'');
  renderUsage($('#usage-details'),usage);
  if(!current)return;
  for(const step of current.task_plan||[])$('#task-plan').append(el('li',step));if(!current.task_plan?.length)$('#task-plan').append(el('li',current.status==='awaiting_review'?'Review the completed application':current.message));
  for(const a of [...(current.actions||[])].slice(-20).reverse())$('#browser-actions').append(el('li',`${toolNames[a.tool]||a.tool} · ${a.label} · ${a.outcome}`));
  const answers=new Map((current.review_fields||[]).map(f=>[f.key,f]));for(const f of current.fields)answers.set(f.key,f);
  for(const f of answers.values()){const row=el('div','','answer');row.append(el('strong',f.label),el('p',typeof f.value==='boolean'?(f.value?'Yes':'No'):String(f.value||'Not filled')));const decision=current.field_decisions?.[f.key];if(decision?.mode==='default')row.append(el('small',`Agent default: ${decision.reason}`));if(!terminal.has(current.status)&&current.status!=='submitting'&&f.kind!=='file')row.append(button('Correct in chat',()=>{$('#chat-text').value=`For “${f.label}”, use `;$('#chat-text').focus();},'quiet-button'));$('#answer-list').append(row);}
}
let previewPending=false;
async function refreshPreview(item){if(previewPending)return;if(terminal.has(item.status)&&!item.browser||(item.career&&!item.application_run_id&&!item.browser))return;previewPending=true;try{const r=await fetch(item.preview_url||`/api/auto-apply/${item.application_run_id||item.run_id}/preview`,{cache:'no-store',signal:AbortSignal.timeout(5000)});if(r.status!==200)return;const blob=await r.blob();if(current?.run_id!==item.run_id)return;if(previewUrl)URL.revokeObjectURL(previewUrl);previewUrl=URL.createObjectURL(blob);$('#browser-preview').src=previewUrl;$('#browser-preview').hidden=false;$('#preview-placeholder').hidden=true;}catch{/* A preview delay does not stop the task. */}finally{previewPending=false;}}
async function encode(file){if(!(file instanceof File)||!file.size||file.size>10*1024*1024)throw new Error('Attach a PDF resume no larger than 10 MB.');const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(binary);}
$('#resume-file').addEventListener('change',async event=>{
  const file=event.target.files[0];if(!file)return;const taskId=current?.career?current.run_id:null;if(current&&!current.career)reset();draft.id=crypto.randomUUID();draft.payload=null;const version=++extractionVersion,id=draft.id;extracting=true;error();addDraft('user',`Attached ${file.name}`);
  try{const encoded=await encode(file);if(version!==extractionVersion)return;resume={name:file.name,encoded};profile={};const result=await request('/resume-profile',{resume_base64:encoded,resume_name:file.name});if(version!==extractionVersion)return;profile=result.profile||{};await persistCandidate({resume_base64:encoded,resume_name:file.name,extracted:!result.warning});await resourceChanged(`I uploaded ${file.name}. Use my updated saved resume for this request.`,taskId);if(draft.id===id&&!selected){const name=[profile.first_name,profile.last_name].filter(Boolean).join(' ');ask(`I’ve saved your resume${name?` for ${name}`:''} for future applications.${profile.education?.length?` Found ${profile.education.length} education records; review them in Your details.`:''}${result.warning?' Some details need your help to complete.':''}`);}}
  catch(e){error(e.message);}finally{if(version===extractionVersion){extracting=false;render();if(view==='materials')renderPage('materials',$('#page-view'),pageContext);else if(!selected)advance();}}
});
function jobLink(text){return text.match(/https:\/\/[^\s<>"）)]+/)?.[0]?.replace(/[.,;，。；]+$/,'')||'';}
async function advance(){
  if(careerMode){if((draft.url||draft.applicationId)&&!current&&!starting&&!restoring)await startCareer(draft.lastText||draft.url);return;}
  if(current||extracting||starting||restoring)return;
  if(!resume){ask('Attach your resume PDF here, and I’ll use it to prepare the application.');return;}
  if(!draft.url){ask('Send the job page you’d like to apply to. I can open the application and work through its steps with you.');return;}
  draft.field=['first_name','last_name','email'].find(k=>!profile[k]?.trim())||null;
  if(draft.field){ask(`What ${labels[draft.field].toLowerCase()} should I use? Send just that detail.`);return;}
  const id=draft.id,version=epoch;starting=true;render();ask('I have what I need to start. I’ll open the application and keep you updated here.');
  try{await candidateWrites;draft.payload ||= {url:draft.url,profile:{...profile},resume_base64:resume.encoded,resume_name:resume.name,auto_submit:false,automation_mode:'hybrid',consent_policy:'ask',reuse_answers:$('#reuse-answers').checked,visual_fallback:$('#visual-fallback').checked,idempotency_key:id,initial_message:draft.lastText||`Apply to ${draft.url} using ${resume.name}.`};const item=await request('',draft.payload);if(epoch===version&&draft.id===id){selected=item.run_id;current=item;logSignature='';actionSignature='';}await refresh();}
  catch(e){if(e.existingRunId&&epoch===version&&draft.id===id){try{await openExisting(e.existingRunId);}catch(openError){error(openError.message);}}else{error(e.message);if(draft.id===id)ask('I couldn’t open this application yet. Check the message above, then send a corrected job link or tell me to try again.');}}finally{starting=false;render();}
}
async function prepare(text){
  if(careerMode){await startCareer(text);return;}
  const id=draft.id;draft.lastText=text;addDraft('user',text);const url=jobLink(text);
  if(url){draft.url=url;draft.id=crypto.randomUUID();draft.payload=null;await advance();return;}
  if(/^(continue|start|go|retry|开始|继续|重试)[.!。！]?$/i.test(text)){await advance();return;}
  if(/^(pause|stop|暂停|停止)[.!。！]?$/i.test(text)){draft.url='';ask('Paused. Send the job link again when you want me to start.');return;}
  const result=await request('/prepare-message',{text,field:draft.field});if(draft.id!==id||selected)return;
  if(Object.keys(result.profile||{}).length){Object.assign(profile,result.profile);await persistCandidate();draft.field=null;draft.id=crypto.randomUUID();draft.payload=null;ask('Got it. I’ve updated your saved details.');await advance();}else ask(result.message||'Send a job link or attach your resume to get started.');
}
async function send(text,key=null,expectedRun=null,context=null){
  text=text.trim();if(!text||chatBusy||starting)return;if(expectedRun&&current?.run_id!==expectedRun)return;
  if(current&&!current.career&&terminal.has(current.status)){if(current.can_reopen&&/^(continue|resume|retry|继续|继续吧|恢复|重试)[.!。！]?$/i.test(text)){await act('resume',{});return;}const url=jobLink(text);if(url){reset(url);return;}error(current.can_reopen?'Use “Resume application” to continue with your saved progress.':'This application has ended. Check its submission status, or send a different job link.');return;}
  if(current?.status==='submitting'||current?.reopening)return;
  if(current&&/^(submit|confirm and submit|提交|确认提交)[.!。！]?$/i.test(text)){error('Use “Submit application” on the review card to confirm this application.');return;}
  chatBusy=true;error();const runId=current?.run_id,version=epoch;render();
  try{if(!runId)await prepare(text);else{const id=failedMessage?.runId===runId&&failedMessage.text===text&&failedMessage.key===key&&JSON.stringify(failedMessage.context)===JSON.stringify(context)?failedMessage.id:crypto.randomUUID();optimistic.set(runId,[{id,role:'user',text,at:new Date().toISOString()}]);render();bottom();failedMessage={id,text,key,runId,context};await request(`/${runId}/chat`,{text,field_key:key,message_id:id,visual_fallback:$('#visual-fallback').checked,...(context?{context}:{})});failedMessage=null;optimistic.delete(runId);await refresh();}if(epoch===version&&$('#chat-text').value.trim()===text){$('#chat-text').value='';growComposer();}}
  catch(e){if(runId)optimistic.delete(runId);error(e.message);}finally{chatBusy=false;render();}
}
async function act(action,body,id=current?.run_id){if(!current||current.run_id!==id||actionBusy||chatBusy)return false;const version=current.version;actionBusy=true;error();render();try{await request(`/${id}/${action}`,{...(action==='reply'?{}:{expected_version:version}),...body});if(body.answers)answerDrafts.clear(id);await refresh();return true;}catch(e){error(e.message);await refresh();return false;}finally{actionBusy=false;render();}}

async function browserWindow(visible){
  if(!current||actionBusy||chatBusy)return;
  const item=current;actionBusy=true;error();render();
  // A busy chat can bump the version between polls; take the latest version and retry once.
  const post=()=>request(`/${item.run_id}/browser-window`,{visible,expected_version:(current?.run_id===item.run_id?current:item)[item.career?'task_version':'version']});
  try{try{await post();}catch(e){if(!/changed/i.test(e.message))throw e;await refresh();await post();}await refresh();}
  catch(e){error(e.message);await refresh();}finally{actionBusy=false;render();}
}

let accountTarget=null;
function openAccount(item){
  const auth=item.account_request;accountTarget={id:item.run_id,applicationId:item.application_run_id,origin:auth.origin,mode:auth.mode};
  $('#account-form').reset();$('#account-origin').textContent=auth.origin;
  $('#account-instructions').textContent=auth.instructions||'';
  $('#account-title').textContent=auth.mode==='register'?'Create your candidate account':'Sign in to your candidate account';
  $('#account-username').value=profile.email||'';$('#account-password').autocomplete=auth.mode==='register'?'new-password':'current-password';
  $('#account-submit').textContent=auth.mode==='register'?'Create account & continue':'Sign in & continue';
  $('#account-consents').replaceChildren();
  for(const c of auth.consents){const label=el('label',c.label,'check-row'),input=el('input');input.type='checkbox';input.dataset.key=c.key;input.required=c.required;label.prepend(input);$('#account-consents').append(label);}
  $('#account-dialog').showModal();
}
$('#account-close').addEventListener('click',()=>$('#account-dialog').close());
$('#account-dialog').addEventListener('close',()=>{$('#account-form').reset();accountTarget=null;});
$('#account-form').addEventListener('submit',async event=>{
  event.preventDefault();if(actionBusy||!accountTarget)return;
  const target=accountTarget;if(current?.run_id!==target.id||current.application_run_id!==target.applicationId||!current.account_request||current.account_request.origin!==target.origin||current.account_request.mode!==target.mode){$('#account-dialog').close();error('The account request changed. Reopen the current account form.');return;}
  const body={expected_version:current.version,origin:target.origin,mode:target.mode,username:$('#account-username').value,password:$('#account-password').value,consents:Object.fromEntries([...document.querySelectorAll('#account-consents input')].map(i=>[i.dataset.key,i.checked]))};
  $('#account-dialog').close();actionBusy=true;error();render();
  try{await request(`/${target.id}/account`,body);await refresh();}catch(e){error(e.message);}finally{body.password='';actionBusy=false;render();}
});
$('#agent-chat-form').addEventListener('submit',event=>{event.preventDefault();send($('#chat-text').value);});
$('#chat-text').addEventListener('keydown',event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();$('#agent-chat-form').requestSubmit();}});
$('#attach-resume').addEventListener('click',()=>$('#resume-file').click());$('#new-chat').addEventListener('click',()=>reset());
$('#pause-agent').addEventListener('click',()=>send(current?.pause_requested?'continue':'pause'));
$('#browser-toggle').addEventListener('click',()=>workspace($('#browser-panel').hidden,{remember:true}));$('#browser-close').addEventListener('click',()=>workspace(false,{remember:true}));
const localBrowserControl=createBrowserControl({current:()=>current,refresh,resume:()=>send('continue')});
$('#take-over').addEventListener('click',()=>browserWindow(true));$('#background-browser').addEventListener('click',()=>browserWindow(true));$('#cancel-attempt').addEventListener('click',()=>act('cancel',{}));
$('#history-toggle').addEventListener('click',()=>$('#history-panel').classList.toggle('open'));
$('#history-scrim').addEventListener('click',()=>$('#history-panel').classList.remove('open'));
document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&!event.altKey&&event.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')){event.preventDefault();reset();$('#chat-text').focus();}});
document.addEventListener('keydown',event=>{if(event.key!=='Escape'||document.querySelector('dialog[open]'))return;if($('#history-panel').classList.contains('open'))$('#history-panel').classList.remove('open');else if(!$('#browser-panel').hidden&&window.innerWidth<=1150)workspace(false);});
const growComposer=()=>{const t=$('#chat-text');t.style.height='auto';t.style.height=`${Math.min(t.scrollHeight,200)}px`;};
$('#chat-text').addEventListener('input',growComposer);
for(const [label,text] of starterPrompts)$('#starters').append(button(label,()=>{const t=$('#chat-text');t.value=text;growComposer();t.focus();t.setSelectionRange(text.length,text.length);},''));
const educationLabels={school:'School / university',degree:'Degree',field_of_study:'Major / field of study',start_date:'Start date (as written on resume)',end_date:'End / expected graduation date',graduation_status:'Graduation status (if known)'};
function educationEditor(record={}){
  const row=el('fieldset','','education-record');row.dataset.id=record.id||'';row.append(el('legend','Education'));
  for(const [key,text] of Object.entries(educationLabels)){const label=el('label',text),input=el('input');input.dataset.component=key;input.value=record[key]||'';input.maxLength=['school','degree','field_of_study'].includes(key)?300:100;label.append(input);row.append(label);}
  row.append(button('Remove education',()=>row.remove(),'quiet-button'));$('#education-fields').append(row);
}
$('#profile-toggle').addEventListener('click',()=>{
  $('#profile-fields').replaceChildren();for(const [key,label]of Object.entries(labels)){if(['school','degree'].includes(key)&&profile.education?.length)continue;const row=el('label',label),input=el('input');input.name=key;input.value=profile[key]||'';input.type=key==='email'?'email':'text';row.append(input);$('#profile-fields').append(row);}
  $('#education-fields').replaceChildren();for(const record of profile.education||[])educationEditor(record);$('#profile-dialog').showModal();
  loadStandardAnswers();
});
async function loadStandardAnswers(){
  // Approved once here, then reused on every application without asking.
  const box=$('#standard-fields');box.replaceChildren();
  try{const{answers}=await request('/standard-answers');for(const item of answers){
    const row=el('label',item.question);let input;
    if(item.kind==='yes_no'){input=el('select');for(const [v,t] of [['','Ask me'],['Yes','Yes'],['No','No']]){const o=el('option',t);o.value=v;input.append(o);}}
    else{input=el('input');input.type='text';input.placeholder='Ask me';}
    input.value=typeof item.value==='boolean'?(item.value?'Yes':'No'):String(item.value||'');input.dataset.topic=item.topic;input.dataset.originalValue=input.value.trim();row.append(input);box.append(row);}
  }catch(e){box.append(el('p','Standard answers are unavailable right now.'));}
}
async function saveStandardAnswers(){
  const inputs=[...$('#standard-fields').querySelectorAll('[data-topic]')].filter(i=>i.value.trim()!==i.dataset.originalValue);if(!inputs.length)return;
  const saved=inputs.map(i=>[i,i.value.trim()]);
  await request('/standard-answers',{answers:Object.fromEntries(saved.map(([i,value])=>[i.dataset.topic,value]))},'PUT');
  for(const [input,value] of saved)input.dataset.originalValue=value;
}
$('#add-education').addEventListener('click',()=>educationEditor());
$('#profile-close').addEventListener('click',()=>$('#profile-dialog').close());
$('#profile-form').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,save=form.querySelector('[type=submit]');save.disabled=true;
  const visualFallback=$('#visual-fallback').checked;
  const education=[...form.querySelectorAll('.education-record')].map(row=>({id:row.dataset.id,...Object.fromEntries([...row.querySelectorAll('[data-component]')].map(i=>[i.dataset.component,i.value.trim()]))}));
  const hadEducation=Boolean(profile.education?.length);profile={...profile,...Object.fromEntries(new FormData(form)),education};
  if(education.length){profile.school=education[0].school;profile.degree=education[0].degree;}else if(hadEducation){profile.school='';profile.degree='';}
  try{const result=await persistCandidate();profile=result.profile;await saveStandardAnswers();saveVisualPreference(visualFallback);$('#profile-dialog').close();await resourceChanged('I updated my saved candidate details. Use the current details.',current?.career?current.run_id:null,{visual_fallback:visualFallback});if(!current){draft.id=crypto.randomUUID();draft.payload=null;ask('Your details are saved for future applications.');advance();}}catch(e){error(e.message);}finally{save.disabled=false;}
});
$('#answer-memory').addEventListener('toggle',async()=>{if(!$('#answer-memory').open||!current||(current.career&&!current.application_run_id))return;const id=current.run_id;try{const{memories}=await request(`/${id}/memory`);if(current?.run_id!==id)return;$('#memory-list').replaceChildren();if(!memories.length)$('#memory-list').append(el('p','No answers remembered yet.'));for(const m of memories){const row=el('div','','answer'),scope=el('select','','memory-scope');scope.setAttribute('aria-label',`Where to reuse ${m.question}`);for(const [v,t] of [['global','All jobs'],['employer','This employer only']]){const o=el('option',t);o.value=v;scope.append(o);}scope.value=m.scope||'global';scope.addEventListener('change',async()=>{try{await request(`/${id}/memory/${m.memory_id}`,{scope:scope.value},'PATCH');}catch(e){error(e.message);scope.value=m.scope||'global';}});row.append(el('strong',m.question),el('p',typeof m.value==='boolean'?(m.value?'Yes':'No'):String(m.value)),scope,button('Forget',async()=>{try{await request(`/${id}/memory/${m.memory_id}`,undefined,'DELETE');row.remove();}catch(e){error(e.message);}},'quiet-button'));$('#memory-list').append(row);}}catch(e){error(e.message);}});
async function restoreCandidate(){
  const version=extractionVersion;
  try{
    const saved=await request('/candidate');if(version!==extractionVersion)return;profile=saved.profile||{};
    if(saved.resume_base64){resume={name:saved.resume_name||'resume.pdf',encoded:saved.resume_base64};render();
      if(saved.needs_extraction){extracting=true;render();const result=await request('/resume-profile',{resume_base64:resume.encoded,resume_name:resume.name});if(version!==extractionVersion)return;profile={...result.profile};await persistCandidate({resume_base64:resume.encoded,resume_name:resume.name,extracted:!result.warning});}
    }
  }catch(e){error(`Could not restore saved details: ${e.message}`);}finally{restoring=false;if(version===extractionVersion)extracting=false;render();if(draft.url||draft.applicationId)advance();}
}
const initialParams=new URLSearchParams(location.search),initialUrl=initialParams.get('url'),initialApplication=initialParams.get('application');
if(careerMode&&initialApplication){draft.applicationId=initialApplication;draft.lastText='Help me review this saved job.';addDraft('user',draft.lastText);}
else if(initialUrl){draft.url=initialUrl;draft.lastText=careerMode?`Help me apply to ${initialUrl}.`:initialUrl;addDraft('user',initialUrl);}
if(careerMode){
  $('#side-nav').hidden=false;$('#side-footer-links').hidden=false;
  for(const link of document.querySelectorAll('[data-view]'))link.addEventListener('click',event=>{event.preventDefault();showView(link.dataset.view);});
}
initTheme();
render();refresh();restoreCandidate();refreshCounts();
if(careerMode){
  const stopNotifications=startAlertNotifications({open:()=>showView('alerts'),onUpdate:data=>{
    const count=data.digests.filter(d=>d.finished_at&&!d.read_at).length;
    $('#count-alerts').textContent=count||'';
  }});
  window.addEventListener('pagehide',stopNotifications,{once:true});
}
if(careerMode&&PAGE_VIEWS.has(initialParams.get('view')))showView(initialParams.get('view'),{push:false});

async function startCareer(text){
  if(starting)return;starting=true;error();const version=epoch;
  try{await candidateWrites;const item=await request('',{text,idempotency_key:draft.id,visual_fallback:$('#visual-fallback').checked,...(draft.applicationId?{application_id:draft.applicationId}:{})});
    if(epoch===version){selected=item.run_id;current=item;attempts=[item,...attempts.filter(a=>a.run_id!==item.run_id)];logSignature='';actionSignature='';}
    await refresh();
  }catch(e){error(e.message);}finally{starting=false;render();}
}
async function reviewMaterial(pack,material,approve){
  if(!current?.career||actionBusy)return;actionBusy=true;error();render();
  try{await request(`/${current.run_id}/materials/${pack.pack_id}/${material.pack_item_id}/review`,{expected_version:material.version,approve});await refresh();}
  catch(e){error(e.message);}finally{actionBusy=false;render();}
}
if(careerMode){
  $('#new-chat span').textContent='Chronicle';
  $('#new-chat use').setAttribute('href','#i-chronicle');
  $('#new-chat').title='Chronicle · new conversation (Ctrl+K)';
  $('#attempts').setAttribute('aria-label','Conversations');
  $('#chat-text').placeholder='Write to Eregion…';
  const errands=['Seek new roles','Weigh my fit','Forge a resume','Practise an interview'];
  [...$('#starters').children].forEach((chip,i)=>{if(errands[i])chip.textContent=errands[i];});
  // Council opens the conversation you were in, or the most recent one.
  $('#nav-council').addEventListener('click',event=>{event.preventDefault();const target=selected||attempts.find(a=>a.career&&!a.archived_at)?.run_id;if(target)select(target);else reset();});
  chronicleSignature='';render();
}
