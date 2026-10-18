"""Site-independent page observations and application navigation boundaries."""
import re
from .upload_status import UPLOAD_STATUS

PAGE_SCAN = r"""() => {""" + UPLOAD_STATUS + r"""
  const visible = el => !!el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden' && !el.closest('[aria-hidden="true"], [inert]');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const root = [...document.querySelectorAll('[role="dialog"], dialog[open]')].filter(visible).at(-1) || document;
  document.querySelectorAll('[data-northstar-action]').forEach(el => el.removeAttribute('data-northstar-action'));
  const controls = [...root.querySelectorAll('button, a[href], [role="button"], input[type="submit"], input[type="button"]')]
    .filter(visible).slice(0, 150).map((el, i) => {
      const id = `ns-action-${i}`; el.setAttribute('data-northstar-action', id);
      const labelled = (el.getAttribute('aria-labelledby') || '').split(' ')
        .map(id => document.getElementById(id)?.textContent || '').join(' ');
      const sections=[];
      for(let parent=el.parentElement;parent && parent!==root;parent=parent.parentElement){
        const heading=clean(parent.querySelector(':scope > legend, :scope > h2, :scope > h3, :scope > h4, :scope > [data-automation-id="formLabel"]')?.textContent);
        if(heading && !sections.includes(heading))sections.push(heading);
      }
      return {id, label:clean(el.getAttribute('aria-label') || labelled || el.innerText || el.value).slice(0,240),
        section:sections.join(' / ').slice(0,500),
        tag:el.tagName, role:el.getAttribute('role'), type:el.type || '', field_id:el.getAttribute('data-northstar-field'),
        href:el.tagName === 'A' ? el.href : null, automation_id:el.getAttribute('data-automation-id'),
        disabled:!!el.disabled || el.getAttribute('aria-disabled') === 'true',
        submits_form:['BUTTON','INPUT'].includes(el.tagName) && ['submit','image'].includes(el.type) && !!el.form};
    });
  const otp = [...document.querySelectorAll('input[autocomplete="one-time-code"], input[name*="otp" i], input[name*="verificationCode" i]')].some(visible);
  const password = [...document.querySelectorAll('input[type="password"]')].some(visible);
  const heading = [...document.querySelectorAll('[aria-current="step"], [data-automation-id="pageHeaderTitle"], h1, h2')]
    .filter(visible).map(el => clean(el.textContent)).join(' / ').slice(0,600);
  const accountHeading = [...root.querySelectorAll('h1, h2, legend, [role="heading"]')].filter(visible)
    .some(el => /sign.?in|log.?in|create.*account|candidate account|sign.?up|register/i.test(el.textContent));
  const identifierInputs = [...root.querySelectorAll('input')].filter(visible)
    .filter(el => !el.disabled && !el.readOnly && ['text','email','tel','file'].includes(el.type));
  const accountSubmit = controls.some(c => c.tag !== 'A' && !c.disabled &&
    /sign.?in|log.?in|create.*account|sign.?up|register/i.test(c.label));
  const onlyInput = identifierInputs.length === 1 ? identifierInputs[0] : null;
  const username = onlyInput && (onlyInput.type === 'email' || ['email','username'].includes(onlyInput.autocomplete) ||
    (onlyInput.type === 'text' && /e.?mail|user.?name|login.?id/i.test(
      [...onlyInput.labels || []].map(el => el.innerText).join(' ') || onlyInput.getAttribute('aria-label') ||
      onlyInput.getAttribute('placeholder') || onlyInput.name || onlyInput.id)));
  const identifier = accountHeading && (accountSubmit || !!username);
  const text = clean((root === document ? document.querySelector('main, [role="main"], form') || document.body : root).innerText).slice(0,6000);
  const errors = [...root.querySelectorAll('[role="alert"], [data-automation-id="errorMessage"], .field-error, .error-message')].filter(visible).filter(el => !isUploadSuccessNotice(el)).map(el=>clean(el.textContent)).filter(Boolean).slice(0,20);
  const currentSteps = [...document.querySelectorAll('[aria-current="step"]')].filter(visible)
    .map(el => clean(el.getAttribute('aria-label') || el.textContent));
  const current_step = currentSteps.length === 1 ? currentSteps[0] : null;
  const document_text = root !== document ? clean((document.querySelector('main, [role="main"]') || document.body).innerText).slice(0,12000) : text;
  return {heading, text, document_text, current_step, controls, errors, authentication_required: password || otp || identifier};
}"""


def action_kind(control, *, has_fields=False):
    """Separate navigation from the final send; never rely on a model's label."""
    text = re.sub(r"\s+", " ", control["label"]).strip().casefold()
    automation = control.get("automation_id") or ""
    if re.fullmatch(r"(?:submit(?: application)?|send application|finish application|complete application|confirm and submit)", text):
        return "submit"
    if text in {"apply now", "apply", "apply for this job", "i'm interested", "i’m interested"}:
        return "submit" if control.get("submits_form") and has_fields else "open_application"
    if re.fullmatch(r"(?:save (?:and|&) )?(?:continue|next)(?: step)?|review application", text):
        return "next"
    if text in {"back", "previous", "previous step"}: return "back"
    if text in {"autofill with resume", "apply with resume", "upload resume", "upload my resume"}: return "resume"
    if text in {"apply manually", "enter manually", "start application", "begin application", "use my last application"}: return "open_application"
    if text in {"sign in", "log in", "login", "sign in with email", "continue with email", "create account", "create an account", "register", "sign up"}: return "authentication"
    if re.fullmatch(r"add(?: another)?(?: work experience| experience| education| school| language| website| certification)", text): return "add_entry"
    # Workday uses Add on repeated sections. Its automation ID gives this short
    # button a bounded purpose without assigning fixed step numbers.
    if text == "add" and automation in {"Add", "addExperience", "addEducation", "addWebsite"}: return "add_entry"
    if not text: return "unavailable"
    if control.get('field_id') or control.get('role') in {'option','checkbox','radio','combobox'}:
        return "field"  # Source-aware field tools own these controls.
    if control.get('submits_form'):
        return "guarded"  # Unknown submit buttons require a browser handoff.
    if re.search(r"\b(submit|send application|finish application|complete application|accept|agree|consent|subscribe|delete|withdraw)\b", text):
        return "guarded"
    return "interact"


def page_state(raw, *, url, fields):
    controls = [{**c, "kind": action_kind(c, has_fields=bool(fields))} for c in raw["controls"]]
    final = [c for c in controls if c["kind"] == "submit" and not c["disabled"]]
    auth = raw["authentication_required"]
    return {"url":url, "heading":raw["heading"], "text":raw["text"], 'current_step':raw.get('current_step'),
        "document_text":raw.get("document_text") if not fields and not auth and not final else None,
        "stage": "authentication" if auth else "review" if final else "application" if fields else "job_page",
        "authentication_required":auth, "errors":raw.get("errors", []), "can_submit":len(final) == 1 and not auth,
        "controls":[c for c in controls if c["kind"] != "unavailable"]}
