"""V4 step 1: the providers' published test widgets on a local form (network to the widget hosts only).

Opt-in: JOB_AGENT_BROWSER_TESTS=1 JOB_AGENT_OFFICIAL_WIDGETS=1. Test keys produce dummy tokens that real sites
reject; no widget is clicked or completed by the test. Passing proves the wiring of detection, hand-off and the
no-submit boundary with real widget markup, not that any real challenge was passed."""
import os
import time

import pytest

from verification_fixtures import JOB, OfficialWidgetBrowser

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1' or os.getenv('JOB_AGENT_OFFICIAL_WIDGETS') != '1',
                                reason='Opt-in: real Chrome and the official widget hosts')


@pytest.fixture
def database(tmp_path):
    from eregion.api.db import create_database
    db = create_database(f"sqlite:///{(tmp_path / 'applications.sqlite').as_posix()}", create_schema_for_tests=True)
    yield db
    db.close()


@pytest.mark.parametrize('scene,status', [
    ('turnstile_pass', 'passive'), ('turnstile_invisible_pass', 'passive'), ('turnstile_interactive', 'active'),
    ('turnstile_fail', 'active'), ('hcaptcha_test', 'active'), ('recaptcha_test', 'active'),
])
def test_official_widgets_are_judged_by_completion_not_by_presence(scene, status):
    b = OfficialWidgetBrowser(scene=scene)
    try:
        assert (b.verification_barrier() is not None) == (status == 'active')  # includes the bounded settle
        state = b.verification_state()
        assert state['status'] == status and state['kind'] == 'captcha'
        assert not any('response' in e and '-' in e.split()[-1] for e in state['evidence'])  # no token values
        assert b.counters() == {'writes': 0, 'nexts': 0, 'uploads': 0, 'submits': 0}
    finally:
        b.close()


def run_loop(database, scene, seconds):
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from test_auto_apply import request
    browsers = []
    def factory(url):
        browsers.append(OfficialWidgetBrowser(scene=scene))
        return browsers[-1]
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=factory)
    run_id = loop.start(request(url=JOB))['run_id']
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and loop.repository.get(run_id)['status'] in {'queued', 'running'}:
        time.sleep(0.3)
    return loop, run_id, browsers


def test_a_self_completing_widget_does_not_hand_off(database):
    loop, run_id, browsers = run_loop(database, 'turnstile_pass', 90)
    try:
        state = loop.repository.get(run_id)
        assert state.get('handoff_kind') is None and not state.get('handoff')
        counters = loop._worker(run_id).submit(browsers[0].counters).result(timeout=30)
        assert counters['submits'] == 0 and counters['writes'] > 0  # the form was filled, nothing submitted
    finally:
        loop.close()


def test_an_interactive_widget_hands_off_and_nothing_acts_while_waiting(database):
    loop, run_id, browsers = run_loop(database, 'turnstile_interactive', 60)
    try:
        state = loop.repository.get(run_id)
        assert state['status'] == 'needs_input' and state['handoff_kind'] == 'verification'
        assert state['handoff']['verification']['evidence'] == ['unfinished turnstile verification widget']
        time.sleep(7)  # several resume checks with the widget still unfinished
        state = loop.repository.get(run_id)
        assert state['status'] == 'needs_input' and state['handoff']['phase'] == 'waiting'
        counters = loop._worker(run_id).submit(browsers[0].counters).result(timeout=30)
        assert counters == {'writes': 0, 'nexts': 0, 'uploads': 0, 'submits': 0}
    finally:
        loop.close()
