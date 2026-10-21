"""Cancelled work can resume without losing progress or crossing submission fences."""
import os
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from eregion.runtime.auto_apply.contracts import ChatRequest, can_reopen
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyConflict, ApplyRepository
from eregion.api.main import create_app
from test_auto_apply import FakeBrowser, database, request, wait


def test_cancelled_attempt_reopens_same_history_and_saved_answers(database):
    repo = ApplyRepository(database.session_factory)
    browsers = []
    def factory(url):
        browser = FakeBrowser(url)
        browsers.append(browser)
        return browser
    loop = ApplicationLoop(repo, browser_factory=factory)
    try:
        run_id = loop.start(request())["run_id"]
        state = wait(loop, run_id)
        state = repo.save(state, answers={"First name":"Updated"},
            answer_drafts={"essay":{"text":"Saved project draft"}}, pause_requested=True)
        cancelled = loop.cancel(run_id, state["version"])
        assert cancelled["status"] == "cancelled" and not cancelled["pause_requested"]
        assert can_reopen(cancelled)
        loop.resume(run_id, cancelled["version"])
        restored = wait(loop, run_id)
        assert restored["status"] == "awaiting_review"
        assert not restored["reopening"] and not restored["pause_requested"]
        assert restored["answers"]["First name"] == "Updated"
        assert restored["answer_drafts"]["essay"]["text"] == "Saved project draft"
        assert browsers[-1].fields[0]["value"] == "Updated"
        assert len(repo.list()) == 1 and len(browsers) == 2
        assert browsers[0].closed and all(b.submissions == 0 for b in browsers)
        assert any(e["status"] == "cancelled" for e in restored["events"])
        with pytest.raises(ApplyConflict):
            loop.resume(run_id, cancelled["version"])
    finally:
        loop.close()


def test_resume_waits_for_old_worker_and_cannot_revive_old_auto_submit(database):
    repo = ApplyRepository(database.session_factory)
    entered, release = Event(), Event()
    browsers = []
    def factory(url):
        browser = FakeBrowser(url)
        if not browsers:
            def pause_fill():
                entered.set()
                assert release.wait(10)
            browser.after_fill = pause_fill
        browsers.append(browser)
        return browser
    loop = ApplicationLoop(repo, browser_factory=factory)
    try:
        run_id = loop.start(request(auto_submit=True))["run_id"]
        assert entered.wait(5)
        state = repo.get(run_id)
        cancelled = loop.cancel(run_id, state["version"])
        reopening = loop.resume(run_id, cancelled["version"])
        assert reopening["reopening"] and reopening["pause_requested"]
        assert not reopening["auto_submit"] and not loop._can_act(run_id)
        with pytest.raises(ApplyConflict, match="reopening"):
            loop.chat(run_id, ChatRequest(text="continue", message_id="too-early"))
        release.set()
        restored = wait(loop, run_id)
        assert restored["status"] == "awaiting_review"
        assert len(browsers) == 2 and all(b.submissions == 0 for b in browsers)
    finally:
        release.set()
        loop.close()


def test_cancel_while_reopen_is_queued_prevents_new_browser(database):
    repo = ApplyRepository(database.session_factory)
    entered, release = Event(), Event()
    loop = ApplicationLoop(repo, browser_factory=lambda _: pytest.fail("Cancelled reopening must not open a browser"))
    def block_worker():
        entered.set()
        release.wait(10)
    try:
        state, _ = repo.create(request())
        state = repo.save(state, status="cancelled")
        loop.executor.submit(block_worker)
        assert entered.wait(5)
        reopening = loop.resume(state["run_id"], state["version"])
        loop.cancel(state["run_id"], reopening["version"])
        release.set()
        loop.executor.submit(lambda: None).result(timeout=5)
        current = repo.get(state["run_id"])
        assert current["status"] == "cancelled" and not current["reopening"]
    finally:
        release.set()
        loop.close()


@pytest.mark.parametrize("status", ["submitting", "submitted", "submission_unknown"])
def test_submission_attempt_cannot_be_reopened_even_after_history_rolls_over(database, status):
    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request())
    state = repo.save(state, status=status)
    assert state["submission_started"]
    assert not can_reopen(state)
    state = repo.save(state, status="failed", events=[])
    assert not can_reopen(state)
    with pytest.raises(ApplyConflict, match="before submission"):
        repo.reopen(state)


def test_legacy_submission_history_is_fenced_without_new_marker():
    assert not can_reopen({"status":"cancelled", "events":[{"status":"submitting"}]})
    assert not can_reopen({"status":"failed", "confirmation":{"text":"Received"}})


def test_reopening_reserves_capacity_and_restart_leaves_resumable_pause(database, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_MAX_OPEN_APPLICATIONS", "3")
    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request())
    state = repo.save(state, status="cancelled")
    others = []
    for i in range(3):
        other, _ = repo.create(request(url=f"https://jobs.lever.co/example/other-{i}", idempotency_key=f"request-other-{i}"))
        others.append(other)
    with pytest.raises(ApplyConflict, match="limit"):
        repo.reopen(state)
    repo.save(others[0], status="cancelled")
    reopened = repo.reopen(state)
    with pytest.raises(ApplyConflict, match="limit"):
        repo.create(request(url="https://jobs.lever.co/example/fourth", idempotency_key="request-fourth"))
    repo.recover()
    recovered = repo.get(reopened["run_id"])
    assert recovered["status"] == "needs_input" and recovered["pause_requested"]
    assert not recovered["reopening"]


def test_pause_and_resume_work_in_autofill_mode(database):
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=FakeBrowser)
    try:
        run_id = loop.start(request())["run_id"]
        wait(loop, run_id)
        loop.chat(run_id, ChatRequest(text="pause", message_id="pause-test"))
        assert loop.repository.get(run_id)["pause_requested"]
        loop.chat(run_id, ChatRequest(text="resume", message_id="resume-test"))
        assert wait(loop, run_id)["status"] == "awaiting_review"
    finally:
        loop.close()


def test_duplicate_api_points_to_existing_attempt_and_resume_keeps_run_id(database):
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=FakeBrowser)
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    with TestClient(app) as client:
        state = client.post("/api/auto-apply", json=request().model_dump()).json()
        state = wait(loop, state["run_id"])
        state = client.post(f"/api/auto-apply/{state['run_id']}/cancel",
            json={"expected_version":state["version"]}).json()
        duplicate = client.post("/api/auto-apply", json=request(idempotency_key="another-chat").model_dump())
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["existing_run_id"] == state["run_id"]
        assert state["can_reopen"]
        resumed = client.post(f"/api/auto-apply/{state['run_id']}/resume",
            json={"expected_version":state["version"]})
        assert resumed.status_code == 202 and resumed.json()["run_id"] == state["run_id"]
        assert wait(loop, state["run_id"])["status"] == "awaiting_review"
        assert len(loop.repository.list()) == 1


@pytest.mark.skipif(os.getenv("JOB_AGENT_BROWSER_TESTS") != "1", reason="Opt-in offline Chrome UI test")
def test_duplicate_new_chat_opens_cancelled_attempt_and_resume_works(database):
    from playwright.sync_api import sync_playwright, expect
    from test_auto_apply_drafting import project_pdf

    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request())
    # Reproduce the existing bug's persisted combination exactly.
    state = repo.save(state, status="cancelled", pause_requested=True)
    repo.save_candidate(request().profile.model_dump(), resume=project_pdf(), extracted=True)
    loop = ApplicationLoop(repo, browser_factory=FakeBrowser)
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    try:
        with TestClient(app, base_url="http://localhost") as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page(locale='en-US')
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def handle(route):
                    req = route.request
                    if not req.url.startswith("http://localhost/"):
                        route.abort()
                        return
                    response = client.request(req.method, req.url, content=req.post_data,
                        headers={k:v for k,v in req.headers.items() if k in {"content-type", "origin"}})
                    route.fulfill(status=response.status_code, body=response.content,
                        headers={"content-type":response.headers.get("content-type", "application/json")})
                page.route("**/*", handle)
                page.goto("http://localhost/apply?mode=application")
                page.locator('#chat-text').fill("https://jobs.lever.co/example/abc123?utm_source=new-chat")
                page.get_by_role('button', name='Send message', exact=True).click()
                expect(page.locator('#agent-status')).to_have_text('Application cancelled', timeout=10000)
                expect(page.locator('#apply-error')).to_be_hidden()
                expect(page.get_by_role('button', name='Resume application', exact=True)).to_be_visible()
                page.get_by_role('button', name='Resume application', exact=True).click()
                expect(page.locator('#agent-status')).to_have_text('Ready for your review', timeout=10000)
                assert len(repo.list()) == 1 and repo.list()[0]["run_id"] == state["run_id"]
                assert not errors
            finally:
                browser.close()
    finally:
        loop.close()
