// Career orchestration reuses the existing application's secure controls and preview.
import {t,tn} from './i18n.js';
export function careerRoute(path,body,{enabled,current,attempts=[]}){
  const legacy={url:`/api/auto-apply${path}`,body};
  if(!enabled||['/candidate','/resume-profile','/prepare-message'].includes(path))return legacy;
  if(!path)return {url:'/api/career/tasks',body};
  const [,id,action]=path.split('/');
  const item=current?.run_id===id?current:attempts.find(a=>a.run_id===id);
  if(!item?.career)return legacy;
  if(action==='preview'&&item.preview_url)return {url:item.preview_url,body};
  if(['account','account-mode','memory','preview'].includes(action)){
    if(!item.application_run_id)throw new Error(t('There is no browser application open for this task yet.'));
    return {url:`/api/auto-apply/${item.application_run_id}${path.slice(id.length+1)}`,body};
  }
  return {url:`/api/career/tasks${path}`,body:['reply','continue'].includes(action)?{...body,application_run_id:item.application_run_id}:body};
}

const local=o=>Object.fromEntries(Object.entries(o).map(([k,v])=>[k,t(v)]));
const stageLabels=local({saved:'Saved',analyzing:'Analyzing',analysis_failed:'Analysis failed',needs_evidence:'Needs your evidence',
  materials_ready:'Materials ready',ready_to_apply:'Ready to apply',applied:'Applied',interviewing:'Interviewing',
  offer:'Offer',rejected:'Rejected',withdrawn:'Withdrawn',archived:'Archived'});
const materialTitles=local({tailored_resume:'Tailored resume',cover_letter:'Cover letter',application_answer:'Application answers'});
const materialStatus=local({awaiting_review:'Ready for your review',approved:'Approved',failed:'Needs attention',rejected:'Revision requested'});

// The one action that moves this job forward; the others stay as quiet links. Names are
// the English source keys; labels are translated where they are shown.
function nextJobAction(job){
  if(['applied','interviewing','offer'].includes(job.status))return 'Practice interview';
  if(['materials_ready','ready_to_apply'].includes(job.status))return 'Apply';
  return job.match_score===null||job.match_score===undefined?'Compare fit':'Prepare materials';
}

// Score text is a separate node so it never becomes markup; the label keeps it meaningful to screen readers.
function scoreNode(el,value,outOf,label){
  const node=el('strong','','score');
  node.append(el('span',`${label}: `,'sr-only'),el('span',String(value)),el('sup',`/${outOf}`));
  return node;
}

export function renderCareerResults({container,item,el,button,send,review,busy=false,renderText=text=>el('p',text)}){
  container.replaceChildren();
  if(!item?.career)return;
  const add=(card,key,at)=>{card.dataset??={};card.dataset.resultKey=key;card.dataset.resultAt=at||'';container.append(card);};
  const link=(label,url)=>{const a=el('a',label,'quiet-button');a.href=url;a.target='_blank';a.rel='noopener noreferrer';return a;};
  for(const job of item.jobs||[]){
    const card=el('article','','card career-job'),head=el('div','','job-head'),title=el('div');
    const scored=job.match_score!==null&&job.match_score!==undefined;
    if(scored)head.append(scoreNode(el,Math.round(job.match_score),100,t('Match score')));
    else if(job.score_range?.length===2&&job.score_range.every(Number.isFinite))
      head.append(scoreNode(el,job.score_range.map(Math.round).join('–'),100,t('Estimated match range')));
    title.append(el('h3',job.title||t('Saved job')),el('p',[job.company,job.location].filter(Boolean).join(' · '),'sub'));
    head.append(title);card.append(head);
    const counts=job.requirement_counts;
    if(counts){
      const line=el('p','','counts');
      for(const [key,label] of [['matched','matched'],['partial','partial'],['missing','missing'],['needs_confirmation','to confirm'],['unknown','unresolved']]){
        if(!counts[key]&&['needs_confirmation','unknown'].includes(key))continue;
        const part=el('span');part.append(el('b',String(counts[key]||0)),el('span',t(label)));line.append(part);
      }
      card.append(line);
    }
    if(job.gaps?.length)card.append(el('p',t('Gaps: {list}.',{list:job.gaps.join(', ')}),'gaps'));
    if(job.analysis_status==='needs_review')card.append(el('p',t('Some requirements remain uncertain. Review the evidence before relying on this range.'),'sub'));
    const unmet=job.match_overview?.confirmed_condition?.filter(c=>c.satisfied===false)||[];
    if(unmet.length)card.append(el('p',t('Unmet conditions: {list}.',{list:unmet.map(c=>c.requirement).join('; ')}),'gaps'));
    const recommendation=item.recommendations?.find(r=>r.application_id===job.application_id);
    if(recommendation){
      // Same tier contract as the saved record: priority / stretch / to_confirm.
      const why=el('div','','recommendation-reason');
      why.append(el('p',t(recommendation.tier_label||'Why this role'),'eyebrow'),el('p',recommendation.reason));
      if(recommendation.gaps?.length)why.append(el('p',t('Gaps: {list}.',{list:recommendation.gaps.join('; ')}),'gaps'));
      if(recommendation.to_confirm?.length)why.append(el('p',t('To confirm: {list}.',{list:recommendation.to_confirm.join('; ')}),'gaps'));
      if(recommendation.next_steps?.length)why.append(el('p',t('Next step: {step}',{step:recommendation.next_steps[0]}),'sub'));
      if(recommendation.score_meaning)why.append(el('p',recommendation.score_meaning,'sub'));
      if(recommendation.caveats)why.append(el('p',recommendation.caveats,'sub'));
      card.append(why);
    }
    card.append(el('p',stageLabels[job.status]||t('Saved'),'stage'));
    const actions=el('div','','actions'),primary=nextJobAction(job);
    const choices=[['Compare fit',t('Compare my fit for {title}.',{title:job.title})],['Prepare materials',t('Prepare a tailored resume and cover letter for {title}.',{title:job.title})],
      ['Apply',t('Apply to {title} using the approved materials if available.',{title:job.title})],['Practice interview',t('Start interview practice for {title}.',{title:job.title})]];
    choices.sort(([a],[b])=>(b===primary)-(a===primary));
    for(const [label,text] of choices){
      const b=button(t(label),()=>send(text,{application_id:job.application_id},item.run_id),label===primary?'primary-button':'secondary-button');
      b.disabled=busy;actions.append(b);
    }
    if(job.url?.startsWith('https://'))actions.append(link(t('Job posting ↗'),job.url));
    card.append(actions);add(card,`job:${job.application_id}`,job.result_at||job.created_at);
  }
  for(const pack of item.materials||[]){for(const material of pack.items||[]){
    const stale=pack.status==='stale',folded=material.status==='approved'&&!stale;
    const title=materialTitles[material.artifact_type]||t('Document');
    const status=stale?t('Needs updating'):(materialStatus[material.status]||({generating:t('Writing {title}…',{title:title.toLowerCase()}),verifying:t('Checking {title}…',{title:title.toLowerCase()}),needs_revision:t('Refining supported claims…')}[material.status]||t('Preparing')));
    const card=el(folded?'details':'article','',folded?'card folded career-material':'card career-material');
    if(folded){const summary=el('summary');summary.append(el('span',title),el('span',t('Approved · v{n}',{n:material.version||1}),'ok'));card.append(summary);}
    else{
      // Drafted → Tempered (your review) → Sealed, the same trail the Forge shows.
      const [at,state]=stale?[1,'wait']:{awaiting_review:[1,'wait'],failed:[0,'wait']}[material.status]||[0,'now'];
      const forge=el('ol','','trail forge');
      ['Drafted','Tempered','Sealed'].forEach((name,i)=>{const st=el('li','',`st ${i<at?'done':i===at?state:''}`);st.append(el('i'),el('span',t(name)));forge.append(st);});
      card.append(el('p',t('Version {n} · {status}',{n:material.version||1,status}),'eyebrow'),el('h3',title),forge);
    }
    const verification=material.verification;
    if(verification){
      const issues=verification.issues?.length||0;
      card.append(el('p',verification.passed?t('Every claim verified against your resume'):tn(issues,'{n} claim needs attention','{n} claims need attention'),verification.passed?'verify':'verify warn'));
    }
    if(material.text){
      if(folded)card.append(el('pre',material.text,'material-text'));
      else{const details=el('details','','doc-text');details.append(el('summary',t('Read document')),el('pre',material.text,'material-text'));card.append(details);}
    }
    const actions=el('div','','actions');
    if(material.status==='awaiting_review'&&!stale){
      const approve=button(t('Approve this version'),()=>review(pack,material,true),'primary-button');approve.disabled=busy;actions.append(approve);
    }
    const revise=button(t('Request changes'),()=>send(t('I want to revise this {title}. Ask me what I would like to change.',{title:title.toLowerCase()}),{pack_id:pack.pack_id,item_id:material.pack_item_id},item.run_id));revise.disabled=busy||['generating','verifying'].includes(material.status);actions.append(revise);
    if(material.artifact_type==='tailored_resume'&&material.text)actions.append(link(t('Download PDF'),`/api/packs/${pack.pack_id}/items/${material.pack_item_id}/resume.pdf`));
    if(material.artifact_type==='cover_letter'&&material.text)actions.append(link(t('Download PDF'),`/api/packs/${pack.pack_id}/items/${material.pack_item_id}/cover-letter.pdf`));
    card.append(actions);add(card,`material:${material.pack_item_id}`,material.created_at);
  }}
  for(const practice of item.interviews||[]){
    const card=el('article','','card career-interview');
    const question=practice.question;
    if(question){
      card.append(el('p',t('Interview question'),'eyebrow'));
      const text=el('div','','interview-question');text.append(renderText(question.question_text||question.text||question.question||t('Your next interview question is ready.')));card.append(text);
      const actions=el('div','','actions');
      actions.append(button(t('Answer in chat'),()=>document.querySelector('#chat-text').focus(),'primary-button'));
      for(const [label,verb] of [['Skip question','Skip the current question.'],['End practice','End this practice and show my report.']]){
        const b=button(t(label),()=>send(t(verb),{interview_id:practice.interview.mock_interview_id},item.run_id));b.disabled=busy;actions.append(b);
      }
      card.append(actions);
    }
    const pending=(practice.feedback_jobs||[]).filter(job=>job.status==='pending').length;
    if(pending)card.append(el('p',tn(pending,'Feedback is being prepared for {n} saved answer. You can continue with the next question.','Feedback is being prepared for {n} saved answers. You can continue with the next question.'),'hint'));
    if((practice.feedback_jobs||[]).some(job=>job.status==='failed')){
      card.append(el('p',t('Some feedback could not be prepared. Your answers are saved.'),'hint'));
      card.append(button(t('Retry saved feedback'),()=>send(t('Retry interview feedback.'),{interview_id:practice.interview.mock_interview_id},item.run_id)));
    }
    const feedbacks=practice.feedback_history?.length?practice.feedback_history:
      practice.latest_feedback?[{evaluation:practice.latest_feedback}]:[];
    for(const feedback of feedbacks){
      const f=feedback.evaluation,row=el('div','','feedback'),dims=el('div','','dims');
      card.append(el('p',feedback.answer_sequence?t('Feedback on answer {n}',{n:feedback.answer_sequence}):t('Answer feedback'),'eyebrow'));
      if(feedback.question_text)card.append(renderText(feedback.question_text));
      row.append(scoreNode(el,f.overall_score,5,t('Overall')));
      for(const [key,label] of [['relevance_score','Relevance'],['specificity_score','Specificity'],['evidence_grounding_score','Evidence'],['structure_score','Structure'],['communication_score','Communication']]){
        if(f[key]==null)continue;const dim=el('div');dim.append(el('span',t(label)),el('b',String(f[key])));dims.append(dim);
      }
      row.append(dims);card.append(row);
      const notes=el('div','','notes');
      for(const [label,values] of [['Strengths',f.strengths],['Improve next',f.improvement_areas],['Missing details',f.missing_answer_elements]]){
        if(!values?.length)continue;
        const section=el('div'),list=el('ul');section.append(el('h4',t(label)));
        for(const value of values){const entry=el('li');entry.append(renderText(value));list.append(entry);}
        section.append(list);notes.append(section);
      }
      if(f.suggested_structure)notes.append(renderText(f.suggested_structure));
      card.append(notes);
    }
    if(practice.report){const r=practice.report,details=el('details','','report');details.open=true;details.append(el('summary',t('Interview report')));
      if(r.average_scores?.overall!=null)details.append(el('p',t('Average score: {n} / 5',{n:r.average_scores.overall})));
      for(const priority of r.practice_priorities||[])details.append(renderText(priority));
      for(const turn of r.question_answer_summaries||[])details.append(el('h4',turn.question),renderText(turn.answer||t('Not answered')));
      card.append(details);
    }
    add(card,`interview:${practice.interview?.mock_interview_id||''}:${question?.question_id||'report'}`,
      practice.report?practice.interview?.completed_at:question?.created_at||practice.interview?.started_at);
  }
}

// Cards are results of earlier turns, not a dashboard pinned below every reply.
// Timestamps let the order survive reloads; undated legacy cards follow the first
// assistant turn instead of perpetually covering the newest conversation.
export function careerTimeline(messages,cards){
  const stamp=value=>{
    // SQLite timestamps are UTC even when serialized without an offset.
    let text=String(value||'');
    if(/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(text)&&!/(Z|[+-]\d{2}:?\d{2})$/i.test(text))text+='Z';
    const n=Date.parse(text);return Number.isFinite(n)?n:null;
  };
  const fallback=Math.max(0,messages.findIndex(m=>m.role==='assistant'));
  const buckets=new Map();
  for(const card of cards){
    const at=stamp(card.dataset.resultAt);let index=fallback;
    if(at!==null&&messages.some(m=>stamp(m.at)!==null)){
      index=0;
      for(let i=0;i<messages.length;i++){
        const when=stamp(messages[i].at);
        if(when!==null&&when<=at)index=i;
        else if(when!==null)break;
      }
    }
    if(!buckets.has(index))buckets.set(index,[]);
    buckets.get(index).push(card);
  }
  for(const list of buckets.values())list.sort((a,b)=>(stamp(a.dataset.resultAt)||0)-(stamp(b.dataset.resultAt)||0));
  if(!messages.length)return cards;
  return messages.flatMap((message,index)=>[message,...(buckets.get(index)||[])]);
}

export function placeCareerResults(container,messages,cards){
  const bubbles=new Map([...container.querySelectorAll(':scope > .message')].map(n=>[n.dataset.messageId,n]));
  const wanted=careerTimeline(messages,cards).map(item=>bubbles.get(item.id)||item);
  const keep=new Set(wanted);
  for(const child of [...container.children])if(!keep.has(child))child.remove();
  // Keep existing nodes (and their focus/open state) when polling changes nothing.
  let before=container.firstChild;
  for(const node of wanted){if(node===before)before=before.nextSibling;else container.insertBefore(node,before);}
}
