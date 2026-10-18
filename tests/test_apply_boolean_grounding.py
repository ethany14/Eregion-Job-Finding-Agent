import pytest
from pydantic import ValidationError
from eregion.runtime.auto_apply.grounding import verify_boolean_answers, BooleanGrounding
from eregion.runtime.provider_usage import current_scope

class Model:
    def __init__(self, result): self.result=result
    def with_structured_output(self, schema):
        assert schema is BooleanGrounding
        return self
    def invoke(self, messages, **kwargs):
        assert kwargs == {"timeout":90,"max_tokens":8000}
        assert current_scope()["purpose"] == "application_answer_verification"
        if isinstance(self.result, Exception): raise self.result
        return self.result

def test_batched_semantic_verification_keeps_model_verdicts():
    assert verify_boolean_answers(Model({"supported":[True,False]}), [{},{}],timeout=120)==[True,False]

@pytest.mark.parametrize("result", [{"supported":[True]}, {"supported":["true",False]}, {"supported":[True,False],"extra":1}])
def test_incomplete_or_nonboolean_verification_fails_closed(result):
    with pytest.raises((ValueError,ValidationError)):
        verify_boolean_answers(Model(result),[{},{}],timeout=120)

def test_provider_failure_does_not_supply_semantic_answers():
    with pytest.raises(RuntimeError,match="truncated"):
        verify_boolean_answers(Model(RuntimeError("truncated")),[{}],timeout=120)
