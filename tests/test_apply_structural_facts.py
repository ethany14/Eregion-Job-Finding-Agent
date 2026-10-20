"""Structural contact facts and the one-turn recovery note (selective-model follow-ups, 2026-10-20)."""
import pytest

from eregion.runtime.auto_apply.agent import HybridApplicationAgent
from eregion.runtime.auto_apply.autofill import autofill, desired_answer
from eregion.runtime.auto_apply.structural_facts import structural_fact
from eregion.runtime.provider_usage import current_scope
from test_auto_apply import field
from test_auto_apply_agent import RepairBrowser, ScriptModel, state


def control(dom_id, kind='text', label='x'):
    return {**field(f'{kind}:{dom_id}:1', kind, label), 'dom_id': dom_id}


@pytest.mark.parametrize('phone, expected', [
    ('+1 312-555-0142', '312-555-0142'), ('+44 20 7946 0958', '20 7946 0958'), ('+1.312.555.0142', '312.555.0142'),
    ('312-555-0142', None),            # no explicit country prefix: the model decides
    ('+13125550142', None),            # prefix not separated: ambiguous split
    ('', None)])
def test_workday_phone_number_takes_the_national_part_of_an_explicit_international_number(phone, expected):
    assert structural_fact(control('phoneNumber--phoneNumber', label='Phone Number'), {'phone': phone}) == expected


@pytest.mark.parametrize('city, expected', [
    ('Ithaca, NY', 'Ithaca'), ('New York, NY', 'New York'),
    ('Ithaca', None), ('Ithaca, New York', None), ('Paris, Île-de-France, FR', None), ('', None)])
def test_workday_city_takes_the_city_of_city_comma_state(city, expected):
    assert structural_fact(control('address--city', label='City'), {'city': city}) == expected


def test_other_controls_and_saved_answers_are_untouched():
    profile = {'phone': '+1 312-555-0142', 'city': 'Ithaca, NY'}
    assert structural_fact(control('phoneNumber--extension'), profile) is None
    assert structural_fact(control('address--city', 'combobox'), profile) is None
    phone = control('phoneNumber--phoneNumber', label='Phone Number')
    assert desired_answer(phone, {'profile': profile, 'answers': {}}) == '312-555-0142'
    assert desired_answer(phone, {'profile': profile, 'answers': {phone['key']: '(312) 555 0142'}}) == '(312) 555 0142'


def test_the_recovery_note_reaches_only_the_first_turn_after_a_yield():
    seen = []

    class Watching(ScriptModel):
        def invoke(self, messages, tools):
            import json
            from langchain_core.messages import HumanMessage
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            seen.append((snap.get('recovery'), current_scope().get('purpose')))
            return super().invoke(messages, tools)
    browser, saved = RepairBrowser(), state(recovery_note='Previous step ended with yielded. Observed progress: True.')
    saved_changes = []
    model = Watching([('observe', {}), ('observe', {}), ('request_input', {'field_keys': ['Country']})])
    HybridApplicationAgent(model_factory=lambda: model).run(browser, saved,
        autofill=lambda: autofill(browser, saved, lambda: b''), active=lambda: True,
        reserve=lambda kind: True, report=lambda *values: None,
        checkpoint=lambda **changes: saved_changes.append(changes))
    assert seen[0][0] and seen[0][1] == 'apply_turn:recovery'
    assert all(note is None and purpose != 'apply_turn:recovery' for note, purpose in seen[1:])
    assert {'recovery_note': None} in saved_changes and saved['recovery_note'] is None
