// Conversation organisation: API calls, the row menu, dialogs, the undo toast and sidebar grouping.
import {t,tServer} from './i18n.js';

export async function api(url,{method='GET',body}={}){
  const response=await fetch(url,{method,headers:{'Content-Type':'application/json'},
    ...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(30000)});
  if(response.status===204)return null;
  const data=await response.json().catch(()=>({}));
  if(!response.ok){
    const detail=data.detail;
    throw new Error(typeof detail==='string'?tServer(detail):Array.isArray(detail)?t('Check the details and try again.'):tServer(detail?.message)||t('Something went wrong. Try again.'));
  }
  return data;
}

export const organise=(taskId,changes)=>api(`/api/career/tasks/${encodeURIComponent(taskId)}`,{method:'PATCH',body:changes});
export const deleteForever=taskId=>api(`/api/career/tasks/${encodeURIComponent(taskId)}`,{method:'DELETE'});
export const emptyTrash=()=>api('/api/career/tasks/trash',{method:'DELETE'});
export const createGroup=name=>api('/api/career/groups',{method:'POST',body:{name}});
export const renameGroup=(id,name)=>api(`/api/career/groups/${encodeURIComponent(id)}`,{method:'PATCH',body:{name}});
export const deleteGroup=id=>api(`/api/career/groups/${encodeURIComponent(id)}`,{method:'DELETE'});

const COLLAPSED_KEY='northstar.collapsedGroups';
export function collapsedGroups(){try{return new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY)||'[]'));}catch{return new Set();}}
export function toggleGroup(id){
  const set=collapsedGroups();set.has(id)?set.delete(id):set.add(id);
  try{localStorage.setItem(COLLAPSED_KEY,JSON.stringify([...set]));}catch{/* Storage is optional. */}
}

// Needs you → Pinned → groups → Recent. A chat that needs the person is never hidden inside a group.
export function sidebarSections(attempts,groups,needsYou,recentLabel=t('Recent')){
  const waiting=attempts.filter(needsYou),rest=attempts.filter(a=>!needsYou(a));
  const known=new Set(groups.map(g=>g.group_id));
  const pinned=rest.filter(a=>a.pinned_at).sort((a,b)=>b.pinned_at.localeCompare(a.pinned_at));
  const unpinned=rest.filter(a=>!a.pinned_at);
  return [
    {key:'needs',label:t('Needs you'),items:waiting},
    {key:'pinned',label:t('Pinned'),items:pinned},
    ...groups.map(g=>({key:`group:${g.group_id}`,label:g.name,group:g,items:unpinned.filter(a=>a.group_id===g.group_id)})),
    {key:'recent',label:recentLabel,items:unpinned.filter(a=>!a.group_id||!known.has(a.group_id))},
  ].filter(s=>s.items.length||s.group);
}

// ---- Floating menu ----
let openState=null;
export function closeMenu(){
  if(!openState)return;
  openState.root.remove();openState.anchor?.setAttribute('aria-expanded','false');
  document.removeEventListener('pointerdown',openState.outside,true);document.removeEventListener('keydown',openState.keys,true);
  const anchor=openState.anchor;openState=null;anchor?.focus({preventScroll:true});
}
// items: {label, icon, action, danger, submenu:[...], checked, separator}
export function openMenu(anchor,items,{x,y}={}){
  closeMenu();
  const root=document.createElement('div');root.className='menu-layer';
  const build=(entries,cls)=>{
    const menu=document.createElement('div');menu.className=cls;menu.setAttribute('role','menu');
    for(const entry of entries){
      if(entry.separator){const hr=document.createElement('hr');menu.append(hr);continue;}
      const item=document.createElement('button');item.type='button';item.setAttribute('role','menuitem');
      item.className=entry.danger?'menu-item danger':'menu-item';
      const mark=document.createElement('span');mark.className='menu-mark';mark.textContent=entry.checked?'✓':'';
      const label=document.createElement('span');label.textContent=entry.label;
      item.append(mark,label);
      if(entry.hint){const hint=document.createElement('span');hint.className='menu-hint';hint.textContent=entry.hint;item.append(hint);}
      if(entry.submenu){
        item.setAttribute('aria-haspopup','menu');const chev=document.createElement('span');chev.className='menu-hint';chev.textContent='›';item.append(chev);
        const showSub=()=>{root.querySelector('.menu.sub')?.remove();const sub=build(entry.submenu,'menu sub');root.append(sub);
          const r=item.getBoundingClientRect(),w=sub.offsetWidth||200;
          sub.style.left=`${r.right+w+8<innerWidth?r.right+4:r.left-w-4}px`;sub.style.top=`${Math.min(r.top-6,innerHeight-sub.offsetHeight-8)}px`;
          sub.querySelector('button')?.focus();};
        item.addEventListener('click',showSub);item.addEventListener('pointerenter',showSub);
        item.addEventListener('keydown',e=>{if(e.key==='ArrowRight'){e.preventDefault();showSub();}});
      }else{
        item.addEventListener('pointerenter',()=>{if(menu.classList.contains('main'))root.querySelector('.menu.sub')?.remove();});
        item.addEventListener('click',()=>{closeMenu();entry.action?.();});
      }
      menu.append(item);
    }
    return menu;
  };
  const main=build(items,'menu main');root.append(main);document.body.append(root);
  const r=anchor.getBoundingClientRect(),left=x??r.left,top=y??r.bottom+4;
  main.style.left=`${Math.min(left,innerWidth-main.offsetWidth-8)}px`;
  main.style.top=`${top+main.offsetHeight>innerHeight-8?Math.max(8,(y??r.top)-main.offsetHeight-4):top}px`;
  anchor.setAttribute('aria-expanded','true');
  const outside=e=>{if(!root.contains(e.target)&&e.target!==anchor)closeMenu();};
  const keys=e=>{
    const buttons=[...(e.target.closest('.menu')||main).querySelectorAll('button')],i=buttons.indexOf(e.target);
    if(e.key==='Escape'){e.preventDefault();e.stopPropagation();if(e.target.closest('.menu.sub')){const sub=e.target.closest('.menu.sub');sub.remove();main.querySelector('[aria-haspopup]')?.focus();}else closeMenu();}
    else if(e.key==='ArrowDown'){e.preventDefault();buttons[(i+1)%buttons.length]?.focus();}
    else if(e.key==='ArrowUp'){e.preventDefault();buttons[(i-1+buttons.length)%buttons.length]?.focus();}
    else if(e.key==='ArrowLeft'&&e.target.closest('.menu.sub')){e.preventDefault();e.target.closest('.menu.sub').remove();main.querySelector('[aria-haspopup]')?.focus();}
    else if(e.key==='Tab')closeMenu();
  };
  document.addEventListener('pointerdown',outside,true);document.addEventListener('keydown',keys,true);
  openState={root,anchor,outside,keys};
  main.querySelector('button')?.focus();
}

// ---- Toast with undo ----
let toastTimer=null;
export function toast(text,{undo}={}){
  let node=document.querySelector('#toast');
  if(!node){node=document.createElement('div');node.id='toast';node.className='toast';node.setAttribute('role','status');document.body.append(node);}
  clearTimeout(toastTimer);node.replaceChildren();
  const label=document.createElement('span');label.textContent=text;node.append(label);
  if(undo){const b=document.createElement('button');b.type='button';b.textContent=t('Undo');
    b.addEventListener('click',()=>{node.hidden=true;clearTimeout(toastTimer);undo();});node.append(b);}
  node.hidden=false;toastTimer=setTimeout(()=>{node.hidden=true;},5000);
}

// ---- Dialogs ----
function modal(build){
  return new Promise(resolve=>{
    const dialog=document.createElement('dialog');dialog.className='small-dialog';
    const form=document.createElement('form');form.method='dialog';dialog.append(form);
    let result=null;
    build(form,value=>{result=value;});
    dialog.addEventListener('close',()=>{dialog.remove();resolve(dialog.returnValue==='ok'?result:null);});
    document.body.append(dialog);dialog.showModal();
  });
}
function actions(form,{confirmLabel,danger}){
  const row=document.createElement('div');row.className='dialog-actions';
  const cancel=document.createElement('button');cancel.type='submit';cancel.value='cancel';cancel.className='text-button';cancel.textContent=t('Cancel');cancel.formNoValidate=true;
  const ok=document.createElement('button');ok.type='submit';ok.value='ok';ok.className=danger?'primary-button danger-button':'primary-button';ok.textContent=confirmLabel;
  row.append(cancel,ok);form.append(row);return ok;
}
export function confirmDialog({title,text,confirmLabel=t('Confirm'),danger=false}){
  return modal((form,set)=>{
    const h=document.createElement('h2');h.textContent=title;const p=document.createElement('p');p.textContent=text;
    form.append(h,p);actions(form,{confirmLabel,danger});set(true);
  }).then(Boolean);
}
export function nameDialog({title,label,value='',confirmLabel=t('Save')}){
  return modal((form,set)=>{
    const h=document.createElement('h2');h.textContent=title;
    const field=document.createElement('label');field.className='field';field.textContent=label;
    const input=document.createElement('input');input.required=true;input.maxLength=80;input.value=value;input.pattern='.*\\S.*';
    field.append(input);form.append(h,field);actions(form,{confirmLabel});
    input.addEventListener('input',()=>set(input.value.trim()));set(value.trim()||null);
    requestAnimationFrame(()=>{input.focus();input.select();});
  });
}
