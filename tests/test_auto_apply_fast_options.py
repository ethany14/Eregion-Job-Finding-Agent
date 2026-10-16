"""Explicit Jev injection keeps scripted runs offline and exercises the fast path."""
import copy
import json

import httpx
import pytest

from eregion.runtime import fast_decisions
from eregion.runtime.auto_apply.agent import ConfiguredToolModel, HybridApplicationAgent
from eregion.runtime.fast_decisions import FastDecisions
from test_auto_apply import FakeBrowser, field
from test_auto_apply_agent import RepairBrowser, ScriptModel, args, state


@pytest.mark.parametrize('enabled', ['true', 'false'])
def test_custom_model_does_not_load_configured_secondary_provider(monkeypatch, enabled):
    monkeypatch.setenv('TYPESAFE_ENABLED', enabled)

    def unexpected_configuration():
        pytest.fail('A scripted model must not enable a real secondary provider.')

    monkeypatch.setattr(fast_decisions, 'configured_decisions', unexpected_configuration)
    model = ScriptModel([('request_input', {'field_keys': ['Country']})])
    result = HybridApplicationAgent(model_factory=lambda: model).run(RepairBrowser(), state(),
        autofill=lambda: {}, active=lambda: True, reserve=lambda kind: True,
        report=lambda *values: None)
    assert result['reason'] == 'needs_input'
    assert model.calls == ['request_input']


class SchoolBrowser(FakeBrowser):
    def __init__(self):
        super().__init__('')
        self.fields = [field('School', 'combobox')]
        self.queries, self.choices = [], []

    def inspect_options(self, key, query=None):
        assert key == 'School'
        self.queries.append(query)
        self.fields[0]['options'] = [
            {'label': 'Georgia Institute of Technology', 'value': 'gt'},
            {'label': 'University of Georgia', 'value': 'uga'},
        ]
        return self.current_options(key)

    def current_options(self, key):
        assert key == 'School'
        return copy.deepcopy(self.fields[0]['options'])

    def choose_option(self, key, label):
        assert key == 'School'
        self.choices.append(label)
        self.fields[0]['value'] = label


@pytest.mark.parametrize('configured', [False, True], ids=['injected', 'production-default'])
@pytest.mark.parametrize('provider_status', [200, 503], ids=['fast-fill', 'main-model-fallback'])
def test_enabled_fast_options_and_provider_failure(monkeypatch, configured, provider_status):
    monkeypatch.setenv('TYPESAFE_ENABLED', 'true')
    requests, configurations = [], []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        answers = {key: {
            'type': 'choice', 'choice': 'o0', 'confidence': 1,
            'probabilities': {option: float(option == 'o0') for option in question['criteria']},
        } for key, question in payload['questions'].items()}
        return httpx.Response(provider_status, json={'model': 'fixture', 'answers': answers})

    decisions = FastDecisions(api_key='synthetic', enabled=True, transport=httpx.MockTransport(respond))

    def configuration():
        configurations.append(True)
        return decisions

    monkeypatch.setattr(fast_decisions, 'configured_decisions', configuration)
    repairs = [] if provider_status == 200 else [
        ('inspect_field', args('School')),
        ('resolve_field', args('School', mode='equivalent', value='Georgia Institute of Technology',
            source_refs=['profile:school'], reason='The full name of the confirmed school.')),
    ]
    model = ScriptModel([('autofill', {'field_keys':['School']}), *repairs, ('ready_for_review', {})])
    if configured:
        # Keep the production factory identity and configuration wiring, with no
        # live main-model construction or requests in this regression test.
        monkeypatch.setattr(ConfiguredToolModel, '__init__', lambda self: None)
        monkeypatch.setattr(ConfiguredToolModel, 'invoke_turn',
            lambda self, messages, tools, snapshot: model.invoke(messages, tools))
        agent = HybridApplicationAgent()
    else:
        agent = HybridApplicationAgent(model_factory=lambda: model, decisions=decisions)

    browser = SchoolBrowser()
    saved = state(answers={})
    saved['profile']['school'] = 'Georgia Tech'
    result = agent.run(browser, saved, autofill=lambda: {}, active=lambda: True,
        reserve=lambda kind: True, report=lambda *values: None)

    assert result['reason'] == 'ready'
    assert len(requests) == 1  # The enabled path really called the mocked Jev API.
    assert requests[0]['questions']['f0']['instructions']['supplied_answer'] == 'Georgia Tech'
    assert len(configurations) == int(configured)
    assert browser.queries[0] is not None  # Dynamic-menu query contract is covered.
    assert browser.choices == ['Georgia Institute of Technology']
    assert model.calls == ['autofill'] + [name for name, _ in repairs] + ['ready_for_review']
    assert browser.submissions == 0
