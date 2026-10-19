// Chat motion: replies that arrive while the conversation is open are revealed progressively,
// and a running agent shows a thinking line. Reduced motion shows everything at once.
const reduced=()=>{try{return matchMedia('(prefers-reduced-motion: reduce)').matches;}catch{return false;}};
const BLOCKS='p,li,h1,h2,h3,h4,h5,h6,pre,blockquote,.code-block,.message-table,hr,ul,ol';
const active=new Set();

// A reply is new when it reached this open conversation recently; older replies render at once.
export function isFresh(message,now=Date.now()){
  const at=Date.parse(message?.at||'');
  return Number.isNaN(at)||now-at<30000;
}

// Reveal already-rendered Markdown text by text, keeping the full text in the DOM's final state.
// Returns a handle whose finish() completes the reveal immediately.
export function reveal(root,{onStep=()=>{},onDone=()=>{}}={}){
  const units=[],walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
  for(let node=walker.nextNode();node;node=walker.nextNode())if(node.nodeValue)units.push({node,text:node.nodeValue});
  const total=units.reduce((sum,u)=>sum+u.text.length,0);
  if(reduced()||!total||document.hidden){onDone();return {finish(){}};}
  const pending=[...root.querySelectorAll(BLOCKS)];
  for(const block of pending)block.classList.add('reveal-pending');
  for(const u of units)u.node.nodeValue='';
  const caret=document.createElement('span');caret.className='reveal-caret';caret.setAttribute('aria-hidden','true');
  // The log announces the finished reply once, not each partial step.
  const live=root.closest('[aria-live]');live?.setAttribute('aria-busy','true');
  root.classList.add('revealing');
  // Long replies speed up so the whole reveal stays between about half a second and three seconds.
  const duration=Math.min(3000,Math.max(500,total*9)),started=performance.now();
  let index=0,offset=0,shown=0,frame=0,done=false;
  const show=node=>{for(let n=node.parentNode;n&&n!==root;n=n.parentNode)n.classList?.remove('reveal-pending');};
  const finish=()=>{
    if(done)return;done=true;cancelAnimationFrame(frame);active.delete(handle);
    if(live&&![...active].some(h=>h.live===live))live.removeAttribute('aria-busy');
    for(const u of units)u.node.nodeValue=u.text;
    for(const block of pending)block.classList.remove('reveal-pending');
    caret.remove();root.classList.remove('revealing');onStep();onDone();
  };
  const step=now=>{
    if(!root.isConnected){finish();return;}
    // Ease out: text arrives quickly at first and settles at the end.
    const t=Math.min(1,(now-started)/duration),target=Math.ceil(total*(1-(1-t)**2));
    while(index<units.length&&shown<target){
      const u=units[index],take=Math.min(u.text.length-offset,target-shown);
      // Cut at a word boundary where possible so words do not flicker in letter by letter.
      let end=offset+take;
      if(end<u.text.length&&!/\s/.test(u.text[end])){
        const next=u.text.slice(end,end+16).search(/\s/),space=u.text.lastIndexOf(' ',end);
        if(next>=0)end+=next;else if(space>offset)end=space+1;
      }
      u.node.nodeValue=u.text.slice(0,end);shown+=end-offset;offset=end;show(u.node);
      u.node.after(caret);
      if(end<u.text.length)break;
      index++;offset=0;
    }
    // Blocks without text, such as rules, appear once the text before them has.
    for(const block of pending)if(block.classList.contains('reveal-pending')&&!block.textContent.trim()&&
      (!units[index]||block.compareDocumentPosition(units[index].node)&Node.DOCUMENT_POSITION_FOLLOWING))block.classList.remove('reveal-pending');
    onStep();
    if(t>=1||index>=units.length){finish();return;}
    frame=requestAnimationFrame(step);
  };
  const handle={finish,live};active.add(handle);
  frame=requestAnimationFrame(step);
  return handle;
}

// Complete every running reveal, for example when the conversation changes or the page is hidden.
export function finishReveals(){for(const handle of [...active])handle.finish();}
globalThis.document?.addEventListener('visibilitychange',()=>{if(document.hidden)finishReveals();});

// The working line: three breathing dots and the agent's current status.
export function thinking(text,doc=document){
  const line=doc.createElement('p');line.className='status-line thinking';line.setAttribute('role','status');
  const dots=doc.createElement('span');dots.className='thinking-dots';dots.setAttribute('aria-hidden','true');
  for(let i=0;i<3;i++)dots.append(doc.createElement('i'));
  const label=doc.createElement('span');label.className='thinking-text';label.textContent=text||'Thinking';
  line.append(dots,label);
  return line;
}

// Mark an element as newly arrived so it eases in once; the class clears itself afterwards.
export function enter(node){
  if(reduced())return node;
  node.classList.add('entering');
  node.addEventListener('animationend',()=>node.classList.remove('entering'),{once:true});
  return node;
}
