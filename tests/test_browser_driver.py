"""E4: the application browser's driver adapter (Playwright default, Patchright opt-in)."""
import os
import sys
import types

import pytest

from eregion.runtime.auto_apply import driver
from eregion.runtime.auto_apply.browser import ApplicationBrowser, BrowserBlocked
from eregion.runtime.auto_apply.errors import failure_detail


def test_default_is_playwright_and_unknown_names_are_refused(monkeypatch):
    monkeypatch.delenv('JOB_AGENT_BROWSER_DRIVER', raising=False)
    assert driver.driver_name() == 'playwright'
    monkeypatch.setenv('JOB_AGENT_BROWSER_DRIVER', ' Patchright ')
    assert driver.driver_name() == 'patchright'
    monkeypatch.setenv('JOB_AGENT_BROWSER_DRIVER', 'selenium')
    with pytest.raises(ValueError, match='must be one of'):
        driver.driver_name()
    with pytest.raises(BrowserBlocked, match='must be one of'):
        ApplicationBrowser('https://jobs.lever.co/example/abc123')


def test_a_missing_selected_driver_is_explicit_not_a_silent_switch(monkeypatch):
    monkeypatch.setenv('JOB_AGENT_BROWSER_DRIVER', 'patchright')
    monkeypatch.setitem(sys.modules, 'patchright.sync_api', None)  # simulate "not installed"
    with pytest.raises(BrowserBlocked, match=r'\(patchright\).*JOB_AGENT_BROWSER_DRIVER=playwright'):
        ApplicationBrowser('https://jobs.lever.co/example/abc123')


def test_both_drivers_errors_are_classified_as_dom_failures():
    for package in driver.DRIVERS:
        error_type = type('Error', (Exception,), {'__module__': f'{package}._impl._errors'})
        assert driver.is_driver_error(error_type('boom'))
        assert failure_detail(error_type('boom'))['error_code'] == 'dom_interaction_failed'
    assert not driver.is_driver_error(ValueError('x'))


def test_each_driver_keeps_its_own_dispatcher_on_a_worker_thread():
    # On a fresh thread: the registry is thread-local, and clearing the main thread's registry
    # would orphan a live dispatcher used by later browser tests.
    import threading
    from eregion.runtime.auto_apply import browser as module
    stopped, outcome = [], {}
    def body():
        make = lambda name: types.SimpleNamespace(name=name, stop=lambda: stopped.append(name))
        first = module._acquire_playwright(lambda: make('playwright'), 'playwright')
        second = module._acquire_playwright(lambda: make('patchright'), 'patchright')
        again = module._acquire_playwright(lambda: make('unused'), 'playwright')
        outcome['shared'] = first is again and first is not second
        module._release_playwright(again); outcome['early'] = list(stopped)
        module._release_playwright(first); module._release_playwright(second)
    worker = threading.Thread(target=body); worker.start(); worker.join(10)
    assert outcome == {'shared': True, 'early': []} and sorted(stopped) == ['patchright', 'playwright']


@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')
@pytest.mark.parametrize('name', driver.DRIVERS)
def test_both_drivers_open_the_same_fixture_form(monkeypatch, name):
    monkeypatch.setenv('JOB_AGENT_BROWSER_DRIVER', name)
    from test_auto_apply_browser import FixtureBrowser
    b = FixtureBrowser('https://job-boards.greenhouse.io/example/jobs/123')
    try:
        assert b.driver == name and type(b.playwright).__module__.startswith(name)
        assert [f['label'] for f in b.observe()]
    finally:
        b.close()
