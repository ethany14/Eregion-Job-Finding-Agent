"""Model and API key settings: stored in .env, the key never leaves the server, local workspace only."""
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from eregion.api import model_settings_routes
from eregion.api.main import create_app
from eregion.domain import model_settings as settings
from eregion.domain.model import create_model

KEY = 'sk-test-0123456789abcdWXYZ'


def test_write_env_keeps_other_lines_comments_and_line_endings(tmp_path):
    path = tmp_path / '.env'
    path.write_bytes(b'# provider\r\nLLM_MODEL_ID=old\r\nexport LLM_BASE_URL=https://a.example/v1\r\nOTHER=1\r\nLLM_MODEL_ID=dup\r\n')
    settings.write_env(path, {'LLM_MODEL_ID': 'new', 'LLM_BASE_URL': None, 'LLM_API_KEY': KEY})
    assert path.read_bytes() == f'# provider\r\nLLM_MODEL_ID=new\r\nOTHER=1\r\nLLM_API_KEY={KEY}\r\n'.encode()
    fresh = tmp_path / 'new' / '.env'
    settings.write_env(fresh, {'LLM_MODEL_ID': 'm'})
    assert fresh.read_text() == 'LLM_MODEL_ID=m\n'


def test_saved_settings_reach_the_next_model_and_the_key_is_never_returned(tmp_path):
    path = tmp_path / '.env'
    view = settings.save_settings({'model_id': 'gpt-test', 'api_key': KEY, 'base_url': 'https://llm.example/v1',
                                   'fast_model_id': 'gpt-fast', 'extraction_model_id': None}, env_path=path, environ={})
    assert KEY not in json.dumps(view)
    assert view['api_key'] == {'set': True, 'source': 'file', 'variable': 'LLM_API_KEY', 'hint': 'WXYZ'}
    assert view['configured'] and view['locked'] == [] and view['fast_model_id'] == 'gpt-fast'
    model = create_model(env_path=path, environ={}, model_factory=lambda **options: options)
    assert model['model'] == 'gpt-test' and model['api_key'] == KEY and model['base_url'] == 'https://llm.example/v1'
    assert create_model(env_path=path, environ={}, purpose='fast', model_factory=lambda **o: o)['model'] == 'gpt-fast'
    # An empty value removes the override, so the fast purpose falls back to the main model.
    settings.save_settings({'fast_model_id': ''}, env_path=path, environ={})
    assert create_model(env_path=path, environ={}, purpose='fast', model_factory=lambda **o: o)['model'] == 'gpt-test'
    assert 'LLM_FAST_MODEL_ID' not in path.read_text()
    # Leaving the key out keeps it; an empty key removes it.
    settings.save_settings({'model_id': 'gpt-other', 'api_key': None}, env_path=path, environ={})
    assert KEY in path.read_text()
    assert not settings.save_settings({'api_key': ''}, env_path=path, environ={})['api_key']['set']


def test_environment_values_are_locked_and_validation_refuses_unsafe_values(tmp_path):
    path = tmp_path / '.env'
    environ = {'LLM_MODEL_ID': 'from-env', 'OPENAI_API_KEY': 'sk-env-key-abcdefgh1234'}
    view = settings.read_settings(env_path=path, environ=environ)
    assert view['model_id'] == 'from-env' and view['locked'] == ['model_id']
    assert view['api_key'] == {'set': True, 'source': 'environment', 'variable': 'OPENAI_API_KEY', 'hint': '1234'}
    with pytest.raises(settings.SettingsLocked, match='server environment'):
        settings.save_settings({'model_id': 'other'}, env_path=path, environ=environ)
    for changes, message in [({'model_id': ''}, 'Choose a model'), ({'model_id': 'two words'}, 'Model name'),
                             ({'api_key': 'sk-1\nLLM_MODEL_ID=x'}, 'API key'), ({'api_key': 'a"b'}, 'API key'),
                             ({'base_url': 'ftp://x.example'}, 'http or https'),
                             ({'base_url': 'https://user:pw@x.example/v1'}, 'user name or password')]:
        with pytest.raises(ValueError, match=message):
            settings.save_settings(changes, env_path=path, environ={})
    assert not path.exists()


def test_model_list_uses_the_typed_key_and_reports_provider_failures_without_the_key(tmp_path):
    path = tmp_path / '.env'
    settings.write_env(path, {'LLM_API_KEY': 'sk-saved-key-0000', 'LLM_BASE_URL': 'https://llm.example/v1'})
    seen = []

    def provider(request):
        seen.append((str(request.url), request.headers['authorization']))
        if request.headers['authorization'] == 'Bearer bad-key-123456':
            return httpx.Response(401, json={'error': 'invalid'})
        return httpx.Response(200, json={'data': [{'id': 'b-model'}, {'id': 'a-model'}, {'id': 'a-model'}]})

    transport = httpx.MockTransport(provider)
    assert settings.list_models(env_path=path, environ={}, transport=transport) == ['a-model', 'b-model']
    assert seen[-1] == ('https://llm.example/v1/models', 'Bearer sk-saved-key-0000')
    settings.list_models(api_key=KEY, base_url='https://other.example/api', env_path=path, environ={}, transport=transport)
    assert seen[-1] == ('https://other.example/api/models', f'Bearer {KEY}')
    with pytest.raises(settings.ModelListError, match='did not accept') as refused:
        settings.list_models(api_key='bad-key-123456', env_path=path, environ={}, transport=transport)
    assert 'bad-key' not in str(refused.value)

    def offline(request):
        raise httpx.ConnectError('down', request=request)
    with pytest.raises(settings.ModelListError, match='Could not reach llm.example'):
        settings.list_models(env_path=path, environ={}, transport=httpx.MockTransport(offline))
    with pytest.raises(settings.ModelListError, match='Enter an API key'):
        settings.list_models(env_path=tmp_path / 'empty.env', environ={}, transport=transport)


def test_api_is_local_only_and_write_only_for_the_key(tmp_path, monkeypatch):
    path = tmp_path / '.env'
    monkeypatch.setattr(model_settings_routes, 'ENV_PATH', path)
    for key in settings.FIELDS.values():
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    app = create_app(run_service=object())
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get('/api/settings/model').json()['configured'] is False
        saved = client.put('/api/settings/model', json={'model_id': 'gpt-test', 'api_key': KEY})
        assert saved.status_code == 200 and KEY not in saved.text and saved.json()['api_key']['hint'] == 'WXYZ'
        assert KEY not in client.get('/api/settings/model').text and KEY in path.read_text()
        assert client.put('/api/settings/model', json={'model_id': 'two words'}).status_code == 422
        assert client.put('/api/settings/model', json={'unknown': 'x'}).status_code == 422
        assert client.put('/api/settings/model', json={'api_key': 'x'},
                          headers={'origin': 'https://another.example'}).status_code == 403
        monkeypatch.setenv('LLM_MODEL_ID', 'from-env')
        locked = client.put('/api/settings/model', json={'model_id': 'gpt-new'})
        assert locked.status_code == 409 and 'server environment' in locked.json()['detail']
    # A page reached through another host name (for example a rebound DNS name) is refused.
    # The app's trusted-host check answers 400 before this route's own loopback check would answer 403.
    with TestClient(app, base_url='http://attacker.example') as client:
        assert client.get('/api/settings/model').status_code in (400, 403)
        assert client.put('/api/settings/model', json={'api_key': 'x'}).status_code in (400, 403)
    assert 'LLM_API_KEY=x' not in path.read_text()


@pytest.fixture
def process_env(monkeypatch):
    # Switches read from the process environment are written to os.environ; restore it afterwards.
    keys = [item['key'] for item in settings.SWITCHES]
    saved = {key: os.environ.get(key) for key in keys}
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    settings._from_file.clear()
    yield os.environ
    for key, value in saved.items():
        os.environ.pop(key, None)
        if value is not None:
            os.environ[key] = value
    settings._from_file.clear()


def test_feature_switches_reach_their_readers_and_secrets_stay_write_only(tmp_path, process_env):
    from dotenv import dotenv_values
    from eregion.runtime.career.jobspy_source import JobSpyConfig
    path = tmp_path / '.env'
    python = tmp_path / 'python.exe'
    python.write_text('')
    view = settings.save_settings({'features': {'TYPESAFE_API_KEY': KEY, 'TYPESAFE_ENABLED': True,
        'JOB_AGENT_JOB_API_FIRST': False, 'JOB_AGENT_JOBSPY_ENABLED': True, 'JOB_AGENT_JOBSPY_PYTHON': str(python),
        'JOB_AGENT_JOBSPY_SITES': 'indeed,linkedin', 'JOB_AGENT_TASK_MEMORY_MODE': True}}, env_path=path, environ=process_env)
    assert KEY not in json.dumps(view)
    features = {f['key']: f for f in view['features']}
    assert features['TYPESAFE_ENABLED']['value'] is True and features['TYPESAFE_API_KEY'] == {**features['TYPESAFE_API_KEY'], 'set': True, 'hint': 'WXYZ'}
    assert features['JOB_AGENT_TASK_MEMORY_MODE'] == {**features['JOB_AGENT_TASK_MEMORY_MODE'], 'value': True, 'applies': 'after restart'}
    assert features['JOB_AGENT_JOBSPY_ENABLED']['value'] is True and features['JOB_AGENT_JOBSPY_PYTHON']['parent'] == 'JOB_AGENT_JOBSPY_ENABLED'
    # The browser driver is not a workspace setting: applications use Playwright.
    assert 'JOB_AGENT_BROWSER_DRIVER' not in features
    # Read through .env on each use.
    stored = dotenv_values(path)
    assert stored['TYPESAFE_ENABLED'] == 'true' and stored['TYPESAFE_API_KEY'] == KEY and stored['JOB_AGENT_JOB_API_FIRST'] == '0'
    # Read from the process environment on each use: applied without a restart.
    assert JobSpyConfig.from_env().enabled and JobSpyConfig.from_env().sites == ('indeed', 'linkedin')
    assert process_env['JOB_AGENT_TASK_MEMORY_MODE'] == 'on'
    # The JobSpy switch turns it off while keeping the interpreter path for later.
    view = settings.save_settings({'features': {'JOB_AGENT_JOBSPY_ENABLED': False}}, env_path=path, environ=process_env)
    assert not JobSpyConfig.from_env().enabled and dotenv_values(path)['JOB_AGENT_JOBSPY_PYTHON'] == str(python)
    assert {f['key']: f['value'] for f in view['features'] if f['kind'] == 'bool'}['JOB_AGENT_JOBSPY_ENABLED'] is False
    settings.save_settings({'features': {'JOB_AGENT_JOBSPY_ENABLED': True}}, env_path=path, environ=process_env)
    assert JobSpyConfig.from_env().enabled


def test_feature_requirements_locks_validation_and_startup_loading(tmp_path, process_env):
    path = tmp_path / '.env'
    with pytest.raises(ValueError, match='needs TypeSafe API key'):
        settings.save_settings({'features': {'TYPESAFE_ENABLED': True}}, env_path=path, environ={})
    with pytest.raises(ValueError, match='needs Score job fit with Jev'):
        settings.save_settings({'features': {'TYPESAFE_CAREER_ENABLED': True}}, env_path=path, environ={})
    with pytest.raises(ValueError, match='needs JobSpy Python interpreter'):
        settings.save_settings({'features': {'JOB_AGENT_JOBSPY_ENABLED': True}}, env_path=path, environ={})
    for features, message in [({'JOB_AGENT_HIDDEN_BROWSER': 'firefox'}, 'choose one of'),
                              ({'JOB_AGENT_BROWSER_DRIVER': 'patchright'}, 'not a setting'),
                              ({'JOB_AGENT_TREG_ENABLED': True}, 'not a setting'),
                              ({'TYPESAFE_ENABLED': 'yes'}, 'on or off'),
                              ({'JOB_AGENT_JOBSPY_PYTHON': str(tmp_path / 'missing.exe')}, 'no file'),
                              ({'LLM_SECRET_THING': True}, 'not a setting')]:
        with pytest.raises(ValueError, match=message):
            settings.save_settings({'features': features}, env_path=path, environ={})
    assert not path.exists()
    # The operator's environment wins and is locked; a value this process loaded from .env is not.
    with pytest.raises(settings.SettingsLocked):
        settings.save_settings({'features': {'JOB_AGENT_HIDDEN_BROWSER': 'headless'}}, env_path=path,
                               environ={'JOB_AGENT_HIDDEN_BROWSER': 'offscreen'})
    settings.write_env(path, {'JOB_AGENT_HIDDEN_BROWSER': 'headless', 'JOB_AGENT_TASK_MEMORY_MODE': 'shadow'})
    process_env['JOB_AGENT_TASK_MEMORY_MODE'] = 'off'
    settings.load_process_switches(env_path=path, environ=process_env)
    assert process_env['JOB_AGENT_HIDDEN_BROWSER'] == 'headless' and process_env['JOB_AGENT_TASK_MEMORY_MODE'] == 'off'
    features = {f['key']: f for f in settings.read_settings(env_path=path, environ=process_env)['features']}
    assert not features['JOB_AGENT_HIDDEN_BROWSER']['locked'] and features['JOB_AGENT_HIDDEN_BROWSER']['value'] == 'headless'
    assert features['JOB_AGENT_TASK_MEMORY_MODE']['locked']


def test_configured_model_lets_the_server_start_before_a_model_is_set(tmp_path, monkeypatch):
    from eregion.domain.model import ConfiguredModel
    from eregion.runtime.model_runtime import model_timeout
    for key in (*settings.FIELDS.values(), 'OPENAI_API_KEY', 'LLM_TIMEOUT'):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / '.env'
    model = ConfiguredModel(purpose='api', env_path=path)
    # What the session runtime touches at startup works without a model.
    assert hasattr(model, 'with_structured_output') and model_timeout(model) == 45.0
    with pytest.raises(ValueError, match='LLM_MODEL_ID'):
        model.invoke('hello')
    settings.save_settings({'model_id': 'gpt-later', 'api_key': KEY}, env_path=path, environ={})
    assert model.model_name == 'gpt-later'
