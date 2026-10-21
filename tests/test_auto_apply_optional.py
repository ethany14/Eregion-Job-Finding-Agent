"""Optional questions requested by the agent must remain answerable in the UI."""
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from eregion.runtime.auto_apply.agent import HybridApplicationAgent
from eregion.runtime.auto_apply.browser import ApplicationBrowser
from eregion.runtime.auto_apply.contracts import ContinueApplication
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.api.main import create_app
from test_auto_apply import FakeBrowser, database, field, request, wait
from test_auto_apply_agent import ScriptModel


GENDER = ["Male", "Female", "Decline To Self Identify"]
ETHNICITY = ["Yes", "No", "Decline To Self Identify"]
VETERAN = ["I am not a protected veteran", "I identify as a protected veteran", "I don't wish to answer"]
RACE = ["American Indian or Alaska Native", "Asian", "Black or African American",
        "Hispanic or Latino", "Native Hawaiian or Other Pacific Islander", "White",
        "Two or more races", "Decline To Self Identify"]


def dropdown(key, label, options):
    return {**field(key, "combobox", label, required=False),
        "options":[{"label":value, "value":value} for value in options]}


class OptionalBrowser(FakeBrowser):
    def __init__(self, url):
        super().__init__(url)
        self.fields = [dropdown("gender", "Gender", GENDER),
            dropdown("ethnicity", "Are you Hispanic/Latino?", ETHNICITY),
            dropdown("veteran", "Veteran Status", VETERAN),
            field("website", label="Website", required=False)]

    def fill(self, current, value):
        super().fill(current, value)
        if current["key"] == "ethnicity" and value == "No" and not any(f["key"] == "race" for f in self.fields):
            self.fields.append(dropdown("race", "Race", RACE))


def requested(labels):
    return lambda snapshot: {"field_keys":[f["key"] for f in snapshot["fields"] if f["label"] in labels]}


def model():
    return ScriptModel([("autofill", {}),
        ("request_input", requested(["Gender", "Are you Hispanic/Latino?", "Veteran Status"])),
        ("autofill", {}), ("request_input", requested(["Race"])),
        ("autofill", {}), ("ready_for_review", {})])


def test_optional_choices_appear_and_conditional_race_is_asked_next(database):
    browser = OptionalBrowser("")
    scripted = model()
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=lambda _: browser,
        agent_factory=lambda: HybridApplicationAgent(model_factory=lambda: scripted))
    try:
        run_id = loop.start(request(automation_mode="hybrid"))["run_id"]
        state = wait(loop, run_id)
        assert state["status"] == "needs_input"
        assert state["pending_fields"] == ["gender", "ethnicity", "veteran"]
        assert state["messages"][-1]["question_keys"] == state["pending_fields"]
        assert "Veteran Status" in state["messages"][-1]["text"]
        assert state["answers"] == {} and all(f["value"] == "" for f in browser.fields)
        answers = {"gender":GENDER[-1], "ethnicity":"No", "veteran":VETERAN[-1]}
        loop.continue_run(run_id, ContinueApplication(expected_version=state["version"], answers=answers))
        state = wait(loop, run_id)
        assert state["pending_fields"] == ["race"]
        assert state["messages"][-1]["question_keys"] == ["race"]
        assert state["answers"] == answers
        assert browser.fields[-1]["options"][-1]["label"] == "Decline To Self Identify"
        loop.continue_run(run_id, ContinueApplication(expected_version=state["version"], answers={"race":RACE[-1]}))
        state = wait(loop, run_id)
        assert state["status"] == "awaiting_review" and not state["pending_fields"]
        assert state["answers"] == {**answers, "race":RACE[-1]}
        assert next(f for f in browser.fields if f["key"] == "website")["value"] == ""
        assert browser.submissions == 0
    finally:
        loop.close()


def test_requested_optional_field_can_enrich_options_before_prompt(database):
    class EnrichingBrowser(OptionalBrowser):
        def __init__(self, url):
            super().__init__(url)
            for f in self.fields:
                f["options"] = []

        def enrich_options(self, fields, **kwargs):
            self.fields[2]["options"] = dropdown("veteran", "Veteran Status", VETERAN)["options"]
            return self.observe()

    class Agent:
        def run(self, *args, **kwargs):
            return {"reason":"needs_input", "field_keys":["veteran"]}

    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=EnrichingBrowser, agent_factory=Agent)
    try:
        state = wait(loop, loop.start(request(automation_mode="hybrid"))["run_id"])
        assert state["messages"][-1]["question_keys"] == ["veteran"]
        assert next(f for f in state["fields"] if f["key"] == "veteran")["options"][-1]["label"] == VETERAN[-1]
    finally:
        loop.close()


def test_requested_optional_known_answer_is_not_asked_again_as_missing_fact(database):
    class Agent:
        def run(self, browser, state, **kwargs):
            return {"reason":"needs_input", "field_keys":["veteran"]}

    repo = ApplyRepository(database.session_factory)
    state, _ = repo.create(request(automation_mode="hybrid"))
    repo.save(state, status="needs_input", answers={"veteran":VETERAN[-1]})
    loop = ApplicationLoop(repo, browser_factory=OptionalBrowser, agent_factory=Agent)
    try:
        state = repo.get(state["run_id"])
        loop.continue_run(state["run_id"], ContinueApplication(expected_version=state["version"]))
        state = wait(loop, state["run_id"])
        assert state["messages"][-1]["question_keys"] == []
        assert "could not finish filling these controls: Veteran Status" in state["messages"][-1]["text"]
    finally:
        loop.close()


@pytest.mark.skipif(os.getenv("JOB_AGENT_BROWSER_TESTS") != "1", reason="Opt-in offline Chrome integration test")
def test_optional_demographic_controls_render_and_fill_real_dropdowns(database):
    from playwright.sync_api import sync_playwright, expect
    from test_auto_apply_drafting import project_pdf

    def select_html(id, label, options):
        return f'<label for="{id}">{label}</label><select id="{id}"><option value="">Select...</option>' + ''.join(
            f'<option value="{i}">{value}</option>' for i, value in enumerate(options)) + '</select>'
    fixture = '<form>' + select_html('gender', 'Gender', GENDER) + select_html('ethnicity', 'Are you Hispanic/Latino?', ETHNICITY)
    fixture += select_html('veteran', 'Veteran Status', VETERAN)
    fixture += '<div id="race-section" hidden>' + select_html('race', 'Race', RACE) + '</div><button type="submit">Submit Application</button></form>'
    fixture += '<script>document.querySelector("#ethnicity").onchange=e=>{document.querySelector("#race-section").hidden=e.target.value!=="1";};</script>'

    class LocalBrowser(ApplicationBrowser):
        def __init__(self, url):
            super().__init__(url, headless=True)

        def _guard_request(self, route):
            if route.request.is_navigation_request() and route.request.frame == self.page.main_frame:
                route.fulfill(content_type="text/html", body=fixture)
            else:
                route.abort()

    repo = ApplyRepository(database.session_factory)
    repo.save_candidate(request().profile.model_dump(), resume=project_pdf(), extracted=True)
    scripted = model()
    loop = ApplicationLoop(repo, browser_factory=LocalBrowser,
        agent_factory=lambda: HybridApplicationAgent(model_factory=lambda: scripted))
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    try:
        run_id = loop.start(request(automation_mode="hybrid"))["run_id"]
        assert wait(loop, run_id)["status"] == "needs_input"
        with TestClient(app, base_url="http://localhost") as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page(locale="en-US",viewport={"width":1280, "height":1000})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                def handle(route):
                    req = route.request
                    if not req.url.startswith('http://localhost/'):
                        route.abort()
                        return
                    response = client.request(req.method, req.url, content=req.post_data,
                        headers={k:v for k,v in req.headers.items() if k in {"content-type", "origin"}})
                    route.fulfill(status=response.status_code, body=response.content,
                        headers={"content-type":response.headers.get("content-type", "application/json")})
                page.route('**/*', handle)
                page.goto('http://localhost/apply?mode=application')
                page.locator('#attempts .attempt').click()
                expect(page.locator('.question-choice')).to_have_count(3)
                page.get_by_role('group', name='Gender', exact=True).get_by_role('button', name=GENDER[-1], exact=True).click()
                page.get_by_role('group', name='Are you Hispanic/Latino?', exact=True).get_by_role('button', name='No', exact=True).click()
                page.get_by_role('group', name='Veteran Status', exact=True).get_by_role('button', name=VETERAN[-1], exact=True).click()
                page.get_by_role('button', name='Submit answers & continue', exact=True).click()
                expect(page.get_by_label('Race', exact=True)).to_be_visible(timeout=10000)
                expect(page.locator('.question-choice')).to_have_count(1)
                page.get_by_label('Race', exact=True).select_option(label=RACE[-1])
                page.get_by_role('button', name='Submit answers & continue', exact=True).click()
                expect(page.locator('#agent-status')).to_have_text('Ready for your review', timeout=10000)
                state = repo.get(run_id)
                values = {f['label']:f['value'] for f in state['fields']}
                assert values == {'Gender':'2', 'Are you Hispanic/Latino?':'1', 'Veteran Status':'2', 'Race':'7'}
                assert not state.get('submission_started') and not errors
            finally:
                browser.close()
    finally:
        loop.close()
