// Counts reported by providers. Missing usage is explicit, never estimated as zero.
import {t,tn} from './i18n.js';
export function usageText(usage){
  if(!usage?.calls)return '';
  const count=n=>Number(n||0).toLocaleString('en-US');
  const known=usage.calls-usage.unknown_usage_calls;
  const tokens=known?t('{input} input / {output} output tokens reported',{input:count(usage.input_tokens),output:count(usage.output_tokens)}):t('Token counts unavailable');
  const missing=usage.unknown_usage_calls?' · '+tn(usage.unknown_usage_calls,'{n} call without complete usage','{n} calls without complete usage',{n:count(usage.unknown_usage_calls)}):'';
  const discarded=usage.discarded?' · '+t('{n} discarded',{n:count(usage.discarded)}):'';
  return `${tn(usage.calls,'{n} model call','{n} model calls',{n:count(usage.calls)})} · ${tokens}${missing}${discarded}`;
}

export function renderUsage(container,usage){
  container.replaceChildren();container.hidden=!usage?.calls;
  if(!usage?.calls)return;
  const heading=document.createElement('summary');heading.textContent=t('Usage by task');container.append(heading);
  const list=document.createElement('ul');
  for(const group of usage.groups||[]){
    const item=document.createElement('li');
    item.textContent=`${group.purpose.replaceAll('_',' ')} · ${group.model}: ${usageText(group)}`;
    list.append(item);
  }
  container.append(list);
}
