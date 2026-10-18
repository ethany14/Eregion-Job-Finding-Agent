"""Observe, act, then observe again in a real browser. No model-authored scripts."""
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.contracts import public_https_url
from eregion.runtime.auto_apply.navigation import action_kind
from eregion.runtime.auto_apply.visual import SECRET_SELECTOR, SCREEN_STATE, VisualBrowser


class ObservationExpired(BrowserBlocked):
    """Pre-action observation invalidated; refresh without executing the stale intent."""


SCAN = r"""({query, offset, text_offset, ref, region_ref, navigation_only, job_only}) => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'
    && !e.closest('[hidden], [inert], [aria-hidden="true"]');
  const clean = s => (s || '').replace(/\s+/g,' ').trim();
  const roots = [document];
  for(let i=0;i<roots.length;i++) for(const e of roots[i].querySelectorAll('*')) if(e.shadowRoot) roots.push(e.shadowRoot);
  const all = selector => roots.flatMap(root=>[...root.querySelectorAll(selector)]);
  const region=region_ref?all('[data-northstar-browse]').find(e=>e.getAttribute('data-northstar-browse')===region_ref):null;
  const inside=e=>{if(!region)return true;for(let node=e;node;node=node.getRootNode()?.host){if(node===region||region.contains(node))return true;}return false;};
  const labelText = e => {const copy=e.cloneNode(true);copy.querySelectorAll('input,textarea,select,button').forEach(x=>x.remove());return copy.textContent;};
  const label = e => clean(e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(' ').map(id=>e.getRootNode().getElementById?.(id)?.textContent || '').join(' ') ||
    [...e.labels || []].map(labelText).join(' ') || e.getAttribute('placeholder') || e.innerText ||
    (e.matches('input[type="button"], input[type="submit"], input[type="reset"]') ? e.value : '') || e.getAttribute('title') || e.name || e.id);
  const secret = e => e.matches('input[type="password"], [data-northstar-secret], input[autocomplete="one-time-code"]') || /otp|verificationcode/i.test(e.name || '');
  window.__northstarBrowseIds ||= {ids:new WeakMap(),next:1,prefix:Math.random().toString(36).slice(2,8)};
  const ids=window.__northstarBrowseIds;
  const mark=e=>{if(!ids.ids.has(e))ids.ids.set(e,`b-${ids.prefix}-${ids.next++}`);const id=ids.ids.get(e);e.setAttribute('data-northstar-browse',id);return id;};
  const elements=all('a[href],button,input,textarea,select,summary,[role="button"],[role="link"],[role="tab"],[role="checkbox"],[role="radio"],[role="combobox"],[role="option"],[role="menuitem"],[contenteditable="true"]')
    .filter(visible).filter(e=>!e.matches('input[type="hidden"]') && !secret(e));
  let controls=elements.map(e=>{
    const ref=mark(e);
    const form=e.form || e.closest('form');
    const name=label(e).slice(0,280), type=e.type || '', role=e.getAttribute('role') || '';
    const editable=e.isContentEditable || e.tagName==='TEXTAREA' || (e.tagName==='INPUT' && !['checkbox','radio','file','submit','button','hidden'].includes(type));
    const search=type==='search' || role==='searchbox' || !!e.closest('[role="search"]') ||
      (editable && /search|keyword|query|find jobs/i.test([name,e.name,e.id,e.getAttribute('placeholder')].join(' ')));
    return {ref,label:name,tag:e.tagName,type,role,editable,search,
      content_link:!e.closest('header,nav,footer,[role="navigation"]') && !!e.closest('main,article,li,[role="main"],[role="listitem"],.posting'),
      context:navigation_only?'':clean((e.closest('li,article,[role="listitem"]') || e.parentElement?.parentElement || e.parentElement)?.innerText).slice(0,360),
      href:e.tagName==='A'?e.href:null,disabled:!!e.disabled || e.getAttribute('aria-disabled')==='true',
      read_only:!!e.readOnly, checked:e.checked ?? e.getAttribute('aria-checked'),
      value:editable || e.tagName==='SELECT' ? String(e.value ?? e.innerText ?? '').slice(0,700) : undefined,
      options:e.tagName==='SELECT'?[...e.options].map(x=>({label:clean(x.label),value:x.value,disabled:x.disabled})).slice(0,120):undefined,
      submits_form:!!form && ['BUTTON','INPUT'].includes(e.tagName) && type==='submit',
      search_form:!!form && (form.getAttribute('role')==='search' || !!form.querySelector('input[type="search"], [role="searchbox"]') ||
        form.method.toLowerCase()==='get' && /search|query|keyword|\bq\b/i.test(form.innerText+' '+[...form.elements].map(x=>x.name).join(' ')))};
  });
  // A view filter or a fresh token is not a change to the underlying page.
  // Hash only source state here; no source text is added to model context.
  const sourceState=[location.href,document.title,document.body?.innerText || '',controls.map(c=>[c.label,c.tag,c.type,c.role,c.href,c.value,c.checked,c.disabled])];
  const sourceString=JSON.stringify(sourceState);
  let hashA=2166136261,hashB=5381;
  for(let i=0;i<sourceString.length;i++){hashA=Math.imul(hashA^sourceString.charCodeAt(i),16777619);hashB=Math.imul(hashB,33)^sourceString.charCodeAt(i);}
  const sourceHash=`${sourceString.length}:${hashA>>>0}:${hashB>>>0}`;
  // Filter the returned view only after hashing the complete non-secret source.
  // Changing region/navigation/query/offset must not reset no-progress guards.
  controls=controls.filter((c,i)=>inside(elements[i]) && (!navigation_only ||
    (elements[i].tagName==='A' && /^https:\/\//.test(elements[i].href)) ||
    (elements[i].matches('button,[role="button"]') && elements[i].type!=='submit' &&
      /^(?:close|dismiss|not now|maybe later|关闭|暂不|稍后)(?:\b|$)/i.test(label(elements[i])))));
  const needle=query.toLowerCase(),terms=needle.split(/\s+/).filter(Boolean);
  const matches=text=>terms.every(term=>text.toLowerCase().includes(term));
  if(ref) controls=controls.filter(c=>c.ref===ref);
  if(needle) controls=controls.filter(c=>matches([c.label,c.role,c.type,c.context,c.href || ''].join(' ')));
  const shadowText = root => {
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT), parts=[];
    for(let node=walker.nextNode();node;node=walker.nextNode()) {
      const parent=node.parentElement;
      if(parent && !parent.closest('style,script,noscript,template') && visible(parent) && clean(node.textContent)) parts.push(clean(node.textContent));
    }
    return parts.join('\n');
  };
  // Keep applicant questions and EEO forms out of a job-description snapshot.
  // Generic browser observations still include every visible control.
  const candidates=all('.job-description,.job__description,.posting-description,.simple-job-description-html,#jobDescriptionText,[data-testid="jobsearch-JobComponent-description"],[data-automation-id="jobPostingDescription"],.jobs-description__content,[itemprop="description"]').filter(visible).filter(inside);
  const descriptions=candidates.filter(e=>!candidates.some(other=>other!==e && e.contains(other)));
  const description=descriptions.length===1 && descriptions[0].innerText.trim().length>=100?descriptions[0]:null;
  const articles=all('article').filter(visible).filter(inside).filter(e=>e.querySelectorAll('h1').length===1);
  const jobRoot=description || (descriptions.length>1?null:region || (articles.length===1?articles[0]:null));
  let jobText='';
  if(job_only && jobRoot) {
    const copy=jobRoot.cloneNode(true);
    copy.querySelectorAll('script,style,noscript,nav,aside,footer,header,.previous-next,.related-jobs,.recommended-jobs,.similar-jobs,.job-recommendations').forEach(e=>e.remove());
    // A detached clone has no innerText layout; preserve block boundaries.
    copy.querySelectorAll('p,div,li,h1,h2,h3,br').forEach(e=>{e.prepend('\n');e.append('\n');});
    jobText=copy.textContent || '';
  }
  const body=navigation_only?'':(job_only?jobText:[(region || document.body)?.innerText || '',...roots.slice(1).filter(r=>visible(r.host)&&inside(r.host)).map(shadowText)].join('\n')).replace(/\n{3,}/g,'\n\n');
  const detailRoot=description?.closest('article,[data-testid="jobsearch-JobComponent"],.jobsearch-JobComponent,[data-testid="viewjob-main-content"]') || jobRoot;
  const headingRoot=job_only?detailRoot:region;
  const detailHeader=detailRoot?.querySelector('[data-testid="desktop-job-header"],.job-header,[itemprop="jobLocation"]');
  const headerText=detailHeader && visible(detailHeader)?detailHeader.innerText.trim().slice(0,1800):'';
  const metadata = selector => {
    if(!detailRoot)return '';
    const values=[...detailRoot.querySelectorAll(selector)].filter(visible).map(e=>clean(e.innerText)).filter(Boolean);
    return new Set(values).size===1?values[0]:'';
  };
  const headings=navigation_only?[]:all('h1,h2,h3,[role="heading"]').filter(visible).filter(inside)
    .filter(e=>!headingRoot || headingRoot.contains(e));
  const regions=navigation_only?[]:all('main,article,section,[role="main"],[role="region"],[role="list"],div').filter(visible).filter(inside).filter(e=>{
    const semantic=e.matches('main,article,section,[role="main"],[role="region"],[role="list"]');
    const scrollable=e.scrollHeight>e.clientHeight+30 && /auto|scroll/.test(getComputedStyle(e).overflowY);
    return (semantic||scrollable) && e.innerText?.trim().length>=80;
  }).map(e=>({ref:mark(e),label:clean(e.getAttribute('aria-label')||e.querySelector('h1,h2,h3,[role="heading"]')?.innerText||e.innerText).slice(0,160),
    context:clean(e.innerText).slice(0,360),
    tag:e.tagName,text_length:e.innerText.length,scrollable:e.scrollHeight>e.clientHeight+30 && /auto|scroll/.test(getComputedStyle(e).overflowY)}));
  const index=needle?body.toLowerCase().indexOf(needle):-1;
  const start=needle && index>=0?Math.max(0,index-200):text_offset;
  const detailTitle=clean(detailRoot?.querySelector('[data-testid="vj-job-title"],h1,h2,[role="heading"]')?.innerText);
  const locationText=metadata('.listing-location,.job-location,[itemprop="jobLocation"],[data-automation-id="locations"],[data-testid="inlineHeader-companyLocation"],[data-testid="job-location"]') ||
    headerText.match(/(?:^|\n)([A-Za-z][A-Za-z .'-]+, [A-Z]{2}(?: \d{5})?)(?=\n|$)/)?.[1] || '';
  // Work-mode badges can be siblings of the postal location. Only the same
  // bounded job header can supply one; search filters and sidebars cannot.
  const mode=headerText.match(/(?:^|\n)\s*(Remote|Hybrid|On[- ]site)\s*(?=\n|$)/i)?.[1] || '';
  const jobLocation=locationText && mode && !locationText.toLowerCase().includes(mode.toLowerCase())?`${locationText}; ${mode}`:locationText;
  return {title:navigation_only?'':document.title,page_evidence_hash:sourceHash,
    primary_heading:job_only && detailTitle?detailTitle:clean((headings.find(e=>e.tagName==='H1') || headings[0])?.innerText),heading:headings.map(e=>clean(e.innerText)).join(' / ').slice(0,600),
    ...(!navigation_only && (description || descriptions.length===0 && articles.length===1)?{job_detail:{body_scope:description?'bounded_selector':'bounded_article',title:detailTitle,
      instruction:'A single bounded posting body is available. Use read_job to capture it instead of rereading search controls.'}}:{}),
    ...(job_only?{job_header_text:headerText,
      job_location:jobLocation,
      job_company:metadata('[itemprop="hiringOrganization"],[data-testid="inlineHeader-companyName"],.job-company')}:{}),
    text:body.slice(start,start+7000),text_offset:start,text_length:body.length,region_ref:region_ref||null,
    ...(job_only?{body_scope:description?'bounded_selector':region?'explicit_region':jobRoot?'bounded_article':'unbounded'}:{}),
    // A text query filters content/controls, not the live identity of regions.
    // Otherwise a filtered read erases a still-visible detail panel's reference.
    regions:regions.slice(0,40),
    controls:controls.slice(offset,offset+90),control_offset:offset,control_count:controls.length,
    errors:navigation_only?[]:all('[role="alert"],.field-error,.error-message').filter(visible).filter(inside).map(e=>clean(e.innerText)).filter(Boolean).slice(0,12),
    viewport:{width:innerWidth,height:innerHeight,scroll_x:scrollX,scroll_y:scrollY}};
}"""


class BrowserSession:
    def __init__(self, browser, *, application=False):
        self.browser, self.application = browser, application
        self.observation = None
        self.scope = None
        self.frame = None
        self.screen = None
        self._tabs = {}

    def _tab_list(self):
        for page in self.browser.context.pages:
            if not page.is_closed() and page not in self._tabs.values():self._tabs[str(uuid4())[:8]] = page
        self._tabs = {key:p for key,p in self._tabs.items() if not p.is_closed()}
        return [{'id':key,'url':p.url,'active':p==self.browser.page} for key,p in self._tabs.items()]

    def _secrets(self):
        visible = ', '.join(s.strip()+':visible' for s in SECRET_SELECTOR.split(','))
        return any(frame.locator(visible).count() or frame.evaluate(r"""() =>
          [...document.querySelectorAll('h1,h2,legend,[role="heading"]')].some(e=>
            e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' &&
            /sign.?in|log.?in|create.*account|candidate account|sign.?up|register|captcha|verification|verify (?:your )?(?:identity|email)/i.test(e.innerText))
        """) for frame in self.browser.page.frames)

    def read(self, *, query='', offset=0, text_offset=0, region_ref=None, observation_id=None, job_only=False):
        if region_ref:self._region(observation_id,region_ref)
        for attempt in range(4):
            try:return self._read(query=query,offset=offset,text_offset=text_offset,region_ref=region_ref,job_only=job_only)
            except Exception as exc:
                navigating=any(text in str(exc).casefold() for text in ('execution context was destroyed','frame was detached','cannot find context','most likely because of a navigation'))
                if not navigating:raise
                if attempt==3 or region_ref:raise BrowserBlocked('The page is still navigating. Use browser_wait, then inspect its current controls.') from None
                self.frame=None
                self.browser.page.wait_for_timeout(250)

    def _read(self, *, query='', offset=0, text_offset=0, region_ref=None, job_only=False):
        if not isinstance(query,str) or len(query)>200:raise BrowserBlocked('Use a short text filter.')
        if any(type(n) is not int or n<0 for n in (offset,text_offset)):raise BrowserBlocked('Offsets must be nonnegative integers.')
        self.browser._check_page()
        page = self.browser.page
        if self.frame not in page.frames:self.frame = None
        self.scope = self.frame or (self.browser.scope if self.application else page.main_frame)
        tabs = self._tab_list()
        if self._secrets():
            navigation=self.scope.evaluate(SCAN,{'query':query,'offset':offset,'text_offset':0,'navigation_only':True})
            result = {'url':page.url,'heading':'Account or verification step','text':
                'Account contents are hidden. You can use the observed public links or dismiss controls. If sign-in or verification is required, ask the user to click Control browser in Agent activity, complete it there and reply to continue. Do not ask for credentials in chat.',
                'controls':navigation['controls'],'control_count':navigation['control_count'],'regions':[],
                'blocked':'credentials','tabs':tabs}
            if not self.application:
                result['next']='This research source requires sign-in. Switch immediately to a public company board or search_jobs; do not repeat the login wall or ask for credentials for public research.'
        else:
            result = {**self.scope.evaluate(SCAN,{'query':query,'offset':offset,'text_offset':text_offset,'region_ref':region_ref,'job_only':job_only}),
                'url':page.url,'frame_url':self.scope.url,'tabs':tabs,
                'frames':[{'id':str(i),'url':f.url,'active':f==self.scope} for i,f in enumerate(page.frames)]}
            if not self.application and re.search(r'just a moment|verif(?:y|ying) you are (?:a )?human|checking your browser|cloudflare ray id|access denied',
                    ' '.join(str(result.get(k,'')) for k in ('title','primary_heading','text'))[:1200],re.I):
                result.update(blocked='bot_challenge',next='Switch immediately to another public source or search_jobs. Headless browsers can be blocked; repeating this page will not verify a job.')
        self.observation = {**result,'query':query,'observation_id':str(uuid4())}
        if self.observation.get('job_detail'):
            self.observation['job_detail']['read_job_arguments']={'url':page.url}
        return self.observation

    def _fresh(self, token):
        if not self.observation or token!=self.observation['observation_id']:
            raise ObservationExpired('The observation is stale. Read the current browser state.')
        if self.browser.page.is_closed() or self.browser.page.url!=self.observation['url']:
            raise ObservationExpired('The page changed. Read it before acting.')

    def _target(self, token, ref):
        self._fresh(token)
        item = next((c for c in self.observation['controls'] if c['ref']==ref),None)
        if not item:raise BrowserBlocked('Choose a control from the latest browser observation.')
        locator = self.scope.locator(f'[data-northstar-browse="{ref}"]')
        if locator.count()!=1 or not locator.is_visible():raise ObservationExpired('The control moved or disappeared. Read the page again.')
        if item['disabled'] or not locator.is_enabled():raise BrowserBlocked('The control is disabled. Wait or inspect the page.')
        if locator.evaluate("e=>e.matches('input[type=password],[data-northstar-secret],[autocomplete=one-time-code]')"):
            raise BrowserBlocked('Use the secure account form for credentials.')
        if (locator.get_attribute('href') is not None and locator.evaluate('e=>e.href') != item['href']):
            raise ObservationExpired('The link destination changed. Read the page again.')
        fresh=self.scope.evaluate(SCAN,{'query':'','offset':0,'text_offset':0,'ref':ref})['controls']
        if not fresh or any(fresh[0].get(k)!=item.get(k) for k in ('label','tag','type','role','submits_form','search_form')):
            raise ObservationExpired('The control changed purpose. Read the page before acting.')
        return item,locator

    def _region(self,token,ref):
        self._fresh(token)
        item=next((r for r in self.observation.get('regions',[]) if r['ref']==ref),None)
        if not item:raise BrowserBlocked('Choose a region from the latest browser observation.')
        locator=self.scope.locator(f'[data-northstar-browse="{ref}"]')
        if locator.count()!=1 or not locator.is_visible():raise ObservationExpired('The panel moved or disappeared. Read the page again.')
        return locator

    @staticmethod
    def _guard(item, *, application=False):
        kind = action_kind(item,has_fields=True)
        if item.get('search_form') and kind not in {'submit','authentication'}:return
        if kind in {'submit','guarded'} or re.search(r'\b(?:buy|purchase|pay|place order|send message|post comment|delete|withdraw|unsubscribe)\b',item['label'],re.I):
            raise BrowserBlocked('This action sends, agrees to, purchases or deletes something. Use the task-specific review workflow.')
        if item.get('type')=='file':raise BrowserBlocked('Use the approved document upload workflow.')
        if item.get('type') in {'checkbox','radio'} or item.get('role') in {'checkbox','radio'}:
            raise BrowserBlocked('Use browser_check for an ordinary toggle; application answers and consent use their dedicated tools.')
        if application and kind=='next':raise BrowserBlocked('Use click_page_action so application validation and review history are preserved.')

    def navigate(self,url):
        fragment=urlsplit(url).fragment
        url = public_https_url(url) + ('#'+fragment if fragment else '')
        if self.application and urlsplit(url).hostname not in self.browser.navigation_hosts:
            raise BrowserBlocked('For another site, follow an observed link or use the main browsing capability.')
        self.frame, self.screen = None,None
        self.browser.navigation_hosts.add(urlsplit(url).hostname)
        try:self.browser.page.goto(url,wait_until='domcontentloaded',timeout=25000)
        except Exception as exc:
            if 'Timeout' not in type(exc).__name__:raise
            # A usable DOM can exist while a tracker is still loading.
        return self.read()

    def click(self, observation_id, ref):
        item,locator = self._target(observation_id,ref)
        self._guard(item,application=self.application)
        if item['href']:
            if item['href'].startswith(self.browser.page.url.split('#')[0]+'#'):pass
            else:
                url = public_https_url(item['href'])
                self.browser.navigation_hosts.add(urlsplit(url).hostname)
        self.screen = None
        locator.click(timeout=8000)
        self.browser.page.wait_for_timeout(350)
        self.frame = None if self.scope not in self.browser.page.frames else self.frame
        return self.read()

    def type(self, observation_id, ref, text):
        item,locator = self._target(observation_id,ref)
        if not isinstance(text,str) or len(text)>6000:raise BrowserBlocked('Type at most 6000 characters.')
        if not item['editable'] or item['read_only']:raise BrowserBlocked('Choose an editable text control.')
        if self.application and not item['search']:
            raise BrowserBlocked('Application answers require the grounded field tools. Free text here is for search queries.')
        self.screen = None
        locator.fill(text)
        return self.read()

    def select(self, observation_id, ref, option):
        item,locator = self._target(observation_id,ref)
        if self.application or re.search(r'consent|agree|authorize|privacy|terms|permission|acknowledge',item['label'],re.I):
            raise BrowserBlocked('Use source-aware application option tools for applicant facts or consent.')
        options = [o for o in item.get('options') or [] if option in {o['value'],o['label']} and not o['disabled']]
        if len(options)!=1:raise BrowserBlocked('Select an exact observed option, or click to inspect a custom dropdown.')
        locator.select_option(value=options[0]['value'])
        self.screen = None
        return self.read()

    def check(self, observation_id, ref, checked):
        item,locator = self._target(observation_id,ref)
        if self.application or re.search(r'consent|agree|authorize|privacy|terms|permission|subscribe|acknowledge',item['label'],re.I):
            raise BrowserBlocked('Use confirmed application answers or ask the user about this consent.')
        if type(checked) is not bool or item['type'] not in {'checkbox','radio'}:
            raise BrowserBlocked('Choose an observed checkbox or radio and a boolean value.')
        locator.set_checked(checked)
        self.screen = None
        return self.read()

    def key(self, observation_id, ref, key):
        item,locator = self._target(observation_id,ref)
        if key not in {'Enter','Tab','Shift+Tab','Escape','ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End'}:
            raise BrowserBlocked('Use a navigation key from the tool schema.')
        choice=item['tag']=='SELECT' or item['type'] in {'checkbox','radio'} or item['role'] in {'checkbox','radio','combobox','option'}
        if choice and key not in {'Tab','Shift+Tab','Escape'} and (self.application or re.search(r'consent|agree|authorize|privacy|terms|permission',item['label'],re.I)):
            raise BrowserBlocked('Use confirmed option/consent tools for this choice.')
        if key=='Enter':
            if item['search']:
                # A search input inside an application form must not accidentally
                # submit that application. Inspect its actual form first.
                safe = locator.evaluate("e=>!e.form || e.form.method==='get' || e.form.getAttribute('role')==='search'")
                if not safe:raise BrowserBlocked('Click the observed search control instead of submitting this form.')
            elif item['editable']:
                if self.application or locator.evaluate('e=>!!e.form'):raise BrowserBlocked('Enter may submit this form. Use its observed action control.')
            else:self._guard(item,application=self.application)
        locator.press(key)
        self.screen = None
        self.browser.page.wait_for_timeout(250)
        return self.read()

    def back(self):
        self.frame,self.screen = None,None
        # A page restored from the back/forward cache fires no new DOMContentLoaded (Patchright
        # waited the full timeout): wait for the navigation to commit, then briefly for loading.
        self.browser.page.go_back(wait_until='commit',timeout=15000)
        try:self.browser.page.wait_for_load_state('domcontentloaded',timeout=5000)
        except Exception as exc:
            if 'Timeout' not in type(exc).__name__:raise
        return self.read()

    def tabs(self, action='list', tab_id=None):
        self._tab_list()
        if action not in {'list','switch','close'}:raise BrowserBlocked('Choose list, switch or close.')
        if action!='list':
            page = self._tabs.get(tab_id)
            if page is None:raise BrowserBlocked('Use an observed tab ID.')
            if action=='close':
                if len(self._tabs)==1:raise BrowserBlocked('Keep one browser tab open, or close the browser task.')
                page.close()
                page = next(p for p in self._tabs.values() if not p.is_closed())
            self.browser.page = page
            self.frame,self.screen = None,None
        return self.read()

    def choose_frame(self, observation_id, frame_id):
        self._fresh(observation_id)
        allowed = {f['id'] for f in self.observation.get('frames',[])}
        if frame_id not in allowed:raise BrowserBlocked('Use an observed frame ID.')
        self.frame = self.browser.page.frames[int(frame_id)]
        if self.application:
            if urlsplit(self.frame.url).hostname not in self.browser.navigation_hosts:
                self.frame = None
                raise BrowserBlocked('This application frame is outside the current allowed site. Use ordinary browsing to inspect other sites.')
            self.browser._preferred_frame = self.frame
        self.screen = None
        return self.read()

    def scroll(self, delta_y, region_ref=None, observation_id=None):
        if isinstance(delta_y,bool) or not isinstance(delta_y,(int,float)) or not math.isfinite(delta_y) or abs(delta_y)>1800:
            raise BrowserBlocked('Scroll at most 1800 pixels, then observe again.')
        if region_ref:
            locator=self._region(observation_id,region_ref)
            if self._secrets():raise BrowserBlocked('Account contents are hidden. Complete the account step before scrolling a detail panel.')
            locator.evaluate('(e,dy)=>e.scrollBy(0,dy)',delta_y)
        else:self.browser.page.mouse.wheel(0,delta_y)
        self.browser.page.wait_for_timeout(200)
        self.screen = None
        return self.read()

    def wait(self):
        self.browser.page.wait_for_timeout(1200)
        self.screen = None
        return self.read()

    def screenshot(self):
        observed = self.read()
        if observed.get('blocked'):raise BrowserBlocked('Account pages use the secure workflow; no account screenshots are sent.')
        before=self._fingerprint()
        image = self.browser.page.screenshot(type='png',scale='css',timeout=5000,
            mask=[f.locator(SECRET_SELECTOR) for f in self.browser.page.frames])
        if self._secrets():raise BrowserBlocked('An account or verification step appeared. No screenshot was sent.')
        if len(image)>6_000_000:raise BrowserBlocked('The viewport image is too large; reduce the browser window.')
        after=self._fingerprint()
        size=self.browser.page.evaluate('() => ({width:innerWidth,height:innerHeight})')
        self.screen = {'id':str(uuid4()),'url':self.browser.page.url,'stamp':after if before==after else None,
            'captured':time.monotonic(),**size}
        return {**observed,'screenshot_id':self.screen['id'],'coordinate_system':'normalized_1000',
            'image_width':size['width'],'image_height':size['height'],'image':image}

    def _fingerprint(self):
        states=[f.evaluate(SCREEN_STATE) for f in self.browser.page.frames]
        return hashlib.sha256(json.dumps(states,sort_keys=True).encode()).hexdigest()

    def click_at(self, screenshot_id, x, y):
        if self.application:raise BrowserBlocked('Use the application computer_click tool so applicant answers stay grounded.')
        if not self.screen or screenshot_id!=self.screen['id']:raise BrowserBlocked('Take a fresh screenshot before a visual action.')
        if any(isinstance(n,bool) or not isinstance(n,(int,float)) or not math.isfinite(n) or not 0<=n<1000 for n in (x,y)):
            raise BrowserBlocked('Use normalized screenshot coordinates between 0 and 1000.')
        screen = self.screen
        current = self.read()
        if current.get('blocked') or current['url']!=screen['url'] or time.monotonic()-screen['captured']>120 or self._fingerprint()!=screen['stamp']:
            raise BrowserBlocked('The page changed. Take another screenshot.')
        # The model chooses the pixel. Hit testing only checks the effect boundary,
        # including controls inside frames/shadow roots or without ARIA roles.
        visual=VisualBrowser(self.browser);visual.screen=screen
        target=visual.target(x,y)
        if target['secret']:raise BrowserBlocked('Use the secure account form for credentials.')
        if target['disabled']:raise BrowserBlocked('This visual target is disabled.')
        self._guard(target)
        if target['href']:
            self.browser.navigation_hosts.add(urlsplit(public_https_url(target['href'])).hostname)
        self.screen=None
        self.browser.page.mouse.click(x*screen['width']/1000,y*screen['height']/1000)
        self.browser.page.wait_for_timeout(350)
        observed=self.read()
        return observed if observed.get('blocked') else self.screenshot()
