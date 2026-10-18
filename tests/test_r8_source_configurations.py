"""R8: credential combinations, offline (no paid call, no install, no login).

What this proves, precisely (review R8-EVIDENCE-001):
- career routing for the two Treg settings (off / on): free boards always, Treg only when
  configured and enabled, no crash without credentials;
- runtime startup for the four combinations with the REAL MCP path: an MCP config file
  loaded from MCP_CONFIG_PATH by the real loader, the real create_session_runtime and
  McpToolManager, and a fake LinkedIn client in place of the upstream process;
- LinkedIn MCP is not wired into career routing (R7 T1/T2 deferred), so the MCP
  combinations route like their Treg counterpart. A real upstream process and account
  are not exercised here.
"""
import json

import pytest

from eregion.runtime.career.linkedin_source import BUSINESS_TOOLS
from test_career_flow import career, execute  # noqa: F401
from test_integrated_p3 import CRAWL_ROWS, Discovery, install_treg, ok
from test_linkedin_mcp_t0 import FakeLinkedIn
from test_remaining_search_completion import prepare

GH = {'url': 'https://job-boards.greenhouse.io/acme/jobs/1', 'title': 'Junior Data Analyst', 'company': 'Acme',
      'location': 'New York, NY'}
NONE = {'TREG_TOKEN': '', 'JOB_AGENT_TREG_ENABLED': '0', 'JOB_AGENT_JOB_API_FIRST': '0'}
TREG = {'TREG_TOKEN': 'fake-test-token', 'JOB_AGENT_TREG_ENABLED': '1', 'JOB_AGENT_JOB_API_FIRST': '1',
        'JOB_AGENT_TREG_SEARCH_PROVIDER': 'crawl4ai'}


@pytest.mark.parametrize('name,env,treg_calls', [('treg_off', NONE, 0), ('treg_on', TREG, 1)])
def test_career_routing_for_each_treg_setting(career, monkeypatch, name, env, treg_calls):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    c = career; prepare(c)
    seen = install_treg(c, lambda r: ok(CRAWL_ROWS)) if env['TREG_TOKEN'] else []
    c.harness.discovery = Discovery(directory=[GH])
    result = execute(c, 'search_jobs', query='junior data analyst', location='New York, NY', use_directory=True)
    assert 'error' not in result
    assert len(seen) == treg_calls
    assert (c.harness._treg() is None) == (treg_calls == 0)
    assert any(j['url'] == GH['url'] for j in result['jobs'])


def _mcp_file(tmp_path):
    path = tmp_path / 'mcp.json'
    path.write_text(json.dumps({'servers': [{
        'server_id': 'linkedin', 'command': 'linkedin-mcp-server', 'args': ['--pinned', '5bdef1c9'],
        'required': False, 'call_timeout_seconds': 120, 'startup_timeout_seconds': 60,
        'tool_allowlist': sorted(BUSINESS_TOOLS), 'read_only_tools': sorted(BUSINESS_TOOLS)}]}), encoding='utf-8')
    return path


@pytest.mark.parametrize('name,env,mcp', [('none', NONE, False), ('treg_only', TREG, False),
                                          ('linkedin_mcp_only', NONE, True), ('both', TREG, True)])
def test_runtime_starts_for_each_combination_through_the_real_mcp_path(tmp_path, monkeypatch, name, env, mcp):
    from eregion.api.session_dependencies import create_session_runtime
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    if mcp:
        monkeypatch.setenv('MCP_CONFIG_PATH', str(_mcp_file(tmp_path)))
    else:
        monkeypatch.delenv('MCP_CONFIG_PATH', raising=False)
    started = []
    runtime = create_session_runtime(migrate_schema=True, database_url=f"sqlite:///{(tmp_path / 'r.db').as_posix()}", model=object(),
                                     mcp_client_factory=lambda config: started.append(config.server_id) or FakeLinkedIn())
    try:
        health = runtime.mcp_health()
        exposed = {n.split('__')[-1] for n in runtime.mcp_manager.public_tool_names}
        if mcp:
            assert started == ['linkedin'] and health.configured_servers == 1 and health.ready_servers == 1
            assert exposed == BUSINESS_TOOLS            # allowlist from the real file config
        else:
            assert started == [] and health.configured_servers == 0 and exposed == set()
    finally:
        runtime.close()
