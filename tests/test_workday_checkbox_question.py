"""Local Chrome tests; no employer or model requests."""
import os
import pytest
from test_auto_apply_browser import FixtureBrowser
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.browser import BrowserBlocked

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Opt-in local Chrome')

HTML = """<main><h1>Application questions</h1><form>
<div data-automation-id="formField-relationship" data-required="true">
<label data-automation-id="formLabel">Prior relationship*</label>
<div><label for="employee">Employee</label><input id="employee" type="checkbox" aria-required="true"></div>
<div><label for="none">None</label><input id="none" type="checkbox" aria-required="true"></div>
</div>
<div data-automation-id="formField-consent"><label for="consent">Required consent*</label><input id="consent" type="checkbox" required></div>
<button type="button" onclick="document.querySelector('h1').textContent='Next page'; this.remove()">Next</button>
</form></main>"""


def test_no_name_workday_options_require_one_choice_not_all_and_keep_consent():
    b=FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        b.page.set_content(HTML)
        fields=b.observe(); employee,none,consent=fields
        assert not employee['required'] and not none['required']
        assert employee['invalid'] and consent['required']
        assert 'Prior relationship' in employee['label']
        b.fill(none,True)
        fields=b.observe()
        assert not fields[0]['invalid'] and fields[0]['value'] is False
        assert fields[2]['required'] and fields[2]['invalid']
        b.fill(fields[2],True)
        fields=b.observe()
        state={'profile':{},'answers':{f['key']:f['value'] for f in fields}}
        s=AgentTools(b,state,autofill=lambda:{},active=lambda:True)
        s.observe()
        control=next(c for c in s.page_context['controls'] if c['kind']=='next')
        s.execute('click_page_action',{'observation_id':s.observation_id,'control_id':control['id']})
        assert b.page.locator('h1').inner_text()=='Next page'
    finally:b.close()


def test_empty_group_and_separate_unanswered_consents_still_block_next():
    b=FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        b.page.set_content(HTML)
        s=AgentTools(b,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
        s.observe(); control=next(c for c in s.page_context['controls'] if c['kind']=='next')
        with pytest.raises(BrowserBlocked):
            s.execute('click_page_action',{'observation_id':s.observation_id,'control_id':control['id']})
        assert b.page.locator('h1').inner_text()=='Application questions'
        b.page.set_content('<form><div data-automation-id="formField-a"><label for="a">Consent A*</label><input id="a" type="checkbox" required checked></div><div data-automation-id="formField-b"><label for="b">Consent B*</label><input id="b" type="checkbox" required></div></form>')
        fields=b.observe()
        assert all(f['required'] for f in fields) and fields[1]['invalid']
    finally:b.close()


def test_application_dialog_keeps_posting_context_but_not_background_actions():
    b=FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        b.page.set_content('<main><h1>Role in San Jose, United States</h1><button>Background action</button><div role="dialog"><h2>Start Your Application</h2><button>Apply Manually</button></div></main>')
        page=b.page_state()
        assert 'United States' in page['document_text']
        assert 'United States' not in page['text']
        assert [c['label'] for c in page['controls']]==['Apply Manually']
        s=AgentTools(b,{'profile':{},'answers':{}},autofill=lambda:{},active=lambda:True)
        s.observe()
        assert 'United States' in s.state['opening_page_evidence']['text']
    finally:b.close()


def test_unchanged_picker_list_is_not_presented_as_confirmed_search_results():
    b=FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        b.page.set_content('<form onsubmit="event.preventDefault()"><div data-automation-id="multiSelectContainer"><label for="major">Major</label><input id="major" enterkeyhint="search"><div role="listbox"><div role="option">Accounting</div></div></div></form>')
        field=b.observe()[0]
        b._settled_options=lambda field,before: b._option_locator(field)
        options=b.inspect_options(field['key'],query='Engineering')
        assert [x['label'] for x in options]==['Accounting']
        evidence=b.option_search_evidence[field['key']]
        assert evidence['input_retained'] and not evidence['list_changed']
        assert 'not confirmed' in evidence['meaning']
    finally:b.close()


@pytest.mark.parametrize("selected", [False, True])
def test_captured_nested_workday_questionnaire_group(selected):
    from pathlib import Path
    html=(Path(__file__).parent/'fixtures/replays/workday-questionnaire-checkbox-group.html').read_text(encoding='utf-8')
    b=FixtureBrowser('https://jobs.lever.co/example/abc123')
    try:
        b.page.set_content(html)
        # outerHTML serializes aria-checked but not the live checked property.
        # Recreate the observed selected state as an ordinary user click.
        if selected:
            b.page.locator('label').last.click()
        fields=b.observe()
        assert len(fields)==5
        assert all(not f['required'] for f in fields)
        assert all('Have you ever worked' in f['label'] for f in fields)
        assert sum(bool(f['invalid']) for f in fields)==(0 if selected else 1)
        state={'profile':{},'answers':{f['key']:f['value'] for f in fields}}
        tools=AgentTools(b,state,autofill=lambda:{},active=lambda:True)
        tools.observe()
        action=next(c for c in tools.page_context['controls'] if c['kind']=='next')
        args={'observation_id':tools.observation_id,'control_id':action['id']}
        if selected:
            tools.execute('click_page_action',args)
            assert b.page.locator('h1').inner_text()=='Next page'
            assert b.page.locator('input:checked').count()==1
        else:
            with pytest.raises(BrowserBlocked):tools.execute('click_page_action',args)
            assert b.page.locator('h1').inner_text()=='Questions'
    finally:b.close()
