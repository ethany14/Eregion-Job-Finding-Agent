"""V1-V3 in real Chrome on synthetic local pages (no real challenge, no network, nothing submitted).

Run per driver: JOB_AGENT_BROWSER_TESTS=1 [JOB_AGENT_BROWSER_DRIVER=patchright]. These prove detection,
positive resume and the hand-off boundaries on local fixtures only; they are not evidence that any
real CAPTCHA or site verification was passed."""
import json
import os
import time

import pytest

from eregion.runtime.auto_apply.contracts import posting
from verification_fixtures import APPLY, JOB, LOGIN, OTHER_JOB, PAGES, ScenarioBrowser

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')

FORM_KEYS = ['text:legalName--firstName:1', 'text:legalName--lastName:1', 'email:email:1']
EXPECTED = {'posting': posting(JOB)[1], 'host': 'example.wd5.myworkdayjobs.com', 'field_keys': FORM_KEYS}


@pytest.mark.parametrize('scene,kind,status', [
    ('passive', 'captcha', 'passive'), ('challenge', 'captcha', 'active'), ('datadome', 'waf', 'active'),
    ('interstitial', 'waf', 'active'), ('otp_email', 'otp', 'active'), ('otp_split', 'otp', 'active'),
    ('otp_label', 'otp', 'active'), ('otp_frame', 'otp', 'active'), ('form', None, 'clear'), ('blank', None, 'clear'),
])
def test_detection_is_structured_and_passive_widgets_do_not_block(scene, kind, status):
    b = ScenarioBrowser(scene=scene)
    try:
        state = b.verification_state()
        assert (state['kind'], state['status']) == (kind, status)
        assert (b.verification_barrier() is not None) == (status == 'active')
        # Evidence is sanitised: a location without query string, no code or token values.
        assert '?' not in state['location'] and 'TEST' not in json.dumps(state)
    finally:
        b.close()


def test_a_detection_error_is_unknown_and_never_cleared():
    b = ScenarioBrowser(scene='form')
    try:
        b.page.title = lambda: (_ for _ in ()).throw(RuntimeError('Execution context was destroyed'))
        state = b.verification_state()
        assert state['status'] == 'unknown' and state['evidence'] == ['detection_error:RuntimeError']
        assert 'could not be checked' in b.verification_barrier()
        assert b.resume_readiness(EXPECTED) == {'ready': False, 'reason': 'verification_unknown', 'verification': b.verification_state()} \
            or not b.resume_readiness(EXPECTED)['ready']
    finally:
        b.close()


@pytest.mark.parametrize('scene,url,ready,reason', [
    ('form', JOB, True, 'application_page'), ('passive', JOB, True, 'application_page'),
    ('blank', JOB, False, 'blank_page'), ('error', JOB, False, 'error_page'), ('challenge', JOB, False, 'verification_active'),
    ('interstitial', JOB, False, 'verification_active'), ('form', OTHER_JOB, False, 'different_application'),
    ('login', LOGIN, False, 'sign_in_page'),
])
def test_resume_needs_the_same_application_form_back(scene, url, ready, reason):
    b = ScenarioBrowser(scene='form')
    try:
        b.show(scene, url)
        result = b.resume_readiness(EXPECTED)
        assert (result['ready'], result['reason']) == (ready, reason)
    finally:
        b.close()


def test_a_removed_challenge_frame_alone_is_not_the_form_coming_back():
    b = ScenarioBrowser(scene='challenge')
    try:
        assert b.resume_readiness(EXPECTED)['reason'] == 'verification_active'
        # The challenge frame detaches and the page goes blank in between (a redirect in progress).
        b.page.evaluate("document.querySelector('iframe').remove(); document.body.innerHTML = ''")
        assert b.resume_readiness(EXPECTED) == {'ready': False, 'reason': 'blank_page'}
    finally:
        b.close()


@pytest.mark.parametrize('scene', ['otp_split', 'otp_label', 'otp_frame', 'otp_email'])
def test_typed_codes_never_reach_scans_evidence_or_page_state(scene):
    b = ScenarioBrowser(scene=scene)
    try:
        for frame in b.page.frames:  # the person types the code
            for box in frame.locator('input').all():
                box.fill('7')
        assert b.observe() == []
        state, page = b.verification_state(), b.page_state([])
        assert '7777' not in json.dumps(state) and '7777' not in json.dumps(page)
        # The preview masks every code input in every frame.
        from eregion.runtime.auto_apply.browser import secret_locators
        masked = {handle.evaluate('e => e.outerHTML') for frame in b.page.frames
                  for locator in secret_locators(frame) for handle in locator.element_handles()}
        typed = {handle.evaluate('e => e.outerHTML') for frame in b.page.frames for handle in frame.locator('input').element_handles()}
        assert typed and masked >= typed
    finally:
        b.close()


def on_owner(loop, run_id, action):
    """Playwright objects belong to the run's worker thread: act as the person, there."""
    return loop._worker(run_id).submit(action).result(timeout=60)


def wait_for(predicate, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.2)
    return False


@pytest.mark.parametrize('scene', ['challenge', 'otp_split'])
def test_the_loop_hands_off_waits_without_acting_and_resumes_once_on_the_real_form(database, scene):
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from test_auto_apply import request
    browsers = []
    def factory(url):
        browsers.append(ScenarioBrowser(scene=scene))
        return browsers[-1]
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=factory)
    try:
        run_id = loop.start(request(url=JOB))['run_id']
        assert wait_for(lambda: loop.repository.get(run_id)['status'] == 'needs_input')
        state = loop.repository.get(run_id)
        assert state['handoff_kind'] == 'verification' and state['handoff']['phase'] == 'waiting'
        browser = browsers[0]
        assert on_owner(loop, run_id, browser.counters) == {'writes': 0, 'nexts': 0, 'uploads': 0, 'submits': 0}
        def person_handles_it():
            for frame in browser.page.frames:
                for box in frame.locator('input[maxlength="1"]').all():
                    box.fill('4')  # an OTP the person types; it must never be stored
            # The person's own typing is not an agent write: count from here on.
            browser.page.evaluate("document.querySelectorAll('iframe').forEach(f => f.remove()); document.body.innerHTML = ''; window.writes = 0")
        on_owner(loop, run_id, person_handles_it)
        time.sleep(7)  # several checks on a blank page: still waiting, nothing filled
        state = loop.repository.get(run_id)
        assert state['status'] == 'needs_input' and state['handoff']['phase'] == 'verifying'
        assert on_owner(loop, run_id, browser.counters)['writes'] == 0
        def form_is_back():
            browser.show('form')
            browser.page.fill('#last', 'Typed-by-person')
        on_owner(loop, run_id, form_is_back)
        assert wait_for(lambda: loop.repository.get(run_id)['status'] != 'needs_input' or
                        (loop.repository.get(run_id).get('handoff') or {}).get('phase') == 'resolved', 30)
        assert wait_for(lambda: loop.repository.get(run_id)['status'] not in {'queued', 'running'}, 60)
        final = loop.repository.get(run_id)
        assert final['handoff']['phase'] == 'resolved' and len(browsers) == 1  # one owner, one browser
        counters = on_owner(loop, run_id, browser.counters)
        assert counters['submits'] == 0
        assert on_owner(loop, run_id, lambda: browser.page.input_value('#last')) == 'Typed-by-person'
        assert '4444' not in json.dumps(final) and '"4"' not in json.dumps(final.get('answers', {}))
    finally:
        loop.close()


@pytest.fixture
def database(tmp_path):
    from eregion.api.db import create_database
    db = create_database(f"sqlite:///{(tmp_path / 'applications.sqlite').as_posix()}", create_schema_for_tests=True)
    yield db
    db.close()


@pytest.mark.parametrize('scene,status,widgets', [
    ('widget_completed', 'passive', [{'provider': 'turnstile', 'rendered': True, 'visible': True, 'response': True}]),
    ('widget_invisible_completed', 'passive', [{'provider': 'turnstile', 'rendered': True, 'visible': False, 'response': True}]),
    ('widget_invisible_idle', 'passive', [{'provider': 'turnstile', 'rendered': True, 'visible': False, 'response': False}]),
    ('widget_unfinished', 'active', [{'provider': 'turnstile', 'rendered': True, 'visible': True, 'response': False}]),
    ('widget_rejected', 'active', [{'provider': 'turnstile', 'rendered': True, 'visible': True, 'response': True}]),
    ('widget_load_failed', 'active', [{'provider': None, 'rendered': False, 'visible': False, 'response': False}]),
])
def test_each_widget_is_reported_with_what_was_observed(scene, status, widgets):
    b = ScenarioBrowser(scene=scene)
    try:
        state = b.verification_state()
        assert state['status'] == status and state['widgets'] == widgets
        assert 'dummy-response' not in json.dumps(state)  # presence only, never the value
        if scene == 'widget_completed':
            assert any('not proof the site accepts it' in e for e in state['evidence'])
        if scene == 'widget_invisible_idle':
            assert any('idle until submit, or still running' in e for e in state['evidence'])
    finally:
        b.close()


@pytest.mark.parametrize('scene', ['widget_unfinished', 'widget_load_failed'])
def test_the_six_second_wait_only_hands_back_and_never_passes(scene):
    b = ScenarioBrowser(scene=scene)
    try:
        started = time.monotonic()
        message = b.verification_barrier()
        assert 5.5 <= time.monotonic() - started < 12
        assert message and 'has not completed or did not load correctly' in message
        assert 'Complete the verification challenge' not in message  # it does not claim there is a puzzle to solve
        assert b.verification_state()['status'] == 'active' and not b.resume_readiness(EXPECTED)['ready']
    finally:
        b.close()


def test_a_completed_widget_that_expires_is_unfinished_again():
    b = ScenarioBrowser(scene='widget_completed')
    try:
        assert b.verification_state()['status'] == 'passive'
        b.page.evaluate("document.querySelector('[name=cf-turnstile-response]').value = ''")  # the response expired
        assert b.verification_state()['status'] == 'active' and not b.resume_readiness(EXPECTED)['ready']
    finally:
        b.close()


def test_a_response_the_site_rejected_is_not_a_cleared_verification():
    b = ScenarioBrowser(scene='widget_rejected')
    try:
        assert 'not accepted' in b.verification_barrier()
        assert b.resume_readiness(EXPECTED) ['reason'] == 'verification_active'
    finally:
        b.close()


def test_a_response_alone_does_not_resume_without_the_application_form():
    b = ScenarioBrowser(scene='widget_completed_error')
    try:
        assert b.verification_state()['status'] == 'passive'  # the widget finished...
        assert b.resume_readiness(EXPECTED) == {'ready': False, 'reason': 'error_page'}  # ...but the form is not back
        b.show('widget_completed', OTHER_JOB)
        assert b.resume_readiness(EXPECTED)['reason'] == 'different_application'
        b.show('widget_completed')
        assert b.resume_readiness(EXPECTED)['ready']  # one observation; the loop still needs two in a row
    finally:
        b.close()
