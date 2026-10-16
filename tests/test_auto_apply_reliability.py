"""Popups, cross-site redirects, identity providers, confirmations, review stability and handoff resume."""
import os
from types import SimpleNamespace

import pytest

from eregion.runtime.auto_apply.browser import ApplicationBrowser
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from test_auto_apply import FakeBrowser, database, request, wait

chrome = pytest.mark.skipif(os.getenv("JOB_AGENT_BROWSER_TESTS") != "1", reason="Offline Chrome opt-in")


def guard_browser(**changes):
    """The request guard and barrier logic without launching Chrome."""
    browser = ApplicationBrowser.__new__(ApplicationBrowser)
    browser.purpose, browser.headless, browser._trusted_until = "application", True, 0
    browser.navigation_hosts = {"careers.example.com"}
    browser._dns_cache = {"careers.example.com": True, "ats.example.net": True, "internal.example": False}
    browser.__dict__.update(changes)
    return browser


def navigation(url):
    calls = []
    route = SimpleNamespace(request=SimpleNamespace(url=url, is_navigation_request=lambda: True,
        frame=SimpleNamespace(parent_frame=None)), abort=lambda: calls.append("abort"),
        continue_=lambda: calls.append("continue"))
    return route, calls


def test_any_public_https_site_may_be_visited_but_never_private_or_plain_http():
    # User decision (2026-10-15): applications move between career sites, ATS hosts,
    # sign-in providers and redirects, so no click or allowlist is required.
    browser = guard_browser()
    route, calls = navigation("https://ats.example.net/apply/123")
    browser._guard_request(route)
    assert calls == ["continue"] and "ats.example.net" in browser.navigation_hosts

    # Never to private addresses or plain HTTP.
    for url in ("https://internal.example/", "http://ats2.example.net/", "https://127.0.0.1/"):
        route, calls = navigation(url)
        browser._guard_request(route)
        assert calls == ["abort"], url

    # In the visible window the user may navigate anywhere public.
    visible = guard_browser(headless=False)
    visible._dns_cache["sso.example.org"] = True
    route, calls = navigation("https://sso.example.org/login")
    visible._guard_request(route)
    assert calls == ["continue"]


def test_identity_provider_pages_are_handed_to_the_user():
    browser = guard_browser(page=SimpleNamespace(url="https://accounts.google.com/signin"))
    assert "accounts.google.com sign-in" in browser.barrier()


def test_review_digest_ignores_clocks_but_not_answers():
    page = {"url": "https://x.example/review", "heading": "Review", "can_submit": True,
        "text": "Phone 555-1234. Saved 2 minutes ago at 10:42 PM."}
    digest = ApplicationLoop._review_digest([], page)
    assert digest == ApplicationLoop._review_digest([], {**page, "text": "Phone 555-1234. Saved just now at 10:44 PM."})
    assert digest != ApplicationLoop._review_digest([], {**page, "text": "Phone 555-9999. Saved 2 minutes ago at 10:42 PM."})


class VerificationBrowser(FakeBrowser):
    challenged = True
    def barrier(self): return "Complete the verification challenge in the open browser, then continue." if self.challenged else None
    def verification_barrier(self): return self.barrier()


def test_run_continues_by_itself_after_verification_is_cleared(database):
    browser = VerificationBrowser("")
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request())["run_id"]
        paused = wait(loop, run_id)
        assert paused["status"] == "needs_input" and paused["handoff_kind"] == "verification"
        browser.challenged = False  # The user solved it in the browser window.
        import time
        deadline = time.monotonic() + 10
        while loop.repository.get(run_id)["status"] == "needs_input" and time.monotonic() < deadline:
            time.sleep(.05)
        assert wait(loop, run_id)["status"] == "awaiting_review"
    finally:
        loop.close()


def test_a_user_pause_is_not_overridden_by_auto_resume(database):
    from eregion.runtime.auto_apply.contracts import ChatRequest
    browser = VerificationBrowser("")
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request(automation_mode="hybrid"))["run_id"]
        wait(loop, run_id)
        loop.chat(run_id, ChatRequest(text="pause", message_id="pause-message-1"))
        browser.challenged = False
        import time
        time.sleep(3.5)
        state = loop.repository.get(run_id)
        assert state["status"] == "needs_input" and state["pause_requested"]
    finally:
        loop.close()


@chrome
def test_closed_popup_returns_to_the_application_tab():
    from test_auto_apply_browser import FixtureBrowser
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('<form id="application"><label for="n">First name</label><input id="n" required>'
            '<button type="button" id="open" onclick="window.popup=window.open(\'about:blank\')">Privacy</button></form>')
        anchor = browser.page
        with browser.context.expect_page() as opened:
            browser.page.click("#open")
        opened.value.close()
        assert browser.is_open()
        assert [f["label"] for f in browser.observe()] == ["First name"]
        assert browser.page is anchor
    finally:
        browser.close()


@chrome
def test_generic_confirmation_is_only_accepted_after_submitting():
    from test_auto_apply_browser import FixtureBrowser
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('<main><h1>Thanks for applying!</h1><p>We have received your application.</p></main>')
        assert browser.confirmation() is None
        assert "received your application" in browser.confirmation(after_submit=True)["text"]
        # An unfinished form with polite text is not a confirmation.
        browser.page.set_content('<main><p>Thank you for your application to Acme.</p><form id="application">'
            '<label for="n">First name</label><input id="n" required><button type="submit">Submit</button></form></main>')
        assert browser.confirmation(after_submit=True) is None
    finally:
        browser.close()


@chrome
def test_offscreen_window_is_shown_without_reloading(monkeypatch):
    monkeypatch.setenv("JOB_AGENT_HIDDEN_BROWSER", "offscreen")
    from test_auto_apply_browser import FixtureBrowser
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        assert browser._offscreen and browser.headless
        assert browser.page.evaluate("navigator.userAgent").find("Headless") == -1
        browser.page.evaluate("window.marker = 'kept'")
        bounds = lambda: browser.context.new_cdp_session(browser.page).send("Browser.getWindowForTarget")["bounds"]
        assert bounds()["left"] < -10000
        browser.set_visible(True)
        assert not browser.headless and bounds()["left"] >= 0
        assert browser.page.evaluate("window.marker") == "kept"  # Same page, no relaunch.
        browser.set_visible(False)
        assert browser.headless and bounds()["left"] < -10000
    finally:
        browser.close()


def test_application_allowance_survives_continue_and_grows_only_on_explicit_grant(database, monkeypatch):
    # W4: the per-step budget resets on every continue, so the Adobe run reserved 589 calls.
    import eregion.runtime.auto_apply.loop as loop_module
    from eregion.runtime.auto_apply.contracts import ContinueApplication
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from test_auto_apply import FakeBrowser, request, wait
    monkeypatch.setattr(loop_module,'APPLICATION_MODEL_CALLS',5)
    repo=ApplyRepository(database.session_factory)
    loop=ApplicationLoop(repo,browser_factory=FakeBrowser)
    try:
        run=loop.start(request())['run_id'];state=wait(loop,run)
        monkeypatch.setattr(loop,'_can_act',lambda run_id:True)  # as if the worker were running
        state=repo.save(state,agent_calls=4,budget_model_base=4)  # a continue reset the step budget
        assert loop._reserve(run,'model') is True   # call 5 of 5
        loop._checkpoint(run)
        assert loop._reserve(run,'model') is False  # the application allowance still holds
        assert repo.get(run)['agent_calls']==5
        state=repo.get(run)
        loop.continue_run(run,ContinueApplication(expected_version=state['version'],extend_budget=True))
        state=repo.get(run)
        assert state['model_call_allowance']==5+loop_module.BUDGET_EXTENSION
        assert state['budget_extensions'][-1]['added']==loop_module.BUDGET_EXTENSION
    finally:
        loop.close()


def test_verbatim_answers_the_model_scopes_broadly_are_reusable_now(database, monkeypatch):
    # §13: LinkedIn and similar facts were re-asked on every application because chat answers
    # became reusable only after review approval. Interpretations still wait for approval.
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from test_auto_apply import FakeBrowser, field, request, wait
    repo=ApplyRepository(database.session_factory)
    loop=ApplicationLoop(repo,browser_factory=FakeBrowser)
    try:
        run=loop.start(request())['run_id'];state=wait(loop,run)
        message={'id':'m1','role':'user','text':'https://www.linkedin.com/in/example and I heard about it on their careers site'}
        state=repo.save(state,messages=[message],fields=[*state['fields'],field('LinkedIn URL'),field('How did you hear about us?')])
        monkeypatch.setattr(loop,'_active',lambda run_id:repo.get(run_id))
        loop._bind_answers(run,[
            {'field_key':'LinkedIn URL','message_id':'m1','quote':'https://www.linkedin.com/in/example','value':'https://www.linkedin.com/in/example','reuse':'all_applications'},
            {'field_key':'How did you hear about us?','message_id':'m1','quote':'on their careers site','value':'Company website','reuse':'this_application'}],
            state.get('chat_revision',0))
        sources=repo.get(run)['answer_sources']
        assert sources['LinkedIn URL']['pending_approval'] is False and sources['LinkedIn URL']['interpretation']=='user_statement'
        assert sources['How did you hear about us?']['pending_approval'] is True
        other,_=repo.create(request().model_copy(update={'url':'https://jobs.lever.co/other/xyz','idempotency_key':'request-0002'}))
        recalled,_=repo.recalled_answers(other,[field('LinkedIn URL'),field('How did you hear about us?')])
        assert recalled=={'LinkedIn URL':'https://www.linkedin.com/in/example'}
    finally:
        loop.close()


def test_changes_made_while_the_user_controlled_the_browser_become_their_answers(database, monkeypatch):
    # §13: resuming after a takeover could overwrite the user's manual corrections.
    from eregion.runtime.auto_apply.autofill import autofill
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from test_auto_apply import FakeBrowser, request, wait
    repo=ApplyRepository(database.session_factory);browser=FakeBrowser('')
    loop=ApplicationLoop(repo,browser_factory=lambda _:browser)
    try:
        run=loop.start(request())['run_id'];state=wait(loop,run)
        before=browser.observe()
        assert before[0]['value']=='Test'
        browser.fields[0]['value']='Alexandra'  # the user's manual correction
        monkeypatch.setattr(loop,'_active',lambda run_id:repo.get(run_id))
        loop._adopt_manual_edits(run,browser,before)
        state=repo.get(run)
        assert state['answers']['First name']=='Alexandra' and state['answer_sources']['First name']['type']=='manual'
        autofill(browser,state,lambda:b'')
        assert browser.fields[0]['value']=='Alexandra'  # not overwritten
    finally:
        loop.close()


def test_application_work_counts_against_its_career_task_budget(database, monkeypatch):
    # W6: only calls inside the starting tool were in the task ledger (17 of 40), so the
    # 60-attempt cap missed the worker's own calls. Workers now run in the task's scope.
    from eregion.runtime.auto_apply.loop import ApplicationLoop
    from eregion.runtime.auto_apply.repository import ApplyRepository
    from eregion.runtime.provider_usage import TaskProviderBudgetExceeded, current_scope
    from test_auto_apply import FakeBrowser, request, wait
    repo=ApplyRepository(database.session_factory)
    loop=ApplicationLoop(repo,browser_factory=FakeBrowser)
    try:
        run=loop.start(request())['run_id'];state=wait(loop,run)
        repo.save(state,origin_task_id='task-123')
        seen=loop._metered(run,lambda:(current_scope().get('task_id'),current_scope().get('run_id'),current_scope().get('ledger') is not None))
        assert seen==('task-123',run,True)
        monkeypatch.setattr(loop,'_active',lambda run_id:repo.get(run_id))
        def spend():raise TaskProviderBudgetExceeded('provider_attempts')
        loop._metered(run,spend)  # parked, not a crashed worker
        state=repo.get(run)
        assert state['status']=='needs_input' and state['agent_reason']=='application_budget'
    finally:
        loop.close()
