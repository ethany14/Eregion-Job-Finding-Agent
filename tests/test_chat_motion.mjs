import test from 'node:test';
import assert from 'node:assert/strict';
import {isFresh,reveal} from '../frontend/web_app/chat-motion.js';

test('Only replies that arrived moments ago are written out; saved replies show at once',()=>{
  const now=Date.parse('2026-10-19T12:00:30Z');
  assert.equal(isFresh({at:'2026-10-19T12:00:10Z'},now),true);
  assert.equal(isFresh({at:'2026-10-19T11:59:00Z'},now),false);
  // Local prompts carry no time; they belong to the open conversation.
  assert.equal(isFresh({},now),true);
});

test('apply.js reveals only new assistant replies in an already open conversation',async()=>{
  const fs=await import('node:fs');
  const source=fs.readFileSync(new URL('../frontend/web_app/apply.js',import.meta.url),'utf8');
  assert.match(source,/const animate=!opened&&!restoring;if\(opened\)finishReveals\(\);/);
  assert.match(source,/if\(animate&&!previous\)\{/);
  assert.match(source,/m\.role==='assistant'&&isFresh\(m\)\)revealing=reveal\(body/);
  // Copying mid-reveal copies the whole reply.
  assert.match(source,/revealing\?\.finish\(\);try\{await navigator\.clipboard\.writeText\(body\.innerText\)/);
  assert.equal(typeof reveal,'function');
});
