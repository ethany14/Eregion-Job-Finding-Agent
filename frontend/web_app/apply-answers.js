// Option clicks only edit a draft; submit sends the whole group once.
import {t,tn} from './i18n.js';
export class AnswerDrafts {
  constructor(){this.runs=new Map();this.steps=new Map();}
  // The visible question survives polling re-renders of the same batch.
  step(id,value){if(value!==undefined)this.steps.set(id,value);return this.steps.get(id)??0;}
  sync(id,fields,suggestions={}){
    const previous=this.runs.get(id)||new Map(),next=new Map();
    for(const f of fields){
      const signature=JSON.stringify([f.label,f.kind,f.group,f.options]),old=previous.get(f.key);
      const entry=old?.signature===signature?old:{signature,value:undefined,edited:false};
      const draft=suggestions[f.key],matches=draft&&['key','label','kind','group'].every(k=>(draft.field?.[k]??null)===(f[k]??null));
      const suggestion=matches?draft:undefined;
      if(!entry.edited&&entry.suggestion?.text!==suggestion?.text)entry.value=suggestion?.text;
      entry.suggestion=suggestion;next.set(f.key,entry);
    }
    this.runs.set(id,next);return next;
  }
  set(id,key,value){const entry=this.runs.get(id)?.get(key);if(entry){entry.value=value;entry.edited=true;}}
  clear(id){this.runs.delete(id);this.steps.delete(id);}
  payload(id){return Object.fromEntries([...(this.runs.get(id)||[])].filter(([,v])=>v.value!==undefined&&String(v.value).trim()!=='').map(([k,v])=>[k,v.value]));}
  complete(id){const entries=this.runs.get(id);return Boolean(entries?.size&&Object.keys(this.payload(id)).length===entries.size);}
}

// Three or more questions are shown one at a time; shorter batches stay side by side.
const STEP_THRESHOLD=3;

export function renderAnswerBatch({container,id,fields,drafts,suggestions={},busy,submit,el,button}){
  fields=fields.filter(f=>!['file','password'].includes(f.kind));
  if(!fields.length)return;
  const stepped=fields.length>=STEP_THRESHOLD;
  const entries=drafts.sync(id,fields,suggestions),form=el('form','',stepped?'answer-batch stepped':'answer-batch');
  const commit=el('button',t('Submit answers & continue'),'primary-button');commit.type='submit';
  const progress=el('p','','fine-print'),counter=el('p','','qcount'),rows=[];
  let step=Math.min(drafts.step(id),fields.length-1);
  const back=button(t('Back'),()=>show(step-1),'text-button'),next=button(t('Next question'),()=>show(step+1),'text-button');
  back.type='button';next.type='button';
  const update=()=>{
    commit.disabled=busy||!drafts.complete(id);
    progress.textContent=t('{ready} of {total} ready. Your answers are sent together.',{ready:Object.keys(drafts.payload(id)).length,total:fields.length});
  };
  function show(index){
    step=Math.max(0,Math.min(fields.length-1,index));drafts.step(id,step);
    if(!stepped)return;
    rows.forEach((row,i)=>{row.hidden=i!==step;});
    counter.textContent=t('Question {n} of {total}',{n:step+1,total:fields.length});
    back.disabled=busy||step===0;next.disabled=busy||step===fields.length-1;
  }
  const advance=key=>{if(stepped&&fields[step]?.key===key&&step<fields.length-1)show(step+1);};
  if(stepped)form.append(counter);
  for(const f of fields){
    const row=el('div','','question-choice'),label=el('label',f.label,'question-label');row.append(label);rows.push(row);
    const {value,suggestion}=entries.get(f.key);
    if(suggestion){
      row.append(el('p',t('Drafted from your resume and details. Edit freely before sending.'),'draft-note'));
      for(const detail of suggestion.missing_details||[])row.append(el('p',t('Add if you can: {detail}',{detail}),'draft-missing'));
    }
    const options=f.kind==='checkbox'?[{label:t('Yes'),value:true},{label:t('No'),value:false}]:(f.options||[]).filter(o=>!o.disabled&&String(o.value)!=='');
    if(options.length&&options.length<=6){
      const choices=el('div','','question-actions');choices.setAttribute('role','group');choices.setAttribute('aria-label',f.label);
      for(const option of options){const answer=f.kind==='checkbox'?option.value:option.label;const b=button(option.label,()=>{drafts.set(id,f.key,answer);for(const other of choices.children)other.setAttribute('aria-pressed','false');b.setAttribute('aria-pressed','true');update();advance(f.key);});b.disabled=busy;b.setAttribute('aria-pressed',String(value===answer));choices.append(b);}row.append(choices);
    }else{
      const multiline=!options.length&&(f.kind==='textarea'||Boolean(suggestion));
      const input=el(options.length?'select':multiline?'textarea':'input');input.disabled=busy;input.id=`batch-${id}-${fields.indexOf(f)}`;label.htmlFor=input.id;
      if(options.length){const placeholder=el('option',t('Choose an answer'));placeholder.value='';input.append(placeholder);for(const o of options){const n=el('option',o.label);n.value=o.label;input.append(n);}}
      else{if(multiline)input.rows=6;else input.type=f.kind==='date'?'date':'text';input.maxLength=Math.min(10000,Number(f.constraints?.max_length)>0?Number(f.constraints.max_length):10000);input.placeholder=t('Your answer');}
      input.value=value??'';input.addEventListener(options.length?'change':'input',()=>{drafts.set(id,f.key,input.value);update();});row.append(input);
    }
    if(suggestion?.evidence?.length){
      const evidence=el('details','','draft-evidence');
      evidence.append(el('summary',tn(suggestion.evidence.length,'Based on {n} line from your resume','Based on {n} lines from your resume')));
      for(const item of suggestion.evidence)evidence.append(el('p',`“${item.quote}”`));row.append(evidence);
    }
    form.append(row);
  }
  const footer=el('div','','batch-footer');
  if(stepped)footer.append(back,next);
  footer.append(commit,progress);form.append(footer);
  show(step);update();
  form.addEventListener('submit',async event=>{event.preventDefault();if(commit.disabled)return;commit.disabled=true;for(const n of form.querySelectorAll('input,textarea,select,button'))n.disabled=true;if(await submit(drafts.payload(id)))drafts.clear(id);else{for(const n of form.querySelectorAll('input,textarea,select,button'))n.disabled=busy;update();}});
  container.append(form);
}

// Workday splits one date into MM / DD / YYYY boxes with the same label (Intel-34: the user typed
// the date three times). Ask once with a date input and send its ISO date to every segment.
export function groupDateSegments(fields){
  const out=[],groups=new Map();
  for(const f of fields){
    const match=f.kind==='text'&&/^(.*)dateSection(?:Month|Day|Year)\b/i.exec(f.key);
    if(!match){out.push(f);continue;}
    let group=groups.get(match[1]);
    if(!group){group={key:`date:${match[1]}`,label:f.label,kind:'date',group:f.group,segments:[]};groups.set(match[1],group);out.push(group);}
    group.segments.push(f.key);
  }
  return out.map(f=>f.segments?.length===1?fields.find(x=>x.key===f.segments[0]):f);
}

function expandDateSegments(fields,answers){
  const expanded={...answers};
  for(const f of fields)if(f.segments&&f.key in expanded){const value=expanded[f.key];delete expanded[f.key];for(const key of f.segments)expanded[key]=value;}
  return expanded;
}

// A question remains answerable while independent browser work continues. Its
// request ID and frozen fields isolate drafts from later pages and polling.
export function renderPendingQuestion({item,container,drafts,busy,reply,el,button,renderText=text=>el('p',text)}){
  const question=item.input_request;
  if(!question||item.pause_requested||item.status==='submitting')return false;
  const fields=groupDateSegments(question.field_keys.map(key=>question.fields?.find(f=>f.key===key)||item.fields?.find(f=>f.key===key)).filter(Boolean));
  if(fields.some(f=>!['file','password'].includes(f.kind)))renderAnswerBatch({container,id:`${item.run_id}:${question.id}`,fields,drafts,suggestions:item.answer_drafts,busy,submit:answers=>reply({request_id:question.id,answers:expandDateSegments(fields,answers)}),el,button});
  else {
    const inConversation=item.messages?.some(m=>m.role==='assistant'&&(m.id===question.id||m.text===question.text));
    if(item.career&&question.text&&!inConversation)container.append(renderText(question.text));
    container.append(el('p',t('Reply in the chat below.'),'status-line'));
  }
  if(['running','queued'].includes(item.status))container.append(el('p',t('I am continuing the independent steps while you answer.'),'status-line'));
  return true;
}
