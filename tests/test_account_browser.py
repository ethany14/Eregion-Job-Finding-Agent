"""Unfamiliar account layouts in real Chrome; all requests are intercepted."""
import json
import os
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from eregion.runtime.auto_apply import authentication
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.contracts import AccountCredentials
from test_auto_apply_workday import WorkdayBrowser, URL

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')


@pytest.fixture
def browser():
    b = WorkdayBrowser(URL)
    try:yield b
    finally:b.close()


def credentials(b, **changes):
    current = authentication.context(b)
    return AccountCredentials(expected_version=1, origin=current['origin'], mode=current['mode'],
        username='candidate@example.com', password='PRIVATE-PASSWORD', **changes)


def test_generic_username_and_unfamiliar_submit_button(browser):
    browser.page.set_content('''<form onsubmit="event.preventDefault(); window.calls=(window.calls||0)+1; this.outerHTML='<h1>Application</h1>'">
      <h1>Sign in</h1><label>Username<input type="text" name="identifier"></label>
      <label>Password<input type="password"></label><button type="submit">Enter candidate portal</button></form>''')
    authentication.authenticate(browser, credentials(browser))
    assert browser.page.evaluate('window.calls') == 1
    assert browser.page_state()['heading'] == 'Application'


def test_single_password_registration_and_email_button_are_supported(browser):
    browser.page.set_content('''<h1>Create your account</h1><form onsubmit="event.preventDefault();this.outerHTML='<h1>Application</h1>';document.querySelector('h1').remove()">
      <input type="email"><input type="password"><button>Continue with email</button></form>''')
    assert authentication.context(browser)['mode']=='register'
    authentication.authenticate(browser,credentials(browser))
    assert browser.page_state()['heading']=='Application'


def test_email_first_then_password_only_without_requesting_credentials_again(browser):
    browser.page.set_content('''<h1>Sign in</h1><form onsubmit="event.preventDefault(); window.email=this.querySelector('input').value;
      this.innerHTML=`<label>Password<input type='password'></label><button>Next</button>`;
      this.onsubmit=event=>{event.preventDefault(); window.calls=1;document.body.innerHTML='<h1>Application</h1>'}">
      <label>Email address<input type="email"></label><button>Continue</button></form>''')
    assert authentication.context(browser)['stage'] == 'identifier'
    assert browser.page_state()['authentication_required']
    authentication.authenticate(browser, credentials(browser))
    assert browser.page.evaluate('window.email') == 'candidate@example.com'
    assert browser.page.evaluate('window.calls') == 1


def test_model_can_resolve_structural_ambiguity_without_page_or_credentials(browser):
    browser.page.set_content('''<p>Personal account content MUST-STAY-LOCAL</p><form onsubmit="event.preventDefault();
      window.correct=this.querySelector('input').value==='candidate@example.com';this.outerHTML='<h1>Application</h1>'">
      <h1>Sign in</h1><input type="text" placeholder="Private display name">
      <input type="password"><button>Enter workspace</button></form>''')
    class Model:
        def invoke(self, messages, tools):
            payload = json.dumps([m.content for m in messages])
            for secret in ['PRIVATE-PASSWORD','candidate@example.com','MUST-STAY-LOCAL','Private display name','Enter workspace',URL]:
                assert secret not in payload
            snapshot = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
            controls = snapshot['controls']
            return AIMessage(content='', tool_calls=[{'id':'1','name':'account_step','args':{
                'username_ids':[next(c['id'] for c in controls if c['type']=='text')],
                'password_ids':[next(c['id'] for c in controls if c['type']=='password')],
                'control_id':next(c['id'] for c in controls if c['submits_form'])}}])
    authentication.authenticate(browser, credentials(browser), model_factory=Model)
    assert browser.page.evaluate('window.correct')


def test_new_consent_on_second_step_pauses_before_credential_submission(browser):
    browser.page.set_content('''<h1>Sign in</h1><form onsubmit="event.preventDefault();this.innerHTML=`<input type='password'>
      <label>Accept terms<input type='checkbox' required></label><button>Continue</button>`;this.onsubmit=()=>{window.bad=true;return false}">
      <input type="email"><button>Continue</button></form>''')
    with pytest.raises(BrowserBlocked, match='new account checkboxes'):
        authentication.authenticate(browser, credentials(browser))
    assert browser.page.evaluate('!!window.bad') is False
    assert browser.page.locator('input[type=password]').input_value() == ''
    assert len(authentication.context(browser)['consents']) == 1


def test_wrong_credentials_are_not_retried_and_password_is_cleared(browser):
    browser.page.set_content('''<h1>Sign in</h1><form onsubmit="event.preventDefault(); window.calls=(window.calls||0)+1;
      document.querySelector('[role=alert]').textContent='Incorrect details'">
      <input type="email"><input type="password"><button>Continue</button><div role="alert"></div></form>''')
    with pytest.raises(BrowserBlocked, match='did not advance'):
        authentication.authenticate(browser, credentials(browser))
    assert browser.page.evaluate('window.calls') == 1
    assert browser.page.locator('input[type=password]').input_value() == ''


def test_verification_is_not_mistaken_for_another_password_request(browser):
    browser.page.set_content('''<h1>Sign in</h1><form onsubmit="event.preventDefault();this.innerHTML='<input autocomplete=one-time-code>'">
      <input type="email"><input type="password"><button>Continue</button></form>''')
    with pytest.raises(BrowserBlocked, match='verification'):
        authentication.authenticate(browser, credentials(browser))
    assert authentication.context(browser) is None


def test_job_form_email_is_not_treated_as_login(browser):
    browser.page.set_content('<header><button>Sign In</button></header><h1>My Information</h1><form><input type="email"><button>Next</button></form>')
    assert authentication.context(browser) is None
    assert not browser.page_state()['authentication_required']


def test_persistent_account_heading_after_login_does_not_block_application(browser):
    browser.page.set_content('''<h1>Candidate account</h1><h2>Apply: Data Analyst</h2><form>
      <label>First name<input name="first_name"></label><label>Last name<input name="last_name"></label>
      <label>Email<input type="email"></label><label>Resume<input type="file"></label>
      <button>Submit application</button></form>''')
    assert authentication.context(browser) is None
    assert not browser.page_state()['authentication_required']
    assert browser.page_state()['can_submit']


def test_pausing_during_model_selection_prevents_credential_fill(browser):
    browser.page.set_content('<h1>Sign in</h1><form><input type="text"><input type="password"><button>Enter</button></form>')
    active = True
    class Model:
        def invoke(self, messages, tools):
            nonlocal active
            active = False
            return AIMessage(content='',tool_calls=[{'id':'1','name':'account_help','args':{}}])
    with pytest.raises(BrowserBlocked):
        authentication.authenticate(browser, credentials(browser), active=lambda:active, model_factory=Model)
    assert browser.page.locator('input[type=password]').input_value() == ''


def test_model_cannot_target_final_application_submission(browser):
    browser.page.set_content('<h1>Sign in</h1><form><input type="text"><input type="password"><button onclick="window.bad=true">Submit application</button></form>')
    class Model:
        def invoke(self, messages, tools):
            controls=json.loads(next(m.content for m in messages if isinstance(m,HumanMessage)))['controls']
            return AIMessage(content='',tool_calls=[{'id':'1','name':'account_step','args':{
                'username_ids':[controls[0]['id']], 'password_ids':[controls[1]['id']], 'control_id':controls[2]['id']}}])
    with pytest.raises(BrowserBlocked,match='not an authorized account action'):
        authentication.authenticate(browser,credentials(browser),model_factory=Model)
    assert browser.page.evaluate('!!window.bad') is False
    assert browser.page.locator('input[type=password]').input_value()==''


def test_password_only_frame_on_existing_site_is_detected(browser):
    browser.page.set_content('<iframe src="/login"></iframe>')
    frame=browser.page.frames[1]
    frame.wait_for_load_state()
    frame.set_content('''<h1>Sign in</h1><form onsubmit="event.preventDefault();this.outerHTML='<h1>Application</h1>'">
      <input type="password"><button>Continue</button></form>''')
    ctx=authentication.context(browser)
    assert ctx['origin']=='https://example.wd5.myworkdayjobs.com'
    authentication.authenticate(browser,credentials(browser))
    assert frame.get_by_role('heading',name='Application').count()==1


def _workday_account(browser):
    from pathlib import Path
    browser.page.set_content((Path(__file__).parent/'fixtures/auto_apply/workday_create_account.html').read_text(encoding='utf-8'))


class NoModel:
    def invoke(self, messages, tools):
        raise AssertionError('The Workday account page is structurally unambiguous.')


def test_workday_overlay_create_account_outside_form_without_model(browser):
    # Intel-31: the visible Create Account control is an overlay outside <form>, the form's
    # submit button is aria-hidden, and a 1x0 px robot-trap text input sits below the form.
    _workday_account(browser)
    controls = authentication.account_browser.catalog(authentication.root(browser))
    assert not any(c['purpose'] == 'unknown' and c['tag'] == 'INPUT' for c in controls)
    assert [c['purpose'] for c in controls if c['tag'] == 'DIV'] == ['register']
    assert authentication.context(browser)['mode'] == 'register'
    authentication.authenticate(browser, credentials(browser), model_factory=NoModel)
    assert browser.page.evaluate('window.created') == {'email':'candidate@example.com','filled':True}
    assert browser.page.locator('[data-automation-id=beecatcher]').count() == 0


def test_workday_switch_to_sign_in_uses_the_form_link_not_the_header(browser):
    _workday_account(browser)
    origin = authentication.context(browser)['origin']
    authentication.switch_mode(browser, 'login', origin, model_factory=NoModel)
    assert browser.page.evaluate('window.switched') == 1
    assert authentication.context(browser)['mode'] == 'login'
    authentication.authenticate(browser, credentials(browser), model_factory=NoModel)
    assert browser.page.evaluate('window.signedIn') == {'email':'candidate@example.com','filled':True}
