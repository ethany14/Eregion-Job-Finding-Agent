import test from 'node:test';
import assert from 'node:assert/strict';
import {AnswerDrafts,renderAnswerBatch,renderPendingQuestion} from '../frontend/web_app/apply-answers.js';

class Element {
  constructor(tag,text=''){this.tag=tag;this.textContent=text;this.children=[];this.attrs={};this.events={};}
  append(...nodes){this.children.push(...nodes);}
  setAttribute(k,v){this.attrs[k]=v;}
  addEventListener(k,f){this.events[k]=f;}
  querySelectorAll(tags){return this.children.flatMap(n=>[...(tags.split(',').includes(n.tag)?[n]:[]),...n.querySelectorAll(tags)]);}
  async fire(event){if(!this.disabled)return this.events[event]?.({preventDefault(){}});}
}
const el=(tag,text)=>new Element(tag,text);
const button=(text,action)=>{const b=el('button',text);b.addEventListener('click',action);return b;};
const fields=[{key:'a',label:'English?',kind:'select',options:[{label:'Yes',value:'1'},{label:'No',value:'2'}]},
  {key:'b',label:'Consent',kind:'checkbox'},{key:'c',label:'Discovery channel',kind:'text'}];

test('pending batch remains answerable during browser work and keeps edits through polling',async()=>{
  const drafts=new AnswerDrafts(),calls=[];
  const item={run_id:'run',status:'running',fields:[],input_request:{id:'question-1',field_keys:['a','b'],fields:fields.slice(0,2)}};
  const draw=()=>{const container=el('div');assert.equal(renderPendingQuestion({item,container,drafts,busy:false,reply:async body=>{calls.push(body);return true;},el,button}),true);return container;};
  let form=draw().children[0];await form.querySelectorAll('button')[0].fire('click');
  assert.equal(calls.length,0);
  // An independent navigation changes the visible page, not the pending question.
  item.fields=[{key:'different-page',kind:'text'}];form=draw().children[0];
  const buttons=form.querySelectorAll('button');assert.equal(buttons[0].attrs['aria-pressed'],'true');
  await buttons[3].fire('click');await form.fire('submit');await form.fire('submit');
  assert.deepEqual(calls,[{request_id:'question-1',answers:{a:'Yes',b:false}}]);
  item.input_request.id='question-2';form=draw().children[0];
  assert.equal(form.querySelectorAll('button').at(-1).disabled,true);
});

test('general agent question points to chat while a pause hides its answer controls',()=>{
  const item={run_id:'run',status:'needs_input',input_request:{id:'q',field_keys:[],fields:[]}},container=el('div');
  const options={item,container,drafts:new AnswerDrafts(),busy:false,reply:()=>assert.fail(),el,button};
  assert.equal(renderPendingQuestion(options),true);
  assert.equal(container.children[0].textContent,'Reply in the chat below.');
  item.pause_requested=true;
  assert.equal(renderPendingQuestion({...options,container:el('div')}),false);
});

test('select all questions, then submit once including an explicit false',async()=>{
  const drafts=new AnswerDrafts(),container=el('div'),calls=[];
  renderAnswerBatch({container,id:'A',fields,drafts,busy:false,submit:async answers=>{calls.push(answers);return true;},el,button});
  const form=container.children[0],buttons=form.querySelectorAll('button'),text=form.querySelectorAll('input')[0];
  await buttons[0].fire('click');await buttons[3].fire('click');
  assert.equal(calls.length,0);assert.equal(buttons.at(-1).disabled,true);
  text.value='LinkedIn';await text.fire('input');assert.equal(buttons.at(-1).disabled,false);
  await form.fire('submit');await form.fire('submit');
  assert.deepEqual(calls,[{a:'Yes',b:false,c:'LinkedIn'}]);assert.deepEqual(drafts.payload('A'),{});
});

test('polling and failed submission preserve choices; changed questions and another run do not reuse them',async()=>{
  const drafts=new AnswerDrafts(),container=el('div');
  renderAnswerBatch({container,id:'A',fields,drafts,busy:false,submit:async()=>false,el,button});
  drafts.set('A','a','No');drafts.set('A','b',false);drafts.set('A','c','University');
  drafts.sync('A',structuredClone(fields));assert.equal(drafts.complete('A'),true);
  drafts.sync('B',fields);assert.equal(drafts.complete('B'),false);
  const again=el('div');renderAnswerBatch({container:again,id:'A',fields,drafts,busy:false,submit:async()=>false,el,button});
  await again.children[0].fire('submit');assert.deepEqual(drafts.payload('A'),{a:'No',b:false,c:'University'});
  drafts.sync('A',[{...fields[0],label:'Different question'},fields[1]]);
  assert.deepEqual(drafts.payload('A'),{b:false});
});

const essay={key:'essay',label:'Describe your LLM project',kind:'textarea',options:[]};
const suggestion={field:{key:essay.key,label:essay.label,kind:essay.kind,group:null},text:'I built a document assistant.',
  evidence:[{source_ref:'resume',quote:'Built a document assistant.'}],missing_details:['What measurable effect did it have?']};

test('resume draft is editable multiline text and sends only after batch submission',async()=>{
  const drafts=new AnswerDrafts(),container=el('div'),calls=[];
  renderAnswerBatch({container,id:'A',fields:[essay],drafts,suggestions:{essay:suggestion},busy:false,
    submit:async answers=>{calls.push(answers);return true;},el,button});
  const form=container.children[0],input=form.querySelectorAll('textarea')[0];
  assert.equal(input.value,suggestion.text);assert.equal(calls.length,0);
  assert.ok(form.querySelectorAll('p').some(p=>p.textContent.includes('What measurable effect')));
  assert.ok(form.querySelectorAll('details').length);
  input.value+='\nIt saved 3 hours per week.';await input.fire('input');
  drafts.sync('A',[essay],{essay:{...suggestion,text:'A newly generated draft'}});
  assert.equal(drafts.payload('A').essay,input.value);
  await form.fire('submit');await form.fire('submit');
  assert.deepEqual(calls,[{essay:input.value}]);assert.equal(input.disabled,true);
});

test('polls cannot restore a deliberately cleared draft; stale questions and runs are isolated',()=>{
  const drafts=new AnswerDrafts();
  drafts.sync('A',[essay],{essay:suggestion});drafts.set('A','essay','');
  drafts.sync('A',[essay],{essay:suggestion});assert.equal(drafts.complete('A'),false);
  drafts.sync('B',[essay]);assert.deepEqual(drafts.payload('B'),{});
  drafts.sync('A',[{...essay,label:'Why do you want this role?'}],{essay:suggestion});
  assert.deepEqual(drafts.payload('A'),{});
});

test('failed submission keeps the edited draft and re-enables the textarea',async()=>{
  const drafts=new AnswerDrafts(),container=el('div');
  renderAnswerBatch({container,id:'A',fields:[essay],drafts,suggestions:{essay:suggestion},busy:false,submit:async()=>false,el,button});
  const form=container.children[0],input=form.querySelectorAll('textarea')[0];
  input.value='My edited answer';await input.fire('input');await form.fire('submit');
  assert.equal(input.disabled,false);assert.deepEqual(drafts.payload('A'),{essay:'My edited answer'});
});

test('Workday MM / DD / YYYY boxes are asked once and the date reaches every segment',async()=>{
  const prefix='text:selfIdentifiedDisabilityData--dateSignedOn-dateSection';
  const segments=['Month','Day','Year'].map(part=>({key:`${prefix}${part}-input:1`,label:'Date*',kind:'text'}));
  const item={run_id:'run',status:'needs_input',fields:[],input_request:{id:'q-date',field_keys:segments.map(f=>f.key),fields:segments}};
  const container=el('div'),calls=[];
  renderPendingQuestion({item,container,drafts:new AnswerDrafts(),busy:false,reply:async body=>{calls.push(body);return true;},el,button});
  const form=container.children[0],inputs=form.querySelectorAll('input');
  assert.equal(inputs.length,1);assert.equal(inputs[0].type,'date');
  inputs[0].value='2026-10-17';await inputs[0].fire('input');await form.fire('submit');
  assert.deepEqual(calls,[{request_id:'q-date',answers:Object.fromEntries(segments.map(f=>[f.key,'2026-10-17']))}]);
});
