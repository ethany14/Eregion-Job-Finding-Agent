"""Suite-wide isolation for offline tests.

Offline tests must never read the maintainer's real .env, use real model credentials or
open outside connections. An explicit live run (JOB_AGENT_LIVE_*=1 or
JOB_AGENT_B6_REPLAY_LIVE_MODEL=1) is the only exception and turns this isolation off.
"""
import ipaddress
import os
import socket
from pathlib import Path

import pytest

REPO_ENV = (Path(__file__).resolve().parent.parent / '.env').resolve()
LIVE = any(v == '1' for k, v in os.environ.items()
           if k.startswith('JOB_AGENT_LIVE_') or k == 'JOB_AGENT_B6_REPLAY_LIVE_MODEL')
DOTENV_READERS = ('eregion.domain.model', 'eregion.domain.jev_fit.settings', 'eregion.runtime.fast_decisions',
                  'eregion.runtime.application_pack.workflow')
FAKE_MODEL = {'LLM_MODEL_ID': 'offline-test-model', 'LLM_API_KEY': 'offline-test-key',
              'LLM_BASE_URL': 'https://offline.invalid/v1'}


def _loopback(address):
    host = address[0] if isinstance(address, tuple) else address
    if not isinstance(host, str):
        return True  # AF_UNIX / named pipes
    if host in {'localhost', ''}:
        return True
    try:
        return ipaddress.ip_address(host.split('%')[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def _offline_isolation(monkeypatch):
    # Saving a candidate resume starts a background resume analysis with the configured
    # provider (agent_runtime/analysis_cache.warm_resume); it must never run in tests.
    # A test that exercises warmup sets JOB_AGENT_ANALYSIS_WARMUP=1 explicitly.
    monkeypatch.setenv('JOB_AGENT_ANALYSIS_WARMUP', '0')
    if not LIVE:
        # Offline tests count qualification calls; the borderline second opinion is
        # exercised by its own test, which enables it explicitly. Live runs use the default.
        monkeypatch.setenv('JOB_AGENT_QUALIFICATION_SECOND_OPINION', '0')
    if LIVE:
        yield
        return
    import importlib
    import dotenv
    real = dotenv.dotenv_values

    def without_repo_env(path=None, *args, **kwargs):
        if path is not None and Path(path).resolve() == REPO_ENV:
            return {}
        return real(path, *args, **kwargs)

    for name in DOTENV_READERS:
        module = importlib.import_module(name)
        if hasattr(module, 'dotenv_values'):
            monkeypatch.setattr(module, 'dotenv_values', without_repo_env)
    monkeypatch.setattr(dotenv, 'dotenv_values', without_repo_env)  # function-local imports
    for key, value in FAKE_MODEL.items():
        monkeypatch.setenv(key, value)
    attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded(method):
        def connect(self, address):
            if self.family in (socket.AF_INET, socket.AF_INET6) and not _loopback(address):
                attempts.append(address)
                raise ConnectionRefusedError(f'offline test attempted an outside connection to {address!r}')
            return method(self, address)
        return connect

    monkeypatch.setattr(socket.socket, 'connect', guarded(original_connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(original_connect_ex))
    yield
    if attempts:
        pytest.fail(f'Offline test attempted outside connections (caught or not): {attempts[:3]}')


def pytest_runtest_logreport(report):
    """On GitHub Actions, publish each failing test as a public annotation.

    Annotations are readable without signing in (the run's logs are not), so a failure on
    the Linux runner can be diagnosed from the API.
    """
    if not os.getenv('GITHUB_ACTIONS') or not report.failed:
        return
    path, line = report.location[0], (report.location[1] or 0) + 1
    detail = str(getattr(report.longrepr, 'reprcrash', None) and report.longrepr.reprcrash.message
                 or report.longreprtext.strip().splitlines()[-1:] or '')
    message = (report.nodeid + ' :: ' + detail).replace('%', '%25').replace('\r', '').replace('\n', '%0A')[:900]
    print(f'\n::error file={path},line={line}::{message}', flush=True)


def _patchright_test_evaluations_read_the_page_world():
    """Patchright evaluates in an isolated world by default, so page-script globals such as
    window.calls read as undefined. Test assertions (only calls made from files under tests/)
    read the page's main world; production code keeps Patchright's isolated default."""
    if (os.getenv('JOB_AGENT_BROWSER_DRIVER') or '').strip().casefold() != 'patchright':
        return
    import sys
    from functools import wraps
    from patchright.sync_api import Frame, Locator, Page
    tests = str(Path(__file__).resolve().parent)
    def main_world_for_tests(method):
        @wraps(method)
        def evaluate(self, *args, **kwargs):
            if 'isolated_context' not in kwargs and str(Path(sys._getframe(1).f_code.co_filename).resolve()).startswith(tests):
                kwargs['isolated_context'] = False
            return method(self, *args, **kwargs)
        return evaluate
    for owner in (Page, Frame, Locator):
        for name in ('evaluate', 'evaluate_handle', 'evaluate_all'):
            if hasattr(owner, name):
                setattr(owner, name, main_world_for_tests(getattr(owner, name)))


_patchright_test_evaluations_read_the_page_world()
