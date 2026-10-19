"""V2/V3: a person handles a verification in the original browser; the loop resumes only on positive evidence.

Fake browser, no model, no network. The browser reports what ApplicationBrowser.resume_readiness
reports (tested against real Chrome in test_verification_browser.py); these tests cover the loop's
owner, freezing, priority of pause/cancel/close, single resume, budgets and manual edits."""
import time

import pytest

from eregion.runtime.auto_apply.contracts import ChatRequest
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from test_auto_apply import FakeBrowser, database, request, wait  # noqa: F401  (fixture)


class HandoffBrowser(FakeBrowser):
    """Shows a challenge until `page` changes; reports readiness the way the real browser does."""
    page = 'challenge'

    def __init__(self, url, **kwargs):
        super().__init__(url, **kwargs)
        self.checks, self.writes = 0, []

    def barrier(self):
        return 'Complete the verification challenge in the open browser, then continue.' if self.page == 'challenge' else None

    def verification_barrier(self):
        return self.barrier()

    def verification_state(self):
        return {'kind': 'captcha', 'status': 'active', 'channel': None, 'evidence': ['visible challenge frame'],
                'location': 'jobs.lever.co/example/abc123'} if self.page == 'challenge' else {'kind': None, 'status': 'clear'}

    def resume_readiness(self, expected):
        self.checks += 1
        self.expected = expected
        reasons = {'challenge': 'verification_active', 'blank': 'blank_page', 'error': 'error_page', 'login': 'sign_in_page',
                   'other_job': 'different_application', 'unknown': 'verification_unknown', 'form': 'application_page'}
        return {'ready': self.page == 'form', 'reason': reasons[self.page]}

    def fill(self, current, value):
        self.writes.append(current['key'])
        super().fill(current, value)


@pytest.fixture
def handed_off(database):
    browser = HandoffBrowser('')
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    drives = []
    original = loop._submit
    def submit(run_id, fn, *args):
        if fn == loop._drive:
            drives.append(run_id)
        return original(run_id, fn, *args)
    loop._submit = submit
    run_id = loop.start(request())['run_id']
    state = wait(loop, run_id)
    assert state['status'] == 'needs_input' and state['handoff_kind'] == 'verification'
    browser.writes.clear(); drives.clear()
    try:
        yield loop, browser, run_id, drives
    finally:
        loop.close()


def check(loop, run_id, browser):
    loop.handoff_checks[run_id] = 0  # skip the 3 s spacing in tests
    loop._resume_after_handoff(run_id, browser)


def test_the_hand_off_records_evidence_and_freezes_the_owner(handed_off):
    loop, browser, run_id, drives = handed_off
    state = loop.repository.get(run_id)
    record = state['handoff']
    assert record['phase'] == 'waiting' and record['verification']['kind'] == 'captcha'
    assert record['expected']['posting'].startswith('lever:') and record['expected']['field_keys']
    # Nothing in flight may act: no write, no Next, no model or tool reservation.
    assert not loop._can_act(run_id) and loop._reserve(run_id, 'model') is False and loop._reserve(run_id, 'tool') is False
    for _ in range(3):
        check(loop, run_id, browser)
    assert browser.writes == [] and drives == [] and loop.repository.get(run_id)['agent_calls'] == state['agent_calls']


@pytest.mark.parametrize('page,phase,words', [
    ('blank', 'verifying', 'blank'), ('error', 'verifying', 'error page'), ('unknown', 'verifying', 'could not be checked'),
    ('login', 'sign_in', 'sign-in page'), ('other_job', 'unconfirmed', 'different job'),
])
def test_a_cleared_challenge_without_the_application_form_never_resumes(handed_off, page, phase, words):
    loop, browser, run_id, drives = handed_off
    browser.page = page
    for _ in range(3):
        check(loop, run_id, browser)
    state = loop.repository.get(run_id)
    assert state['status'] == 'needs_input' and state['handoff']['phase'] == phase and words in state['message']
    assert drives == [] and browser.writes == []


def test_one_clear_observation_is_not_enough_and_two_resume_exactly_once(handed_off):
    loop, browser, run_id, drives = handed_off
    browser.page = 'form'
    check(loop, run_id, browser)
    browser.page = 'challenge'  # the frame came back
    check(loop, run_id, browser)
    browser.page = 'form'
    check(loop, run_id, browser)
    assert loop.repository.get(run_id)['status'] == 'needs_input' and drives == []
    check(loop, run_id, browser)
    for _ in range(3):
        check(loop, run_id, browser)  # later checks after the resume do nothing
    assert drives == [run_id]
    state = wait(loop, run_id)
    assert state['handoff']['phase'] == 'resolved' and state['status'] == 'awaiting_review' and state['handoff_kind'] is None


@pytest.mark.parametrize('stop', ['pause', 'cancel', 'close'])
def test_pause_cancel_and_close_win_over_a_ready_page(handed_off, stop):
    loop, browser, run_id, drives = handed_off
    browser.page = 'form'
    check(loop, run_id, browser)  # first positive check
    if stop == 'pause':
        loop.chat(run_id, ChatRequest(text='pause', message_id='pause-message-1'))
    elif stop == 'cancel':
        loop.cancel(run_id, loop.repository.get(run_id)['version'])
    else:
        loop.close()
    check(loop, run_id, browser)  # would have been the second positive check
    state = loop.repository.get(run_id)
    assert drives == [] and state['status'] in {'needs_input', 'cancelled'}
    if stop == 'pause':
        assert state['pause_requested']


def test_a_state_change_during_the_check_is_not_overwritten(handed_off):
    loop, browser, run_id, drives = handed_off
    browser.page = 'form'
    check(loop, run_id, browser)
    original = browser.resume_readiness
    def late(expected):
        result = original(expected)
        # The person writes in chat while the check runs: the late positive result must not resume.
        state = loop.repository.get(run_id)
        loop.repository.save(state, messages=[*state.get('messages', []), {'id': 'm', 'role': 'user', 'text': 'wait'}])
        return result
    browser.resume_readiness = late
    check(loop, run_id, browser)
    assert drives == [] and loop.repository.get(run_id)['status'] == 'needs_input'


def test_manual_edits_during_the_hand_off_become_answers_and_budgets_carry_over(handed_off):
    loop, browser, run_id, drives = handed_off
    before = loop.repository.get(run_id)
    first = next(f for f in browser.fields if f['label'] == 'First name')
    first['value'] = 'Manually typed'  # the person corrected a field while in the browser
    browser.page = 'form'
    check(loop, run_id, browser); check(loop, run_id, browser)
    after = wait(loop, run_id)
    assert after['answers'][first['key']] == 'Manually typed'
    assert after['answer_sources'][first['key']]['type'] == 'manual'
    assert first['value'] == 'Manually typed'  # the resumed autofill did not overwrite it
    for key in ('agent_calls', 'agent_tools', 'budget_model_base', 'budget_tool_base'):
        assert after.get(key, 0) >= before.get(key, 0)


def test_a_restarted_loop_does_not_infer_a_cleared_verification(database):
    browser = HandoffBrowser('')
    repo = ApplyRepository(database.session_factory)
    first = ApplicationLoop(repo, browser_factory=lambda _: browser)
    run_id = first.start(request())['run_id']
    assert wait(first, run_id)['handoff']['phase'] == 'waiting'
    first.close()
    browser.page = 'form'  # whatever the page shows now, the old summary is not evidence
    second = ApplicationLoop(repo, browser_factory=lambda _: browser)
    try:
        time.sleep(4)
        state = repo.get(run_id)
        assert state['status'] == 'needs_input' and state['handoff']['phase'] == 'waiting'
        # Continuing re-reads the actual page; a challenge that is still there hands off again.
        browser.page = 'challenge'
        second.chat(run_id, ChatRequest(text='continue', message_id='continue-1'))
        again = wait(second, run_id)
        assert again['status'] == 'needs_input' and again['handoff_kind'] == 'verification' and again['run_id'] == run_id
    finally:
        second.close()


def test_a_challenge_that_returns_after_resuming_uses_the_same_run_and_owner(handed_off):
    loop, browser, run_id, drives = handed_off
    browser.page = 'form'
    browser.after_fill = lambda: setattr(browser, 'page', 'challenge')  # the site challenges again mid-form
    check(loop, run_id, browser); check(loop, run_id, browser)
    state = wait(loop, run_id)
    assert drives == [run_id] and state['status'] == 'needs_input' and state['handoff_kind'] == 'verification'
    assert state['handoff']['phase'] == 'waiting' and loop.browsers[run_id] is browser and browser.submissions == 0
