// The Road (map, ledger and board) and The Forge (materials library).
import {api,openMenu,toast} from './conversations.js';
import {t,tn,locale} from './i18n.js';

const local=o=>Object.fromEntries(Object.entries(o).map(([k,v])=>[k,t(v)]));
const LABELS=local({saved:'Saved',analyzing:'Analyzing',analysis_failed:'Analysis failed',needs_evidence:'Needs evidence',
  materials_ready:'Materials ready',ready_to_apply:'Ready to apply',applied:'Applied',interviewing:'Interviewing',
  offer:'Offer',rejected:'Rejected',withdrawn:'Withdrawn',archived:'Archived'});
const COLUMNS=[['Sighted',['saved','analyzing','analysis_failed']],['Prepared',['needs_evidence','materials_ready','ready_to_apply']],
  ['Sent',['applied']],['In interview',['interviewing']],['Offer',['offer']],['Rejected',['rejected']]].map(([title,statuses])=>[t(title),statuses]);
const CLOSED_COLUMN=[t('Withdrawn & archived'),['withdrawn','archived']];
const fmtDate=(iso,opts={month:'short',day:'numeric'})=>{try{return new Date(iso).toLocaleDateString(locale,opts);}catch{return '';}};
function relative(iso){
  const days=Math.floor((Date.now()-Date.parse(iso))/864e5);
  return days<=0?t('Today'):days===1?t('Yesterday'):days<7?t('{n}d ago',{n:days}):fmtDate(iso);
}
// ---- The trail: one line for every process ----
const STATIONS=[['Sighted',['saved','analyzing','analysis_failed']],['Prepared',['needs_evidence','materials_ready','ready_to_apply']],
  ['Sent',['applied']],['In interview',['interviewing']],['Offer',['offer']]].map(([name,statuses])=>[t(name),statuses]);
const STATION_OF=Object.fromEntries(STATIONS.flatMap(([,statuses],i)=>statuses.map(s=>[s,i])));
const REACHED={rejected:2,withdrawn:2,archived:0};
function trail(el,count,{at,state='now',labels=[]}){
  const ol=el('ol','',labels.length?'trail':'trail compact');
  for(let i=0;i<count;i++){
    const li=el('li','',`st ${i<at?'done':i===at?state:''}`);li.append(el('i'));
    if(labels[i])li.append(el('span',labels[i]));ol.append(li);
  }
  return ol;
}
function roadTrail(el,app,waiting){
  const closed=app.status in REACHED,at=closed?REACHED[app.status]:STATION_OF[app.status]??0;
  return trail(el,STATIONS.length,{at,state:closed?'done':waiting?'wait':app.status==='offer'?'gold':'now'});
}
function remember(key,fallback){try{return localStorage.getItem(key)||fallback;}catch{return fallback;}}
function store(key,value){try{localStorage.setItem(key,value);}catch{/* Storage is optional. */}}

// ================= Applications =================
export async function applicationsPage(ctx,container,header){
  const {el,button,icon}=ctx;
  let mode=remember('northstar.applicationsMode','overview'),weeks=Number(remember('northstar.applicationsWeeks','10')),filter='active',data=null,showClosed=false;
  const seg=el('div','','segmented');seg.setAttribute('role','group');seg.setAttribute('aria-label',t('Layout'));
  const modes={overview:button(t('Map'),()=>setMode('overview'),''),board:button(t('Board'),()=>setMode('board'),'')};
  seg.append(modes.overview,modes.board);
  const range=el('select','','page-select');range.setAttribute('aria-label',t('Time range'));
  for(const n of [4,10,26]){const o=el('option',t('Last {n} weeks',{n}));o.value=String(n);range.append(o);}range.value=String(weeks);
  range.addEventListener('change',()=>{weeks=Number(range.value);store('northstar.applicationsWeeks',range.value);load();});
  const closedToggle=el('label','','page-check');const closedBox=el('input');closedBox.type='checkbox';
  closedToggle.append(closedBox,el('span',t('Show withdrawn & archived')));closedBox.addEventListener('change',()=>{showClosed=closedBox.checked;draw();});
  const top=header(ctx,t('The Road'),[range,closedToggle]);top.querySelector('h1').after(el('span',t('every application, from first sight to offer'),'page-sub'),seg);
  const body=el('div','','page-body');container.replaceChildren(top,body);

  function setMode(next){mode=next;store('northstar.applicationsMode',next);draw();}
  async function load(){
    try{data=await api(`/api/applications/overview?weeks=${weeks}`);}catch(e){ctx.error(e.message);data=null;}
    draw();
  }
  function draw(){
    for(const [name,b] of Object.entries(modes))b.setAttribute('aria-pressed',String(mode===name));
    range.hidden=mode!=='overview';closedToggle.hidden=mode!=='board';
    body.replaceChildren();
    if(!data){body.append(el('p',t('Applications could not be loaded.'),'page-empty'));return;}
    if(!data.applications.length){
      const empty=el('div','','page-empty');empty.append(el('p',t('No applications yet. Share a job link in a conversation and it will appear here.')));
      empty.append(button(t('Start a conversation'),()=>ctx.send(''),'primary-button'));body.append(empty);return;
    }
    mode==='board'?drawBoard():drawOverview();
  }

  function drawOverview(){
    const needs=ctx.needsYou(),totals=data.totals,waiting=data.applications.filter(a=>needs.has(a.application_id)).length;
    const figs=el('div','','figs');
    for(const [value,label,cls] of [[totals.tracked,'Set out'],[totals.applied,'Sent'],[totals.interviewing,'In interview'],
      [totals.response_rate==null?'–':`${totals.response_rate}`,'Heard back',totals.response_rate==null?'':'pct'],[waiting,'Beacons lit','beacon']]){
      const f=el('div','',`fig ${cls||''}`),num=el('b',String(value));if(cls==='pct')num.append(el('small','%'));
      f.append(num,el('span',t(label)));
      if(cls==='beacon'&&waiting){f.classList.add('link');f.tabIndex=0;f.setAttribute('role','button');
        const go=()=>{filter='needs';draw();body.querySelector('.ftabs')?.scrollIntoView({behavior:'smooth'});};f.addEventListener('click',go);f.addEventListener('keydown',e=>{if(e.key==='Enter')go();});}
      figs.append(f);
    }
    const lower=el('div','','road-lower');lower.append(tableSection(needs),weeklyChart());
    body.append(figs,roadMap(needs),lower);
  }

  // The road: five stations along one winding line; each application is a stone at its station.
  function roadMap(needs){
    const NS='http://www.w3.org/2000/svg',wrap=el('div','','road-map');
    const svg=document.createElementNS(NS,'svg');svg.setAttribute('viewBox','0 0 1180 250');svg.setAttribute('preserveAspectRatio','none');svg.setAttribute('aria-hidden','true');
    const segments=['M20 190 C150 190 170 110 280 110','S450 200 560 180','S720 60 830 80','S1000 160 1160 110'];
    const points=[[66,186],[280,110],[560,180],[830,80],[1100,122]],tops=[0,136,0,114,146];
    const open=data.applications.filter(a=>a.status in STATION_OF);
    const at=STATIONS.map(([,statuses])=>open.filter(a=>statuses.includes(a.status)));
    const furthest=at.map(list=>list.length>0).lastIndexOf(true);
    const path=(d,cls)=>{const p=document.createElementNS(NS,'path');p.setAttribute('d',d);p.setAttribute('class',cls);svg.append(p);};
    path(segments.join(' '),'road-ahead');
    if(furthest>0)path(segments.slice(0,furthest).join(' '),'road-travelled');
    points.forEach(([x,y],i)=>{
      if(i===points.length-1){path(`M${x} ${y-14}v28M${x-14} ${y}h28M${x-10} ${y-10}l20 20M${x+10} ${y-10}l-20 20`,'road-offer');return;}
      const c=document.createElementNS(NS,'circle');c.setAttribute('cx',x);c.setAttribute('cy',y);c.setAttribute('r',5);
      c.setAttribute('class',i<=furthest?'road-stop travelled':'road-stop');svg.append(c);
    });
    wrap.append(svg);
    STATIONS.forEach(([name],i)=>{
      const list=at[i].sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
      const station=el('div','',`station${i===4?' offer':''}`);station.style.left=`${points[i][0]/11.8}%`;station.style.top=`${tops[i]}px`;
      station.append(el('div',name,'t'),el('div',list.length?t(['{n} saved','{n} being forged','{n} awaiting reply','{n} in conversation','{n} in hand'][i],{n:list.length}):t('none yet'),'c'));
      const stones=el('div','','stones');
      for(const app of list.slice(0,3)){
        const stone=button((app.company||app.title||'?').trim()[0].toUpperCase(),()=>ctx.openApplication(app.application_id),`stone${needs.has(app.application_id)?' bc':''}${i===4?' gold':''}`);
        stone.title=`${app.company||t('Application')} · ${app.title||''}`;stone.setAttribute('aria-label',t('Open {name}',{name:app.company||t('application')}));stones.append(stone);
      }
      if(list.length>3)stones.append(el('span',`+${list.length-3}`,'stone more'));
      station.append(stones);wrap.append(station);
    });
    return wrap;
  }

  function chartShell(title,note,draw,rows){
    const card=el('section','','chart'),head=el('div','','chart-head'),table=button(t('Table'),()=>{showTable=!showTable;render();},'text-button chart-toggle');
    let showTable=false;head.append(el('h3',title),el('span',note,'chart-note'),table);
    const area=el('div');card.append(head,area);
    function render(){table.textContent=showTable?t('Chart'):t('Table');table.setAttribute('aria-pressed',String(showTable));area.replaceChildren(showTable?dataTable(rows):draw());}
    render();return card;
  }
  function dataTable(rows){
    const tableNode=el('table','','mini-table'),thead=el('thead'),tr=el('tr');
    for(const h of rows[0])tr.append(el('th',h));thead.append(tr);tableNode.append(thead);
    const tbody=el('tbody');for(const r of rows.slice(1)){const row=el('tr');for(const c of r)row.append(el('td',String(c)));tbody.append(row);}
    tableNode.append(tbody);return tableNode;
  }
  function weeklyChart(){
    const counts=data.weekly.map(w=>w.count),max=Math.max(1,...counts),total=counts.reduce((a,b)=>a+b,0);
    const rows=[[t('Week of'),t('Applications sent')],...data.weekly.map(w=>[fmtDate(w.week_start),w.count])];
    return chartShell(t('Sent per week'),t('{n} total',{n:total}),()=>{
      const wrap=el('div','','weeks-wrap'),plot=el('div','','weeks'),labels=el('div','','wl'),tip=el('div','','chart-tip');tip.hidden=true;
      const every=data.weekly.length>12?4:2;
      data.weekly.forEach((w,i)=>{
        const last=i===data.weekly.length-1,col=el('div','','week'),bar=el('i','',last?'now':'');
        bar.style.height=w.count?`${100*w.count/max}%`:'2px';
        if(last&&w.count)bar.append(el('em',String(w.count)));
        col.tabIndex=0;col.setAttribute('aria-label',t('Week of {date}: {n} sent',{date:fmtDate(w.week_start),n:w.count}));
        const show=()=>{const end=new Date(Date.parse(w.week_start)+6*864e5);tip.replaceChildren(el('b',`${fmtDate(w.week_start)} – ${fmtDate(end)}`),el('span',tn(w.count,'{n} application sent','{n} applications sent')));
          tip.hidden=false;const r=col.getBoundingClientRect(),p=wrap.getBoundingClientRect();tip.style.left=`${Math.min(r.left-p.left+r.width/2,p.width-90)}px`;tip.style.top=`${Math.max(0,r.top-p.top+col.offsetHeight-bar.offsetHeight-58)}px`;col.classList.add('hover');};
        const hide=()=>{tip.hidden=true;col.classList.remove('hover');};
        col.addEventListener('pointerenter',show);col.addEventListener('pointerleave',hide);col.addEventListener('focus',show);col.addEventListener('blur',hide);
        col.append(bar);plot.append(col);
        // The neighbour of "This week" stays blank so the two labels never collide.
        labels.append(el('span',last?t('This week'):i%every===0&&i<data.weekly.length-2?fmtDate(w.week_start):''));
      });
      wrap.append(plot,labels,tip);return wrap;
    },rows);
  }

  function tableSection(needs){
    const section=el('section','','app-table'),tabs=el('div','','ftabs'),apps=data.applications;
    const groups={active:a=>!['rejected','withdrawn','archived'].includes(a.status),all:a=>a.status!=='archived',
      needs:a=>needs.has(a.application_id),interviewing:a=>a.status==='interviewing',rejected:a=>a.status==='rejected',withdrawn:a=>a.status==='withdrawn'};
    const names=local({active:'Active',all:'All',needs:'Need you',interviewing:'Interviewing',rejected:'Rejected',withdrawn:'Withdrawn'});
    for(const [key,test] of Object.entries(groups)){
      const n=apps.filter(test).length;if(key==='needs'&&!n)continue;
      const b=button('',()=>{filter=key;draw();},key===filter?'on':'');b.append(el('span',names[key]),el('b',String(n)));b.setAttribute('aria-pressed',String(key===filter));tabs.append(b);
    }
    const rows=apps.filter(groups[filter]||groups.active).sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
    const table=el('div','','ledger'),head=el('div','','lhead');
    const stations=el('span','','stations');for(const [name] of STATIONS)stations.append(el('span',name));
    head.append(el('span',t('Company')),stations,el('span',t('Fit')),el('span',t('Last step')));table.append(head);
    for(const a of rows){
      const row=el('div','',`lrow${['rejected','withdrawn','archived'].includes(a.status)?' closed':''}`),co=el('div','','co');
      const name=el('b',a.company||t('Unknown company'));if(needs.has(a.application_id))name.append(el('span','','dot'));
      co.append(name,el('span',`${a.title||'—'}${a.status in REACHED?` · ${LABELS[a.status]}`:''}`));
      row.append(co,roadTrail(el,a,needs.has(a.application_id)),el('span',a.match_score==null?'—':String(Math.round(a.match_score)),'fit'),el('span',relative(a.updated_at),'when'));
      row.tabIndex=0;row.setAttribute('role','link');row.setAttribute('aria-label',t('Open {name} {title}, {status}',{name:a.company||t('application'),title:a.title||'',status:LABELS[a.status]||a.status}));
      const open=()=>ctx.openApplication(a.application_id);row.addEventListener('click',open);row.addEventListener('keydown',e=>{if(e.key==='Enter')open();});
      table.append(row);
    }
    section.append(tabs,rows.length>0?table:el('p',t('Nothing here.'),'page-empty'));return section;
  }

  // ---- Board ----
  async function move(app,target){
    try{
      await api(`/api/applications/${encodeURIComponent(app.application_id)}/status`,{method:'PATCH',
        body:{target_status:target,expected_version:app.version,...(target==='applied'?{applied_at:new Date().toISOString()}:{})}});
      toast(t('{name} moved to {status}',{name:app.company||t('Application'),status:LABELS[target]}));
    }catch(e){toast(t('Couldn’t move {name}: {error}',{name:app.company||t('this application'),error:e.message}));}
    await load();
  }
  function cardMenu(app,anchor){
    const moves=(app.next_statuses||[]).filter(s=>s!=='analyzing').map(s=>({label:t('Move to {status}',{status:LABELS[s]}),action:()=>move(app,s),danger:s==='archived'}));
    openMenu(anchor,[{label:t('Open conversation'),action:()=>ctx.openApplication(app.application_id)},...(moves.length?[{separator:true},...moves]:[])]);
  }
  function drawBoard(){
    const needs=ctx.needsYou(),board=el('div','','board'),columns=showClosed?[...COLUMNS,CLOSED_COLUMN]:COLUMNS;
    board.style.setProperty('--cols',String(columns.length));
    let dragging=null;
    const cols=columns.map(([title,statuses])=>{
      const col=el('section','','bcol'),items=data.applications.filter(a=>statuses.includes(a.status)).sort((a,b)=>b.updated_at.localeCompare(a.updated_at));
      const h=el('h4',title);h.append(el('span',String(items.length)));col.append(h);
      const list=el('div','','bcol-list');col.append(list);
      col.addEventListener('dragover',e=>{if(dragging&&statuses.some(s=>dragging.next_statuses.includes(s))){e.preventDefault();col.classList.add('drop-ok');}});
      col.addEventListener('dragleave',e=>{if(!col.contains(e.relatedTarget))col.classList.remove('drop-ok');});
      col.addEventListener('drop',e=>{e.preventDefault();col.classList.remove('drop-ok');const target=statuses.find(s=>dragging?.next_statuses.includes(s));if(dragging&&target)move(dragging,target);});
      for(const a of items){
        const card=el('article','',['rejected','withdrawn','archived'].includes(a.status)?'bcard closed':'bcard');card.draggable=true;
        if(needs.has(a.application_id))card.append(el('span','','dot'));
        const more=button('',()=>cardMenu(a,more),'more');more.append(icon('dots'));more.setAttribute('aria-label',t('Options for {name}',{name:a.company||t('application')}));more.setAttribute('aria-haspopup','menu');
        const open=button('',()=>ctx.openApplication(a.application_id),'bcard-open');open.append(el('b',a.company||t('Unknown company')),el('span',a.title||'','role'));
        const foot=el('div','','ft');
        foot.append(el('span',a.match_score==null?LABELS[a.status]:t('Match {n}',{n:Math.round(a.match_score)})),el('span',relative(a.updated_at)));
        card.append(open,foot,more);
        card.addEventListener('dragstart',e=>{dragging=a;e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain',a.application_id);card.classList.add('dragging');
          for(const [c,[,s]] of cols.map((c,i)=>[c,columns[i]]))c.classList.toggle('drop-blocked',!s.some(x=>a.next_statuses.includes(x))&&!s.includes(a.status));});
        card.addEventListener('dragend',()=>{dragging=null;card.classList.remove('dragging');for(const c of cols)c.classList.remove('drop-blocked','drop-ok');});
        list.append(card);
      }
      if(!items.length)list.append(el('p','—','bcol-empty'));
      return col;
    });
    board.append(...cols);
    body.append(el('p',t('Drag a card to update its stage, or use its ⋯ menu. Only moves the application allows are accepted.'),'page-note'),board);
  }
  await load();
}

// ================= Materials =================
export async function materialsPage(ctx,container,header){
  const {el,button,icon}=ctx;
  let items=[],typeFilter='all',statusFilter='',query='',selected=null,candidate=null,detail=null,versionIndex=null;
  const search=el('input','','page-search');search.type='search';search.placeholder=t('Search materials');search.setAttribute('aria-label',t('Search materials'));
  const upload=button('',()=>document.querySelector('#resume-file')?.click(),'text-button');upload.append(icon('plus'),el('span',t('Upload resume')));
  const top=header(ctx,t('The Forge'),[search,upload]);top.querySelector('h1').after(el('span',t('every document, drafted, tempered and sealed'),'page-sub'));
  const layout=el('div','','mat'),filters=el('nav','','mfil'),list=el('div','','mlist'),preview=el('aside','','mprev');
  filters.setAttribute('aria-label',t('Filter materials'));layout.append(filters,list,preview);container.replaceChildren(top,layout);
  search.addEventListener('input',()=>{query=search.value.trim().toLowerCase();drawList();});

  const status=m=>m.stale?'outdated':m.status==='approved'?'approved':m.status==='awaiting_review'?'review':m.status==='failed'?'failed':'draft';
  const statusText=local({approved:'Approved',review:'Needs review',outdated:'Outdated',failed:'Needs attention',draft:'In progress'});
  // Drafted → Tempered (your review) → Sealed (approved). Outdated work returns to the anvil.
  const FORGE=['Drafted','Tempered','Sealed'].map(name=>t(name));
  const forgeAt={draft:[0,'now'],failed:[0,'wait'],review:[1,'wait'],outdated:[1,'wait'],approved:[2,'gold']};
  const forgeTrail=(m,labelled)=>{const [at,state]=forgeAt[status(m)];return trail(el,3,{at,state,labels:labelled?FORGE:[]});};
  async function load(){
    try{
      const [mats,cand]=await Promise.all([api('/api/materials'),api('/api/auto-apply/candidate').catch(()=>null)]);
      items=mats.materials;candidate=cand?.resume_base64?cand:null;
    }catch(e){ctx.error(e.message);items=[];}
    if(selected&&!items.some(m=>m.pack_item_id===selected.pack_item_id))selected=null;
    if(!selected&&items.length)await choose(items[0],false);
    drawFilters();drawList();drawPreview();
  }
  function drawFilters(){
    filters.replaceChildren();
    const types=[['all','All',items.length+(candidate?1:0)],['base','Base resume',candidate?1:0],['tailored_resume','Tailored resumes'],['cover_letter','Cover letters'],['application_answer','Application answers']].map(([key,label,n])=>[key,t(label),n]);
    for(const [key,label,n] of types){
      const count=n??items.filter(m=>m.artifact_type===key).length;if(key==='application_answer'&&!count)continue;
      const b=button('',()=>{typeFilter=key;drawFilters();drawList();},typeFilter===key?'on':'');b.append(el('span',label),el('span',String(count),'n'));b.setAttribute('aria-pressed',String(typeFilter===key));filters.append(b);
    }
    filters.append(el('p',t('Status'),'side-label'));
    for(const key of ['approved','review','outdated']){
      const count=items.filter(m=>status(m)===key).length;
      const b=button('',()=>{statusFilter=statusFilter===key?'':key;drawFilters();drawList();},statusFilter===key?'on':'');b.append(el('span',statusText[key]),el('span',String(count),'n'));b.setAttribute('aria-pressed',String(statusFilter===key));filters.append(b);
    }
  }
  const thumb=()=>{const t=el('span','','thumb');for(const w of [100,100,70,100])t.append(Object.assign(el('i'),{style:`width:${w}%`}));return t;};
  function drawList(){
    list.replaceChildren();
    if(candidate&&['all','base'].includes(typeFilter)&&!statusFilter&&(!query||candidate.resume_name.toLowerCase().includes(query))){
      list.append(el('p',t('Base'),'mg'));
      const row=button('',()=>{selected={base:true};detail=null;drawList();drawPreview();},selected?.base?'mi on':'mi');
      const text=el('span','','t');text.append(el('b',candidate.resume_name||'resume.pdf'),el('small',t('Your original resume · used for every application')));
      row.append(thumb(),text);list.append(row);
    }
    const visible=typeFilter==='base'?[]:items.filter(m=>(typeFilter==='all'||m.artifact_type===typeFilter)&&(!statusFilter||status(m)===statusFilter)
      &&(!query||`${m.label} ${m.company} ${m.title} ${m.source_question||''}`.toLowerCase().includes(query)));
    let group='';
    for(const m of visible){
      const g=`${m.company||t('Unknown company')} · ${m.title||''}`;if(g!==group){group=g;list.append(el('p',g,'mg'));}
      const row=button('',()=>choose(m),selected?.pack_item_id===m.pack_item_id?'mi on':'mi'),text=el('span','','t');
      text.append(el('b',m.artifact_type==='application_answer'&&m.source_question?m.source_question:m.label),el('small',`${m.document_version==null?t('Draft'):t('Version {n}',{n:m.document_version})} · ${relative(m.updated_at)}`));
      row.append(thumb(),text,forgeTrail(m,false),el('span',statusText[status(m)],`st ${status(m)}`));list.append(row);
    }
    if(!visible.length&&!(candidate&&['all','base'].includes(typeFilter)))list.append(el('p',items.length?t('No materials match.'):t('No materials yet. Ask for a tailored resume in a conversation.'),'page-empty'));
  }
  async function choose(m,draw=true){
    selected=m;detail=null;versionIndex=null;if(draw){drawList();drawPreview();}
    try{detail=await api(`/api/materials/${encodeURIComponent(m.pack_id)}/${encodeURIComponent(m.pack_item_id)}`);}catch(e){ctx.error(e.message);}
    if(selected===m&&draw)drawPreview();
  }
  function drawPreview(){
    preview.replaceChildren();
    if(!selected){preview.append(el('p',t('Select a document to preview it.'),'page-empty'));return;}
    if(selected.base){
      preview.append(el('h2',candidate.resume_name||'resume.pdf'),el('p',t('Your original resume. Tailored versions always trace back to it.'),'sub'));
      const actions=el('div','','actions');
      const download=button('',()=>{const bytes=Uint8Array.from(atob(candidate.resume_base64),c=>c.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:'application/pdf'}));
        const a=Object.assign(document.createElement('a'),{href:url,download:candidate.resume_name||'resume.pdf'});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);},'primary-button');
      download.append(icon('dl'),el('span',t('Download PDF')));
      actions.append(download,button(t('Replace'),()=>document.querySelector('#resume-file')?.click(),'secondary-button'));preview.append(actions);return;
    }
    const m=selected;
    preview.append(el('h2',m.artifact_type==='application_answer'&&m.source_question?m.source_question:m.label),el('p',[m.company,m.title].filter(Boolean).join(' · '),'sub'));
    const versions=detail?.versions||[];const shown=versions[versionIndex??versions.length-1];
    const vers=el('div','','vers');
    versions.forEach((v,i)=>{const b=button(`v${v.version}${i===versions.length-1?t(' · current'):''}`,()=>{versionIndex=i;drawPreview();},(versionIndex??versions.length-1)===i?'on':'');vers.append(b);});
    if(m.verified!=null)vers.append(el('span',m.verified?t('Every claim verified against your resume'):tn(m.issues,'{n} claim needs attention','{n} claims need attention'),m.verified?'verify':'verify warn'));
    preview.append(forgeTrail(m,true),vers);
    const sheet=el('div','','sheet vellum');sheet.append(el('pre',shown?.text||(detail?t('This version has no text yet.'):t('Loading…'))));preview.append(sheet);
    const actions=el('div','','actions'),s=status(m);
    if(s==='review'){const approve=button(t('Approve this version'),async()=>{approve.disabled=true;
      try{await api(`/api/packs/${encodeURIComponent(m.pack_id)}/items/${encodeURIComponent(m.pack_item_id)}/approve`,{method:'POST',body:{expected_version:m.version,idempotency_key:`library-approve:${m.pack_item_id}:${m.version}`}});toast(t('Approved'));await load();}
      catch(e){ctx.error(e.message);approve.disabled=false;}},'primary-button');actions.append(approve);}
    if(s==='outdated'){const regen=button(t('Regenerate'),async()=>{regen.disabled=true;regen.textContent=t('Regenerating…');
      try{await api(`/api/packs/${encodeURIComponent(m.pack_id)}/items/${encodeURIComponent(m.pack_item_id)}/regenerate`,{method:'POST',body:{expected_version:m.version,idempotency_key:`library-regenerate:${m.pack_item_id}:${m.version}`}});toast(t('Regenerated'));await load();}
      catch(e){ctx.error(e.message);regen.disabled=false;regen.textContent=t('Regenerate');}},'primary-button');actions.append(regen);}
    if(m.pdf){const a=el('a','','secondary-button');a.href=m.pdf;a.append(icon('dl'),el('span',t('Download PDF')));actions.append(a);}
    actions.append(button(t('Open conversation'),()=>ctx.openApplication(m.application_id),'secondary-button'),
      button(t('Copy text'),async()=>{try{await navigator.clipboard.writeText(shown?.text||'');toast(t('Copied'));}catch{toast(t('Select the text to copy it'));}},'secondary-button'));
    if(s==='review')actions.append(button(t('Request changes'),()=>ctx.openApplication(m.application_id,t('I want to revise the {label} for {company}. Ask me what I would like to change.',{label:m.label.toLowerCase(),company:m.company||t('this job')})),'secondary-button'));
    preview.append(actions);
  }
  await load();
}
