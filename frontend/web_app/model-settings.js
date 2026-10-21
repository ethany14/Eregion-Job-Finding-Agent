// Model, API key and feature settings. Keys are write-only: the server returns only whether one is
// saved and its last characters, so a key field starts empty and an empty field keeps the saved key.
import {api,toast} from './conversations.js';
import {t,tServer} from './i18n.js';

const endpoint='/api/settings/model';
const el=(tag,text='',cls='')=>{const n=document.createElement(tag);n.textContent=text;n.className=cls;return n;};
const button=(text,cls='text-button')=>{const b=el('button',text,cls);b.type='button';return b;};

function showState(trigger,settings){
  const needed=!settings?.configured;
  trigger.dataset.state=needed?'needed':'ready';
  trigger.querySelector('.setup-needed').hidden=!needed;
  trigger.title=needed?t('Connect a model to start'):t('Model: {model}',{model:settings.model_id});
}

export async function initModelSettings(trigger){
  let settings=null;
  try{settings=await api(endpoint);showState(trigger,settings);
    if(!settings.configured)toast(t('Connect a model and API key to start. Open “Model and API key” in the sidebar.'));}
  catch{/* Settings stay reachable from the button; the dialog reports the error. */}
  trigger.addEventListener('click',async()=>{
    try{settings=await api(endpoint);}catch(e){toast(e.message);return;}
    const saved=await openDialog(settings);
    if(saved){settings=saved;showState(trigger,settings);}
  });
}

function openDialog(settings){
  return new Promise(resolve=>{
    let result=null,removeKey=false;
    const locked=new Set(settings.locked||[]);
    const dialog=el('dialog','','alert-settings model-settings'),form=el('form');
    const heading=el('div','','dialog-heading');heading.append(el('h2',t('Model and API key')));
    const close=button('×','icon-button');close.setAttribute('aria-label',t('Close'));close.addEventListener('click',()=>dialog.close());
    heading.append(close);form.append(heading);
    form.append(el('p',t('Eregion sends your requests to this provider with your key. The key is saved in this computer’s .env file and is never shown again.'),'dialog-note'));

    const lockNote=()=>el('span',t('Set by the server environment. Change it there and restart the server.'),'field-note');
    function field(name,label,{type='text',placeholder='',value='',help=''}={}){
      const row=el('label',t(label)),input=el('input');input.type=type;input.name=name;input.value=value;
      input.placeholder=placeholder;input.autocomplete='off';input.spellcheck=false;row.append(input);
      if(locked.has(name)){input.disabled=true;row.append(lockNote());}
      else if(help)row.append(el('span',t(help),'field-note'));
      return {row,input};
    }

    const keyState=settings.api_key||{};
    const key=field('api_key','API key',{type:'password',placeholder:keyState.set?(keyState.hint?t('Saved key ending in {hint}',{hint:keyState.hint}):t('A key is saved')):t('Paste your API key'),
      help:keyState.set?'Leave empty to keep the saved key.':''});
    form.append(key.row);
    if(keyState.source==='environment'&&!locked.has('api_key')){
      key.row.append(el('span',t('The server environment supplies {name}. A key saved here takes priority over it.',{name:keyState.variable}),'field-note'));
    }
    if(keyState.set&&keyState.source==='file'&&keyState.variable==='LLM_API_KEY'&&!locked.has('api_key')){
      const remove=button(t('Remove saved key'));
      remove.addEventListener('click',()=>{removeKey=!removeKey;remove.textContent=removeKey?t('Keep saved key'):t('Remove saved key');
        key.input.disabled=removeKey;key.input.value='';key.input.placeholder=removeKey?t('The saved key will be removed'):t('Saved key ending in {hint}',{hint:keyState.hint});});
      key.row.append(remove);
    }
    const base=field('base_url','Endpoint (optional)',{placeholder:'https://api.openai.com/v1',value:settings.base_url,
      help:'Any OpenAI-compatible endpoint. Leave empty for the provider default.'});
    form.append(base.row);

    const model=field('model_id','Model',{placeholder:t('Type a model name or load the list'),value:settings.model_id});
    const options=el('datalist');options.id='model-options';model.input.setAttribute('list',options.id);
    const load=button(t('Load models'),'secondary-button'),loaded=el('span','','field-note');loaded.setAttribute('aria-live','polite');
    const loadRow=el('div','','model-load');loadRow.append(load,loaded);
    model.row.append(options);form.append(model.row,loadRow);
    load.addEventListener('click',async()=>{
      load.disabled=true;loaded.textContent=t('Loading…');
      try{
        const {models}=await api(`${endpoint}/models`,{method:'POST',body:{api_key:key.input.value.trim()||null,base_url:base.input.value.trim()||null}});
        options.replaceChildren(...models.map(id=>{const o=el('option');o.value=id;return o;}));
        loaded.textContent=models.length?t('{n} models available. Pick one from the Model field.',{n:models.length}):t('The provider listed no models. Type the model name.');
        if(!locked.has('model_id'))model.input.focus();
      }catch(e){loaded.textContent=e.message;}finally{load.disabled=false;}
    });

    const more=el('details','','model-more');more.append(el('summary',t('Separate models for routine steps (optional)')));
    const fast=field('fast_model_id','Fast model',{value:settings.fast_model_id,help:'Used for short routine decisions. Empty uses the main model.'});
    const extraction=field('extraction_model_id','Extraction model',{value:settings.extraction_model_id,help:'Used to read postings and resumes. Empty uses the main model.'});
    more.append(fast.row,extraction.row);more.open=Boolean(settings.fast_model_id||settings.extraction_model_id);form.append(more);

    const features=renderFeatures(settings.features||[]);form.append(features.section);

    const failure=el('p','','danger');failure.setAttribute('role','alert');form.append(failure);
    const controls=el('div','','dialog-actions'),save=el('button',t('Save'),'primary-button');save.type='submit';controls.append(save);form.append(controls);
    form.addEventListener('submit',async event=>{
      event.preventDefault();failure.textContent='';save.disabled=true;
      // An empty field that was never set is left unchanged, so features can be saved before a model is chosen.
      const value=(name,input)=>locked.has(name)||(!input.value.trim()&&!settings[name])?null:input.value.trim();
      const body={model_id:value('model_id',model.input),base_url:value('base_url',base.input),
        fast_model_id:value('fast_model_id',fast.input),extraction_model_id:value('extraction_model_id',extraction.input),
        api_key:locked.has('api_key')?null:removeKey?'':key.input.value.trim()||null,features:features.changes()};
      try{result=await api(endpoint,{method:'PUT',body});toast(t('Model settings saved. Your next request uses them.'));dialog.close();}
      catch(e){failure.textContent=e.message;}finally{save.disabled=false;}
    });
    dialog.addEventListener('close',()=>{dialog.remove();resolve(result);});
    dialog.append(form);document.body.append(dialog);dialog.showModal();
    requestAnimationFrame(()=>{const first=[key.input,model.input].find(i=>!i.disabled&&!(i===key.input&&settings.api_key?.set));first?.focus();});
  });
}

// On/off switches and options, grouped as the server lists them. Only changed values are sent.
function renderFeatures(list){
  const section=el('section','','model-features'),controls=new Map();
  section.append(el('h3',t('Features')),el('p',t('Optional parts of Eregion. Saved in the same .env file.'),'field-note'));
  const groups=new Map();
  for(const item of list){if(!groups.has(item.group))groups.set(item.group,[]);groups.get(item.group).push(item);}
  const noteFor=item=>item.locked?t('Set by the server environment. Change it there and restart the server.'):
    [tServer(item.help),item.applies==='after restart'?t('Applies after the server restarts.'):''].filter(Boolean).join(' ');
  // Settings that belong to a switch are shown only while that switch is on.
  const sync=()=>{for(const {item,row} of controls.values())if(item.parent)row.hidden=controls.get(item.parent)?.input.getAttribute('aria-checked')!=='true';};
  for(const [name,items] of groups){
    const group=el('fieldset','','feature-group');group.append(el('legend',tServer(name)));
    for(const item of items){
      const note=noteFor(item);let row,input,remove=null;
      if(item.kind==='bool'){
        row=el('div','','feature-switch');
        const text=el('div','','feature-text'),label=el('span',tServer(item.label),'feature-label');label.id=`feature-${item.key}`;
        text.append(label);if(note)text.append(el('span',note,'field-note'));
        input=el('button','','toggle');input.type='button';input.setAttribute('role','switch');
        input.setAttribute('aria-checked',String(item.value));input.setAttribute('aria-labelledby',label.id);
        input.addEventListener('click',()=>{input.setAttribute('aria-checked',String(input.getAttribute('aria-checked')!=='true'));sync();});
        row.append(text,input);
      }else{
        row=el('label',tServer(item.label),'feature-field');
        if(item.kind==='choice'){
          input=el('select');
          for(const option of item.options){const o=el('option',option||t('None'));o.value=option;input.append(o);}
          input.value=item.value;
        }else{
          input=el('input');input.type=item.kind==='secret'?'password':'text';input.autocomplete='off';input.spellcheck=false;
          if(item.kind==='secret')input.placeholder=item.set?(item.hint?t('Saved, ending in {hint}',{hint:item.hint}):t('Saved')):t('Not set');
          else{input.value=item.value;input.placeholder=item.kind==='path'?t('Path to python.exe'):t('Not set');}
        }
        row.append(input);if(note)row.append(el('span',note,'field-note'));
        if(item.kind==='secret'&&item.set&&!item.locked){
          remove=button(t('Remove'));
          remove.addEventListener('click',()=>{const removing=remove.dataset.removing!=='true';remove.dataset.removing=String(removing);
            remove.textContent=removing?t('Keep'):t('Remove');input.disabled=removing;input.value='';});
          row.append(remove);
        }
      }
      if(item.parent)row.classList.add('feature-child');
      input.name=item.key;input.disabled=item.locked;
      group.append(row);controls.set(item.key,{item,input,remove,row});
    }
    section.append(group);
  }
  sync();
  return {section,changes(){
    const changed={};
    for(const {item,input,remove} of controls.values()){
      if(item.locked)continue;
      if(item.kind==='bool'){const on=input.getAttribute('aria-checked')==='true';if(on!==item.value)changed[item.key]=on;}
      else if(item.kind==='secret'){if(remove?.dataset.removing==='true')changed[item.key]='';else if(input.value.trim())changed[item.key]=input.value.trim();}
      else if(input.value.trim()!==item.value)changed[item.key]=input.value.trim();
    }
    return changed;
  }};
}
