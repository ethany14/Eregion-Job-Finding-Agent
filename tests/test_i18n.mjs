// Every interface string passed to t()/tn() as a literal has a Chinese translation that keeps
// its {placeholders}, so new text cannot silently stay English in the Chinese interface.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync,readdirSync} from 'node:fs';
import {ZH} from '../frontend/web_app/i18n-zh.js';
import {t,tn,lang} from '../frontend/web_app/i18n.js';

const dir=new URL('../frontend/web_app/',import.meta.url);
const files=readdirSync(dir).filter(name=>name.endsWith('.js')&&!name.startsWith('i18n'));
const LIT="'((?:[^'\\\\\\n]|\\\\.)*)'";

function literals(){
  const keys=new Set();
  for(const name of files){
    const src=readFileSync(new URL(name,dir),'utf8');
    for(const m of src.matchAll(new RegExp(`(?<![\\w.])(?:t|tServer)\\(${LIT}`,'g')))keys.add(m[1].trim());
    for(const m of src.matchAll(new RegExp(`(?<![\\w.])tn\\([^,]+,${LIT},${LIT}`,'g')))keys.add(m[2].trim());
  }
  return [...keys].filter(Boolean);
}

test('every literal interface string has a Chinese translation', ()=>{
  const keys=literals();
  assert.ok(keys.length>300,`only ${keys.length} strings found; the scan pattern no longer matches the code`);
  const missing=keys.filter(key=>!(key in ZH));
  assert.deepEqual(missing,[]);
});

test('translations keep their placeholders', ()=>{
  for(const [key,value] of Object.entries(ZH)){
    for(const placeholder of key.match(/\{\w+\}/g)||[])assert.ok(value.includes(placeholder),`${key} -> ${value}`);
  }
});

test('outside a browser the interface stays English', ()=>{
  assert.equal(lang,'en');
  assert.equal(t('Moved to {name}',{name:'Work'}),'Moved to Work');
  assert.equal(tn(1,'{n} answer','{n} answers'),'1 answer');
  assert.equal(tn(3,'{n} answer','{n} answers'),'3 answers');
});
