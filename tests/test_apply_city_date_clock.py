"""E1.2 city format, split dates and the model's clock (Intel-34 follow-ups). Semantic choices stay
with the model: code only keeps sources, verifies conservation and supplies the clock."""
from datetime import datetime, timezone

import pytest

from test_auto_apply import FakeBrowser, field
from eregion.runtime.auto_apply.agent import AgentTools, current_clock
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.browser import BrowserBlocked


def tools_for(fields, **state):
    browser = FakeBrowser('')
    browser.fields = fields
    browser.type_field = lambda key, value: browser.fill(next(f for f in browser.fields if f['key'] == key), value)
    data = {'profile': {'city': 'Ithaca, NY'}, 'answers': {}, 'automation_mode': 'hybrid', **state}
    tools = AgentTools(browser, data, autofill=lambda: {}, active=lambda: True)
    tools.observe()
    return browser, tools


def test_city_literal_is_left_to_the_model_and_a_verbatim_part_is_accepted():
    city = field('text:city:1', label='City', required=False)
    state_box = field('combobox:countryRegion:1', kind='combobox', label='State')
    state_box['options'] = [{'label': 'New York', 'value': 'NY'}]
    browser, tools = tools_for([city, state_box])
    autofill(browser, tools.state, lambda: b'')
    assert browser.fields[0]['value'] == ''  # host autofill does not type "Ithaca, NY" on its own
    resolve = lambda value: tools.execute('resolve_field', {'observation_id': tools.observation_id, 'field_key': 'text:city:1',
        'mode': 'format', 'value': value, 'source_refs': ['profile:city'], 'reason': 'City box beside a separate State control.'})
    with pytest.raises(BrowserBlocked, match='Preserve the source data'):
        resolve('Ithica')
    tools.observe()
    assert resolve('Ithaca')['verified'] and browser.fields[0]['value'] == 'Ithaca'


def test_a_date_answered_once_fills_every_workday_segment():
    # The answer form sends one ISO date to each MM / DD / YYYY segment of the same date.
    prefix = 'text:selfIdentifiedDisabilityData--dateSignedOn-dateSection'
    fields = [field(prefix + part + '-input:1', label='Date*') for part in ('Month', 'Day', 'Year')]
    browser, tools = tools_for(fields)
    tools.state['answers'] = {f['key']: '2026-10-17' for f in fields}
    autofill(browser, tools.state, lambda: b'')
    assert [f['value'] for f in browser.fields] == ['10', '17', '2026']


def test_clock_uses_the_users_time_zone_before_the_machines(monkeypatch):
    late_evening = datetime(2026, 10, 18, 0, 23, tzinfo=timezone.utc)  # 20:23 in New York
    monkeypatch.delenv('JOB_AGENT_USER_TIMEZONE', raising=False)
    clock = current_clock({'timezone': 'America/New_York'}, now=late_evening)
    assert clock == {'date': '2026-10-17', 'time': '20:23', 'utc_offset': '-04:00',
                     'timezone': 'America/New_York', 'source': 'profile'}
    monkeypatch.setenv('JOB_AGENT_USER_TIMEZONE', 'Asia/Shanghai')
    assert current_clock({}, now=late_evening)['date'] == '2026-10-18'
    assert current_clock({}, now=late_evening)['source'] == 'app_setting'
    monkeypatch.setenv('JOB_AGENT_USER_TIMEZONE', 'Not/AZone')
    assert current_clock({}, now=late_evening)['source'] == 'machine_local_unconfirmed'


def test_observation_carries_the_clock_with_its_source():
    browser, tools = tools_for([field('text:note:1', label='Note', required=False)])
    clock = tools.observe()['clock']
    assert set(clock) == {'date', 'time', 'utc_offset', 'timezone', 'source'}
