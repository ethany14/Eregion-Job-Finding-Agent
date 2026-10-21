"""Resume-grounded drafts stay editable until the user sends an answer."""
import base64
from io import BytesIO
import os
from pathlib import Path

import pytest

from eregion.runtime.auto_apply.agent import AgentTools, HybridApplicationAgent
from eregion.runtime.auto_apply.autofill import autofill, desired_answer
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.contracts import ContinueApplication
from eregion.runtime.auto_apply.drafting import resume_context, visible_drafts
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.api.auto_apply_routes import public
from test_auto_apply import FakeBrowser, database, field, request, wait
from test_auto_apply_agent import ScriptModel, args


PROJECT = "Built an LLM document assistant for the support team to search internal guides."
QUESTION = "Describe something you built with LLMs that changed how someone else worked. What was the measurable effect?"
DRAFT = "I built an LLM document assistant that helped the support team search internal guides."


def project_pdf():
    from reportlab.pdfgen.canvas import Canvas
    buffer = BytesIO()
    canvas = Canvas(buffer)
    for i, line in enumerate(["Test Candidate", "Projects", PROJECT]):
        canvas.drawString(40, 750 - i * 20, line)
    canvas.save()
    return buffer.getvalue()


def draft_args(**changes):
    return {"text": DRAFT, "evidence": [{"source_ref": "resume", "quote": PROJECT}],
            "missing_details": ["How much time did the support team save, and how was it measured?"], **changes}


def session(**changes):
    browser = FakeBrowser("")
    browser.fields = [field("essay", "textarea", QUESTION)]
    state = {"profile": request().profile.model_dump(), "answers": {},
             "resume_context": {"text": PROJECT}, **changes}
    tools = AgentTools(browser, state, autofill=lambda: autofill(browser, state, lambda: b""), active=lambda: True)
    return tools, browser, state


def draft(tools, **changes):
    snapshot = tools.observe()
    return tools.execute("draft_answer", {"observation_id": snapshot["observation_id"],
        "field_key": "essay", **draft_args(**changes)})


def test_old_attempt_resume_is_available_without_profile_extraction_or_reupload(database):
    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request(resume_base64=base64.b64encode(project_pdf()).decode()))
    context = repo.agent_context(state["run_id"])
    assert PROJECT in context["resume_context"]["text"]
    assert context["resume_context"]["page_count"] == 1
    assert "resume_context" not in repo.get(state["run_id"])
    assert "resume_context" not in public(context)
    tools, _, _ = session(**context)
    assert PROJECT in tools.observe()["resume_context"]["text"]


def test_unreadable_resume_does_not_stop_known_fact_autofill():
    context = resume_context(b"%PDF-1.4\nunreadable")
    assert context["text"] == "" and context["warning"]
    tools, browser, state = session(resume_context=context)
    browser.fields.append(field("First name"))
    tools.observe()
    tools.execute("autofill", {})
    assert browser.fields[-1]["value"] == "Test"
    with pytest.raises(BrowserBlocked, match="Cite text actually"):
        draft(tools)


def test_draft_has_evidence_but_is_not_a_confirmed_answer_or_browser_input():
    tools, browser, state = session()
    assert draft(tools)["saved"]
    assert tools.observe()["answer_drafts"]["essay"]["text"] == DRAFT
    assert tools.state["answer_drafts"]["essay"]["missing_details"]
    assert state["answers"] == {}
    assert desired_answer(browser.fields[0], state) is None
    tools.execute("autofill", {})
    assert browser.fields[0]["value"] == ""
    with pytest.raises(BrowserBlocked):
        tools.source(browser.fields[0], "draft:essay")
    with pytest.raises(BrowserBlocked):
        tools.execute("ready_for_review", {})
    state["answers"]["essay"] = "User's correction"
    assert visible_drafts(state, browser.fields) == {}
    assert desired_answer(browser.fields[0], state) == "User's correction"
    with pytest.raises(BrowserBlocked, match="existing answer"):
        draft(tools)


def test_optional_draft_does_not_hold_review():
    # Veeva pilot (2026-10-15): optional essays with drafts kept ready_for_review refusing.
    tools, browser, state = session()
    assert draft(tools)["saved"]
    browser.fields[0]["required"] = False
    tools.observe()
    assert tools.execute("ready_for_review", {})["host_validation_required"]


@pytest.mark.parametrize("evidence", [
    [{"source_ref": "resume", "quote": "Saved 50 hours every week"}],
    [{"source_ref": "page", "quote": PROJECT}],
    [{"source_ref": "message:assistant", "quote": PROJECT}],
    [],
])
def test_draft_rejects_missing_or_non_candidate_evidence(evidence):
    tools, browser, state = session(messages=[{"id":"assistant", "role":"assistant", "text":PROJECT}])
    with pytest.raises(BrowserBlocked):
        draft(tools, evidence=evidence)
    assert not state.get("answer_drafts") and browser.fields[0]["value"] == ""


def test_draft_can_include_explicit_user_measurement_and_checks_length_and_freshness():
    tools, browser, state = session(messages=[{"id":"effect", "role":"user", "text":"It saved 3 hours per week."}])
    evidence = [*draft_args()["evidence"], {"source_ref":"message:effect", "quote":"saved 3 hours per week"}]
    draft(tools, text=DRAFT + " It saved 3 hours per week.", evidence=evidence, missing_details=[])
    assert len(state["answer_drafts"]["essay"]["evidence"]) == 2
    browser.fields[0]["constraints"] = {"max_length":"20"}
    with pytest.raises(BrowserBlocked, match="character limit"):
        draft(tools)
    old = tools.observe()["observation_id"]
    tools.observe()
    with pytest.raises(BrowserBlocked, match="Stale"):
        tools.execute("draft_answer", {"observation_id":old, "field_key":"essay", **draft_args()})
    browser.fields[0]["label"] = "Why this company?"
    assert not visible_drafts(state, browser.fields)


@pytest.mark.parametrize("label,kind", [
    ("LinkedIn Profile", "text"), ("Describe your visa sponsorship needs", "textarea"),
    ("Explain your salary expectations", "textarea"), ("I agree to the privacy terms", "checkbox"),
    (QUESTION, "select"),
])
def test_drafting_does_not_bypass_structured_or_sensitive_answers(label, kind):
    tools, browser, state = session()
    browser.fields[0].update(label=label, kind=kind)
    with pytest.raises(BrowserBlocked, match="open-ended"):
        draft(tools)
    assert state["answers"] == {}


def test_draft_survives_restart_then_user_edited_answer_is_filled(database):
    repo = ApplyRepository(database.session_factory)
    browsers = []
    def browser_factory(url):
        browser = FakeBrowser(url)
        browser.fields = [field("linkedin", label="LinkedIn Profile"), field("essay", "textarea", QUESTION)]
        browsers.append(browser)
        return browser
    def grounded_draft(snapshot):
        assert PROJECT in snapshot["resume_context"]["text"]
        return args("essay", **draft_args())(snapshot)
    model = ScriptModel([("autofill", {}), ("draft_answer", grounded_draft),
                         ("request_input", {"field_keys":["linkedin", "essay"]})])
    loop = ApplicationLoop(repo, browser_factory=browser_factory,
        agent_factory=lambda: HybridApplicationAgent(model_factory=lambda: model))
    try:
        run_id = loop.start(request(automation_mode="hybrid",
            resume_base64=base64.b64encode(project_pdf()).decode()))["run_id"]
        paused = wait(loop, run_id)
        assert paused["status"] == "needs_input"
        assert set(paused["pending_fields"]) == {"linkedin", "essay"}
        assert "I drafted answers" in paused["messages"][-1]["text"]
        assert paused["answers"] == {} and not repo.memories(paused["profile"])
        assert public(paused)["answer_drafts"]["essay"]["text"] == DRAFT
        assert browsers[0].fields[1]["value"] == "" and browsers[0].submissions == 0
    finally:
        loop.close()
    model = ScriptModel([("autofill", {}), ("ready_for_review", {})])
    restored = ApplicationLoop(repo, browser_factory=browser_factory,
        agent_factory=lambda: HybridApplicationAgent(model_factory=lambda: model))
    try:
        state = repo.get(run_id)
        assert public(state)["answer_drafts"]["essay"]["text"] == DRAFT
        answer = DRAFT + " It saved 3 hours per week."
        restored.continue_run(run_id, ContinueApplication(expected_version=state["version"],
            answers={"linkedin":"https://www.linkedin.com/in/test", "essay":answer}))
        review = wait(restored, run_id)
        assert review["status"] == "awaiting_review"
        assert browsers[-1].fields[1]["value"] == answer
        assert public(review)["answer_drafts"] == {}
        assert review["answers"]["essay"] == answer
        assert browsers[-1].submissions == 0
    finally:
        restored.close()


@pytest.mark.skipif(os.getenv("JOB_AGENT_BROWSER_TESTS") != "1", reason="Opt-in offline Chrome UI test")
def test_resume_draft_ui_edits_and_sends_through_real_api(database):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright, expect
    from eregion.api.main import create_app

    repo = ApplyRepository(database.session_factory)
    pdf = project_pdf()
    repo.save_candidate(request().profile.model_dump(), resume=pdf, extracted=True)
    application_browser = FakeBrowser("")
    application_browser.fields = [field("linkedin", label="LinkedIn Profile"), field("essay", "textarea", QUESTION)]
    model = ScriptModel([("draft_answer", args("essay", **draft_args())),
        ("request_input", {"field_keys":["linkedin", "essay"]}), ("autofill", {}), ("ready_for_review", {})])
    loop = ApplicationLoop(repo, browser_factory=lambda _: application_browser,
        agent_factory=lambda: HybridApplicationAgent(model_factory=lambda: model))
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    try:
        run_id = loop.start(request(automation_mode="hybrid", resume_base64=base64.b64encode(pdf).decode()))["run_id"]
        assert wait(loop, run_id)["status"] == "needs_input"
        with TestClient(app, base_url="http://localhost") as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page(locale="en-US",viewport={"width":1440, "height":1100})
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
                page.locator("#attempts .attempt").click()
                answer = page.get_by_label(QUESTION, exact=True)
                expect(answer).to_have_value(DRAFT)
                expect(page.locator(".draft-missing")).to_contain_text("How much time")
                page.locator(".draft-evidence summary").click()
                expect(page.locator(".draft-evidence")).to_contain_text(PROJECT)
                assert not page.get_by_role("button", name="Submit answers & continue").is_enabled()
                screenshot = os.getenv("JOB_AGENT_DRAFT_SCREENSHOT")
                page.locator('#chat-scroll').evaluate("el => el.style.scrollBehavior = 'auto'")
                page.locator('#chat-scroll').evaluate('el => el.scrollTop = el.scrollHeight')
                if screenshot:
                    page.screenshot(path=screenshot)
                page.set_viewport_size({"width":390, "height":844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.locator('#chat-scroll').evaluate('el => el.scrollTop = el.scrollHeight')
                if screenshot:
                    page.screenshot(path=str(Path(screenshot).with_stem(Path(screenshot).stem + '-mobile')))
                edited = DRAFT + " It saved 3 hours per week."
                answer.fill(edited)
                page.get_by_label("LinkedIn Profile", exact=True).fill("https://www.linkedin.com/in/test")
                page.wait_for_timeout(2200)  # The normal polling render must preserve edits.
                expect(answer).to_have_value(edited)
                page.get_by_role("button", name="Submit answers & continue").click()
                expect(page.locator("#agent-status")).to_have_text("Ready for your review", timeout=10000)
                assert repo.get(run_id)["answers"]["essay"] == edited
                assert application_browser.fields[1]["value"] == edited
                assert application_browser.submissions == 0
                assert not errors
            finally:
                browser.close()
    finally:
        loop.close()
