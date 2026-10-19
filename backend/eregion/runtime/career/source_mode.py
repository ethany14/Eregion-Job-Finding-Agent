"""One source-order decision for the career prompt, snapshot and tool descriptions.

JOB_AGENT_JOB_API_FIRST selects the order (default on). When it is off, every
output here is the unchanged browser-first text, byte for byte.
"""
import os

from eregion.paths import PROJECT_ROOT

from .prompt import SYSTEM, TRIMMED_SYSTEM


def api_first_enabled(environ=None, env_path=PROJECT_ROOT / '.env'):
    """JOB_AGENT_JOB_API_FIRST from the environment (an explicit empty value counts), else `.env`, else on."""
    environment = os.environ if environ is None else environ
    if 'JOB_AGENT_JOB_API_FIRST' in environment:
        value = environment['JOB_AGENT_JOB_API_FIRST']
    else:
        from dotenv import dotenv_values
        try:
            value = dotenv_values(env_path).get('JOB_AGENT_JOB_API_FIRST') if env_path.is_file() else None
        except OSError:
            value = None
        value = '1' if value is None else value
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

# (browser-first text, API-first text); each must occur exactly once in its prompt.
FULL_PROMPT_EDITS = (
    ('Prefer browser search on real job websites. For LinkedIn,\n'
     '  start at https://www.linkedin.com/jobs/search/ and operate its search/filter controls.\n'
     '  It is also a useful first source for broad searches; select other sites for the role.',
     'Start with search_jobs, which searches verified company-board\n'
     '  APIs and configured LinkedIn MCP (no API key). A LinkedIn-only request uses the configured\n'
     '  LinkedIn source, never other boards. Use the browser only for an explicit page check\n'
     '  or missing/conflicting evidence; an already-open page does not require a browser read.'),
    ('- search_jobs is a supplementary HTTP/API path when browsing fails, broader coverage helps\n'
     '  or the user requests it.',
     '- search_jobs is the primary discovery path; browsing supplements it for named sites,\n'
     '  explicit page checks or missing evidence.'),
    ('same browser-first approach', 'same API-first approach'),
)
TRIMMED_PROMPT_EDITS = (
    ('Prefer real job websites; LinkedIn starts at https://www.linkedin.com/jobs/search/ '
     'with its controls, and is useful for broad searches.',
     'Start with search_jobs (verified board APIs plus configured LinkedIn MCP); a LinkedIn-only '
     'request uses the configured LinkedIn source. Browser only for explicit page checks or '
     'missing/conflicting evidence.'),
    ('search_jobs supplements browsing when it fails, coverage helps or requested.',
     'search_jobs is the primary discovery path; browsing supplements it.'),
    ('unattended browser-first research', 'unattended API-first research'),
)
TOOL_DESCRIPTIONS = {
    'search_jobs': 'Primary discovery: search verified company-board APIs (the board directory by '
        'default) and configured LinkedIn MCP, without a paid key. A LinkedIn-only request uses LinkedIn only. Returns observed '
        'posting links; read, qualify, import, score and recommend them. Optional boards contain '
        'platform and company board token.',
    'read_job': 'Read a posting by URL through the job API/HTTP first; the browser is used only with '
        'an observed region_ref or when API text is missing. On a split results/detail page supply '
        'region_ref and observation_id to exclude other jobs.',
}
SNAPSHOT_INSTRUCTION = (
    'Search verified company-board APIs and configured LinkedIn MCP with search_jobs. '
    'A LinkedIn-only request uses the configured LinkedIn source, never other boards. Use the browser '
    'only for an explicit page check or missing/conflicting evidence; an already-open page does not '
    'require a browser read.')


def prompt_for(prompt, api_first):
    """API-first wording for the shipped prompts; any other prompt is returned unchanged."""
    if not api_first:
        return prompt
    edits = FULL_PROMPT_EDITS if prompt == SYSTEM else TRIMMED_PROMPT_EDITS if prompt == TRIMMED_SYSTEM else ()
    for old, new in edits:
        prompt = prompt.replace(old, new)
    return prompt


def tool_descriptions(api_first):
    return dict(TOOL_DESCRIPTIONS) if api_first else {}


def snapshot_policy(api_first, source_restriction):
    if not api_first:
        return {}
    return {'source_policy': {'api_first': True, 'source_restriction': source_restriction,
                              'instruction': SNAPSHOT_INSTRUCTION}}
