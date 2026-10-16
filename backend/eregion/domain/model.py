"""Shared model configuration and structured invocation."""

from __future__ import annotations

import os
import hashlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from eregion.paths import PROJECT_ROOT
from typing import Any, TypeVar

from dotenv import dotenv_values
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, PrivateAttr, ValidationError
from eregion.domain.reasoning import resolve, configured_overrides, SCHEMA_PURPOSES, escalated, escalation_enabled, rejection_retry_enabled, reasoning_parameter_rejected

Result = TypeVar("Result", bound=BaseModel)
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
GEMINI_OPENAI_COMPATIBILITY_HOST = "generativelanguage.googleapis.com"
GEMINI_MISSING_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


class _AsyncBudgetStop(Exception):
    """LangChain's async gather assumes every error also subclasses Exception."""
    def __init__(self, stop):
        self.stop = stop
        super().__init__(str(stop))


class ProviderCompatibleChatOpenAI(ChatOpenAI):
    """Patch provider-specific wire metadata without changing shared messages.

    Gemini 3 requires a thought signature when a function-call message is sent
    back with its tool result. The installed OpenAI/LangChain compatibility
    stack drops Gemini's ``extra_content`` while normalizing the first response.
    Google's documented fallback signature is added only for Gemini's
    OpenAI-compatible endpoint and only when the signature is absent.
    """

    _reasoning_settings: dict = PrivateAttr(default_factory=dict)
    _reasoning_purpose: str | None = PrivateAttr(default=None)
    _legacy_reasoning: str | None = PrivateAttr(default=None)
    _has_reasoning_configuration: bool = PrivateAttr(default=False)
    _interview_rejection_retry: bool = PrivateAttr(default=False)

    def _attempt_options(self, kwargs):
        from eregion.runtime.provider_usage import current_scope
        scope = current_scope()
        purpose = scope.get('reasoning_purpose', self._reasoning_purpose or scope.get('purpose'))
        fallback = self._legacy_reasoning if self._has_reasoning_configuration else self.reasoning_effort
        level = resolve(self._reasoning_settings, purpose, kwargs.get('reasoning_effort', fallback))
        higher = escalated(level) if scope.get('reasoning_retry') and escalation_enabled() else level
        options = dict(kwargs)
        if higher is not None:
            options['reasoning_effort'] = higher
        return options, {'effective_reasoning': higher, 'reasoning_escalation': higher != level}

    def _budget_sizes(self, messages, stop, options):
        # An experiment budget also reserves before sending; without this the replay's first
        # request was refused as unknown_input_size because only the task cap triggered it.
        if not (os.getenv('JOB_AGENT_TASK_MAX_INPUT_TOKENS', '').strip() or os.getenv('JOB_AGENT_EXPERIMENT_ID', '').strip()):
            return {}
        from eregion.runtime.provider_usage import input_reservation
        return {'input_token_reservation': input_reservation(self._get_request_payload(messages, stop=stop, **options))}

    def _without_sdk_retries(self):
        updates = {'max_retries': 0}
        if self.root_client is not None:
            client = self.root_client.with_options(max_retries=0)
            updates.update(root_client=client, client=client.chat.completions)
        if self.root_async_client is not None:
            client = self.root_async_client.with_options(max_retries=0)
            updates.update(root_async_client=client, async_client=client.chat.completions)
        return self.model_copy(update=updates)

    def _transport_retry_delay(self, exc, retry):
        from openai import APIConnectionError, APIStatusError
        if retry >= self._sdk_retries():
            return None
        root = self.root_client or self.root_async_client
        if isinstance(exc, APIStatusError):
            if not root._should_retry(exc.response):
                return None
            headers = exc.response.headers
        elif isinstance(exc, APIConnectionError):
            headers = None
        else:
            return None
        from openai._models import FinalRequestOptions
        options = FinalRequestOptions.construct(method='post', url='/chat/completions', max_retries=self._sdk_retries())
        return root._calculate_retry_timeout(self._sdk_retries() - retry, options, headers)

    def _sdk_retries(self):
        return self.max_retries if self.max_retries is not None else (self.root_client or self.root_async_client).max_retries

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        import time
        from eregion.runtime.provider_usage import provider_call, record_chat_usage
        from eregion.runtime.telemetry import model_metrics
        sizes = model_metrics(messages, kwargs.get('tools', []))
        from eregion.runtime.provider_usage import usage_scope, current_scope
        options, metadata = self._attempt_options(kwargs)
        model = self._without_sdk_retries() if self._sdk_retries() else self
        number = 0
        # One retry budget for the whole call: a plain (no-reasoning) retry continues the
        # transport-retry count instead of restarting it, with at least one plain attempt.
        used = 0
        for plain in (False, True):
            for retry in range(used, max(used + 1, self._sdk_retries() + 1)):
                wire = self._retry_options(options, retry)
                with usage_scope(**metadata, attempt=current_scope().get('attempt', 1) + number,
                                 **({'queue_wait_seconds': 0.0} if number else {})):
                    number += 1
                    try:
                        with provider_call(self.model_name, **sizes, **self._budget_sizes(messages, stop, wire)) as usage:
                            usage.update(self._wire_metrics(messages, stop, wire))
                            result = ChatOpenAI._generate(model, messages, stop=stop, run_manager=run_manager, **wire)
                            record_chat_usage(usage, result)
                            return result
                    except Exception as exc:
                        delay = self._transport_retry_delay(exc, retry)
                        if delay is not None:
                            time.sleep(delay)
                            continue
                        if not self._can_retry_plain(exc, plain, options): raise
                        options.update(reasoning_effort=None, _omit_reasoning=True)
                        used = retry + 1
                        metadata = {'effective_reasoning': None, 'reasoning_escalation': False, 'reasoning_fallback': True}
                        break

    def _wire_metrics(self, messages, stop, options):
        """P1 measurement of the exact request body; never changes or blocks the request."""
        try:
            from eregion.runtime.provider_usage import current_scope
            from eregion.runtime.wire_metrics import key_for, measure
            payload = self._get_request_payload(messages, stop=stop, **options)
            return measure(payload, key_for(current_scope(), self.model_name))
        except Exception:
            return {'wire_metrics_error': True}

    def _retry_options(self, options, retry):
        if not self._sdk_retries(): return options
        return {**options, 'extra_headers': {**options.get('extra_headers', {}), 'x-stainless-retry-count': str(retry)}}

    def _can_retry_plain(self, exc, plain, options):
        # Only an explicit "parameter not supported" response; a connection error or
        # timeout whose text happens to mention reasoning is not a rejection.
        return (not plain and (self._interview_rejection_retry or rejection_retry_enabled())
                and options.get('reasoning_effort') and reasoning_parameter_rejected(exc))

    async def agenerate(self, *args, **kwargs):
        try:
            return await super().agenerate(*args, **kwargs)
        except _AsyncBudgetStop as exc:
            raise exc.stop from None

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from eregion.runtime.provider_usage import TaskProviderBudgetExceeded
        try:
            return await self._agenerate_attempt(messages, stop, run_manager, **kwargs)
        except TaskProviderBudgetExceeded as exc:
            raise _AsyncBudgetStop(exc) from None

    async def _agenerate_attempt(self, messages, stop=None, run_manager=None, **kwargs):
        import asyncio
        from eregion.runtime.provider_usage import provider_call, record_chat_usage
        from eregion.runtime.telemetry import model_metrics
        from eregion.runtime.provider_usage import usage_scope, current_scope
        options, metadata = self._attempt_options(kwargs)
        model = self._without_sdk_retries() if self._sdk_retries() else self
        number = 0
        # One retry budget for the whole call: a plain (no-reasoning) retry continues the
        # transport-retry count instead of restarting it, with at least one plain attempt.
        used = 0
        for plain in (False, True):
            for retry in range(used, max(used + 1, self._sdk_retries() + 1)):
                wire = self._retry_options(options, retry)
                with usage_scope(**metadata, attempt=current_scope().get('attempt', 1) + number,
                                 **({'queue_wait_seconds': 0.0} if number else {})):
                    number += 1
                    try:
                        with provider_call(self.model_name, **model_metrics(messages, kwargs.get('tools', [])),
                                           **self._budget_sizes(messages, stop, wire)) as usage:
                            usage.update(self._wire_metrics(messages, stop, wire))
                            result = await ChatOpenAI._agenerate(model, messages, stop=stop, run_manager=run_manager, **wire)
                            record_chat_usage(usage, result)
                            return result
                    except Exception as exc:
                        delay = self._transport_retry_delay(exc, retry)
                        if delay is not None:
                            await asyncio.sleep(delay)
                            continue
                        if not self._can_retry_plain(exc, plain, options): raise
                        options.update(reasoning_effort=None, _omit_reasoning=True)
                        used = retry + 1
                        metadata = {'effective_reasoning': None, 'reasoning_escalation': False, 'reasoning_fallback': True}
                        break

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        omit_reasoning = kwargs.pop('_omit_reasoning', False)
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if omit_reasoning:
            payload.pop('reasoning_effort', None)
        base_url = str(self.openai_api_base or "")
        if GEMINI_OPENAI_COMPATIBILITY_HOST not in base_url.casefold():
            return payload
        for message in payload.get("messages", []):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            first_call = message["tool_calls"][0]
            extra_content = first_call.setdefault("extra_content", {})
            google = extra_content.setdefault("google", {})
            google.setdefault(
                "thought_signature", GEMINI_MISSING_THOUGHT_SIGNATURE
            )
        return payload


def optional_setting(settings: Mapping[str, Any], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def create_model(
    *,
    env_path: Path = DEFAULT_ENV_PATH,
    environ: Mapping[str, str] | None = None,
    model_factory: Callable[..., Any] = ProviderCompatibleChatOpenAI,
    purpose: str | None = None,
) -> Any:
    """Create the configured chat model with environment values taking priority."""
    environment = os.environ if environ is None else environ
    settings = {**dotenv_values(env_path), **environment}
    key = {'fast': 'LLM_FAST_MODEL_ID', 'extraction': 'LLM_EXTRACTION_MODEL_ID',
           'search_qualification': 'LLM_EXTRACTION_MODEL_ID'}.get(purpose)
    model_id = (optional_setting(settings, key) if key else None) or optional_setting(settings, "LLM_MODEL_ID")
    if not model_id:
        raise ValueError("Set LLM_MODEL_ID in the environment or project's .env file.")
    options: dict[str, Any] = {
        "model": model_id,
        "temperature": 0,
        "max_retries": 0,
        "timeout": 45.0,
    }
    api_key = optional_setting(settings, "LLM_API_KEY")
    base_url = optional_setting(settings, "LLM_BASE_URL")
    timeout = optional_setting(settings, "LLM_TIMEOUT")
    max_retries = optional_setting(settings, "LLM_MAX_RETRIES")
    if api_key is not None:
        options["api_key"] = api_key
    if base_url is not None:
        options["base_url"] = base_url
    if timeout is not None:
        try:
            seconds = float(timeout)
        except ValueError as exc:
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.") from exc
        if not 0 < seconds < float("inf"):
            raise ValueError("LLM_TIMEOUT must be a positive number of seconds.")
        options["timeout"] = seconds
    if max_retries is not None:
        try:
            retries = int(max_retries)
        except ValueError as exc:
            raise ValueError("LLM_MAX_RETRIES must be a non-negative integer.") from exc
        if retries < 0:
            raise ValueError("LLM_MAX_RETRIES must be a non-negative integer.")
        options["max_retries"] = retries
    reasoning_key = 'LLM_EXTRACTION_REASONING' if purpose in {'extraction', 'search_qualification'} else 'LLM_AGENT_REASONING' if purpose in {'agent', 'fast'} else None
    reasoning = optional_setting(settings, reasoning_key) if reasoning_key else None
    # Bounded reading task; explicit user configuration always takes precedence.
    if purpose == 'search_qualification' and reasoning is None:
        reasoning = 'low'
    legacy_reasoning = reasoning
    reasoning = resolve(settings, purpose, reasoning)
    if reasoning:
        if reasoning not in {'none', 'minimal', 'low', 'medium', 'high'}:
            raise ValueError(f'{reasoning_key} has an unsupported reasoning level.')
        options['reasoning_effort'] = reasoning
    model = model_factory(**options)
    if isinstance(model, ProviderCompatibleChatOpenAI):
        model._reasoning_settings = configured_overrides(settings)
        model._reasoning_purpose = purpose
        model._legacy_reasoning = legacy_reasoning
        model._has_reasoning_configuration = True
    return model


def model_settings_fingerprint(*, env_path: Path = DEFAULT_ENV_PATH, environ=None) -> str:
    """Opaque cache identity. Never return or log credentials or raw settings."""
    settings = {**dotenv_values(env_path), **(os.environ if environ is None else environ)}
    keys = ('LLM_FAST_MODEL_ID', 'LLM_EXTRACTION_MODEL_ID', 'LLM_AGENT_REASONING', 'LLM_EXTRACTION_REASONING', 'LLM_MODEL_ID','LLM_BASE_URL','LLM_API_KEY','LLM_TIMEOUT','LLM_MAX_RETRIES',
        'OPENAI_API_KEY','OPENAI_API_BASE','OPENAI_BASE_URL','OPENAI_ORG_ID')
    keys = (*keys, *configured_overrides(settings), 'LLM_INTERVIEW_REASONING', 'JOB_AGENT_REASONING_ESCALATION')
    if settings.get('JOB_AGENT_REASONING_REJECTION_RETRY') == '1':
        keys = (*keys, 'JOB_AGENT_REASONING_REJECTION_RETRY')
    return hashlib.sha256(json.dumps({key:optional_setting(settings,key) for key in keys},
        sort_keys=True).encode()).hexdigest()


def invoke_structured(
    model: Any,
    schema: type[Result],
    system_message: str,
    human_message: str,
    config: Any = None,
) -> Result:
    """Invoke a Pydantic structured model using the baseline message layout."""
    from eregion.runtime.coordination.model_usage import invoke_model
    from eregion.runtime.provider_usage import current_scope, usage_scope, adapter_attempt
    purpose = current_scope().get('purpose', 'structured')
    semantic_purpose = SCHEMA_PURPOSES.get(schema.__name__, schema.__name__)
    retrying = current_scope().get('reasoning_retry', False)
    for retry in (False, True):
        with usage_scope(purpose=purpose + ':' + schema.__name__, reasoning_purpose=semantic_purpose,
                         reasoning_retry=retrying or retry,
                         attempt=current_scope().get('attempt', 1) + int(retry),
                         **({'queue_wait_seconds': 0.0} if retry else {})), adapter_attempt(type(model).__name__):
            try:
                return invoke_model(_invoke_structured, model, schema, system_message, human_message, config)
            except Exception as exc:
                from langchain_core.exceptions import OutputParserException
                invalid = isinstance(exc, (ValidationError, OutputParserException)) or (
                    isinstance(exc, ValueError) and str(exc) == 'The model did not return valid structured output.')
                if retry or retrying or current_scope().get('reasoning_retry_owner') or not escalation_enabled() or not invalid:
                    raise


def _invoke_structured(model, schema, system_message, human_message, config):
    from eregion.runtime.coordination.model_usage import current_model_call

    call = current_model_call()
    if call is not None and isinstance(model, ChatOpenAI):
        # One reservation corresponds to one provider request; retries must
        # acquire their own reservation. Raw usage replaces fallback estimates.
        updates = {"max_tokens": call["output_tokens"], "max_retries": 0}
        # The SDK client was initialized earlier; changing only the model field
        # would leave its internal HTTP retries enabled.
        if model.root_client is not None:
            client = model.root_client.with_options(max_retries=0)
            updates.update(root_client=client, client=client.chat.completions)
        if model.root_async_client is not None:
            client = model.root_async_client.with_options(max_retries=0)
            updates.update(root_async_client=client, async_client=client.chat.completions)
        model = model.model_copy(update=updates)
        response = model.with_structured_output(schema, include_raw=True).invoke(
            [("system", system_message), ("human", human_message)], config=config)
        raw_usage = getattr(response.get("raw"), "usage_metadata", None)
        if raw_usage:
            call["usage"] = {key: int(raw_usage.get(key, 0)) for key in ("input_tokens", "output_tokens")}
        if response.get("parsing_error") or response.get("parsed") is None:
            raise ValueError("The model did not return valid structured output.")
        result = response["parsed"]
        return result if isinstance(result, schema) else schema.model_validate(result)
    structured_model = model.with_structured_output(schema)
    result = structured_model.invoke(
        [("system", system_message), ("human", human_message)], config=config
    )
    if isinstance(result, schema):
        return result
    return schema.model_validate(result)


from collections import OrderedDict
from threading import RLock
_clients, _clients_lock = OrderedDict(), RLock()

def shared_model(*, env_path=DEFAULT_ENV_PATH, purpose=None):
    key = (model_settings_fingerprint(env_path=env_path), purpose, create_model)
    with _clients_lock:
        if key not in _clients:
            _clients[key] = create_model(env_path=env_path, purpose=purpose)
            while len(_clients) > 8:
                _clients.popitem(last=False)
        _clients.move_to_end(key)
        return _clients[key]
