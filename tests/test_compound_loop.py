"""Compound lowering uses the shared loop, journal and provider transcript."""
from copy import deepcopy
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from eregion.runtime.core.engine import AgentLoop, Turn, LoopStop
from eregion.runtime.auto_apply.agent import tool


class Host:
    def __init__(self, result=None, interrupt=None, malformed=False):
        self.result, self.interrupt, self.malformed = result or {}, interrupt, malformed
        self.effects, self.checkpoints, self.messages = [], [], []
        self.version, self.running = 0, True
    def active(self): return self.running
    def revision(self): return self.version
    def observe(self):
        return Turn('Test', {'state': 'ready'}, [tool('compound', 'Plan'),
            tool('write', 'Write', {'value': {'type': 'integer'}})], self.version, batchable=frozenset({'write'}))
    def invoke(self, messages, tools, turn):
        self.messages.append(messages)
        return AIMessage(content='', tool_calls=[{'id': 'parent', 'name': 'compound', 'args': {}}]) if len(self.messages) == 1 else AIMessage(content='Done')
    def expand_call(self, call, turn):
        return [{'id': f'parent:{i}', 'name': 'write', 'args': {'value': 'bad' if self.malformed and i == 1 else i}} for i in range(3)]
    def progress(self, *args): pass
    def on_text(self, *args): return {'reason': 'done'}
    def execute(self, call, turn):
        assert self.checkpoints[-1]['inflight']['call_id'] == call['id']
        self.effects.append(call['args']['value'])
        if self.interrupt == 'steer': self.version += 1
        if self.interrupt == 'cancel': self.running = False
        if self.interrupt == 'stop': raise LoopStop('cancelled')
        return self.result
    def after_action(self, *args): pass
    def action_error(self, exc): return {'error': str(exc)}
    def persist(self, runtime): self.checkpoints.append(deepcopy(runtime))


def test_children_journaled_parent_answered_once():
    host = Host()
    AgentLoop(host).run()
    assert host.effects == [0, 1, 2]
    messages = [m for m in host.messages[1] if isinstance(m, ToolMessage)]
    assert len(messages) == 1 and messages[0].tool_call_id == 'parent'
    assert 'write' in messages[0].content


@pytest.mark.parametrize('result', [{'error': 'failed'}, {'verified': False}, {'failed': ['x']},
    {'unresolved_fields': ['x']}, {'scope_status': 'partial'}])
def test_failure_stops_dependent_effects(result):
    host = Host(result)
    AgentLoop(host).run()
    assert host.effects == [0]
    assert 'deferred' in next(m.content for m in host.messages[1] if isinstance(m, ToolMessage))


def test_all_child_schemas_checked_before_first_effect():
    host = Host(malformed=True)
    AgentLoop(host).run()
    assert not host.effects


@pytest.mark.parametrize('interrupt', ['steer', 'cancel', 'stop'])
def test_interruption_between_children_does_not_execute_remaining_actions(interrupt):
    host = Host(interrupt=interrupt)
    AgentLoop(host, slice_steps=1).run()
    assert host.effects == [0]
    saved = host.checkpoints[-1]
    host.running, host.interrupt = True, None
    AgentLoop(host, runtime=saved).run()
    assert host.effects == [0]  # Resume asks the model; it never replays the tail.
