"""DOM actions for public application pages and multi-step candidate workflows.

This adapter never invents answers and never retries a submit click. Dynamic or
unrecognized controls are returned to the candidate for browser interaction.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from threading import local
from urllib.parse import parse_qs, urlsplit

from .errors import BrowserBlocked
from .contracts import GREENHOUSE, LEVER, posting, public_https_url
from .matching import clean, equivalent, resolve_option, search_text
from .navigation import PAGE_SCAN, page_state

SCAN = r"""() => {
  const visible = el => !!(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden';
  const clean = text => (text || '').replace(/\s+/g, ' ').trim().slice(0, 1000);
  const nearbyLabel = el => {
    for(let node=el.parentElement, depth=0;node && depth<4;node=node.parentElement,depth++){
      const direct=node.querySelector(':scope > label, :scope > legend');
      if(direct && !direct.contains(el))return clean(direct.textContent);
      if(node.matches('[data-field-path], fieldset'))break;
    }
    return '';
  };
  // A <label> that wraps its own control (Lever EEO selects) must not absorb the
  // option text ("Veteran statusSelect ...I identify..."), or saved answers never match.
  const ownText = (lab, el) => {
    if (!lab.contains(el)) return clean(lab.textContent);
    const copy = lab.cloneNode(true);
    copy.querySelectorAll('select, option, input, textarea, button, [role="listbox"], [role="option"]').forEach(n => n.remove());
    return clean(copy.textContent);
  };
  const label = el => clean([...new Set(Array.from(el.labels || []).map(x => ownText(x, el)))].join(' ') ||
    el.closest('[data-automation-id^="formField-"]')?.querySelector('label')?.textContent ||
    // An explicit reference names the control better than the nearest label,
    // which can belong to a sibling field. Skip a self-reference (its value).
    (el.getAttribute('aria-labelledby') || '').split(' ').filter(id => id && id !== el.id).map(id => document.getElementById(id)?.textContent || '').join(' ').trim() ||
    (el.matches('button[aria-haspopup="listbox"]') && nearbyLabel(el)) ||
    el.getAttribute('aria-label') ||
    el.closest('.application-question')?.querySelector('.application-label')?.textContent || nearbyLabel(el) || el.name || el.id);
  const root = [...document.querySelectorAll('[role="dialog"], dialog[open]')].filter(visible).at(-1) || document;
  document.querySelectorAll('[data-northstar-field]').forEach(el => el.removeAttribute('data-northstar-field'));
  const elements = Array.from(root.querySelectorAll('input, textarea, select, [role="combobox"], button[aria-haspopup="listbox"], button[aria-pressed][data-option]'));
  const counts = new Map(), fields = [], groups = new Set();
  for (const [index, el] of elements.entries()) {
    if (el.getAttribute('role') === 'combobox' && !['INPUT','BUTTON','SELECT'].includes(el.tagName) && el.querySelector('input:not([type="hidden"])')) continue;
    // select2 (Lever school pickers) hides the real <select> and renders a combobox named by
    // its displayed text. The select carries the question, options and required flag.
    if (el.closest('.select2-container')) continue;
    const select2 = el.tagName === 'SELECT' && el.classList.contains('select2-hidden-accessible') &&
      !!el.nextElementSibling?.matches('.select2-container') && visible(el.nextElementSibling);
    const buttonChoice = el.matches('button[aria-pressed][data-option]');
    const kind = buttonChoice ? 'radio' : el.tagName === 'SELECT' ? 'select' : el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' ? 'combobox' :
      el.name === 'location' && el.parentElement.querySelector('input[name="selectedLocation"]') ? 'location' : (el.type || 'text');
    if (['hidden', 'password', 'submit', 'button', 'reset', 'image'].includes(kind) ||
        el.hasAttribute('data-northstar-secret') || el.autocomplete === 'one-time-code' || /otp|verificationcode/i.test(el.name || '') || el.disabled ||
        el.getAttribute('data-automation-id') === 'beecatcher') continue;
    const hiddenField = kind !== 'file' && !select2 && (!visible(el) || el.closest('[aria-hidden="true"]'));
    if(hiddenField && kind === 'radio')continue;
    if (kind === 'file' && !el.closest('form, #application, .application-form, [data-automation-id], [role="form"], [role="tabpanel"], [data-field-path]')) continue;
    if (kind === 'file' && el.closest('form, [role="form"], [role="tabpanel"]') && !visible(el.closest('form, [role="form"], [role="tabpanel"]'))) continue;
    if (kind === 'file' && !label(el)) continue; // Ignore an unlabelled resume-autofill dropzone.
    const id = `ns-${index}`; el.setAttribute('data-northstar-field', id);
    const wrapper = el.closest('[data-automation-id^="formField-"], .application-question, .form-field, .field, [data-field-path]');
    const group = el.closest('fieldset, [role="group"]');
    const sections=[];
    for(let parent=el.parentElement;parent && parent!==root;parent=parent.parentElement){
      const heading=clean(parent.querySelector(':scope > legend, :scope > h2, :scope > h3, :scope > h4, :scope > [data-automation-id="formLabel"], :scope > label')?.textContent);
      if(heading && !sections.includes(heading))sections.push(heading);
    }
    const described = attr => (el.getAttribute(attr) || '').split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
    const errors = [...described('aria-errormessage'), ...described('aria-describedby').filter(x => /error/i.test(x.id)),
      ...Array.from(wrapper?.querySelectorAll('[role="alert"], [data-automation-id="errorMessage"], .field-error, .error-message') || [])]
      .filter(visible).map(x => clean(x.textContent)).filter(Boolean);
    if (el.validationMessage) errors.push(el.validationMessage);
    const choice = kind === 'combobox' || kind === 'select' || kind === 'radio' ||
      ['listbox','tree','true'].includes(el.getAttribute('aria-haspopup')) ||
      !!el.closest('[data-automation-id="multiSelectContainer"], [role="combobox"]');
    let committed = false, checkboxGroup = false;
    let text = label(el), options = [], value = el.getAttribute('aria-valuetext') || el.value || '', required = el.required || el.getAttribute('aria-required') === 'true';
    // An empty Workday date segment shows its own placeholder ("MM", "YYYY") as its value.
    if (/dateSection(Month|Day|Year)/.test(el.id) && /^(MM|DD|YYYY)$/.test(value)) value = '';
    let invalid = el.getAttribute('aria-invalid') === 'true' || (!!el.validity && !el.validity.valid) || errors.length > 0;
    let parentRequired = !!el.closest('.application-question.required, [data-required="true"]');
    if (buttonChoice) {
      const owner=el.parentElement;
      if(groups.has(owner))continue;groups.add(owner);
      const members=elements.filter(x=>x.parentElement===owner && x.matches('button[aria-pressed][data-option]'));
      text=clean(wrapper?.querySelector(':scope > label')?.textContent || nearbyLabel(el));
      options=members.map(x=>{const token=`ns-${elements.indexOf(x)}`;x.setAttribute('data-northstar-field',token);return {label:clean(x.textContent),value:x.getAttribute('data-option'),id:token,button:true};});
      value=members.find(x=>x.getAttribute('aria-pressed')==='true')?.getAttribute('data-option') || '';
      committed=!!value;
      required=required || !!wrapper?.querySelector('[required], [aria-required="true"], label[class*="required"]');
    } else if (kind === 'radio') {
      const group = el.name || id; if (groups.has(group)) continue; groups.add(group);
      const members = elements.filter(x => x.type === 'radio' && (x.name || `ns-${elements.indexOf(x)}`) === group);
      // Without a legend (Ashby), the question is the nearest enclosing label/heading that is
      // not one of the options. Falling back to the first option's text named a race field
      // "Hispanic or Latino", and saved answers were applied to the wrong question.
      const optionTexts = new Set(members.map(x => label(x)));
      const groupTitle = () => {
        let node = members[0].parentElement;
        while (node && !members.every(m => node.contains(m))) node = node.parentElement;
        for (let up = 0; node && up < 4; up++, node = node.parentElement) {
          const found = [...node.querySelectorAll('label, legend, h2, h3, h4, [class*="title" i], [class*="heading" i], [class*="question" i]')]
            .filter(x => !members.some(m => x.contains(m) || (m.labels && [...m.labels].includes(x))))
            .map(x => clean(x.textContent)).find(t => t && t.length <= 400 && !optionTexts.has(t));
          if (found) return found;
        }
        return '';
      };
      text = clean(el.closest('fieldset')?.querySelector('legend')?.textContent || el.closest('.application-question')?.querySelector('.application-label')?.textContent || groupTitle() || text);
      options = members.map((x) => {const token=`ns-${elements.indexOf(x)}`;x.setAttribute('data-northstar-field',token);return {label:label(x),value:x.value,id:token};});
      // Show the chosen option, not a generic "on" shared by every Ashby radio.
      const chosen = members.find(x => x.checked);
      value = chosen ? (chosen.value && chosen.value !== 'on' ? chosen.value : label(chosen)) : ''; required = members.some(x => x.required);
    } else if (kind === 'select') {
      options = Array.from(el.options).map(x => ({label: clean(x.textContent), value:x.value, disabled:x.disabled}));
      const part=clean(el.options[0]?.textContent).replace(/\.+$/, '');
      if(/^(month|year|day)$/i.test(part) && text && !text.toLowerCase().includes(part.toLowerCase()))text+=` ${part}`;
    }
    else if (kind === 'checkbox') {
      const description = clean(el.getAttribute('description') || (el.getAttribute('aria-describedby') || '').split(' ')
        .filter(id => id.endsWith('-description')).map(id => document.getElementById(id)?.textContent || '').join(' '));
      if (description && !text.includes(description)) text = `${description} — ${text}`;
      value = el.checked;
      const peers = elements.filter(x => x.type === 'checkbox' && x.name && x.name === el.name);
      const leverGroup = !!el.closest('[data-qa="checkboxes"]');
      // Greenhouse repeats native required on every member of a multi-choice
      // question. Its shared legend/error describes the group, not each choice.
      const errorId = el.getAttribute('aria-errormessage');
      const greenhouseGroup = el.name.endsWith('[]') && group?.querySelector(':scope > legend.checkbox__description') &&
        errorId && peers.every(x => x.closest('fieldset, [role="group"]') === group && x.getAttribute('aria-errormessage') === errorId);
      if (peers.length > 1 && (parentRequired || leverGroup || greenhouseGroup)) {
        checkboxGroup = true;
        const groupRequired = parentRequired || peers.some(x => x.required);
        // Only discard native valueMissing. Keep visible website errors and
        // independent required consent controls intact.
        if (el.validity?.valueMissing && el.validationMessage && errors.at(-1) === el.validationMessage) errors.pop();
        invalid = el.getAttribute('aria-invalid') === 'true' || errors.length > 0 ||
          (groupRequired && peers[0] === el && !peers.some(x => x.checked));
        required = false;
        parentRequired = false;
      }
      const groupLabel = clean(el.closest('.application-question')?.querySelector('.application-label')?.textContent);
      if (peers.length > 1 && groupLabel) text = `${groupLabel.replace(/[✱*]/g, '').trim()} — ${text}`;
    } else if (kind === 'file') {
      // Upload buttons may be labelled only 'Attach'; use the containing
      // accessible group's document label (e.g. Cover Letter).
      const uploadLabel = clean((group?.getAttribute('aria-labelledby') || '').split(/\s+/)
        .map(id => document.getElementById(id)?.textContent || '').join(' ') || group?.getAttribute('aria-label'));
      if (uploadLabel) text = uploadLabel;
      value = el.files?.length ? el.files[0].name : '';
      // Workday clears the native input after transferring the file. Read the
      // successful attachment cards within this upload control, not page text.
      const upload = el.closest('[data-automation-id="attachments-FileUpload"]');
      const attached = [...(upload?.querySelectorAll('[data-automation-id="file-upload-item"]') || [])]
        .filter(item => item.querySelector('[data-automation-id="file-upload-successful"]'))
        .map(item => clean(item.querySelector('[data-automation-id="file-upload-item-name"]')?.textContent)).filter(Boolean);
      if (attached.length) value = [...new Set(attached)].join('; ');
      if ((el.id === 'resume' || el.name === 'resume')) { text = 'Resume/CV'; required = true; }
    } else if (kind === 'combobox') {
      const shell = el.closest('.select__container, .select-shell');
      if (shell) {
        value = clean(Array.from(shell.querySelectorAll('.select__single-value, .select__multi-value__label')).map(x => x.textContent).join('; '));
        // Telephone country widgets collapse the selected label to a flag and
        // shared dial code. Keep the observed flag identity (US vs CA at +1).
        const flag = shell.querySelector('.select__single-value .iti__flag');
        const region = flag && [...flag.classList].map(c => /^iti__([a-z]{2})$/.exec(c)).find(Boolean);
        if (region && /^\+\d{1,3}$/.test(value)) value = `${region[1].toUpperCase()} ${value}`;
        committed=!!value;
      }
      else if (el.tagName === 'BUTTON') {
        value = clean(el.textContent);
        if (/^(select|choose)(?: one| an? option)?(?:\.\.\.)?$/i.test(value)) value = '';
      }
    } else if (kind === 'location') {
      value = el.parentElement.querySelector('input[name="selectedLocation"]').value ? el.value : '';
      text = clean(el.closest('.application-question')?.querySelector('.application-label .text')?.textContent) || 'Current location';
    }
    if (choice && !['select','radio'].includes(kind)) {
      const shell = el.closest('[data-automation-id="multiSelectContainer"], [role="combobox"]');
      const selected = [...(shell?.querySelectorAll('[data-automation-id="selectedItem"], [data-automation-id="selectedItemLabel"], [data-automation-id="selectedPrompt"]') || [])].filter(visible).map(x => clean(x.textContent));
      if (selected.length) {value = [...new Set(selected)].join('; ');committed=true;}
    }
    // Workday ids carry a per-render instance number ("education-148--fieldOfStudy" was
    // "education-10--fieldOfStudy" before a re-render). Keys drop it; which entry a field
    // belongs to is its occurrence order below. dom_id keeps the raw id for locating.
    const stableId = (el.id || '').replace(/^([A-Za-z]+)-\d+--/, '$1--');
    const base = `${kind}:${el.name || stableId || (buttonChoice && wrapper?.getAttribute("data-field-path")) || text}${kind === 'checkbox' ? ':' + el.value : ''}`;
    const occurrence = (counts.get(base) || 0) + 1; counts.set(base, occurrence);
    const key = `${base}:${occurrence}`.slice(0, 1000);
    // Modal menus hide sibling controls using aria-hidden. Count their stable
    // occurrences before excluding them, or row 2 becomes row 1 while editing.
    if(hiddenField)continue;
    fields.push({id, dom_id:el.id, name:el.name || '', key, kind, label:text || 'Unlabelled field', required:!checkboxGroup && !!(required || parentRequired || text.includes('*') || text.includes('✱')), options, value, invalid,
      choice_control:choice, value_committed:committed, ...(select2 ? {select2:true} : {}), errors:[...new Set(errors)], help:clean(described('aria-describedby').map(x=>x.textContent).join(' ')),
      group:clean(group?.querySelector('legend')?.textContent || group?.getAttribute('aria-label')), section:sections.join(' / ').slice(0,500),
      constraints:{read_only:!!el.readOnly, pattern:el.getAttribute('pattern'), max_length:el.getAttribute('maxlength'), min_length:el.getAttribute('minlength'),
        placeholder:el.getAttribute('placeholder'), input_mode:el.getAttribute('inputmode'), autocomplete:el.getAttribute('autocomplete')}});
  }
  return fields;
}"""


OPTION_SCAN = r"""els => {
  const clean = value => (value || '').replace(/\s+/g, ' ').trim();
  const members = new Set(els);
  // An explicit accessible name excludes decorative text such as a hidden
  // "Select" affordance inside the option row.
  // Workday rows announce state ("Job Board not checked"); the inner item is just "Job Board".
  const name = el => clean(el.getAttribute('aria-label')).replace(/,?\s+(?:not checked|checked)$/i, '') || clean(el.innerText);
  return els.map((el, index) => {
    const text = name(el);
    // Nested wrappers describe the same choice. Keep the outer clickable node,
    // but retain distinct nested tree choices with different text.
    for (let parent = el.parentElement; parent; parent = parent.parentElement) {
      if (members.has(parent) && text && name(parent) === text) return null;
    }
    return {index, label:text || clean(el.getAttribute('aria-label')),
      value:clean(el.getAttribute('data-value')) || clean(el.querySelector('[class*="canonicalSchoolResultName"]')?.textContent) || text || clean(el.getAttribute('aria-label')),
      role:el.getAttribute('role'), automation_id:el.getAttribute('data-automation-id'),
      // Workday categories ("Adobe Source") carry a side chevron that opens their sub-list.
      expandable:el.hasAttribute('aria-expanded') || !!el.querySelector('[aria-expanded], [aria-haspopup]') ||
        el.matches('[data-uxi-multiselectlistitem-hassidecharm="true"]') || !!el.querySelector('[data-uxi-multiselectlistitem-hassidecharm="true"]'),
      disabled:el.getAttribute('aria-disabled') === 'true'};
  }).filter(Boolean);
}"""

# Cookie banners covering a form cost the agent minutes of page reads (Palantir pilot,
# 2026-10-15). Privacy first: choose "reject"/"necessary only" when offered; otherwise hide
# the banner without accepting. Only known consent containers are touched.
CONSENT = r"""() => {
  const boxes = '.cc-window, #onetrust-banner-sdk, #onetrust-consent-sdk, #CybotCookiebotDialog, #truste-consent-track, ' +
    '#usercentrics-root, .osano-cm-dialog, [class*="cookie-banner"], [id*="cookie-banner"], [class*="cookieBanner"], [id*="cookieBanner"]';
  const visible = e => e.offsetParent !== null || getComputedStyle(e).position === 'fixed';
  const banners = [...document.querySelectorAll(boxes)].filter(visible);
  if (!banners.length) return '';
  const reject = /^(reject( all)?|decline( all)?|deny( all)?|necessary only|only necessary|essential only|only essential|use necessary cookies only|reject non-essential( cookies)?|refuse( all)?)$/i;
  for (const banner of banners) {
    const button = [...banner.querySelectorAll('button, a, [role="button"]')]
      .find(b => visible(b) && reject.test((b.innerText || b.getAttribute('aria-label') || '').trim()));
    if (button) { button.click(); return 'rejected'; }
  }
  for (const banner of banners) banner.style.setProperty('display', 'none', 'important');
  return 'hidden';
}"""

_playwright_runtime = local()
MAX_FIELDS = 400
# A hidden browser is a real Chrome window placed off-screen by default. Sites
# treat headless Chrome as automation far more often; JOB_AGENT_HIDDEN_BROWSER=
# headless restores the previous behaviour.
OFFSCREEN = {"left": -32000, "top": -32000}
ON_SCREEN = {"left": 80, "top": 60, "width": 1280, "height": 900}
TRUSTED_NAVIGATION_SECONDS = 10
IDENTITY_PROVIDERS = re.compile(r"(?:^|\.)(?:accounts\.google\.com|login\.microsoftonline\.com|login\.live\.com|"
    r"okta\.com|oktapreview\.com|onelogin\.com|auth0\.com|appleid\.apple\.com|linkedin\.com)$")
CONFIRMATION_TEXT = re.compile(r"application (?:has been |was |is )?(?:submitted|received)\b|"
    r"thank(?:s| you) for (?:applying|your application|submitting)|we(?:'ve|’ve| have) received your application|"
    r"successfully (?:submitted|applied)|your application (?:has been|was) (?:sent|submitted|received)", re.I)
SUBMISSION_ERROR = re.compile(r"\b(?:error|failed|failure|unsuccessful|unable to|could not|couldn't|try again)\b|"
    r"\b(?:not|never)\s+(?:\w+\s+){0,4}(?:submitted|received|sent|completed?|applied)\b", re.I)


_warned = set()


def _warn_once(message, *args):
    if message not in _warned:
        _warned.add(message)
        logging.getLogger(__name__).warning(message, *args)


def hidden_mode():
    mode = os.getenv("JOB_AGENT_HIDDEN_BROWSER", "offscreen").strip().casefold()
    return mode if mode in {"offscreen", "headless"} else "offscreen"


def context_options():
    """Pin the page language instead of inheriting the OS locale.

    On a non-English Windows install, sites such as LinkedIn otherwise serve
    localized UI text that then leaks into captured job descriptions.
    """
    locale = os.getenv("JOB_AGENT_BROWSER_LOCALE", "en-US").strip() or "en-US"
    language = locale.split("-")[0]
    return {"locale": locale, "extra_http_headers": {"Accept-Language": f"{locale},{language};q=0.9"}}


def _acquire_playwright(start):
    # Several task browsers share a worker thread. Playwright's sync dispatcher
    # must be started once on that thread, even with separate Chrome processes.
    if getattr(_playwright_runtime, 'instance', None) is None:
        _playwright_runtime.instance = start()
        _playwright_runtime.users = 0
    _playwright_runtime.users += 1
    return _playwright_runtime.instance


def _release_playwright(instance):
    if getattr(_playwright_runtime, 'instance', None) is not instance:
        return
    _playwright_runtime.users -= 1
    if _playwright_runtime.users == 0 and not getattr(_playwright_runtime, 'pooled', False):
        _playwright_runtime.instance = None
        instance.stop()


def date_segment(field):
    """A Workday month/day/year segment input (its format lives only in its id)."""
    return bool(re.search(r'dateSection(Month|Day|Year)', field.get("dom_id") or field.get("key") or ""))


class ApplicationBrowser:
    def __init__(self, url, *, headless=None, session_store=None, owner_key=None, purpose='application'):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            # Installing Chrome does not install the Python automation package.
            # Retrying cannot repair a missing dependency in the server's venv.
            raise BrowserBlocked(
                "Browser automation dependencies are missing or incomplete. "
                "Run python -m pip install -r requirements.txt in the same Python "
                "environment used to start the local server, restart the server, "
                "then try again."
            ) from exc
        self.purpose = purpose
        self.headless = True if headless is None else headless
        if purpose == 'browse':
            self.url = public_https_url(url)
            if urlsplit(url).fragment:self.url += '#' + urlsplit(url).fragment
            self.platform, self.identity = 'generic', self.url
        else:
            self.platform, self.identity, self.url = posting(url)
        self.playwright = _acquire_playwright(lambda: sync_playwright().start())
        self._closed = False
        self.browser = None
        self.option_cache = {}
        self.choice_fields = set()
        self._option_owner = None
        self._choice_queries = {}
        self.navigation_hosts = set(GREENHOUSE if self.platform == 'greenhouse' else LEVER if self.platform == 'lever' else {urlsplit(self.url).hostname})
        self.session_store, self.owner_key = session_store, owner_key
        self.session_site = urlsplit(self.url).hostname
        self._dns_cache, self._last_session_save = {}, 0
        self._page_context = None
        self._document_url = None
        self._trusted_until, self._offscreen = 0, False
        try:
            self.browser = self._launch(self.headless)
            login_state = session_store.load(owner_key, self.session_site) if session_store and owner_key else None
            self._session_revision=session_store.revision(owner_key,self.session_site) if session_store and owner_key and hasattr(session_store,'revision') else None
            self.context = self.browser.new_context(viewport={"width": 1280, "height": 900}, **context_options(),
                **({"storage_state":login_state} if login_state else {}))
            self.context.set_default_timeout(8000)
            self.context.route("**/*", self._guard_request)
            self.page = self.context.new_page()
            self._anchor = self.page  # The application tab; popups fall back to it.
            self.context.on("page", self._popup)
            try:self.page.goto(self.url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:
                if purpose!='browse' or 'Timeout' not in type(exc).__name__ or not self.page.locator('body').count():raise
            # A server redirect is followed without passing through _guard_request, so add the
            # landing site here under the same rule (public HTTPS only).
            landed = urlsplit(self.page.url)
            if (purpose != 'browse' and landed.scheme == 'https' and landed.hostname
                    and landed.hostname not in self.navigation_hosts and self._public_host(landed.hostname)):
                self.navigation_hosts.add(landed.hostname)
            self.scope = self.page
            # ATS analytics/images can keep `load` pending after the application
            # is usable. Wait for form controls, not every third-party resource.
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
            try:
                self.page.locator('input:not([type="hidden"]), textarea, select, [role="combobox"], button, a[href]').first.wait_for(state="attached", timeout=15_000)
            except PlaywrightTimeout:
                pass  # Keep the page available for challenge/manual inspection.
            self._settle()
        except BaseException:
            self.close()
            raise

    def _popup(self, page):
        self.page = page
        self.option_cache.clear()

    def _launch(self, headless):
        channel = os.getenv('JOB_AGENT_BROWSER_CHANNEL','chrome')
        self._offscreen = bool(headless and hidden_mode() == 'offscreen')
        actual_headless = headless and not self._offscreen
        key = (channel, actual_headless, self._offscreen)
        pool = getattr(_playwright_runtime, 'browsers', None)
        if pool is not None and key in pool and pool[key].is_connected():
            return pool[key]
        browser = self.playwright.chromium.launch(channel=channel, headless=actual_headless,
            **({'args': [f"--window-position={OFFSCREEN['left']},{OFFSCREEN['top']}", '--window-size=1280,900']} if self._offscreen else {}))
        if pool is not None:
            pool[key] = browser
        return browser

    def _place_window(self, visible):
        """Move the off-screen window into view (or back) without reloading the page."""
        cdp = self.context.new_cdp_session(self.page)
        try:
            window = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": {"windowState": "normal"}})
            cdp.send("Browser.setWindowBounds", {"windowId": window, "bounds": dict(ON_SCREEN if visible else OFFSCREEN)})
        except Exception:
            raise BrowserBlocked("Could not move the browser window. Retry from Agent activity.") from None
        finally:
            try:cdp.detach()
            except Exception:pass

    def set_visible(self, visible):
        """User-only handoff. Move an off-screen window, or relaunch a headless one.

        An off-screen window keeps its page as is. Headless Chrome cannot change
        mode in place, so it relaunches with session state without replaying actions. Current URLs, cookies, local
        storage and session storage are retained; unsaved DOM/file inputs are not.
        The caller must pause/fence the agent and invalidate submission review.
        """
        if type(visible) is not bool:raise BrowserBlocked('Choose whether to show the browser window.')
        if not self.is_open():raise BrowserBlocked('The browser is closed. Resume the task to reopen it in the background.')
        if self.headless == (not visible):
            if visible:self.page.bring_to_front()  # Only an explicit UI action may focus Chrome.
            return
        self._check_page()
        if self._offscreen:
            self._place_window(visible)
            self.headless = not visible
            if visible:self.page.bring_to_front()
            return
        pages=[]
        for page in self.context.pages:
            if page.is_closed() or page.url=='about:blank':continue
            public_https_url(page.url)
            try:session=page.evaluate('() => ({origin:location.origin,values:Object.fromEntries(Object.entries(sessionStorage))})')
            except Exception:session=None
            pages.append((page.url,session,page==self.page))
        state=self.context.storage_state()
        replacement=None
        try:
            replacement=self._launch(not visible)
            context=replacement.new_context(viewport={'width':1280,'height':900},storage_state=state,**context_options())
            context.set_default_timeout(8000)
            context.route('**/*',self._guard_request)
            active=None
            for url,session,selected in pages:
                page=context.new_page()
                cdp,script_id=None,None
                if session:
                    cdp=context.new_cdp_session(page)
                    cdp.send('Page.enable')
                    script_id=cdp.send('Page.addScriptToEvaluateOnNewDocument',{'source':
                        '(()=>{const restored = '+json.dumps(session)+'; if(location.origin===restored.origin) '
                        'for(const [key,value] of Object.entries(restored.values))sessionStorage.setItem(key,value);})();'})['identifier']
                try:page.goto(url,wait_until='domcontentloaded',timeout=20000)
                except Exception as exc:
                    if 'Timeout' not in type(exc).__name__ or not page.locator('body').count():raise
                finally:
                    if cdp:
                        cdp.send('Page.removeScriptToEvaluateOnNewDocument',{'identifier':script_id})
                        cdp.detach()
                if selected:active=page
            if active is None:raise BrowserBlocked('The current browser tab could not be restored.')
        except Exception:
            if 'context' in locals():context.close()
            if replacement and not _pooled_browser(replacement):replacement.close()
            raise BrowserBlocked('Could not switch browser mode. The original session is still available; retry from Agent activity.') from None
        previous, previous_context = self.browser, self.context
        self.browser,self.context,self.page=replacement,context,active
        self._anchor=active
        self.headless=not visible
        self.scope=active
        self.context.on('page',self._popup)
        self.option_cache.clear();self.choice_fields.clear();self._choice_queries.clear()
        self._option_owner,self._page_context,self._document_url,self._preferred_frame=None,None,None,None
        previous_context.close()
        if not _pooled_browser(previous):
            previous.close()
        if visible:active.bring_to_front()

    def save_login_state(self, *, force=False):
        if not self.session_store or not self.owner_key or not hasattr(self, "page") or not self.is_open(): return
        if not force and time.monotonic() - self._last_session_save < 10: return
        if not force and hasattr(self.session_store,'revision') and self.session_store.revision(self.owner_key,self.session_site)!=getattr(self,'_session_revision',None):return
        self._last_session_save = time.monotonic()
        try:
            state = self.context.storage_state()
            self.session_store.save(self.owner_key, self.session_site, state)
            if hasattr(self.session_store,'revision'):self._session_revision=self.session_store.revision(self.owner_key,self.session_site)
        except OSError as exc:
            # Browsing still works; sign-ins just are not kept between sessions.
            _warn_once("Browser sign-ins are not saved between sessions: %s", exc)
        except Exception:
            pass

    def reload_shared_login(self):
        if not self.session_store or not self.owner_key or not hasattr(self.session_store,'revision'):return False
        revision=self.session_store.revision(self.owner_key,self.session_site)
        if revision==getattr(self,'_session_revision',None):return False
        state=self.session_store.load(self.owner_key,self.session_site)
        if not state:return False
        self._check_page()
        self.context.add_cookies(state.get('cookies',[]))
        for origin in state.get('origins',[]):
            if origin.get('origin')==urlsplit(self.page.url).scheme+'://'+urlsplit(self.page.url).netloc:
                self.page.evaluate('(items)=>{for(const item of items)localStorage.setItem(item.name,item.value)}',origin.get('localStorage',[]))
        self._session_revision=revision
        self.page.reload(wait_until='domcontentloaded',timeout=20000)
        self.scope=self.page
        self.option_cache.clear();self.choice_fields.clear();self._choice_queries.clear()
        self._page_context,self._document_url=None,None
        self._settle()
        return True

    def _public_host(self, host):
        if host not in self._dns_cache:
            try:
                addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
                self._dns_cache[host] = bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
            except OSError:
                self._dns_cache[host] = False
        return self._dns_cache[host]

    def _guard_request(self, route):
        url = urlsplit(route.request.url)
        host = url.hostname or ""
        blocked = host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local"))
        try:
            blocked = blocked or not ipaddress.ip_address(host).is_global
        except ValueError:
            pass
        if route.request.is_navigation_request():
            # Top-level redirects and embedded application frames stay on the
            # selected ATS. CAPTCHA providers may use their own child frames.
            try:top_level = route.request.frame.parent_frame is None
            except Exception:top_level = True # A popup's request can precede frame creation.
            if top_level:
                # Any public HTTPS site may be visited (user decision 2026-10-15): applications
                # move between company career pages, ATS hosts, sign-in providers and redirects.
                # Private networks, plain HTTP, password typing and unreviewed submits stay blocked.
                if (self.purpose != 'browse' and host not in self.navigation_hosts and not blocked
                        and url.scheme == "https" and self._public_host(host)):
                    self.navigation_hosts.add(host)
                blocked = blocked or url.scheme != "https" or (self.purpose != 'browse' and host not in self.navigation_hosts)
        if blocked or url.scheme not in {"http", "https"} or not self._public_host(host):
            route.abort()
        else:
            route.continue_()

    def _trusted_navigation(self):
        return time.monotonic() < self._trusted_until or not self.headless

    def _allowed_page(self, page):
        return self.purpose == 'browse' or urlsplit(page.url).hostname in self.navigation_hosts

    def _live_page(self):
        """Keep working in an open application tab when a popup closes or leaves the site."""
        page = self.page
        if not page.is_closed() and page is not getattr(self, '_anchor', page) and page.url == 'about:blank':
            try:page.wait_for_url(lambda url: url != 'about:blank', timeout=5000)
            except Exception:pass
        if not page.is_closed() and (page is getattr(self, '_anchor', page) or self._allowed_page(page)):
            return page
        candidates = [p for p in self.context.pages if not p.is_closed() and p.url != 'about:blank' and self._allowed_page(p)]
        if not candidates:
            return None if page.is_closed() else page
        self.page = candidates[-1]
        self.option_cache.clear(); self._choice_queries.clear()
        self._option_owner, self._preferred_frame = None, None
        return self.page

    def _check_page(self):
        if self._live_page() is None or self.page.is_closed():
            raise BrowserBlocked("The browser was closed. Continue to reopen the application.")
        allowed = self.navigation_hosts
        current = urlsplit(self.page.url)
        if current.hostname not in allowed and self.purpose != 'browse':
            # Server redirects bypass _guard_request; the same public-HTTPS rule applies here.
            if current.scheme == "https" and current.hostname and self._public_host(current.hostname):
                allowed.add(current.hostname)
            else:
                raise BrowserBlocked("The application navigated to a page that is not a public HTTPS site.")
        try:
            platform, identity = posting(self.page.url)[:2]
        except ValueError:
            platform, identity = None, self.identity  # ATS-specific confirmation path is checked separately.
        if platform == "generic":
            # Company sites, sign-in providers and other non-ATS pages are part of the
            # application route; only a recognised posting for another job is "different".
            identity = self.identity
        # An employer careers page embedding the Greenhouse form names the same job by gh_jid
        # (careers.roblox.com/jobs/8072244?gh_jid=8072244).
        if (self.platform == "greenhouse" and identity != self.identity
                and parse_qs(urlsplit(self.page.url).query).get("gh_jid") == [self.identity.rsplit(":", 1)[-1]]):
            identity = self.identity
        if self.platform in {"greenhouse", "lever", "workday"} and identity != self.identity and ("/job/" in self.page.url or self.platform != "workday"):
            raise BrowserBlocked("The browser is showing a different job. Reopen this attempt.")
        self.scope = self.page
        preferred = getattr(self,'_preferred_frame',None)
        if preferred in self.page.frames and urlsplit(preferred.url).hostname in allowed:
            self.scope = preferred
        for frame in self.page.frames[1:]:
            if self.scope != self.page:break
            if urlsplit(frame.url).hostname in allowed and frame.locator('input[type="email"]:visible, input[type="password"]:visible, input[autocomplete="username"]:visible').count():
                self.scope = frame
                break
        if self._document_url != self.page.url:
            self._document_url = self.page.url
            self.option_cache.clear()
            self._choice_queries.clear()

    def page_state(self, fields=None):
        fields = self.observe() if fields is None else fields
        raw = self.scope.evaluate(PAGE_SCAN)
        self._page_context = page_state(raw, url=self.page.url, fields=fields)
        return self._page_context

    def click_page_action(self, action):
        # Re-identify the exact observed target, not its ephemeral DOM index.
        page = self.page_state()
        candidates = [c for c in page["controls"] if all(c.get(k) == action.get(k)
            for k in ("label", "href", "automation_id", "kind")) and not c["disabled"]]
        target = next((c for c in candidates if c["id"] == action["id"]), None)
        if target is None or action["kind"] in {"submit", "unavailable", "guarded", "field"}:
            raise BrowserBlocked("This navigation control changed or requires final submission review.")
        if target.get("href"):
            url = public_https_url(target["href"])
            # Clicking a visible application link may transfer from a company's
            # career site to its ATS. Never grant a model-supplied arbitrary URL.
            self.navigation_hosts.add(urlsplit(url).hostname)
        locator = self.scope.locator(f'[data-northstar-action="{target["id"]}"]')
        # Adobe: an open Field of Study prompt covered "Save and Continue" (click timed out).
        self._close_other_menu(None)
        self._trusted_until = time.monotonic() + TRUSTED_NAVIGATION_SECONDS
        locator.click(timeout=8000)
        self._settle()
        if self.platform == "workday" and "/apply/" in self.page.url:
            # Workday's route shell renders before its account/application step.
            # Wait for that step, not the always-present header Sign In button.
            try:
                self.page.locator('input:visible, textarea:visible, [data-automation-id="SignInWithEmailButton"]:visible, [data-automation-id="pageHeaderTitle"]:visible').first.wait_for(timeout=8000)
            except Exception:
                pass
        self.option_cache.clear()
        self._check_page()
        self.save_login_state()

    def _clear_consent(self):
        """Take a cookie banner out of the way without consenting (reject if offered, else hide)."""
        try:
            outcome = self.page.evaluate(CONSENT)
        except Exception:
            return None
        if outcome:
            self._settle(800)
        return outcome

    def observe(self):
        self._check_page()
        self._clear_consent()
        fields = self.scope.evaluate(SCAN)
        if len(fields) > MAX_FIELDS:
            raise BrowserBlocked("This form has too many controls for this release. Complete it manually.")
        for field in fields:
            if field["key"] in self.choice_fields:
                field["choice_control"] = True
            if field["key"] in self._choice_queries:
                field["search_text"] = self._choice_queries[field["key"]]
                if str(field["value"]) == self._choice_queries[field["key"]] and not field.get("value_committed"):
                    field["value"] = "" # Search text is not a committed selection.
                elif field["value"]:
                    self._choice_queries.pop(field["key"],None)
            if field["kind"] in {"combobox", "location"} or field.get("choice_control") and field["kind"] not in {"select", "radio"}:
                field["options"] = self.option_cache.get(field["key"], [])
        return fields

    def pump(self):
        page = self._live_page() if self.is_open() else None
        if page is not None and not page.is_closed():
            page.wait_for_timeout(50)

    def field(self, key):
        field = next((item for item in self.observe() if item["key"] == key), None)
        if field is None:
            raise BrowserBlocked("The form changed. Observe the page again.", code='stale_observation')
        return field

    def locate(self, field):
        # React hydration can discard our observation markers. Prefer persistent
        # IDs, then unique names, before using the temporary marker.
        if field.get("dom_id"):
            identified = self.scope.locator(f'[id={json.dumps(field["dom_id"])}]')
            if identified.count() == 1:return identified
        if field.get("name") and field["kind"] not in {"radio", "checkbox"}:
            named = self.scope.locator(f'[name={json.dumps(field["name"])}]')
            if named.count() == 1:
                return named
        return self.scope.locator(f'[data-northstar-field="{field["id"]}"]')

    def click_field(self, key):
        field = self.field(key)
        if field["kind"] in {"checkbox", "radio", "file", "password"}:
            raise BrowserBlocked("Use an explicit answer to change this field.")
        locator = self.locate(field)
        if locator.evaluate("el => el.tagName === 'BUTTON' && el.type !== 'button'"):
            raise BrowserBlocked("Browser tools cannot click submit controls.")
        locator.click(timeout=5000)
        self._option_owner = key

    def _option_locator(self, field):
        locator = self.locate(field)
        owner = locator.get_attribute("aria-controls") or locator.get_attribute("aria-owns")
        if owner:
            for item in owner.split():
                root = self.scope.locator(f'[id={json.dumps(item)}]')
                if root.count():
                    choices = root.locator('[role="option"]:visible, [role="treeitem"]:visible, [role="menuitem"]:visible, [data-automation-id="promptOption"]:visible, [data-automation-id="promptLeafNode"]:visible')
                    return choices
        if field["kind"] == "location":
            return self.scope.locator('.dropdown-results > *:visible')
        # A global portal is usable only for the field whose menu we opened.
        # Otherwise inspecting field B can return field A's still-open choices.
        if self._option_owner != field['key']:
            return self.scope.locator(':not(*)')
        # Some select libraries portal the sole open menu to document.body.
        menus = self.scope.locator('[role="listbox"]:visible:not([data-automation-id="selectedItemList"]), [role="tree"]:visible, [role="menu"]:visible, [data-automation-id="promptOptionList"]:visible')
        # A single popup may contain both a role=listbox and an ATS menu wrapper.
        # Count independent menu roots, not nested descriptions of one popup.
        roots = menus.evaluate_all("els => els.flatMap((el, index) => els.some(parent => parent !== el && parent.contains(el)) ? [] : [index])")
        if len(roots) == 1:
            return menus.nth(roots[0]).locator('[role="option"]:visible, [role="treeitem"]:visible, [role="menuitem"]:visible, [data-automation-id="promptOption"]:visible, [data-automation-id="promptLeafNode"]:visible')
        return self.scope.locator('.select__menu:visible .select__option')

    def _close_other_menu(self, key):
        """An open menu can cover the next control (Adobe: the source prompt hid the
        previous-worker radio, whose click then timed out)."""
        if self._option_owner and self._option_owner != key:
            previous = next((item for item in self.observe() if item['key'] == self._option_owner), None)
            if previous and self._option_locator(previous).count():
                self.locate(previous).press('Escape')
                self._settle()
                if self._option_locator(previous).count():
                    # Workday prompts ignore Escape but close when focus leaves them.
                    self.locate(previous).press('Tab')
                    self._settle()
                if self._option_locator(previous).count():
                    raise BrowserBlocked('The previous dropdown is still open. Close it before opening another field.', code='dom_interaction_failed')

    def inspect_options(self, key, query=None):
        self._close_other_menu(key)
        field = self.field(key)
        if field["kind"] in {"select", "radio"}:
            options = field["options"]
            if query is None or not str(query).strip():
                return options
            # Native controls cannot filter themselves like an autocomplete.
            # Search their observed choices before pagination, keeping every
            # equivalent match so ambiguous answers still require resolution.
            matches = [option for option in options if equivalent(field["label"], query, option)]
            if matches:
                return matches
            terms = clean(query).split()
            return [option for option in options if terms and any(
                all(term in clean(option.get(part, "")) for term in terms)
                for part in ("label", "value"))]
        if field["kind"] not in {"combobox", "location", "text", "search", "tel"}:
            return []
        locator = self.locate(field)
        editable = locator.evaluate("e => e.matches('input,textarea') || e.isContentEditable") and locator.is_editable()
        # Some searchable pickers explicitly advertise Enter as the search key.
        # Filling their input alone only changes the query, not the option list.
        enter_search = bool(field.get("choice_control") and
                            locator.get_attribute("enterkeyhint") == "search")
        # A second click closes an open React Select before a search can run.
        if query is None and key in self._choice_queries and editable:
            locator.fill("")
            self._choice_queries.pop(key,None)
            if enter_search:
                locator.press("Enter")
        if (self._option_owner != key and locator.get_attribute("aria-expanded") != "true") or not self._option_locator(field).count():
            # Workday separates its search textbox from the menu-opening icon.
            # Scope the observed icon to this field's container, never the page.
            icon = locator.locator('xpath=ancestor::*[@data-automation-id="multiSelectContainer"][1]').locator('[data-automation-id="promptIcon"]')
            if icon.count() == 1 and icon.is_visible():
                icon.click(timeout=5000)
            else:
                self.click_field(key)
        self._option_owner = key
        before = None
        if query and editable:
            before = self._option_labels(field)
            locator.fill(str(query))
            self._choice_queries[key] = str(query)
            if enter_search:
                locator.press("Enter")
        options = self._await_options(field)
        if before is not None:
            # The previous list stays on screen until the results replace it (Adobe: a
            # Field of Study search returned the unfiltered list). Wait for a changed,
            # settled list, bounded.
            options = self._settled_options(field, before)
        try:
            options.first.wait_for(state="visible", timeout=1000)
        except Exception:
            return []
        return self._read_options(key, options)

    def _option_labels(self, field):
        try:
            return self._option_locator(field).all_inner_texts()
        except Exception:
            return []

    def _settled_options(self, field, before, seconds=6):
        deadline, last, stable = time.monotonic() + seconds, None, 0
        while time.monotonic() < deadline:
            labels = self._option_labels(field)
            stable = stable + 1 if labels == last else 0
            last = labels
            if stable >= 2 and (labels != before or not labels):
                break
            self.page.wait_for_timeout(200)
        return self._option_locator(field)

    def _await_options(self, field, seconds=8):
        """The menu root is chosen when the locator is built, so build it again until the
        popup has rendered (Adobe's Workday prompt took seconds and read as empty)."""
        deadline = time.monotonic() + seconds
        options = self._option_locator(field)
        while not options.count() and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
            options = self._option_locator(field)
        return options

    def _read_options(self, key, options):
        items = [{k: v for k, v in item.items() if k != "index"}
                 for item in options.evaluate_all(OPTION_SCAN)]
        if items: self.choice_fields.add(key)
        self.option_cache[key] = items
        return items

    def current_options(self, key):
        """Read the currently open menu without resetting its search or category."""
        field = self.field(key)
        if field['kind'] in {'select', 'radio'}:
            return field.get('options', [])
        if self._option_owner != key:
            return []
        return self._read_options(key, self._option_locator(field))

    def explore_option(self, key, label):
        query = self._choice_queries.get(key)
        self.choose_option(key, label)
        if query is not None: self._choice_queries[key] = query
        self._settle()
        # Read the new level without clicking the field again (which can close
        # the popup or jump back to its root).
        return self._read_options(key, self._await_options(self.field(key), seconds=5))

    def choose_option(self, key, label):
        field = self.field(key)
        from .autofill import matches as field_matches
        if field_matches(field, label):
            return
        if field["kind"] in {"select", "radio"}:
            return self.fill(field, label)
        options = self._option_locator(field)
        if not options.count():
            # Changing a filled picker (Adobe: UW-Madison degree "Masters" -> "Bachelors"):
            # inspection returned early without opening it, so open it here.
            self.inspect_options(key)
            options = self._option_locator(self.field(key))
        # Match only the active field's observed options, never a global text hit.
        choices = options.evaluate_all(OPTION_SCAN)
        matching = [(item["index"], item) for item in choices if item['label'].strip().casefold() == label.strip().casefold() and not item['disabled']]
        if not matching or resolve_option(field["label"], label, [item for _, item in matching]) is None:
            raise BrowserBlocked("The option changed or is ambiguous. Inspect the field again.", code='options_changed')
        options.nth(matching[0][0]).click(timeout=5000)
        if not matching[0][1]['expandable']: self._choice_queries.pop(key,None)
        self._settle()

    def _settle(self, timeout=1500):
        """Wait for a short quiet DOM interval, bounded even on animated pages."""
        started = time.monotonic()
        try:
            self.page.wait_for_load_state('domcontentloaded', timeout=timeout)
            remaining = max(1, timeout - int((time.monotonic() - started) * 1000))
            self.page.evaluate("""timeout => new Promise(resolve => {
                let quiet, deadline;
                const finish = () => { observer.disconnect(); clearTimeout(quiet); clearTimeout(deadline); resolve(); };
                const observer = new MutationObserver(records => {
                    if (records.every(r => r.type === 'attributes' && r.attributeName.startsWith('data-northstar-'))) return;
                    clearTimeout(quiet); quiet = setTimeout(finish, 150);
                });
                observer.observe(document.documentElement, {subtree:true, childList:true, attributes:true, characterData:true});
                quiet = setTimeout(finish, 150); deadline = setTimeout(finish, timeout);
            })""", remaining)
        except Exception:
            pass  # Navigation/replaced documents are inspected by the next action.

    def _type_segment(self, locator, value):
        """Workday date segments mishandle a set value (Adobe: month "08" became "08/2008",
        then 12/2012 after a select-all). Focus and type digits as a person does (a display
        layer covers the inputs, so a click times out), then verify the segment kept them."""
        locator.focus()
        self.page.keyboard.type(str(value), delay=60)
        self.page.wait_for_timeout(200)
        if locator.input_value() != str(value):
            raise BrowserBlocked("The date segment did not keep the typed value. Check it in the browser.", code='value_not_retained')

    def fill_plain(self, field, value):
        """Fill one already observed ordinary input; the caller verifies the pass."""
        self._check_page()
        locator = self.locate(field)
        if date_segment(field):
            return self._type_segment(locator, value)
        if not locator.evaluate("e => (e.matches('textarea') || e.matches('input:not([type=password]):not([type=hidden]):not([type=file]):not([type=checkbox]):not([type=radio])')) && e.getAttribute('role') !== 'combobox'"):
            raise BrowserBlocked('The control changed; observe it before filling.', code='stale_observation')
        locator.fill(str(value), timeout=5000)
        locator.press('Tab')

    def type_field(self, key, value):
        field = self.field(key)
        if field["kind"] not in {"text", "email", "tel", "url", "textarea", "number", "search", "combobox", "location"}:
            raise BrowserBlocked("This field does not accept text.")
        locator = self.locate(field)
        if date_segment(field):
            self._type_segment(locator, value)
            return self._settle()
        if field.get('choice_control') or field['kind'] in {'combobox', 'location'}:
            locator.fill('')
            locator.press_sequentially(str(value), delay=0, timeout=8000)
        else:
            locator.fill(str(value), timeout=8000)
        if field.get("choice_control") or field["kind"] in {"combobox","location"}:
            self._choice_queries[key] = str(value)
        if field["kind"] not in {"combobox", "location"}:
            locator.press("Tab")
            # A few controls commit only after physical key events. Retain the
            # fast fill path, then fall back when the site discards that value.
            if not field.get('choice_control') and locator.input_value() != str(value):
                locator.fill('')
                locator.press_sequentially(str(value), delay=0, timeout=8000)
                locator.press('Tab')
        self._settle()

    def press_key(self, key, value):
        if value not in {"ArrowDown", "ArrowUp", "Escape", "Tab"}:
            raise BrowserBlocked("That key is not available to the form agent.")
        self.locate(self.field(key)).press(value)

    def screenshot_field(self, key):
        locator = self.locate(self.field(key))
        # Crop to the surrounding control. Never capture other browser tabs.
        return locator.locator("..").screenshot(type="png", timeout=5000)

    def preview(self):
        return self.page.screenshot(type="jpeg", quality=55, timeout=2000,
            mask=[frame.locator('input[type="password"], [data-northstar-secret], input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="verificationCode" i]') for frame in self.page.frames])

    def enrich_options(self, fields, *, active=lambda: True):
        deadline = time.monotonic() + 20
        for field in fields:
            if not active() or time.monotonic() >= deadline:
                break
            if field["kind"] == "combobox" and not field["value"] and not field["options"]:
                try:
                    self.inspect_options(field["key"])
                    self.press_key(field["key"], "Escape")
                except Exception:
                    pass
        return self.observe()

    def is_open(self):
        return any(not page.is_closed() for page in self.context.pages)

    def barrier(self):
        host = urlsplit(self.page.url).hostname or ""
        if self.purpose != 'browse' and IDENTITY_PROVIDERS.search(host):
            # The agent never enters identity-provider credentials.
            return (f"This application uses {host} sign-in. Complete it with Control browser in Agent activity, "
                "then send continue here.")
        verification = self.verification_barrier()
        if verification:return verification
        from .authentication import context
        if context(self):
            return "This site requires a candidate account. Use the secure account form to let the agent sign in or register. Keep your password out of chat."
        return None

    def verification_barrier(self):
        if self.scope.locator('input[autocomplete="one-time-code"]:visible, input[name*="otp" i]:visible, input[name*="verificationCode" i]:visible').count():
            return "Complete the site's email verification in Control browser, then send continue here."
        if self.scope.get_by_text(re.compile(r"^(?:check your email|verify your email(?: address)?|verify your account)$", re.I)).count():
            return "Complete the site's email verification, then send continue here. Your application will resume in this browser."
        # DataDome renders an actual access restriction in a visible iframe
        # without a challenge title. Do not confuse passive CAPTCHA widgets
        # on normal forms with this dedicated verification page.
        for frame in self.page.frames:
            location = urlsplit(frame.url)
            if ((location.hostname or "").endswith(".captcha-delivery.com")
                    and location.path.startswith("/captcha/") and frame.parent_frame):
                try:
                    if frame.frame_element().is_visible():
                        return ("The site is showing a verification or access restriction. "
                            "Use Control browser in Agent activity to inspect it and complete any available verification, "
                            "then send continue here. No application was submitted.")
                except Exception:
                    continue  # A detached frame will be checked on the next observation.
        # An embedded CAPTCHA widget alone is not evidence of a challenge.
        if self.page.locator('iframe[title*="challenge" i]:visible, iframe[title*="verification" i]:visible').count():
            return "Complete the verification challenge in the open browser, then continue."
        return None

    def fill(self, field, value):
        upload_filename = field.get('upload_filename')
        field = self.field(field["key"])
        from .autofill import matches
        if field["kind"] != "file" and matches(field, value):
            return
        self._close_other_menu(field["key"])
        locator = self.locate(field)
        kind = field["kind"]
        if kind == "file":
            filename = upload_filename or ('cover-letter.pdf' if 'cover letter' in field['label'].casefold() else 'resume.pdf')
            locator.set_input_files({"name": filename, "mimeType": "application/pdf", "buffer": value})
            upload = locator.locator('xpath=ancestor::*[@data-automation-id="attachments-FileUpload"][1]')
            if upload.count():
                upload.locator('[data-automation-id="file-upload-successful"]').last.wait_for(state="visible", timeout=10000)
        elif kind == "checkbox":
            if not isinstance(value, bool):
                raise BrowserBlocked("Choose a checkbox answer explicitly.")
            locator.set_checked(value)
        elif kind in {"select", "radio"}:
            option = resolve_option(field["label"], value, field["options"])
            if option is None:
                raise BrowserBlocked("Choose an exact option from the application form.", code='option_mapping_required')
            if kind == "select":
                # select2 keeps the real select hidden and redraws from its change event.
                locator.select_option(value=option["value"], force=bool(field.get("select2")))
            else:
                selected = self.scope.locator(f'[data-northstar-field="{option["id"]}"]')
                if option.get("button"): selected.click()
                else: selected.check()
        elif kind == "combobox" or field.get("choice_control"):
            options = self.inspect_options(field["key"])
            option = resolve_option(field["label"], value, options)
            if option is None:
                options = self.inspect_options(field["key"], query=search_text(field["label"], value))
                option = resolve_option(field["label"], value, options)
            if option is None:
                raise BrowserBlocked("Select an option from this field's available choices.", code='option_mapping_required')
            self.choose_option(field["key"], option["label"])
        elif kind == "location":
            # Lever searches on keystrokes; a pasted value reports "No location found".
            locator.fill('')
            locator.press_sequentially(str(value), delay=40)
            results = self.scope.locator('.dropdown-results')
            option = results.get_by_text(str(value), exact=True)
            try:
                try:
                    option.wait_for(state="visible", timeout=5000)
                except Exception:
                    # Suggestions often extend the typed place ("Ithaca, NY" -> "Ithaca, NY, USA").
                    # Pick it only when exactly one suggestion starts with what was typed.
                    wanted = ' '.join(str(value).casefold().split())
                    items = [item for item in results.locator('*').all()
                             if item.is_visible() and ' '.join(item.inner_text().casefold().split()).startswith(wanted)
                             and not item.locator('*').count()]
                    option = items[0] if len(items) == 1 else option
                if option.count() != 1:
                    raise BrowserBlocked("Choose the intended location suggestion in the browser, then continue.", code='dom_interaction_failed')
                option.click()
            except Exception as exc:
                raise BrowserBlocked("Choose the intended location suggestion in the browser, then continue.", code='dom_interaction_failed') from exc
        elif kind in {"text", "email", "tel", "url", "textarea", "number", "search"} and date_segment(field):
            self._type_segment(locator, value)
        elif kind in {"text", "email", "tel", "url", "textarea", "number", "search"}:
            locator.fill(str(value))
            locator.press("Tab")
        else:
            raise BrowserBlocked("Complete this control in the browser, then continue.", code='dom_interaction_failed')
        self._settle()

    def confirmation(self, *, after_submit=False):
        self._check_page()
        fields = self.observe()
        raw = self.scope.evaluate(PAGE_SCAN)
        page = page_state(raw, url=self.page.url, fields=fields)
        text = raw["text"]
        # Polite text and a success-looking URL are not proof while an
        # application can still be sent, has validation errors, or remains editable.
        if SUBMISSION_ERROR.search(text):
            return None
        if any(SUBMISSION_ERROR.search(error) or not CONFIRMATION_TEXT.search(error)
               for error in raw.get("errors", [])):
            return None
        if any(c["kind"] == "submit" or (c.get("submits_form") and c["kind"] == "guarded"
               and c["label"].strip().casefold() != "search") for c in page["controls"]):
            return None  # Includes a disabled button while a request is in flight.
        if any(f.get("invalid") or (f["kind"] != "search" and (f.get("required") or f.get("value")))
               for f in fields):
            return None
        selectors = ('#application_confirmation, .application-confirmation, .application-success, '
                     '[data-testid="application-confirmation"], [data-automation-id="applicationConfirmation"], '
                     '[data-automation-id="applicationSubmitted"], [data-automation-id="successMessage"]')
        for container in (self.scope, self.page):
            matches = container.locator(selectors)
            for index in range(matches.count()):
                element = matches.nth(index)
                if element.is_visible():
                    message = element.inner_text().strip()
                    if CONFIRMATION_TEXT.search(message) and not SUBMISSION_ERROR.search(message):
                        return {"url": self.page.url, "text": message[:400]}
        if after_submit:
            match = CONFIRMATION_TEXT.search(text)
            if match:
                start = max(0, match.start() - 120) if match else 0
                return {"url": self.page.url, "text": " ".join(text[start:start + 400].split())}
        return None

    def submit(self):
        self._check_page()
        page = self.page_state()
        buttons = [c for c in page["controls"] if c["kind"] == "submit" and not c["disabled"]]
        if not page["can_submit"] or len(buttons) != 1:
            raise BrowserBlocked("A single final submit button could not be identified.")
        button = self.scope.locator(f'[data-northstar-action="{buttons[0]["id"]}"]')
        self.submit_clicked = True  # From here on the employer may have received it.
        button.click(timeout=8000)  # Exactly one attempt, even if the response times out.

    def wait_confirmation(self):
        import time
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            proof = self.confirmation(after_submit=True)
            if proof:
                return proof
            self.page.wait_for_timeout(500)
        return None

    def close(self):
        if getattr(self, '_closed', False):
            return
        try:
            if self.browser is not None:
                if not self.session_store or not hasattr(self.session_store,'revision') or self.session_store.revision(self.owner_key,self.session_site)==getattr(self,'_session_revision',None):
                    self.save_login_state(force=True)
                if hasattr(self, 'context'):
                    self.context.close()
                if _pooled_browser(self.browser):
                    # The last task owns the last context, not a reusable live
                    # session. Evict before close: a delayed disconnect event
                    # must not hand the next task a closing Chrome process.
                    if not self.browser.contexts:
                        pool = getattr(_playwright_runtime, 'browsers', {})
                        for key, browser in list(pool.items()):
                            if browser is self.browser:pool.pop(key, None)
                        self.browser.close()
                else:
                    self.browser.close()
        finally:
            self._closed = True
            _release_playwright(self.playwright)


def form_digest(fields):
    return hashlib.sha256(json.dumps([
        {key: field.get(key) for key in ("key", "label", "kind", "value", "required", "options")}
        for field in fields], sort_keys=True).encode()).hexdigest()


def _pooled_browser(browser):
    return browser in getattr(_playwright_runtime, 'browsers', {}).values()

def start_worker_browsers():
    _playwright_runtime.pooled = True
    _playwright_runtime.browsers = {}

def stop_worker_browsers():
    for browser in getattr(_playwright_runtime, 'browsers', {}).values():
        try:
            browser.close()
        except Exception:
            pass
    _playwright_runtime.browsers = {}
    instance = getattr(_playwright_runtime, 'instance', None)
    if instance:
        instance.stop()
    _playwright_runtime.instance, _playwright_runtime.users = None, 0
    _playwright_runtime.pooled = False
