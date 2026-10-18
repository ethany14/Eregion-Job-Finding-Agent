"""Select account controls from a value-free structural catalog.

No page prose, labels, screenshots, URLs or credentials are sent to a model.
Only enumerated purposes inferred locally and DOM control structure leave the
browser. Ambiguous semantic questions remain an explicit account handoff.
"""
import json
import re
from eregion.runtime.core.model import ModelInterrupted, ModelRequests

from langchain_core.messages import HumanMessage, SystemMessage
from eregion.runtime.model_output import output_problem
from .browser import BrowserBlocked


SCAN = r"""root => {
  // A text input too small to see (e.g. a 1x0 px robot trap) is not a candidate control.
  const sized = e => !e.matches('input:not([type="checkbox"]):not([type="radio"])')
    || (r => r.width > 2 && r.height > 2)(e.getBoundingClientRect());
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'
    && !e.closest('[aria-hidden="true"], [inert]') && sized(e);
  const label = e => ([...e.labels || []].map(x => x.innerText).join(' ') || e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(' ').map(id => document.getElementById(id)?.innerText || '').join(' ') ||
    e.getAttribute('placeholder') || (e.matches('input') ? e.name || e.id : e.innerText) || '').trim();
  const passwords = [...root.querySelectorAll('input[type="password"], input[data-northstar-secret]')].filter(visible);
  const forms = [...new Set(passwords.map(e => e.form).filter(Boolean))];
  if (forms.length === 1) {
    // Some portals render the visible submit and the mode link beside the <form>. Widen to the
    // largest container that holds the same credential fields and no page header/nav/footer.
    const landmarks = 'header, nav, footer, [role="banner"], [role="navigation"], [role="contentinfo"]';
    const count = e => e.querySelectorAll('input[type="password"]').length;
    let scope = forms[0];
    while (scope !== root && scope.parentElement && scope.parentElement !== document.body
        && root.contains(scope.parentElement) && !scope.parentElement.querySelector(landmarks)
        && count(scope.parentElement) === count(forms[0])) scope = scope.parentElement;
    root = scope;
  }
  const elements = [...root.querySelectorAll('input, button, a[href], [role="button"]')]
    .filter(visible).filter(e => !e.matches('input[type="hidden"]')).slice(0, 120);
  return elements.map((e, i) => {
    const id = `account-${i}`; e.setAttribute('data-northstar-account', id);
    return {id, label:label(e), tag:e.tagName, type:e.hasAttribute('data-northstar-secret') ? 'password' : e.type || '', autocomplete:e.autocomplete || '',
      disabled:!!e.disabled || !!e.readOnly || e.getAttribute('aria-disabled') === 'true',
      submits_form:!!e.form && e.type === 'submit'};
  }); // Never read credential values. Labels are used only by local classification.
}"""


def catalog(root):
    result = []
    for item in root.evaluate(SCAN):
        text = item.pop('label').casefold()
        autocomplete = item.pop('autocomplete')
        kind = item['type']
        if kind == 'password':purpose = 'password'
        elif autocomplete == 'one-time-code':purpose = 'verification'
        elif item['tag'] == 'INPUT' and (kind == 'email' or autocomplete in {'email','username'} or kind == 'text' and re.search(r'e.?mail|user.?name|login.?id', text)):purpose = 'username'
        elif kind in {'checkbox','radio'}:purpose = 'consent'
        elif re.search(r'forgot|reset|google|apple|microsoft|facebook|linkedin|single sign|\bsso\b', text):purpose = 'external_or_recovery'
        elif re.search(r'submit application|send application|finish application|complete application|confirm and submit|delete|withdraw|subscribe', text) or re.fullmatch(r'apply(?: now| for this job)?|i.?m interested',text):purpose = 'forbidden'
        elif re.search(r'create.*account|register|sign.?up', text):purpose = 'register'
        elif re.search(r'sign.?in|log.?in', text):purpose = 'login'
        elif re.fullmatch(r'continue|next|submit|verify|continue with email', text):purpose = 'continue'
        else:purpose = 'unknown'
        result.append({**item, 'purpose':purpose})
    return result


def choose(browser, controls, *, mode, switch=False, model_factory=None,
           active=lambda: True, reserve=lambda kind: True):
    from .agent import ConfiguredToolModel, tool, TEXT
    if not reserve('model'):
        raise BrowserBlocked('The account step reached its decision limit. Continue to allow more steps.')
    tools = [tool('account_step', 'Select credential fields and one account control from the structural observation.', {
        'username_ids':{'type':'array', 'items':TEXT}, 'password_ids':{'type':'array', 'items':TEXT}, 'control_id':TEXT}),
        tool('account_help', 'The structural observation is insufficient to identify the intended account action.')]
    messages = [SystemMessage(content=(
        'Choose one step of the authorized candidate account flow. Only structural metadata is available; '
        'credential values and page content are intentionally absent. The executor fills supplied credential handles. '
        'Handle email-first, password-only and confirmation fields. Do not change login into registration. '
        'For switch_mode_only choose the requested mode link/button with no fields. '
        'Never choose consent, external_or_recovery or forbidden controls. If ambiguous use account_help. '
        'Return a single tool call; do not guess unknown facts.')),
        HumanMessage(content=json.dumps({'mode':mode,'switch_mode_only':switch,'controls':controls}))]
    model = (model_factory or ConfiguredToolModel)()
    requests = ModelRequests()
    try:
        response = requests.call(lambda: model.invoke(messages, tools), active=active,
            timeout=getattr(model, 'timeout', 45) + 2, pump=browser.pump)
    except ModelInterrupted:
        raise BrowserBlocked('The account action was paused.') from None
    except TimeoutError:
        raise BrowserBlocked('The model timed out while selecting account controls. Retry the secure account step.') from None
    except BrowserBlocked:
        raise
    except Exception:
        raise BrowserBlocked('The model could not select account controls. Retry the secure account step.') from None
    finally:
        requests.close()
    calls = getattr(response, 'tool_calls', None) or []
    if output_problem(response) or len(calls) != 1:
        raise BrowserBlocked('The model did not return a usable account action. Retry the secure account step.')
    call = calls[0]
    if call['name'] == 'account_help':
        raise BrowserBlocked('Several account controls are ambiguous. Open the intended email sign-in form in the browser, then retry securely.')
    args = call.get('args', {})
    if call['name'] != 'account_step' or set(args) != {'username_ids','password_ids','control_id'}:
        raise BrowserBlocked('The model selected an invalid account action. Retry the secure account step.')
    if any(not isinstance(args[k], list) or not all(isinstance(v,str) for v in args[k]) for k in ('username_ids','password_ids')):
        raise BrowserBlocked('The model selected invalid account fields.')
    if not isinstance(args['control_id'], str):raise BrowserBlocked('The account control was not identified.')
    return args
