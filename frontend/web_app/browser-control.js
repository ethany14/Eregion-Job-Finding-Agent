// Direct user input to the local browser. Never send these values through chat.
import {t} from './i18n.js';
export function createBrowserControl({current, refresh, resume}) {
  const dialog = document.querySelector('#browser-control-dialog');
  const image = dialog.querySelector('#control-image');
  const status = dialog.querySelector('#control-status');
  const field = dialog.querySelector('#control-text');
  const tabs = dialog.querySelector('#control-tabs');
  const viewport = dialog.querySelector('.control-viewport');
  let target = null, frame = null, busy = false, pointer = null, timer = null, generation = 0;
  const note = text => { status.textContent = text; };
  const controls = () => {
    dialog.setAttribute('aria-busy', String(busy));
    for (const node of dialog.querySelectorAll('[data-input-control]')) node.disabled = busy || !frame;
  };
  async function post(body, destination = target) {
    const response = await fetch(destination.url, {method:'POST', cache:'no-store',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
      signal:AbortSignal.timeout(body.action === 'start' ? 90000 : 15000)});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : t('Browser control needs attention.'));
    return data;
  }
  async function show(next, expected) {
    const loaded = new Image(); loaded.src = next.image; await loaded.decode();
    if (generation !== expected || !dialog.open) return;
    image.src = next.image; image.hidden = false; frame = next;
    dialog.querySelector('#control-address').textContent = next.url;
    // Preserve the select while focused; polling should not close its menu.
    const signature = JSON.stringify(next.tabs);
    if (tabs.dataset.signature !== signature) {
      tabs.replaceChildren(...next.tabs.map((tab, i) => {
        const option = document.createElement('option'); option.value = tab.id;
        option.textContent = `${i+1}. ${tab.url || t('New tab')}`; option.selected = tab.active;
        return option;
      }));
      tabs.dataset.signature = signature;
    }
  }
  async function action(payload, quiet = false) {
    if (busy || !target || !frame || !dialog.open) return;
    const version = generation, destination = target, previous = frame;
    busy = true; controls();
    try {
      const result = await post({...payload, session:previous.session,
        ...(payload.action === 'frame' ? {} : {frame_id:previous.frame_id})}, destination);
      await show(result, version);
      if (generation === version && !quiet) note(t('You have control. The agent is paused.'));
    } catch (error) {
      if (generation === version) {
        note(error.message);
        // Inputs are never retried. Only refresh the image after a stale frame.
        if (payload.action !== 'frame') {
          try { await show(await post({action:'frame',session:previous.session}, destination), version); }
          catch { /* Keep the original actionable error. */ }
        }
      }
    } finally {
      if (generation === version) { busy = false; controls(); }
    }
  }
  async function close(continueTask = false) {
    if (busy) { note(t('Wait for the current browser action to finish.')); return; }
    const destination = target, session = frame?.session;
    ++generation; clearInterval(timer); timer = null; target = frame = pointer = null;
    field.value = ''; field.type = 'password'; dialog.querySelector('#control-show-text').checked = false;
    image.removeAttribute('src'); image.hidden = true; dialog.close();
    if (destination && session) {
      try { await post({action:'end',session}, destination); } catch { /* Resuming already revokes control. */ }
    }
    await refresh();
    if (continueTask && current()?.run_id === destination?.id) await resume();
  }
  async function open() {
    if (dialog.open) return;
    const item = current(); if (!item) return;
    const version = ++generation;
    target = {id:item.run_id, url:`/api/${item.career?'career/tasks':'auto-apply'}/${item.run_id}/browser-control`};
    frame = null; busy = true; pointer = null; field.value = ''; controls();
    image.hidden = true; dialog.showModal(); note(t('Pausing the agent and connecting to your current browser…'));
    try {
      await show(await post({action:'start',expected_version:item.career?item.task_version:item.version}), version);
      if (generation === version) {
        note(t('You have control. Click the page, scroll, or type below. The agent is paused.'));
        image.focus();
        timer = setInterval(() => {
          if (!document.hidden && !pointer && document.activeElement !== tabs) action({action:'frame'}, true);
        }, 1200);
      }
    } catch (error) { if (generation === version) note(error.message); }
    finally { if (generation === version) { busy = false; controls(); await refresh(); } }
  }
  function point(event) {
    const box = image.getBoundingClientRect();
    return {x:Math.max(0,Math.min(frame.width-1,(event.clientX-box.left)*frame.width/box.width)),
      y:Math.max(0,Math.min(frame.height-1,(event.clientY-box.top)*frame.height/box.height))};
  }
  image.addEventListener('pointerdown', event => {
    if (busy || !frame || event.button !== 0) return;
    event.preventDefault(); image.focus(); image.setPointerCapture(event.pointerId); pointer = point(event);
  });
  image.addEventListener('pointerup', event => {
    if (!pointer || !frame) return;
    const start = pointer, end = point(event); pointer = null;
    if (Math.hypot(end.x-start.x,end.y-start.y)>6)
      action({action:'drag',...start,end_x:end.x,end_y:end.y});
    else action({action:'click',...end});
  });
  image.addEventListener('pointercancel',()=>{pointer=null;});
  image.addEventListener('contextmenu',event=>event.preventDefault());
  image.addEventListener('wheel',event=>{
    event.preventDefault(); if(frame) action({action:'scroll',...point(event),delta_y:Math.max(-1600,Math.min(1600,event.deltaY*(event.deltaMode===1?20:1)))});
  },{passive:false});
  const allowed = new Set(['Enter','Escape','Backspace','Delete','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Home','End','PageUp','PageDown']);
  image.addEventListener('keydown',event=>{
    // Tab stays local for keyboard accessibility; the toolbar sends a remote Tab.
    if (event.isComposing || event.key==='Tab') return;
    if (allowed.has(event.key)) {event.preventDefault(); action({action:'key',key:event.key});}
    else if ((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a') {event.preventDefault();action({action:'key',key:'ControlOrMeta+A'});}
    else if (event.key.length===1&&!event.ctrlKey&&!event.metaKey&&!event.altKey) {
      event.preventDefault(); field.focus(); field.value += event.key;
      note(t('Type here, then choose Type text to send the complete value to the selected page field.'));
    }
  });
  dialog.querySelector('#control-text-form').addEventListener('submit',event=>{
    event.preventDefault(); if(!field.value||busy)return;
    const text=field.value; field.value=''; action({action:'text',text});
  });
  dialog.querySelector('#control-show-text').addEventListener('change',event=>{field.type=event.target.checked?'text':'password';});
  dialog.querySelector('#control-key-send').addEventListener('click',()=>action({action:'key',key:dialog.querySelector('#control-key').value}));
  dialog.querySelector('#control-scroll-up').addEventListener('click',()=>action({action:'scroll',delta_y:-550}));
  dialog.querySelector('#control-scroll-down').addEventListener('click',()=>action({action:'scroll',delta_y:550}));
  dialog.querySelector('#control-refresh').addEventListener('click',()=>action({action:'frame'}));
  tabs.addEventListener('change',()=>action({action:'tab',tab_id:tabs.value}));
  dialog.querySelector('#control-close').addEventListener('click',()=>close());
  dialog.querySelector('#control-resume').addEventListener('click',()=>close(true));
  dialog.querySelector('#control-zoom').addEventListener('change',event=>viewport.classList.toggle('actual-size',event.target.checked));
  dialog.addEventListener('cancel',event=>{event.preventDefault();close();});
  return {open};
}
