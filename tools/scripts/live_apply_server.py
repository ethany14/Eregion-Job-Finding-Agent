"""Eregion for the real-site job-apply test (docs plan 2026-10-15): never submits.

    python tools/scripts/live_apply_server.py --data <empty folder> [--port 8020]

Starts the normal app with three guards installed before anything runs:
- the browser's submit action raises, so no form can be submitted by any path;
- every application starts with auto_submit off, so runs stop at the review step;
- the database and browser sign-in store live in --data, apart from everyday Eregion.
Everything else (model, tools, approvals, sign-in hand-off) is the production code.
"""
import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SubmissionBlocked(RuntimeError):
    pass


# Test-only third guard: a final-submission button the app might misclassify as an ordinary
# button (for example "Send" or "Finish") is still never clicked. "Apply", "Next",
# "Continue" and "Review" stay allowed so the form can be opened and stepped through.
FINAL_WORDING = re.compile(r'\b(?:submit|send(?: my)? application|send|finish(?: application)?|'
                           r'complete(?: my)? application|confirm and (?:apply|submit))\b', re.I)


def install_guards(data):
    from eregion.runtime.auto_apply import browser, loop, repository, visual
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    from eregion.runtime.auto_apply.browser_sessions import BrowserSessionStore

    def blocked(self, *args, **kwargs):
        raise SubmissionBlocked('Live apply test: submission is disabled.')
    browser.ApplicationBrowser.submit = blocked

    original_click = browser.ApplicationBrowser.click_page_action

    def click_page_action(self, action):
        if FINAL_WORDING.search(str(action.get('label') or '')):
            raise BrowserBlocked('Live apply test: final-submission controls are never clicked.')
        return original_click(self, action)
    browser.ApplicationBrowser.click_page_action = click_page_action

    original_guard = visual.VisualBrowser.guard

    def guard(target):
        if FINAL_WORDING.search(str(target.get('label') or '')):
            raise BrowserBlocked('Live apply test: final-submission controls are never clicked.')
        return original_guard(target)
    visual.VisualBrowser.guard = staticmethod(guard)
    original_create = repository.ApplyRepository.create

    def create(self, request, *args, **kwargs):
        return original_create(self, request.model_copy(update={'auto_submit': False}), *args, **kwargs)
    repository.ApplyRepository.create = create
    original_init = loop.ApplicationLoop.__init__

    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.session_store = BrowserSessionStore(Path(data) / 'browser-sessions')
    loop.ApplicationLoop.__init__ = init
    return blocked


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--data', required=True)
    parser.add_argument('--port', type=int, default=8020)
    # The real-site test plan caps each application at 60 model calls (W6 acceptance).
    parser.add_argument('--max-model-calls', type=int, default=60)
    args = parser.parse_args()
    os.environ['APPLY_MAX_MODEL_CALLS'] = str(args.max_model_calls)
    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    os.environ['JOB_AGENT_DATABASE_URL'] = 'sqlite:///' + (data / 'eregion-apply-test.sqlite3').as_posix()
    os.environ.setdefault('JOB_AGENT_HIDDEN_BROWSER', 'offscreen')
    sys.path[:0] = [str(ROOT / 'backend'), str(ROOT / 'tools')]
    import subprocess
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT,
                   env={**os.environ, 'PYTHONPATH': str(ROOT / 'backend')}, check=True)
    install_guards(data)
    import uvicorn
    uvicorn.run('eregion.api.main:app', host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
