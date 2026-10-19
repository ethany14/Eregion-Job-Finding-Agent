"""Model connection settings and feature switches the person edits from the workspace.

The values live in the project's `.env`, the same file `create_model` reads on every
model creation, so a saved change applies to the next conversation turn or application
without a restart. A variable set in the server's process environment takes priority
over `.env`; such a field is reported as locked and cannot be changed here. The API key
is never returned: only whether one is set, where it comes from and its last characters.

Feature switches (Jev, Treg, JobSpy, browser driver and others) are stored the same way. Some
are read through `.env` on each use; the others are read from the process environment, so a
saved value is also set in this process and loaded from `.env` when the server starts.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from threading import Lock
from typing import Any, Mapping, MutableMapping
from urllib.parse import urlsplit

import httpx
from dotenv import dotenv_values

from eregion.domain.model import DEFAULT_ENV_PATH, optional_setting

FIELDS = {
    'model_id': 'LLM_MODEL_ID',
    'fast_model_id': 'LLM_FAST_MODEL_ID',
    'extraction_model_id': 'LLM_EXTRACTION_MODEL_ID',
    'base_url': 'LLM_BASE_URL',
    'api_key': 'LLM_API_KEY',
}
# Written unquoted, so a value may not contain whitespace, quotes, a backslash or '#'.
_TOKEN = re.compile(r'[^\s"\'#\\]+')
_DEFAULT_BASE_URL = 'https://api.openai.com/v1'
ON, OFF = ('true', 'false'), ('1', '0')
TRUE = {'1', 'true', 'yes', 'on'}


def _switch(key, group, label, kind='bool', *, default='', values=ON, options=(), process=False, restart=False,
            requires=(), help=''):
    return {'key': key, 'group': group, 'label': label, 'kind': kind, 'default': default, 'values': values,
            'options': options, 'process': process, 'restart': restart, 'requires': requires, 'help': help}


# kind: bool (stored as values[0] / values[1]), choice (one of options), secret (write-only), path.
# process: read from the process environment only. restart: read once when the server starts.
SWITCHES = [
    _switch('TYPESAFE_ENABLED', 'Jev fit scoring', 'Score job fit with Jev', default='false',
            requires=('TYPESAFE_API_KEY',), help='Fast, cached fit scores. Needs a TypeSafe API key.'),
    _switch('TYPESAFE_API_KEY', 'Jev fit scoring', 'TypeSafe API key', 'secret'),
    _switch('TYPESAFE_CAREER_ENABLED', 'Jev fit scoring', 'Also use Jev for memory, question and evidence helpers',
            default='false', requires=('TYPESAFE_ENABLED',)),
    _switch('TYPESAFE_FIT_RESOLVE_UNKNOWNS', 'Jev fit scoring', 'Ask the main model about requirements Jev is unsure of',
            default='false', help='Off by default: it made scores less consistent in testing.'),
    _switch('JOB_AGENT_JOB_API_FIRST', 'Job sources', 'Search company job boards first', default='1', values=OFF),
    _switch('JOB_AGENT_TREG_ENABLED', 'Job sources', 'Treg paid search', default='0', values=OFF,
            requires=('TREG_TOKEN',), help='Spends money on your Treg account, within the configured caps.'),
    _switch('TREG_TOKEN', 'Job sources', 'Treg token', 'secret'),
    _switch('JOB_AGENT_TREG_SEARCH_PROVIDER', 'Job sources', 'Treg LinkedIn search provider', 'choice',
            options=('', 'anyapi', 'crawl4ai'), help='None refuses paid LinkedIn search.'),
    _switch('JOB_AGENT_JOBSPY_PYTHON', 'Job sources', 'JobSpy Python interpreter', 'path', process=True,
            help='The separate Python that has python-jobspy installed. Empty turns JobSpy off.'),
    _switch('JOB_AGENT_JOBSPY_SITES', 'Job sources', 'JobSpy sites', 'choice', default='indeed', process=True,
            options=('indeed', 'linkedin', 'indeed,linkedin')),
    _switch('JOB_AGENT_BROWSER_DRIVER', 'Applications', 'Browser driver', 'choice', default='playwright', process=True,
            options=('playwright', 'patchright'), help='Patchright is optional and must be installed.'),
    _switch('JOB_AGENT_HIDDEN_BROWSER', 'Applications', 'Background browser', 'choice', default='offscreen',
            process=True, options=('offscreen', 'headless')),
    _switch('JOB_AGENT_TASK_MEMORY_MODE', 'Applications', 'Task memory', 'choice', default='off', process=True,
            restart=True, options=('off', 'shadow', 'on'), help='Recovery summaries for long applications.'),
    _switch('JOB_AGENT_ANALYSIS_WARMUP', 'Resume and materials', 'Analyse an uploaded resume right away',
            default='1', values=OFF, process=True, help='Uses model tokens when a resume is uploaded.'),
    _switch('JOB_AGENT_OPT_MATERIAL_PATCHES', 'Resume and materials', 'Revise cover letters paragraph by paragraph',
            default='0', values=OFF, process=True),
]
SWITCH = {item['key']: item for item in SWITCHES}
# Values this process copied from `.env` into its environment; anything else there is the operator's.
_from_file: dict[str, str] = {}
_write_lock = Lock()


class SettingsLocked(ValueError):
    """The field is set in the server's environment, which overrides `.env`."""


class ModelListError(RuntimeError):
    """The provider's model list could not be read; the message is safe to show."""


def _file_values(env_path: Path) -> dict[str, str]:
    return {k: v for k, v in dotenv_values(env_path).items() if v is not None} if Path(env_path).is_file() else {}


def _key_status(environ: Mapping[str, str], file: Mapping[str, str]) -> dict[str, Any]:
    for source, values, key in (('environment', environ, 'LLM_API_KEY'), ('file', file, 'LLM_API_KEY'),
                                ('environment', environ, 'OPENAI_API_KEY'), ('file', file, 'OPENAI_API_KEY')):
        value = optional_setting(values, key)
        if value:
            return {'set': True, 'source': source, 'variable': key, 'hint': value[-4:] if len(value) >= 12 else ''}
    return {'set': False, 'source': None, 'variable': None, 'hint': ''}


def read_settings(*, env_path: Path = DEFAULT_ENV_PATH, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    environ = os.environ if environ is None else environ
    file = _file_values(env_path)
    result: dict[str, Any] = {}
    for field, key in FIELDS.items():
        if field != 'api_key':
            result[field] = optional_setting(environ, key) or optional_setting(file, key) or ''
    result['api_key'] = _key_status(environ, file)
    result['locked'] = [field for field, key in FIELDS.items() if optional_setting(environ, key)]
    result['configured'] = bool(result['model_id'] and result['api_key']['set'])
    result['features'] = [_feature(item, environ, file) for item in SWITCHES]
    return result


def _locked(item, environ):
    value = environ.get(item['key'])
    if value is None or (item['process'] and _from_file.get(item['key']) == value):
        return False
    # Treg settings honour an explicitly empty environment value too.
    return bool(str(value).strip()) or item['key'].startswith(('TREG', 'JOB_AGENT_TREG', 'JOB_AGENT_JOB_API'))


def _raw(item, environ, file):
    key = item['key']
    if _locked(item, environ) or (item['process'] and key in _from_file):
        return str(environ.get(key) or '').strip()
    return str(file.get(key) or '').strip()


def _feature(item, environ, file):
    raw = _raw(item, environ, file)
    view = {k: item[k] for k in ('key', 'group', 'label', 'kind', 'help')}
    view.update(locked=_locked(item, environ), options=list(item['options']),
                applies='after restart' if item['restart'] else 'now')
    if item['kind'] == 'secret':
        view['set'], view['hint'] = bool(raw), raw[-4:] if len(raw) >= 12 else ''
    elif item['kind'] == 'bool':
        view['value'] = (raw or item['default']).lower() in TRUE
    else:
        view['value'] = raw or item['default']
    return view


def _feature_value(item, value):
    if item['kind'] == 'bool':
        if not isinstance(value, bool):
            raise ValueError(f"{item['label']}: choose on or off.")
        return item['values'][0] if value else item['values'][1]
    if not isinstance(value, str):
        raise ValueError(f"{item['label']}: enter text.")
    value = value.strip()
    if item['kind'] == 'choice' and value not in item['options']:
        raise ValueError(f"{item['label']}: choose one of {', '.join(o or 'none' for o in item['options'])}.")
    if item['kind'] == 'secret' and value and (len(value) > 500 or not _TOKEN.fullmatch(value)):
        raise ValueError(f"{item['label']} cannot contain spaces, quotes, a backslash or '#'.")
    if item['kind'] == 'path' and value and (len(value) > 400 or any(c in value for c in '"\'#\r\n')
                                             or not Path(value).is_file()):
        raise ValueError(f"{item['label']}: no file at that path.")
    return value


def load_process_switches(*, env_path: Path = DEFAULT_ENV_PATH, environ: MutableMapping[str, str] | None = None) -> None:
    """At server start, give process-environment switches their saved `.env` values (the environment still wins)."""
    environ = os.environ if environ is None else environ
    file = _file_values(env_path)
    for item in SWITCHES:
        key = item['key']
        if item['process'] and key not in environ and str(file.get(key) or '').strip():
            environ[key] = _from_file[key] = str(file[key]).strip()


def _clean(field: str, value: str) -> str:
    value = value.strip()
    if not value:
        return ''
    if len(value) > (500 if field == 'api_key' else 300) or not _TOKEN.fullmatch(value):
        label = 'API key' if field == 'api_key' else 'Endpoint' if field == 'base_url' else 'Model name'
        raise ValueError(f'{label} cannot contain spaces, quotes, a backslash or "#".')
    if field == 'base_url':
        parts = urlsplit(value)
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            raise ValueError('Endpoint must be an http or https URL without a user name or password.')
    return value


def save_settings(changes: Mapping[str, Any], *, env_path: Path = DEFAULT_ENV_PATH,
                  environ: MutableMapping[str, str] | None = None) -> dict[str, Any]:
    """Apply changes: None leaves a field unchanged, '' removes it from `.env`.
    `features` maps a switch key to its new value: a bool for an on/off switch, text otherwise."""
    environ = os.environ if environ is None else environ
    updates: dict[str, str | None] = {}
    for key, value in (changes.get('features') or {}).items():
        item = SWITCH.get(key)
        if item is None:
            raise ValueError(f'{key} is not a setting this page can change.')
        if value is None:
            continue
        if _locked(item, environ):
            raise SettingsLocked(f'{key} is set in the server environment, which overrides this setting. '
                                 'Change it there and restart the server.')
        cleaned = _feature_value(item, value)
        updates[key] = cleaned if cleaned != '' else None
    _check_requirements(updates, env_path, environ)
    for field, value in changes.items():
        if field not in FIELDS or value is None:
            continue
        key = FIELDS[field]
        if optional_setting(environ, key):
            raise SettingsLocked(f'{key} is set in the server environment, which overrides this setting. '
                                 'Change it there and restart the server.')
        cleaned = _clean(field, value)
        if field == 'model_id' and not cleaned:
            raise ValueError('Choose a model.')
        updates[key] = cleaned or None
    if updates:
        write_env(Path(env_path), updates)
        for key, value in updates.items():
            item = SWITCH.get(key)
            if item and item['process']:
                # Read from the process environment on each use: apply now, not only after a restart.
                if value is None:
                    environ.pop(key, None)
                    _from_file.pop(key, None)
                else:
                    environ[key] = _from_file[key] = value
    return read_settings(env_path=env_path, environ=environ)


def _check_requirements(updates, env_path, environ):
    """A switch that needs a key or another switch cannot be turned on without it."""
    file = _file_values(env_path)

    def value(key):
        return (updates[key] or '') if key in updates else _raw(SWITCH[key], environ, file)

    def on(key):
        item = SWITCH[key]
        return value(key).lower() in TRUE if item['kind'] == 'bool' else bool(value(key))

    for key, item in SWITCH.items():
        if item['requires'] and on(key) and any(k in updates for k in (key, *item['requires'])):
            for needed in item['requires']:
                if not on(needed):
                    raise ValueError(f"{item['label']} needs {SWITCH[needed]['label']}.")


def write_env(path: Path, updates: Mapping[str, str | None]) -> None:
    """Replace, add or remove `KEY=value` lines, keeping every other line, comment and line ending."""
    with _write_lock:
        text = path.read_bytes().decode('utf-8-sig') if path.is_file() else ''
        newline = '\r\n' if '\r\n' in text else '\n'
        lines, seen = [], set()
        for line in text.splitlines():
            match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            key = match.group(1) if match else None
            if key in updates:
                if key not in seen and updates[key] is not None:
                    lines.append(f'{key}={updates[key]}')
                seen.add(key)
                continue
            lines.append(line)
        lines += [f'{key}={value}' for key, value in updates.items() if key not in seen and value is not None]
        content = newline.join(lines) + (newline if lines else '')
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix='.env.', suffix='.tmp')
        try:
            with os.fdopen(handle, 'w', encoding='utf-8', newline='') as stream:
                stream.write(content)
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def list_models(*, api_key: str | None = None, base_url: str | None = None, env_path: Path = DEFAULT_ENV_PATH,
                environ: Mapping[str, str] | None = None, transport: httpx.BaseTransport | None = None) -> list[str]:
    """Read the provider's model list (an OpenAI-compatible GET /models; no model is called)."""
    environ = os.environ if environ is None else environ
    settings = {**_file_values(env_path), **environ}
    key = _clean('api_key', api_key or '') or optional_setting(settings, 'LLM_API_KEY') or optional_setting(settings, 'OPENAI_API_KEY')
    if not key:
        raise ModelListError('Enter an API key first.')
    base = (_clean('base_url', base_url or '') or optional_setting(settings, 'LLM_BASE_URL')
            or optional_setting(settings, 'OPENAI_BASE_URL') or optional_setting(settings, 'OPENAI_API_BASE') or _DEFAULT_BASE_URL)
    host = urlsplit(base).hostname or base
    try:
        with httpx.Client(timeout=10.0, transport=transport, follow_redirects=False) as client:
            response = client.get(base.rstrip('/') + '/models', headers={'Authorization': f'Bearer {key}'})
    except httpx.HTTPError as exc:
        raise ModelListError(f'Could not reach {host}.') from exc
    if response.status_code in (401, 403):
        raise ModelListError('The provider did not accept this API key.')
    if response.status_code != 200:
        raise ModelListError(f'{host} returned HTTP {response.status_code} for its model list.')
    try:
        rows = response.json().get('data', [])
        models = sorted({str(row['id']) for row in rows if isinstance(row, dict) and row.get('id')})
    except (ValueError, AttributeError) as exc:
        raise ModelListError(f'{host} did not return a model list.') from exc
    return models[:500]
