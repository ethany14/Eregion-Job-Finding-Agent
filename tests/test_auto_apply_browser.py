"""Opt-in real Chrome tests. All employer requests are intercepted locally."""
import os
from pathlib import Path

import pytest

from eregion.runtime.auto_apply.browser import ApplicationBrowser, BrowserBlocked
from eregion.runtime.auto_apply.contracts import ContinueApplication, ChatRequest
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.repository import ApplyRepository
from eregion.api.db import create_database
from test_auto_apply import request, wait

pytestmark = pytest.mark.skipif(os.getenv("JOB_AGENT_BROWSER_TESTS") != "1",
    reason="Set JOB_AGENT_BROWSER_TESTS=1 to run offline Chrome integration tests.")
FIXTURES = Path(__file__).parent / "fixtures" / "auto_apply"


def test_generic_page_control_opens_details_but_cannot_send_form_or_toggle_consent():
    browser = FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        browser.page.set_content('''<form onsubmit="event.preventDefault();window.sent=true">
            <button type="button" onclick="document.querySelector('#details').hidden=false">Edit education</button>
            <section id="details" hidden><label>Major<input name="major"></label></section>
            <button type="submit">Finish and send</button>
            <button type="button" onclick="window.accepted=true">Accept privacy terms</button>
        </form>''')
        controls = browser.page_state()['controls']
        browser.click_page_action(next(c for c in controls if c['label']=='Edit education'))
        assert any(f['label']=='Major' for f in browser.observe())
        for label in ('Finish and send','Accept privacy terms'):
            target = next(c for c in browser.page_state()['controls'] if c['label']==label)
            with pytest.raises(BrowserBlocked):browser.click_page_action(target)
        assert browser.page.evaluate('!window.sent && !window.accepted')
    finally:
        browser.close()


def test_answering_during_background_work_keeps_focus_and_sends_one_batch(tmp_path):
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright, expect
    from eregion.api.main import create_app
    from test_auto_apply import FakeBrowser, field

    database = create_database(f"sqlite:///{(tmp_path/'async-ui.sqlite').as_posix()}",create_schema_for_tests=True)
    repo = ApplyRepository(database.session_factory)
    repo.clear_candidate()
    loop = ApplicationLoop(repo,browser_factory=FakeBrowser)
    saved, _ = repo.create(request())
    fields = [field('city',label='Location'),field('consent','checkbox',label='Consent')]
    question = {'id':'question-async-ui','text':'Which location and consent should I use?',
        'field_keys':['city','consent'],'fields':fields}
    saved = repo.save(saved,status='running',fields=fields,pending_fields=question['field_keys'],
        input_request=question,messages=[{'id':question['id'],'role':'assistant','text':question['text']}])
    app = create_app(run_service=object(),session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    try:
        with TestClient(app,base_url='http://localhost') as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome',headless=True)
            page = browser.new_page()
            replies, errors = [], []
            page.on('pageerror',lambda error:errors.append(str(error)))
            def handle(route):
                req = route.request
                if req.url.endswith('/reply'):replies.append(req.post_data_json)
                response = client.request(req.method,req.url,content=req.post_data,
                    headers={k:v for k,v in req.headers.items() if k in {'content-type','origin'}})
                route.fulfill(status=response.status_code,body=response.content,
                    headers={'content-type':response.headers.get('content-type','application/json')})
            page.route('http://localhost/**',handle)
            page.route('https://fonts.googleapis.com/**',lambda route:route.abort())
            page.goto('http://localhost/apply?mode=application')
            page.locator('#attempts button').click()
            location = page.locator('#conversation-actions input')
            location.fill('Ithaca')
            location.evaluate("e=>e.dataset.marker='same-input'")
            current = repo.get(saved['run_id'])
            repo.save(current,fields=[field('independent-step')],message='Filling another control.')
            page.wait_for_timeout(2400)  # One normal UI poll while the user is typing.
            expect(location).to_be_focused()
            expect(location).to_have_attribute('data-marker','same-input')
            expect(location).to_have_value('Ithaca')
            page.get_by_role('button',name='No',exact=True).click()
            page.get_by_role('button',name='Submit answers & continue',exact=True).click()
            expect(page.locator('#conversation-actions form')).to_have_count(0)
            assert replies == [{'request_id':question['id'],'answers':{'city':'Ithaca','consent':False}}]
            assert repo.get(saved['run_id'])['answers'] == {'city':'Ithaca','consent':False}
            assert not errors
            browser.close()
    finally:
        loop.close()
        database.engine.dispose()


def test_duplicate_city_suggestions_select_once_and_repeated_fill_does_not_reopen():
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('''<form id="application"><label for="city">Location (City)*</label>
            <div class="select__container"><span class="select__single-value"></span>
            <input id="city" role="combobox" aria-required="true" aria-controls="choices"></div></form>
            <div id="choices" role="listbox" hidden></div>
            <script>
              window.openCount=0; window.selectCount=0;
              const input=document.querySelector('input'), menu=document.querySelector('#choices');
              input.onclick=()=>{window.openCount++; menu.hidden=false; input.setAttribute('aria-expanded','true');
                menu.innerHTML='';
                for (let i=0; i<2; i++) {
                  const option=document.createElement('div'); option.role='option';
                  option.textContent='Ithaca, New York, United States';
                  option.onclick=()=>{window.selectCount++; document.querySelector('span').textContent=option.textContent;
                    input.value=''; menu.hidden=true; input.setAttribute('aria-expanded','false');};
                  menu.append(option);
                }
              };
            </script>''')
        city = browser.observe()[0]
        for _ in range(3):
            browser.fill(city, "Ithaca, New York, United States")
            browser.choose_option(city["key"], "Ithaca, New York, United States")
        assert browser.field(city["key"])["value"] == "Ithaca, New York, United States"
        assert browser.page.evaluate("[window.openCount, window.selectCount]") == [1, 1]
    finally:
        browser.close()


def test_compact_chat_reply_fills_real_controls_without_model(tmp_path):
    from test_auto_apply_conversation import UnavailableAgent

    class ChatBrowser(FixtureBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page.set_content('''<form id="application">
                <label for="city">Location (City)*</label><select id="city" required>
                    <option value="">Choose</option><option value="ithaca-ny">Ithaca, New York, United States</option></select>
                <label for="english">Are you fluent in English?*</label><select id="english" required>
                    <option value="">Choose</option><option value="1">Yes</option><option value="2">No</option></select>
                <label for="referral">How did you hear about this job?*</label><input id="referral" required>
                <label for="sponsor">Do you now or will you in the future require immigration sponsorship to work at Cloudflare?*</label>
                <select id="sponsor" required><option value="">Choose</option><option value="3">Yes</option><option value="4">No</option></select>
                <label><input id="consent" type="checkbox" required description="Please acknowledge the Candidate Privacy Policy">Acknowledge/Confirm</label>
            </form>''')

        def submit(self):
            pytest.fail("Chat should never submit this incomplete form")

    database = create_database(f"sqlite:///{(tmp_path / 'chat-browser.sqlite').as_posix()}", create_schema_for_tests=True)
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=ChatBrowser, agent_factory=UnavailableAgent)
    try:
        run_id = loop.start(request(automation_mode="hybrid", url="https://job-boards.greenhouse.io/example/jobs/123"))["run_id"]
        initial = wait(loop, run_id)
        assert len(initial["messages"][-1]["question_keys"]) == 5
        loop.chat(run_id, ChatRequest(text="ithaca，yes, linkedin, yes", message_id="batch-browser-1"))
        state = wait(loop, run_id)
        assert [f["value"] for f in state["fields"]] == ["ithaca-ny", "1", "linkedin", "3", False]
        assert state["pending_fields"] == [state["fields"][-1]["key"]]
        assert state["messages"][-1]["question_keys"] == state["pending_fields"]
    finally:
        loop.close()
        database.close()


class FixtureBrowser(ApplicationBrowser):
    def __init__(self, url):
        super().__init__(url, headless=True)

    def _guard_request(self, route):
        if route.request.is_navigation_request() and route.request.frame == self.page.main_frame:
            route.fulfill(content_type="text/html", body=(FIXTURES / f"{self.platform}.html").read_text(encoding="utf-8"))
        else:
            route.abort()


@pytest.mark.parametrize("city_kind", ["select", "combobox"])
@pytest.mark.parametrize("scenario", ["numbered", "comma_region", "shrunk_numbered", "shrunk_comma"])
def test_numbered_city_clarification_fills_real_controls_without_model(tmp_path, city_kind, scenario):
    from test_auto_apply_conversation import UnavailableAgent

    class ClarificationBrowser(FixtureBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page.set_content('''<form id="application">
                <label for="city">Location (City)*</label><select id="city" required>
                    <option value="">Choose</option>
                    <option value="ithaca-ny">Ithaca, New York, United States</option>
                    <option value="ithaca-mi">Ithaca, Michigan, United States</option>
                    <option value="ithaca-ne">Ithaca, Nebraska, United States</option></select>
                <label for="referral">How did you hear about this job?*</label><input id="referral" required>
                <label><input id="consent" type="checkbox" required>Acknowledge/Confirm</label>
            </form>''')
            if city_kind == "combobox":
                self.page.evaluate('''() => {
                    const select = document.querySelector('#city');
                    const labels = [...select.options].slice(1).map(o => o.label);
                    const container = document.createElement('div');
                    container.className = 'select__container';
                    container.innerHTML = '<span class="select__single-value"></span>' +
                        '<input id="city" role="combobox" aria-required="true" aria-controls="city-options">';
                    select.replaceWith(container);
                    const input = container.querySelector('input');
                    const menu = document.createElement('div');
                    menu.id = 'city-options'; menu.role = 'listbox'; menu.hidden = true;
                    document.body.append(menu);
                    const render = () => {
                        menu.replaceChildren(); menu.hidden = false;
                        input.setAttribute('aria-expanded', 'true');
                        for (const label of labels.filter(s => s.toLowerCase().includes(input.value.toLowerCase()))) {
                            const option = document.createElement('div');
                            option.role = 'option'; option.textContent = label;
                            option.onclick = () => {
                                container.querySelector('span').textContent = label;
                                input.value = ''; menu.hidden = true;
                                input.setAttribute('aria-expanded', 'false');
                            };
                            menu.append(option);
                        }
                    };
                    input.onclick = render; input.oninput = render;
                    input.onkeydown = e => { if (e.key === 'Escape') {
                        menu.hidden = true; input.setAttribute('aria-expanded', 'false');
                    }};
                }''')

        def submit(self):
            pytest.fail("A city/referral answer must never submit an application")

    database = create_database(f"sqlite:///{(tmp_path / 'numbered-chat.sqlite').as_posix()}", create_schema_for_tests=True)
    repo = ApplyRepository(database.session_factory)
    loop = ApplicationLoop(repo, browser_factory=ClarificationBrowser, agent_factory=UnavailableAgent, retry_delays=())
    try:
        run_id = loop.start(request(automation_mode="hybrid", url="https://job-boards.greenhouse.io/example/jobs/123"))["run_id"]
        initial = wait(loop, run_id)
        city, referral, consent = initial["fields"]
        if scenario.startswith("shrunk"):
            # Reproduce the saved state after the old parser put NY in referral.
            loop.chat(run_id, ChatRequest(text="NY、", field_key=referral["key"], message_id="old-bad-source"))
            wait(loop, run_id)
            loop.chat(run_id, ChatRequest(text="yes", field_key=consent["key"], message_id="explicit-consent"))
            wait(loop, run_id)
        loop.chat(run_id, ChatRequest(text="ithaca", field_key=city["key"], message_id="ambiguous-city-1"))
        ambiguous = wait(loop, run_id)
        assert "matches multiple options" in ambiguous["messages"][-1]["text"]
        expected_keys = [city["key"]] if scenario.startswith("shrunk") else [city["key"], referral["key"], consent["key"]]
        assert ambiguous["messages"][-1]["question_keys"] == expected_keys
        reply = {"numbered": "ithaca newyork 2.linkedin", "comma_region": "ithaca， NY、",
            "shrunk_numbered": "ithaca ny 2. linkedin", "shrunk_comma": "ithaca newyork  ， linkedin"}[scenario]
        loop.chat(run_id, ChatRequest(text=reply, message_id="numbered-correction-1"))
        state = wait(loop, run_id)
        selected_city = "ithaca-ny" if city_kind == "select" else "Ithaca, New York, United States"
        assert [f["value"] for f in state["fields"]] == [selected_city,
            "" if scenario == "comma_region" else "linkedin", scenario.startswith("shrunk")]
        remaining = [] if scenario.startswith("shrunk") else ([referral["key"], consent["key"]] if scenario == "comma_region" else [consent["key"]])
        assert state["pending_fields"] == remaining
        assert state["messages"][-1]["question_keys"] == remaining
        assert "Location" not in state["messages"][-1]["text"]
        assert "AI tool call failed" in state["messages"][-1]["text"]
    finally:
        loop.close()
        database.close()


@pytest.mark.parametrize("url", ["https://job-boards.greenhouse.io/example/jobs/123", "https://jobs.lever.co/example/abc123"])
def test_real_browser_form_resume_question_and_confirmation(tmp_path, url):
    database = create_database(f"sqlite:///{(tmp_path / 'browser.sqlite').as_posix()}", create_schema_for_tests=True)
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=FixtureBrowser)
    try:
        run_id = loop.start(request(url=url))["run_id"]
        paused = wait(loop, run_id)
        assert paused["status"] == "needs_input", paused["message"]
        fields = paused["fields"]
        assert any(field["kind"] == "file" and field["value"] == "resume.pdf" for field in fields)
        assert not any(field["kind"] == "hidden" for field in fields)
        answers = {field["key"]: (True if field["kind"] == "checkbox" else "yes") for field in fields
                   if field["kind"] in {"checkbox", "radio", "select"}}
        loop.continue_run(run_id, ContinueApplication(expected_version=paused["version"], answers=answers))
        review = wait(loop, run_id)
        assert review["status"] == "awaiting_review", review["message"]
        loop.continue_run(run_id, ContinueApplication(expected_version=review["version"], confirm_submit=True))
        done = wait(loop, run_id)
        assert done["status"] == "submitted", done["message"]
        assert "application" in done["confirmation"]["text"].lower()
    finally:
        loop.close(); database.close()


def test_browser_rejects_different_posting_and_ignores_job_description_thanks():
    browser = FixtureBrowser("https://jobs.lever.co/example/abc123")
    try:
        browser.page.evaluate("document.querySelector('h1').textContent='Thank you for applying'")
        assert browser.confirmation() is None
        browser.page.goto("https://jobs.lever.co/other/different/apply")
        with pytest.raises(BrowserBlocked, match="different job"):
            browser.observe()
    finally: browser.close()


def test_greenhouse_react_select_hidden_inputs_and_attach_label():
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('''<form id="application">
          <div class="select__container"><label for="country">Country*</label>
            <div class="select-shell"><div class="select__single-value">Canada</div>
              <input id="country" role="combobox" aria-required="true" value="">
              <input aria-hidden="true" tabindex="-1" required value="">
            </div>
          </div>
          <label for="resume">Attach</label><input id="resume" type="file">
        </form>''')
        fields = browser.observe()
        assert len(fields) == 2
        assert fields[0]["value"] == "Canada" and fields[0]["required"]
        assert fields[1]["label"] == "Resume/CV" and fields[1]["required"]
    finally: browser.close()


def test_lever_required_checkbox_group_does_not_require_every_option():
    browser = FixtureBrowser("https://jobs.lever.co/example/abc123")
    try:
        browser.page.set_content('''<form><div class="application-question">
          <div class="application-label">Languages ✱</div>
          <ul data-qa="checkboxes">
          <li><label><input name="languages" type="checkbox" value="en" required>English</label></li>
          <li><label><input name="languages" type="checkbox" value="es" required>Spanish</label></li></ul>
        </div></form>''')
        fields = browser.observe()
        assert not any(field["required"] for field in fields)
        assert fields[0]["invalid"]
        browser.fill(fields[0], True)
        fields = browser.observe()
        assert fields[0]["value"] is True and fields[1]["value"] is False
        assert not any(field["invalid"] for field in fields)
    finally: browser.close()


def test_lever_location_requires_a_selected_suggestion():
    browser = FixtureBrowser("https://jobs.lever.co/example/abc123")
    try:
        browser.page.set_content('''<form><div class="application-question"><div class="application-label"><div class="text">Current location ✱</div></div>
          <div><input name="location" value="Boston" required><input type="hidden" name="selectedLocation"></div>
        </div></form>''')
        location = browser.observe()[0]
        assert location["kind"] == "location" and not location["value"]
        browser.page.locator('input[name="selectedLocation"]').evaluate("el => el.value = 'fixture-location-id'")
        assert browser.observe()[0]["value"] == "Boston"
    finally: browser.close()


def test_web_application_flow_with_actual_api_and_offline_employer(tmp_path, monkeypatch):
    import base64
    import json
    from langchain_core.messages import AIMessage, HumanMessage
    from fastapi.testclient import TestClient
    from playwright.sync_api import sync_playwright, expect
    from types import SimpleNamespace
    from eregion.api.main import create_app
    from eregion.runtime.auto_apply.resume_profile import ResumeFacts
    from test_auto_apply_personalization import resume_pdf
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from test_auto_apply_agent import ScriptModel

    class ResumeModel:
        def with_structured_output(self, schema): return self
        def invoke(self, messages):
            return ResumeFacts(facts=[
                {"field":"first_name", "value":"Alex", "quote":"Alex Demo"},
                {"field":"last_name", "value":"Demo", "quote":"Alex Demo"},
                {"field":"school", "value":"Massachusetts Institute of Technology", "quote":"Massachusetts Institute of Technology"}])
    monkeypatch.setattr("eregion.domain.model.create_model", lambda **kwargs: ResumeModel())

    class ChatModel(ScriptModel):
        def __init__(self):
            super().__init__([("autofill", {}), ("ready_for_review", {})])

        def invoke(self, messages, tools):
            snapshot = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage)))
            reply = next((m for m in snapshot["user_messages"]
                          if m["text"] == "No" and not m.get("bound_answers")), None)
            if reply:
                target = next(f for f in snapshot["fields"] if "sponsor" in f["label"].lower())
                return AIMessage(content="", tool_calls=[{"id":"map-reply", "name":"record_answers", "args":{
                    "observation_id":snapshot["observation_id"], "answers":[{
                        "field_key":target["key"], "message_id":reply["id"], "quote":"No", "value":"No"}]}}])
            return super().invoke(messages, tools)

    database = create_database(f"sqlite:///{(tmp_path / 'web.sqlite').as_posix()}", create_schema_for_tests=True)
    loop = ApplicationLoop(ApplyRepository(database.session_factory), browser_factory=FixtureBrowser,
        agent_factory=lambda: HybridApplicationAgent(model_factory=ChatModel))
    app = create_app(run_service=object(), session_runtime=SimpleNamespace(database=database))
    app.state.auto_apply = loop
    try:
        with TestClient(app, base_url="http://localhost") as client, sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width":1440,"height":1100})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def handle(route):
                req = route.request
                response = client.request(req.method, req.url, content=req.post_data,
                    headers={key:value for key,value in req.headers.items() if key in {"content-type", "origin"}})
                route.fulfill(status=response.status_code, body=response.content,
                    headers={"content-type":response.headers.get("content-type", "application/json")})
            page.route("http://localhost/**", handle)
            page.route("https://fonts.googleapis.com/**", lambda route: route.abort())
            page.goto("http://localhost/apply?mode=application")
            expect(page.locator('#welcome')).to_be_visible()
            expect(page.locator('#browser-panel')).to_be_hidden()
            page.locator('input[name="resume"]').set_input_files({"name":"resume.pdf", "mimeType":"application/pdf", "buffer":base64.b64decode(resume_pdf())})
            expect(page.locator('#agent-messages')).to_contain_text('Alex Demo', timeout=15000)
            page.locator('#chat-text').fill('Please apply to https://job-boards.greenhouse.io/example/jobs/123')
            page.get_by_role('button', name='Send message', exact=True).click()
            status = page.locator('#agent-status')
            expect(status).to_have_text('Waiting for your input', timeout=20000)
            expect(page.locator("#agent-messages")).to_contain_text("sponsorship")
            page.locator('#pause-agent').click()
            expect(status).to_contain_text('Paused', timeout=10000)
            page.locator('#pause-agent').click()
            expect(status).to_have_text('Waiting for your input', timeout=20000)
            page.locator("#chat-text").fill("No")
            page.get_by_role("button", name="Send message", exact=True).click()
            expect(status).to_have_text("Ready for your review", timeout=20000)
            expect(page.locator("#agent-messages .user").last).to_contain_text("No")
            page.get_by_role('button', name='Review answers', exact=True).click()
            expect(page.locator('#answer-list')).to_contain_text('Alex')
            expect(page.locator("#browser-preview")).to_be_visible(timeout=10000)
            expect(page.locator("#browser-actions")).to_contain_text("Fill known details")
            # The agent interpreted the chat reply, so it is not remembered until approved.
            page.locator("#answer-memory summary").click()
            expect(page.locator("#memory-list")).to_contain_text("No answers remembered yet")
            approval = page.locator("#conversation-actions .approval-row")
            expect(approval).to_contain_text("sponsorship")
            expect(approval.get_by_role("checkbox", name="Remember")).to_be_checked()
            screenshot = os.getenv("JOB_AGENT_APPLY_SCREENSHOT")
            if screenshot:
                page.screenshot(path=screenshot, full_page=True)
            page.locator('#browser-close').click()
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#chat-scroll').evaluate('el => el.scrollTop = el.scrollHeight')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            if screenshot: page.screenshot(path=str(Path(screenshot).with_stem(Path(screenshot).stem + '-mobile')), full_page=True)
            page.get_by_role("button", name="Submit application", exact=True).click()
            expect(status).to_have_text("Application received", timeout=20000)
            assert len(loop.repository.list()) == 1
            assert loop.repository.list()[0]["status"] == "submitted"
            # Submitting with Remember checked approves the answer for later applications.
            [memory] = loop.repository.memories(loop.repository.list()[0]["profile"])
            assert memory["value"] == "No" and memory["status"] == "approved"
            assert not errors
            browser.close()
    finally:
        loop.close(); database.close()


def test_readonly_portal_dropdown_and_replaced_dom_nodes():
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content((FIXTURES / "hybrid.html").read_text(encoding="utf-8"))
        fields = browser.observe()
        first = next(f for f in fields if f["dom_id"] == "first_name")
        # Framework replacement removes the temporary marker after observation.
        browser.page.evaluate("""() => {
          const input=document.querySelector('#first_name');
          const replacement=input.cloneNode(true); replacement.removeAttribute('data-northstar-field');
          input.replaceWith(replacement);
        }""")
        browser.locate(first).fill("Alex")
        assert browser.field(first["key"])["value"] == "Alex"
        country = next(f for f in fields if f["dom_id"] == "country")
        options = browser.inspect_options(country["key"])
        assert [o["label"] for o in options] == ["Canada", "United States"]
        browser.choose_option(country["key"], "Canada")
        assert browser.field(country["key"])["value"] == "Canada"
        browser.fill(country, "United States")
        assert browser.field(country["key"])["value"] == "United States"
        browser.fill(country, "US")
        assert browser.field(country["key"])["value"] == "United States"
    finally:
        browser.close()


def test_actual_browser_agent_repairs_keyboard_only_field_after_autofill():
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    from eregion.runtime.auto_apply.autofill import autofill
    from test_auto_apply_agent import ScriptModel, args

    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content((FIXTURES / "hybrid.html").read_text(encoding="utf-8"))
        fields = browser.observe()
        name = next(f["key"] for f in fields if f["dom_id"] == "first_name")
        country = next(f["key"] for f in fields if f["dom_id"] == "country")
        saved = {"profile": request().profile.model_dump(), "answers": {country: "Canada"}}
        reports = []
        model = ScriptModel([("autofill", {}),
            ("type_from_source", args(name, source_ref="profile:first_name")), ("ready_for_review", {})])
        result = HybridApplicationAgent(model_factory=lambda: model).run(browser, saved,
            autofill=lambda: autofill(browser, saved, lambda: b"", progress=lambda *values: reports.append(values)),
            active=lambda: True, reserve=lambda kind: True, report=lambda *values: reports.append(values))
        assert result["reason"] == "ready"
        assert browser.field(name)["value"] == "Test"
        assert browser.field(country)["value"] == "Canada"
        assert any(values[-1] == "needs_agent" for values in reports)  # One progress event per autofill pass.
        assert browser.page.evaluate("window.submissions") == 0
    finally:
        browser.close()


def test_paused_browser_keeps_dispatching_background_requests():
    from threading import Event
    from eregion.runtime.auto_apply.worker import BrowserWorker

    received = Event()
    browsers = []
    worker = BrowserWorker(lambda: browsers[0].pump() if browsers else None)
    class BackgroundBrowser(FixtureBrowser):
        def _guard_request(self, route):
            if route.request.url.endswith("/fixture-options"):
                received.set()
                route.fulfill(content_type="application/json", body='["Canada"]')
            else:
                super()._guard_request(route)
    def setup():
        browser = BackgroundBrowser("https://job-boards.greenhouse.io/example/jobs/123")
        browsers.append(browser)
        browser.page.evaluate("setTimeout(() => fetch('/fixture-options'), 100)")
    try:
        worker.submit(setup).result(timeout=15)
        # No new worker command: the background route is handled during the pause.
        assert received.wait(3)
    finally:
        worker.submit(lambda: [b.close() for b in browsers]).result(timeout=10)
        worker.shutdown()


def test_searching_an_open_dropdown_does_not_toggle_it_closed():
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('''<form><div class="select__container">
          <label for="school">School</label><span class="select__single-value"></span>
          <input id="school" role="combobox" aria-expanded="false" aria-controls="school-options">
          </div></form><div id="school-options" role="listbox" hidden></div>
          <script>
            const input=document.querySelector('#school'), menu=document.querySelector('#school-options');
            function populate(label) {
              menu.innerHTML=''; const option=document.createElement('div'); option.role='option'; option.textContent=label;
              option.onclick=()=>{document.querySelector('.select__single-value').textContent=label; menu.hidden=true; input.setAttribute('aria-expanded','false');};
              menu.append(option);
            }
            populate('Aalborg University');
            input.onclick=()=>{const expanded=input.getAttribute('aria-expanded')==='true'; input.setAttribute('aria-expanded',String(!expanded));menu.hidden=expanded;};
            input.oninput=()=>{menu.innerHTML='';setTimeout(()=>populate('Massachusetts Institute of Technology'),80);};
          </script>''')
        school = browser.observe()[0]
        browser.fill(school, "MIT")
        assert browser.field(school["key"])["value"] == "Massachusetts Institute of Technology"
    finally:
        browser.close()


def test_checkbox_observation_includes_the_actual_privacy_question():
    browser = FixtureBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        browser.page.set_content('''<form><label for="consent">Acknowledge/Confirm</label>
          <input type="checkbox" id="consent" description="Please review and acknowledge the candidate privacy policy." required></form>''')
        question = browser.observe()[0]
        assert "candidate privacy policy" in question["label"]
        assert "Acknowledge/Confirm" in question["label"]
    finally:
        browser.close()


def test_slow_tracking_resource_does_not_prevent_using_loaded_form():
    class SlowResourceBrowser(FixtureBrowser):
        def _guard_request(self, route):
            if route.request.is_navigation_request():
                route.fulfill(content_type="text/html", body='<form><label for="first_name">First Name</label><input id="first_name"></form><img src="/never-finished.png">')
            else:
                self.pending_resource = route  # Deliberately keep the image pending.
    browser = SlowResourceBrowser("https://job-boards.greenhouse.io/example/jobs/123")
    try:
        assert browser.observe()[0]["label"] == "First Name"
        assert browser.page.evaluate("document.readyState") != "complete"
    finally:
        browser.close()


def test_lever_select2_school_picker_is_the_hidden_required_select():
    # Veeva pilot (2026-10-15): the scanner read select2's rendered box, named by its
    # displayed text and optional, and the agent retried it ~23 times.
    browser = FixtureBrowser("https://jobs.lever.co/example/abc123")
    try:
        browser.page.set_content('''<form><li class="application-question custom-question">
          <div class="application-label"><div class="text">Please select your college or University:<span class="required">✱</span></div></div>
          <div class="application-field"><select name="cards[a][field1]" class="select2-hidden-accessible" aria-hidden="true" tabindex="-1" required
              style="border:0;clip:rect(0 0 0 0);height:1px;width:1px;overflow:hidden;position:absolute;padding:0;margin:-1px"
              onchange="document.getElementById('shown').textContent=this.selectedOptions[0].textContent">
            <option value="">If you are unable to find your college or university, please select "Other"</option>
            <option value="Cornell College">Cornell College</option><option value="Cornell University">Cornell University</option></select>
          <span class="select2 select2-container"><span class="select2-selection" role="combobox" aria-labelledby="shown" tabindex="0">
            <span id="shown" class="select2-selection__rendered">If you are unable to find your college or university, please select "Other"</span></span></span>
          </div></li></form>''')
        fields = browser.observe()
        assert [(f["kind"], f["label"], f["required"]) for f in fields] == [
            ("select", "Please select your college or University:✱", True)]
        browser.fill(fields[0], "Cornell University")
        assert browser.observe()[0]["value"] == "Cornell University"
        assert browser.page.locator('#shown').inner_text() == "Cornell University"
    finally: browser.close()

