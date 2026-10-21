// Scheduling is optional. On-demand discovery remains an ordinary chat request.
import {api,toast} from './conversations.js';
import {renderMarkdown} from './chat-markdown.js';
import {t,tn,tServer,locale} from './i18n.js';

const endpoint='/api/career/alerts';
const configKeys=['name','preferences','sites','local_time','weekdays','timezone','max_results','min_score','enabled','visual_fallback'];
const dayNames=['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map(day=>t(day));
const localDate=value=>new Date(value).toLocaleString(locale,{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const configOf=s=>Object.fromEntries(configKeys.map(k=>[k,k==='weekdays'?(s[k]??[0,1,2,3,4,5,6]):s[k]]));
let updatePage=()=>{};
const stored=key=>{try{return localStorage.getItem(key);}catch{return null;}};

export function startAlertNotifications({open,onUpdate}){
  let stopped=false,timer;
  const tick=async()=>{
    try{
      const data=await api(endpoint);if(stopped)return;
      onUpdate(data);
      updatePage(data);
      if(stored('northstar.jobNotifications')==='on'&&'Notification' in window&&Notification.permission==='granted'){
        for(const digest of [...data.digests].reverse()){
          if(!digest.finished_at||digest.read_at)continue;
          const key=`northstar.jobNotification.${digest.run_id}`;
          if(stored(key))continue;
          const notification=new Notification(digest.recommendations.length?t('Your job matches are ready'):t('Your job search has an update'),{
            body:`${digest.config.name} · ${tn(digest.recommendations.length,'{n} new recommendation','{n} new recommendations')}`,tag:digest.run_id});
          localStorage.setItem(key,'shown');
          notification.onclick=()=>{window.focus();open();notification.close();};
        }
      }
    }catch{/* Digests stay on the server when notification delivery is unavailable. */}
    finally{if(!stopped)timer=setTimeout(tick,15000);}
  };
  tick();return ()=>{stopped=true;clearTimeout(timer);};
}

export async function jobAlertsPage(ctx,container,header){
  const {el,button}=ctx;
  const top=header(ctx,t('Job alerts'),[button(t('Find jobs now'),()=>ctx.send(t('Find and recommend jobs that match my resume and preferences.')),'primary-button')]);
  const body=el('div','','page-body narrow');
  container.replaceChildren(top,body);
  let data,signature='';
  function receive(next){
    const nextSignature=JSON.stringify(next);
    if(nextSignature===signature)return;
    data=next;signature=nextSignature;draw();
  }
  updatePage=next=>{if(body.isConnected&&!container.hidden)receive(next);};
  async function load(){
    try{receive(await api(endpoint));}catch(e){ctx.error(e.message);}
  }
  async function notificationPermission(){
    if(!('Notification' in window)){toast(t('This browser does not support notifications. Your digests are saved here.'));return;}
    try{
      if(stored('northstar.jobNotifications')==='on'){
        localStorage.removeItem('northstar.jobNotifications');draw();return;
      }
      const permission=await Notification.requestPermission();
      if(permission==='granted')localStorage.setItem('northstar.jobNotifications','on');
      else toast(t('Browser notifications are off. You can still read every digest here.'));
      draw();
    }catch{toast(t('Browser notifications could not be enabled. Your digests are saved here.'));}
  }
  function draw(){
    body.replaceChildren();
    body.append(el('p',t('Ask in chat whenever you want a fresh search. Add a schedule only when you want Eregion to look for new roles regularly.'),'alerts-intro'));
    const actions=el('div','','alerts-toolbar');
    actions.append(button(t('New schedule'),()=>edit(),data.schedules.length?'text-button':'primary-button'),
      button(stored('northstar.jobNotifications')==='on'?t('Turn off browser notifications'):t('Enable browser notifications'),notificationPermission),
      button(t('Refresh'),load));body.append(actions);
    body.append(el('p',t('Scheduled searches run while this computer and the backend are running. Searches start at your chosen time; the digest arrives when research finishes. Browser popups need this page open and notification permission.'),'alerts-note'));
    for(const schedule of data.schedules){
      const card=el('article','','card alert-schedule');
      const days=schedule.weekdays??[0,1,2,3,4,5,6];
      const cadence=days.length===7?t('Every day'):days.map(i=>dayNames[i]).join(t(', '));
      card.append(el('h3',schedule.name),el('p',`${schedule.enabled?cadence:t('Paused')} · ${schedule.local_time} · ${schedule.timezone}`,'sub'),
        el('p',schedule.preferences,'alert-preferences'));
      if(schedule.enabled)card.append(el('p',t('Next search: {date}',{date:localDate(schedule.next_run_at)}),'stage'));
      const controls=el('div','','actions');
      const run=button(t('Search now'),async()=>{
        run.disabled=true;
        try{await api(`${endpoint}/${schedule.alert_id}/run`,{method:'POST',body:{idempotency_key:crypto.randomUUID()}});toast(t('Search started. You can follow it in the conversation.'));await ctx.refresh();await load();}
        catch(e){ctx.error(e.message);}finally{run.disabled=false;}
      },'primary-button');
      controls.append(run,button(t('Edit'),()=>edit(schedule)),button(schedule.enabled?t('Pause schedule'):t('Resume schedule'),async()=>{
        try{await api(`${endpoint}/${schedule.alert_id}`,{method:'PUT',body:{expected_version:schedule.version,config:{...configOf(schedule),enabled:!schedule.enabled}}});await load();}
        catch(e){ctx.error(e.message);}
      }));card.append(controls);body.append(card);
    }
    body.append(el('h2',t('Search updates'),'alerts-section-title'));
    if(!data.digests.length)body.append(el('p',t('Scheduled recommendations will appear here and in their conversations.'),'page-empty'));
    for(const digest of data.digests){
      const block=el('article','','card alert-digest');
      block.append(el('p',`${localDate(digest.created_at)} · ${tServer(digest.status.replaceAll('_',' '))}`,'eyebrow'),el('h3',digest.config.name));
      block.append(el('p',tn(digest.recommendations.length,'{n} new recommendation','{n} new recommendations'),'sub'));
      if(digest.summary)block.append(renderMarkdown(digest.summary));
      for(const job of digest.recommendations){
        const row=el('div','','digest-job');
        const score=job.match_score==null?(job.score_range?.length===2?`${job.score_range.map(Math.round).join('–')}/100`:t('Unresolved')):`${Math.round(job.match_score)}/100`;
        row.append(el('strong',score,'digest-score'),el('h4',job.title),
          el('p',[job.company,job.location].filter(Boolean).join(' · '),'sub'),el('p',job.reason));
        if(job.caveats)row.append(el('p',job.caveats,'sub'));
        const link=el('a',t('Job posting ↗'),'quiet-button');link.href=job.url;link.target='_blank';link.rel='noopener noreferrer';row.append(link);block.append(row);
      }
      const controls=el('div','','actions');
      if(digest.task_id)controls.append(button(t('Open conversation'),async()=>{
        await ctx.refresh();ctx.open(digest.task_id);
        await api(`${endpoint}/digests/${digest.run_id}/read`,{method:'POST'}).catch(()=>{});
      },'primary-button'));
      if(!digest.read_at&&digest.finished_at)controls.append(button(t('Mark read'),async()=>{
        try{await api(`${endpoint}/digests/${digest.run_id}/read`,{method:'POST'});await load();}catch(e){ctx.error(e.message);}
      }));
      block.append(controls);body.append(block);
    }
  }
  function edit(schedule){
    const dialog=el('dialog','','alert-settings'),form=el('form');
    const title=el('div','','dialog-heading');title.append(el('h2',schedule?t('Edit scheduled search'):t('New scheduled search')));
    title.append(button('×',()=>dialog.close(),'icon-button'));form.append(title);
    const defaults={name:t('Daily matches'),preferences:'',sites:[],weekdays:[0,1,2,3,4,5,6],local_time:'09:00',timezone:Intl.DateTimeFormat().resolvedOptions().timeZone||'UTC',
      max_results:5,min_score:70,enabled:true,visual_fallback:false,...(schedule?configOf(schedule):{})};
    const inputs={};
    function field(key,label,type='text'){
      const row=el('label',t(label)),input=el(type==='textarea'?'textarea':'input');if(type!=='textarea')input.type=type;
      input.name=key;input.value=key==='sites'?defaults[key].join('\n'):String(defaults[key]);
      input.required=key!=='sites';row.append(input);form.append(row);inputs[key]=input;return input;
    }
    field('name','Name').maxLength=100;
    field('preferences','Roles, location and what matters to you','textarea').maxLength=3000;
    const sites=field('sites','Preferred websites (optional, one HTTPS URL per line)','textarea');sites.placeholder='https://careers.example.com/';
    field('local_time','Start searching at','time');field('timezone','Timezone');
    const weekdays=el('fieldset'),legend=el('legend',t('Search on'));weekdays.append(legend);
    const dayInputs=dayNames.map((day,index)=>{
      const row=el('label','','check-row'),input=el('input');input.type='checkbox';input.name='weekdays';input.value=String(index);
      input.checked=defaults.weekdays.includes(index);row.append(input,document.createTextNode(day));weekdays.append(row);return input;
    });form.append(weekdays);
    const count=field('max_results','Maximum recommendations','number');count.min=1;count.max=10;
    const score=field('min_score','Minimum match score','number');score.min=0;score.max=100;
    for(const [key,label] of [['enabled','Enable scheduled search'],['visual_fallback','Allow non-login page screenshots to the model for visual browser backup']]){
      const row=el('label','','check-row'),input=el('input');input.type='checkbox';input.checked=defaults[key];inputs[key]=input;row.append(input,document.createTextNode(t(label)));form.append(row);
    }
    const failure=el('p','','danger');failure.setAttribute('role','alert');form.append(failure);
    const controls=el('div','','dialog-actions'),save=el('button',t('Save schedule'),'primary-button');save.type='submit';controls.append(save);form.append(controls);
    const idempotencyKey=crypto.randomUUID();
    form.addEventListener('submit',async event=>{
      event.preventDefault();save.disabled=true;failure.textContent='';
      const config={...defaults};
      for(const [key,input] of Object.entries(inputs))config[key]=input.type==='checkbox'?input.checked:input.type==='number'?Number(input.value):input.value.trim();
      config.sites=config.sites.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
      config.weekdays=dayInputs.filter(input=>input.checked).map(input=>Number(input.value));
      if(!config.weekdays.length){failure.textContent=t('Choose at least one day.');save.disabled=false;return;}
      try{await api(schedule?`${endpoint}/${schedule.alert_id}`:endpoint,{method:schedule?'PUT':'POST',body:{config,
        ...(schedule?{expected_version:schedule.version}:{idempotency_key:idempotencyKey})}});dialog.close();await load();}
      catch(e){failure.textContent=e.message;}finally{save.disabled=false;}
    });
    dialog.addEventListener('close',()=>dialog.remove());dialog.append(form);document.body.append(dialog);dialog.showModal();
  }
  await load();
}
