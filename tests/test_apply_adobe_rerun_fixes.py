"""Adobe re-test 2026-10-16 (apply-test-06): date segments, cited month padding, page-local autofill scope."""
import pytest
from test_auto_apply import FakeBrowser, field
from eregion.runtime.auto_apply.browser import BrowserBlocked
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.education import date_format, format_date
from eregion.runtime.auto_apply.experience import normalize_records

# The real Adobe month segment: the id names the segment, the help names the whole date.
MONTH = {'key': 'text:workExperience--startDate-dateSectionMonth-input:1', 'dom_id': 'workExperience-4--startDate-dateSectionMonth-input',
         'label': 'From*', 'help': 'current value is MM/YYYY', 'kind': 'text'}


def test_segment_id_decides_the_format_over_the_whole_date_help():
    assert date_format(MONTH) == 'MM'
    assert format_date(MONTH, '8', 'month') == '08'
    assert format_date({**MONTH, 'key': MONTH['key'].replace('Month', 'Year'), 'dom_id': ''}, '2026', 'year') == '2026'
    # A real whole-date control still uses its hint.
    assert date_format({'key': 'text:start:1', 'label': 'Start date (MM/YYYY)', 'kind': 'text'}) == 'MM/YYYY'


def test_model_may_zero_pad_a_cited_month_component():
    job = normalize_records([{'title': 'Research', 'company': 'Cornell University', 'start_date': 'Aug 2026', 'current': 'Present'}])[0]
    month = {**field('m', required=True), **{k: v for k, v in MONTH.items() if k != 'key'}, 'value': '2'}
    browser = FakeBrowser(''); browser.fields = [month]
    state = {'profile': {'experience': [job]}, 'answers': {}}
    session = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    obs = session.observe()
    result = session.execute('resolve_field', {'observation_id': obs['observation_id'], 'field_key': 'm', 'mode': 'format', 'value': '08',
        'source_refs': [f"experience:{job['id']}:start_month"], 'reason': 'Resume start month 8 written as MM.'})
    assert 'error' not in result and browser.fields[0]['value'] == '08'
    # Padding is not licence to change the month.
    obs = session.observe()
    with pytest.raises(BrowserBlocked):
        session.execute('resolve_field', {'observation_id': obs['observation_id'], 'field_key': 'm', 'mode': 'format', 'value': '09',
            'source_refs': [f"experience:{job['id']}:start_month"], 'reason': 'wrong'})
    assert browser.fields[0]['value'] == '08'


class PagedBrowser(FakeBrowser):
    page = {'url': 'https://example.test/apply/info', 'heading': 'My Information'}

    def page_state(self, fields=None):
        return dict(self.page)


def test_scope_chosen_on_one_page_does_not_suppress_the_next_pages_entry_autofill():
    browser = PagedBrowser(''); browser.fields = [field('country')]
    state = {'profile': {}, 'answers': {'country': 'United States', 'next_page_known': 'known answer'}}
    session = AgentTools(browser, state, autofill=lambda: autofill(browser, state, lambda: b''), active=lambda: True)
    session.observe()
    session.execute('autofill', {'field_keys': ['country']})
    assert state['autofill_scope'] == ['country']
    browser.page = {'url': 'https://example.test/apply/experience', 'heading': 'My Experience'}
    browser.fields = [field('next_page_known')]
    session.observe()
    session._host_literal_autofill = True
    try:
        result = session.execute('autofill', {})
    finally:
        session._host_literal_autofill = False
    assert result['filled'] == ['next_page_known'] and browser.fields[0]['value'] == 'known answer'
    assert state['autofill_scope'] is None


def test_scope_still_holds_on_its_own_page():
    browser = PagedBrowser(''); browser.fields = [field('work'), field('education')]
    state = {'profile': {}, 'answers': {'work': 'confirmed work', 'education': 'other fact'}}
    session = AgentTools(browser, state, autofill=lambda: autofill(browser, state, lambda: b''), active=lambda: True)
    session.observe()
    session.execute('autofill', {'field_keys': ['work']})
    session.observe()
    session.execute('autofill', {})
    assert browser.fields[1]['value'] == ''
