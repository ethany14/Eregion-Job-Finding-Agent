"""The submission boundary, durable recovery, and candidate-answer contract."""
import base64
import copy
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from eregion.api.db import create_database, upgrade_database
from eregion.api.main import create_app
from eregion.runtime.auto_apply.contracts import (
    Candidate, ContinueApplication, StartApplication, candidate_answer, posting,
)
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyConflict, ApplyRepository


def request(**changes):
    return StartApplication(**{
        "url": "https://jobs.lever.co/example/abc123", "idempotency_key": "request-0001",
        "automation_mode": "autofill",
        "profile": {"first_name": "Test", "last_name": "Candidate", "email": "test@example.com"},
        "resume_base64": base64.b64encode(b"%PDF-1.4\nfixture").decode(), **changes})


def field(key, kind="text", label=None, required=True):
    return {"id": key, "key": key, "kind": kind, "label": label or key,
        "required": required, "value": "", "invalid": False, "options": []}


class FakeBrowser:
    def __init__(self, url, *, question=False, uncertain=False, submit_error=False):
        self.fields = [field("First name"), field("Last name"), field("Email", "email"), field("Resume", "file")]
        if question:
            self.fields.append(field("Do you need sponsorship?"))
        self.submissions = 0
        self.closed = False
        self.uncertain, self.submit_error = uncertain, submit_error
        self.after_fill = None

    def is_open(self): return not self.closed
    def observe(self): return copy.deepcopy(self.fields)
    def barrier(self): return None
    def fill(self, current, value):
        match = next(item for item in self.fields if item["key"] == current["key"])
        match["value"] = current.get("upload_filename", "resume.pdf") if current["kind"] == "file" else value
        if self.after_fill: self.after_fill()
    def confirmation(self):
        return {"url": "https://jobs.lever.co/example/abc123/thanks", "text": "Application received"} if self.submissions and not self.uncertain else None
    def submit(self):
        self.submit_clicked = True  # Like ApplicationBrowser: the error below happens during the click.
        self.submissions += 1
        if self.submit_error: raise TimeoutError("unsafe provider details must not be returned")
    def wait_confirmation(self): return self.confirmation()
    def close(self): self.closed = True


@pytest.fixture
def database(tmp_path):
    db = create_database(f"sqlite:///{(tmp_path / 'applications.sqlite').as_posix()}", create_schema_for_tests=True)
    yield db
    db.close()


def wait(loop, run_id):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        state = loop.repository.get(run_id)
        if state["status"] not in {"queued", "running", "submitting"}:
            return state
        time.sleep(.01)
    raise AssertionError(f"Application did not pause: {state['status']}")


@pytest.mark.parametrize("url", [
    "https://jobs.lever.co.evil.example/a/b", "http://jobs.lever.co/a/b", "https://user:pass@jobs.lever.co/a/b",
    "https://localhost/a/b", "file:///resume.pdf", "https://jobs.lever.co:8080/a/b",
    "https://job-boards.greenhouse.io/acme", "https://127.0.0.1/jobs/123", "https://example.com/",
])
def test_rejects_unsupported_navigation(url):
    with pytest.raises(ValueError): posting(url)


def test_posting_identity_ignores_tracking_and_lever_apply_suffix():
    assert posting("https://jobs.lever.co/acme/abc?utm_source=test")[1:] == posting("https://jobs.lever.co/acme/abc/apply")[1:]
    assert posting("https://boards.greenhouse.io/acme/jobs/123")[1] == posting("https://job-boards.greenhouse.io/acme/jobs/123")[1]


def test_no_guessed_sensitive_answers_and_strict_upload():
    profile = request().profile.model_dump()
    assert candidate_answer(field("Do you need sponsorship?"), profile, {}) is None
    assert candidate_answer(field("Ethnicity", "select"), profile, {}) is None
    assert candidate_answer(field("First name*"), profile, {}) == "Test"
    assert candidate_answer(field("Consent", "checkbox"), profile, {"Consent": False}) is False
    with pytest.raises(ValidationError): request(resume_base64=base64.b64encode(b"not a pdf").decode())
    with pytest.raises(ValidationError): Candidate(first_name="T", last_name="C", email="invalid")


def test_questions_review_edit_and_single_submission(database):
    browser = FakeBrowser("", question=True)
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        first = loop.start(request())
        paused = wait(loop, first["run_id"])
        assert paused["status"] == "needs_input"
        assert browser.submissions == 0
        loop.continue_run(first["run_id"], ContinueApplication(expected_version=paused["version"], answers={"Do you need sponsorship?": "Yes"}))
        review = wait(loop, first["run_id"])
        assert review["status"] == "awaiting_review"
        loop.continue_run(first["run_id"], ContinueApplication(expected_version=review["version"], answers={"First name": "Updated"}))
        review = wait(loop, first["run_id"])
        assert browser.fields[0]["value"] == "Updated"
        loop.continue_run(first["run_id"], ContinueApplication(expected_version=review["version"], confirm_submit=True))
        done = wait(loop, first["run_id"])
        assert done["status"] == "submitted" and done["confirmation"]
        assert browser.submissions == 1
        with pytest.raises(ApplyConflict): loop.continue_run(first["run_id"], ContinueApplication(expected_version=done["version"], confirm_submit=True))
    finally: loop.close()


@pytest.mark.parametrize("submit_error", [False, True])
def test_uncertain_submission_is_never_retried_even_after_restart(database, submit_error):
    repo = ApplyRepository(database.session_factory)
    browser = FakeBrowser("", uncertain=True, submit_error=submit_error)
    loop = ApplicationLoop(repo, browser_factory=lambda _: browser)
    run_id = loop.start(request(auto_submit=True))["run_id"]
    done = wait(loop, run_id)
    assert done["status"] == "submission_unknown"
    assert "unsafe provider" not in str(done)
    loop.close()
    recovered = ApplicationLoop(repo, browser_factory=lambda _: pytest.fail("Submission must not be replayed"))
    try:
        assert repo.get(run_id)["status"] == "submission_unknown"
        with pytest.raises(ApplyConflict): recovered.start(request(idempotency_key="different-request"))
        assert browser.submissions == 1
    finally: recovered.close()


def test_failure_before_the_submit_click_is_reported_as_not_submitted(database):
    # Pilot 1 (2026-10-15): a check that fails before any click used to read "may have
    # reached the employer". Nothing was sent, so the run says so and can be reviewed again.
    from eregion.runtime.auto_apply.browser import BrowserBlocked
    browser = FakeBrowser("")
    def refuse():
        raise BrowserBlocked("A single final submit button could not be identified.")
    browser.submit = refuse
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request(auto_submit=True))["run_id"]
        done = wait(loop, run_id)
        assert done["status"] == "needs_input" and done["message"].startswith("Not submitted:")
        assert "could not be identified" in done["message"] and browser.submissions == 0
    finally: loop.close()


def test_changed_form_requires_fresh_review(database):
    browser = FakeBrowser("")
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request())["run_id"]
        review = wait(loop, run_id)
        browser.fields.append({**field("New required question"), "value": "Manually entered"})
        loop.continue_run(run_id, ContinueApplication(expected_version=review["version"], confirm_submit=True))
        assert wait(loop, run_id)["status"] == "awaiting_review"
        assert browser.submissions == 0
    finally: loop.close()


def test_review_does_not_survive_browser_reopen(database):
    browsers = []
    def factory(url):
        browser = FakeBrowser(url); browsers.append(browser); return browser
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=factory)
    try:
        run_id = loop.start(request())["run_id"]
        review = wait(loop, run_id)
        browsers[0].close()
        loop.continue_run(run_id, ContinueApplication(expected_version=review["version"], confirm_submit=True))
        assert wait(loop, run_id)["status"] == "awaiting_review"
        assert len(browsers) == 2 and sum(b.submissions for b in browsers) == 0
    finally: loop.close()


def test_restart_fences_submission_and_recovers_paused_work(database):
    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request())
    repo.save(state, status="submitting")
    another, _ = repo.create(request(url="https://jobs.lever.co/acme/other", idempotency_key="another-request"))
    repo.save(another, status="running")
    repo.recover()
    assert repo.get(state["run_id"])["status"] == "submission_unknown"
    assert repo.get(another["run_id"])["status"] == "needs_input"


def test_idempotency_and_stale_version(database):
    repo = ApplyRepository(database.session_factory)
    state, created = repo.create(request())
    replay, created_again = repo.create(request())
    assert created and not created_again and replay == state
    with pytest.raises(ApplyConflict): repo.create(request(auto_submit=True))
    repo.save(state, status="needs_input")
    with pytest.raises(ApplyConflict): repo.save(state, status="submitting")


def test_capacity_does_not_break_idempotent_replay(database, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_MAX_OPEN_APPLICATIONS", "3")
    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request())
    for index in range(2):
        repo.create(request(url=f"https://jobs.lever.co/example/other{index}", idempotency_key=f"request-extra-{index}"))
    replay, created = repo.create(request())
    assert replay["run_id"] == state["run_id"] and not created
    with pytest.raises(ApplyConflict, match="limit"):
        repo.create(request(url="https://jobs.lever.co/example/overflow", idempotency_key="request-overflow"))


def test_existing_contact_value_is_reconciled_to_candidate_profile(database):
    browser = FakeBrowser("")
    browser.fields[2]["value"] = "stale@example.com"
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request())["run_id"]
        assert wait(loop, run_id)["status"] == "awaiting_review"
        assert browser.fields[2]["value"] == "test@example.com"
    finally: loop.close()


def test_cancellation_stops_before_submit(database):
    browser = FakeBrowser("")
    entered, release = threading.Event(), threading.Event()
    def pause_fill(): entered.set(); release.wait(5)
    browser.after_fill = pause_fill
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser)
    try:
        run_id = loop.start(request(auto_submit=True))["run_id"]
        assert entered.wait(5)
        current = loop.repository.get(run_id)
        loop.cancel(run_id, current["version"])
        release.set()
    finally:
        release.set(); loop.close()
    assert browser.submissions == 0 and loop.repository.get(run_id)["status"] == "cancelled"


def test_api_lifecycle_origin_guard_and_private_fields(database):
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=FakeBrowser)
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    with TestClient(app) as client:
        assert client.get("/apply").status_code == 200
        assert client.get("/web-assets/apply.js").status_code == 200
        assert client.post("/api/auto-apply", json=request().model_dump(), headers={"Origin":"https://evil.example"}).status_code == 403
        response = client.post("/api/auto-apply", json=request().model_dump())
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        review = wait(loop, run_id)
        public = client.get(f"/api/auto-apply/{run_id}").json()
        assert "profile" not in public and "answers" not in public and "resume_base64" not in public
        assert client.post(f"/api/auto-apply/{run_id}/continue", json={"expected_version":1,"confirm_submit":True}).status_code == 409
        assert client.post(f"/api/auto-apply/{run_id}/continue", json={"expected_version":review["version"],"answers":{"invented":"answer"}}).status_code == 409
        response = client.post(f"/api/auto-apply/{run_id}/cancel", json={"expected_version":review["version"]})
        assert response.status_code == 200 and response.json()["status"] == "cancelled"
        assert client.get("/api/auto-apply/missing").status_code == 404


def test_migration_upgrades_existing_database(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migration.sqlite').as_posix()}"
    upgrade_database(url)
    db = create_database(url)
    try:
        state, _ = ApplyRepository(db.session_factory).create(request())
        assert ApplyRepository(db.session_factory).get(state["run_id"])["status"] == "queued"
    finally: db.close()
