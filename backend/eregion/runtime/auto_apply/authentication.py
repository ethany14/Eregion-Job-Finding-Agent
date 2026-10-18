"""Credential execution on the browser worker, never in an LLM tool payload."""
import re
import time
from urllib.parse import urlsplit

from .browser import BrowserBlocked
from . import account_browser

PASSWORDS = 'input[type="password"]:visible, input[data-northstar-secret]:visible'
LOGIN = re.compile(r"^(?:sign in|log in|login)$", re.I)
REGISTER = re.compile(r"^(?:create (?:an? )?account|register|sign up)$", re.I)


def root(browser):
    dialogs = browser.scope.locator('[role="dialog"]:visible, dialog[open]:visible')
    return dialogs.last if dialogs.count() else browser.scope.locator('body')


def origin(browser):
    parsed = urlsplit(browser.scope.url)
    return f'{parsed.scheme}://{parsed.netloc}'


def email_first(browser):
    scope = root(browser)
    if not scope.locator('input:not([type="hidden"]):visible').count():return False
    # A profile/contact form with an email field is not a login step. Require
    # account-specific heading context, not a header's unrelated Sign In link.
    headings = scope.locator('h1:visible, h2:visible, legend:visible, [role="heading"]:visible').all_text_contents()
    if not any(re.search(r'sign.?in|log.?in|create.*account|candidate account|sign.?up|register', text, re.I) for text in headings):
        return False
    # Portals often keep an account heading after sign-in. A normal application
    # form must not be treated as another credential step on that heading alone.
    controls = account_browser.catalog(scope)
    if any(c['tag'] != 'A' and c['purpose'] in {'login', 'register'}
           and not c['disabled'] for c in controls):
        return True
    inputs = [c for c in controls if c['tag'] == 'INPUT' and not c['disabled']
              and c['type'] in {'text', 'email', 'tel', 'file'}]
    return len(inputs) == 1 and inputs[0]['purpose'] == 'username'


def context(browser):
    browser._check_page()
    if browser.verification_barrier():return None
    passwords = root(browser).locator(PASSWORDS)
    if not passwords.count() and not email_first(browser): return None
    mode = "register" if passwords.count() and (passwords.count() > 1 or passwords.first.get_attribute("autocomplete") == "new-password") else "login"
    if re.search(r'create.*account|sign.?up|register', ' '.join(root(browser).locator('h1:visible, h2:visible, legend:visible').all_text_contents()), re.I):
        mode = 'register'
    fields = browser.observe()
    return {"origin": origin(browser), "mode":mode,
        "stage":"password" if passwords.count() else "identifier",
        "instructions":browser.page_state()["text"],
        "consents":[{"key":f["key"], "label":f["label"], "required":f["required"]}
                    for f in fields if f["kind"] == "checkbox"]}


def switch_mode(browser, mode, origin, **options):
    current = context(browser)
    if not current or current["origin"] != origin:
        raise BrowserBlocked("The account page changed. Review the current site before continuing.")
    if current["mode"] == mode: return
    pattern = REGISTER if mode == "register" else LOGIN
    # This only opens the alternate account form; it never clicks an account
    # submit button with populated credentials.
    links = root(browser).get_by_role("link", name=pattern).filter(visible=True)
    if not links.count():
        links = root(browser).get_by_role("button", name=pattern).filter(visible=True)
    if links.count() != 1:
        controls = account_browser.catalog(root(browser))
        # The catalog excludes page headers, so one remaining mode control is the form's own link.
        local = [c for c in controls if c['purpose'] == mode and not c['disabled']]
        if len(local) == 1:
            plan = {'username_ids':[], 'password_ids':[], 'control_id':local[0]['id']}
        else:
            plan = account_browser.choose(browser, controls, mode=mode, switch=True, **options)
        if plan['username_ids'] or plan['password_ids']:
            raise BrowserBlocked('Switching account mode must not send credentials.')
        target = _validate(controls, plan, mode, switch=True)
        links = root(browser).locator(f'[data-northstar-account="{target["id"]}"]')
    if not options.get('active',lambda:True)():raise BrowserBlocked('The account action was paused.')
    links.click()
    browser.page.wait_for_timeout(500)
    latest = context(browser)
    if not latest or latest["origin"] != origin or latest["mode"] != mode:
        raise BrowserBlocked("The account form is still loading or needs browser interaction. Continue after it opens.")


def authenticate(browser, credentials, *, expected=None, active=lambda: True,
                 model_factory=None, reserve=lambda kind: True):
    current = context(browser)
    if not current or (expected is not None and current != expected) or current["origin"] != credentials.origin or current["mode"] != credentials.mode:
        raise BrowserBlocked("The account form changed. Review the site and account mode, then try again.")
    if set(credentials.consents) != {f['key'] for f in current['consents']}:
        raise BrowserBlocked('Review each account checkbox in the secure form before continuing.')
    touched = []
    try:
        for _ in range(6):
            if not active():raise BrowserBlocked('The account action was paused.')
            browser._check_page()
            if origin(browser) != credentials.origin:
                raise BrowserBlocked('The account moved to another site. Review its origin in the secure account form before continuing.')
            verification = browser.verification_barrier()
            if verification:raise BrowserBlocked(verification)
            current = context(browser)
            if not current:
                # Workday shows an empty route shell first (Intel-32: the agent asked to sign in again).
                if hasattr(browser, 'wait_for_step'):browser.wait_for_step()
                browser.save_login_state(force=True)
                return
            if current['mode'] != credentials.mode:
                raise BrowserBlocked('The site changed account mode. Review login or registration before continuing.')
            _consents(current, credentials.consents)
            controls = account_browser.catalog(root(browser))
            plan = _conventional(controls, credentials.mode)
            if plan is None:
                plan = account_browser.choose(browser, controls, mode=credentials.mode,
                    model_factory=model_factory, active=active, reserve=reserve)
            if not active():raise BrowserBlocked('The account action was paused.')
            # Do not act on a page that changed while the model was deciding.
            browser._check_page()
            if origin(browser) != credentials.origin or controls != account_browser.catalog(root(browser)):
                raise BrowserBlocked('The account controls changed. Retry using the current secure form.')
            target = _validate(controls, plan, credentials.mode)
            if not reserve('tool'):raise BrowserBlocked('The account step reached its action limit. Continue to allow more steps.')
            scope = root(browser)
            for key in plan['username_ids']:
                scope.locator(f'[data-northstar-account="{key}"]').fill(credentials.username)
            for key in plan['password_ids']:
                password = scope.locator(f'[data-northstar-account="{key}"]')
                touched.append(password)
                password.evaluate("el => el.setAttribute('data-northstar-secret', 'true')")
                password.fill(credentials.password.get_secret_value())
            for key, value in credentials.consents.items():
                if key in {f['key'] for f in current['consents']}:
                    browser.fill(browser.field(key), value)
            if not active():raise BrowserBlocked('The account action was paused.')
            scope.locator(f'[data-northstar-account="{target["id"]}"]').click(timeout=8000)
            shape = lambda items: [(c['id'],c['tag'],c['type'],c['purpose']) for c in items]
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                browser.page.wait_for_timeout(200)
                browser._check_page()
                if origin(browser) != credentials.origin or browser.verification_barrier():break
                if shape(account_browser.catalog(root(browser))) != shape(controls):break
            else:
                # A credential submission is never retried unchanged. This also
                # avoids duplicate account creation and account lockouts.
                raise BrowserBlocked('The account step did not advance. The site may have rejected the details; review the browser and retry securely with corrected details.')
            # A validation message/disabled button can change without advancing.
            latest = account_browser.catalog(root(browser))
            if shape(latest) == shape(controls) and origin(browser) == credentials.origin:
                raise BrowserBlocked('The site has not advanced past this account step. Review its message before retrying securely.')
        raise BrowserBlocked('The account flow needs another step. Continue from the current secure account form.')
    except BrowserBlocked:
        raise
    except Exception:
        raise BrowserBlocked("The account action could not be confirmed. Check the browser before retrying.") from None
    finally:
        # If rejected, do not leave the supplied password in a live DOM.
        try:
            for password in touched:
                if password.count() and password.is_visible():password.fill('')
            for password in browser.scope.locator(PASSWORDS).all(): password.fill("")
        except Exception:
            pass


def _consents(current, choices):
    expected = {f['key'] for f in current['consents']}
    if not expected.issubset(choices):
        raise BrowserBlocked('Review the new account checkboxes in the secure form before continuing.')
    if any(f['required'] and not choices[f['key']] for f in current['consents']):
        raise BrowserBlocked('The site requires an account agreement you have not accepted. Review it before continuing.')


def _conventional(controls, mode):
    fields = [c for c in controls if c['tag'] == 'INPUT' and not c['disabled']]
    if any(c['purpose'] == 'unknown' and c['type'] in {'text','email'} for c in fields):return None
    usernames = [c['id'] for c in fields if c['purpose'] == 'username']
    passwords = [c['id'] for c in fields if c['purpose'] == 'password']
    actions = [c for c in controls if c['tag'] != 'A' and not c['disabled']
        and c['type'] not in {'password','email','text','checkbox','radio'}
        and c['purpose'] in {mode,'continue'}]
    if not actions:
        actions = [c for c in controls if c['submits_form'] and not c['disabled'] and c['purpose'] == 'unknown']
    if len(actions) != 1 or not (usernames or passwords):return None
    return {'username_ids':usernames,'password_ids':passwords,'control_id':actions[0]['id']}


def _validate(controls, plan, mode, *, switch=False):
    selected = {c['id']:c for c in controls}
    for key, purpose in [('username_ids','username'),('password_ids','password')]:
        for token in plan[key]:
            field = selected.get(token, {})
            # The model may identify an unlabelled text input as a username, but
            # never repurpose a checkbox, OTP, file or other credential field.
            allowed = field.get('purpose') == purpose or (purpose == 'username' and field.get('purpose') == 'unknown' and field.get('type') in {'text','email'})
            if field.get('tag') != 'INPUT' or not allowed or field.get('disabled'):
                raise BrowserBlocked('The selected credential field is unavailable or has another purpose.')
    target = selected.get(plan['control_id'], {})
    if (target.get('disabled') or target.get('tag') not in {'BUTTON','INPUT','DIV','SPAN','A'}
            or target.get('purpose') not in ({mode} if switch else {mode,'continue','unknown'})
            or target.get('tag') == 'INPUT' and target.get('type') not in {'submit','button'}
            or not switch and target.get('tag') == 'A'):
        raise BrowserBlocked('This control is not an authorized account action.')
    if not switch and not (plan['username_ids'] or plan['password_ids']):
        raise BrowserBlocked('Select the credential fields before sending the account form.')
    return target
