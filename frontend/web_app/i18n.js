// Interface language: English (the source text) or Simplified Chinese. An explicit choice is
// remembered on this device; until then a Chinese browser opens in Chinese. The <head> script
// sets <html lang> before first paint; this module translates interface text only. Chat
// messages, employer form labels and the person's own data are never translated.
import {ZH} from './i18n-zh.js';

const KEY='eregion.lang';

export function chosenLanguage(){
  // Outside a browser (unit tests under Node) the source language keeps results stable.
  if(typeof document==='undefined')return 'en';
  try{const saved=globalThis.localStorage?.getItem(KEY);if(saved==='en'||saved==='zh')return saved;}catch{/* Storage is optional. */}
  return /^zh/i.test(globalThis.navigator?.language||'')?'zh':'en';
}
export const lang=chosenLanguage();
export const locale=lang==='zh'?'zh-CN':'en';

// t('Moved to {name}', {name}) -> the Chinese text when it exists, else the English source.
// Keys are stored trimmed; a translation carries its own spacing (Chinese needs none around it).
export function t(text,vars){
  let out=lang==='zh'&&typeof text==='string'&&(ZH[text]??ZH[text.trim()])||text;
  if(vars)out=String(out).replace(/\{(\w+)\}/g,(match,key)=>key in vars?String(vars[key]):match);
  return out;
}
// tn(n, '{n} answer', '{n} answers'): English picks the singular or plural; Chinese has one form,
// stored under the plural text.
export function tn(n,one,other,vars={}){
  return t(lang==='zh'?other:n===1?one:other,{n,...vars});
}
// Server status text is translated only on an exact, known match; anything else stays as sent.
export const tServer=text=>typeof text==='string'?t(text):text;

const ATTRIBUTES=['aria-label','title','placeholder','alt'];
// Translate the static interface (text nodes and attributes) under root. Exact matches only, so
// names, links and other data in the markup stay untouched.
export function translateTree(root=document.body){
  if(lang!=='zh'||!root)return;
  const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT,{acceptNode:node=>
    node.parentElement?.closest('script,style,textarea,[data-no-translate]')?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
  for(let node=walker.nextNode();node;node=walker.nextNode()){
    const text=node.nodeValue,trimmed=text.trim();
    if(trimmed&&ZH[trimmed])node.nodeValue=text.replace(trimmed,ZH[trimmed]);
  }
  for(const element of root.querySelectorAll(ATTRIBUTES.map(a=>`[${a}]`).join(','))){
    for(const name of ATTRIBUTES){const value=element.getAttribute(name);if(value&&ZH[value.trim()])element.setAttribute(name,ZH[value.trim()]);}
  }
}

export function setLanguage(next){
  try{localStorage.setItem(KEY,next);}catch{/* The reload below still applies it once. */}
  location.reload();
}

// The switch shows the other language, written in that language.
export function initLanguageToggle(button){
  if(!button)return;
  const other=lang==='zh'?'en':'zh';
  button.textContent=other==='zh'?'中':'EN';
  button.lang=other==='zh'?'zh-CN':'en';
  button.setAttribute('aria-label',other==='zh'?'切换到中文':'Switch to English');
  button.title=other==='zh'?'中文':'English';
  button.addEventListener('click',()=>setLanguage(other));
}

export function initLanguage(){
  document.documentElement.lang=locale;
  if(document.title&&ZH[document.title]&&lang==='zh')document.title=ZH[document.title];
  translateTree(document.body);
  document.documentElement.classList.remove('i18n-pending');
}
