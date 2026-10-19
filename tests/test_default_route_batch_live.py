"""Explicit live default-route acceptance; no applications or schedules.

New runs use tiered-recommendations-v4 (stop is the model's choice; the reply states what remains and offers to continue): no minimum count; counts are reported.
Historical runs keep their recorded version and verdict."""
import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from test_career_flow import career

# Acceptance version for new runs. Historical runs keep their recorded version and result.
ACCEPTANCE_VERSION = os.getenv('JOB_AGENT_LIVE_ACCEPTANCE', 'tiered-recommendations-v7')
LEGACY_GOAL = ('Find 15 current junior data analyst positions in New York for my saved resume. '
        'Aim for at least 10 verified, scored, qualifying recommendations. Use LinkedIn MCP '
        'and public company job-board APIs first. Keep the role, seniority and location '
        'constraints; report insufficient results honestly. Include each recommended job URL '
        'and match score. Do not apply, generate materials, or create a schedule.')
# The user's changed request (tiered-recommendations-v1): relevant opportunities with gaps and
# items to confirm stated, not only fully confirmed ones; constraints unchanged.
TIERED_GOAL = ('Find 15 current junior data analyst positions in New York for my saved resume. '
        'Aim for at least 10 real, relevant opportunities: strong matches, roles worth trying with '
        'their gaps stated, and roles where I must confirm something such as GPA or work status. '
        'Use LinkedIn MCP and public company job-board APIs first. Keep the role, seniority and '
        'location constraints; report insufficient results honestly. Include each job URL and match '
        'score. Do not apply, generate materials, or create a schedule.')
# tiered-recommendations-v2: the same request without a count target (2026-10-13).
def open_goal(search):
    return OPEN_GOAL.replace('junior data analyst positions in New York', search)


OPEN_GOAL = ('Find current junior data analyst positions in New York for my saved resume. Show real, '
        'relevant opportunities: strong matches, roles worth trying with their gaps stated, and roles '
        'where I must confirm something such as GPA or work status. Use LinkedIn MCP and public company '
        'job-board APIs first. Keep the role, seniority and location constraints; report honestly what '
        'was searched and why results are few if they are. Include each job URL and match score. '
        'Do not apply, generate materials, or create a schedule.')
# Live-readiness scenario set (spec section 9): different applicants, roles, levels and places.
# 'analyst' is the request every earlier run used, so its history stays comparable.
SCENARIOS = {
    'analyst': {'applicant': 'analyst', 'search': 'junior data analyst positions in New York'},
    'backend': {'applicant': 'backend', 'search': 'senior backend engineer positions in Remote US'},
    'student': {'applicant': 'student', 'search': 'machine learning internship positions in California'},
    'designer': {'applicant': 'designer', 'search': 'junior UX designer positions in Austin, TX'},
}
SCENARIO = os.getenv('JOB_AGENT_LIVE_SCENARIO', 'analyst')
OPEN_VERSIONS = {'tiered-recommendations-v2', 'tiered-recommendations-v3', 'tiered-recommendations-v4', 'tiered-recommendations-v5', 'tiered-recommendations-v6', 'tiered-recommendations-v7'}
GOAL = {'tiered-recommendations-v7': open_goal(SCENARIOS[SCENARIO]['search']), 'tiered-recommendations-v6': open_goal(SCENARIOS[SCENARIO]['search']), 'tiered-recommendations-v5': open_goal(SCENARIOS[SCENARIO]['search']), 'tiered-recommendations-v4': OPEN_GOAL, 'tiered-recommendations-v3': OPEN_GOAL, 'tiered-recommendations-v2': OPEN_GOAL, 'tiered-recommendations-v1': TIERED_GOAL}.get(ACCEPTANCE_VERSION, LEGACY_GOAL)
FLAGS = {'JOB_AGENT_JOB_API_FIRST': '1', 'JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS': '60',
         'JOB_AGENT_TASK_MAX_INPUT_TOKENS': '1000000'}
# Explicit experiment profiles. "expanded" is the user-approved one-off experiment
# (2026-10-13): LinkedIn 100, provider 100, input 1.5M, 1500 s; audit budget unchanged.
PROFILES = {
    'baseline': {'provider_attempts': 60, 'input_tokens': 1_000_000, 'deadline_seconds': 900, 'linkedin_calls': 50},
    'expanded': {'provider_attempts': 100, 'input_tokens': 1_500_000, 'deadline_seconds': 1500, 'linkedin_calls': 100},
}

@pytest.mark.skipif(os.getenv('JOB_AGENT_LIVE_DEFAULT_BATCH') != '1', reason='Explicit paid live opt-in')
def test_default_route_search(career, monkeypatch):
    from eregion.runtime.career.discovery import JobDiscovery
    from eregion.runtime.career.service import CareerService
    from eregion.runtime.career.external_tool_usage import ExternalToolLedger
    from eregion.runtime.mcp.config import load_mcp_runtime_config
    from eregion.runtime.mcp.manager import McpToolManager
    from eregion.runtime.registry import ToolRegistry
    from eregion.api.services.fit_analysis_service import FitAnalysisService
    from eregion.domain.jev_fit.settings import Settings
    from eregion.domain.pdf_rendering import render_letter_pdf
    from scripts.e2e_repair_evaluation import APPLICANTS
    from test_remaining_live import settle_live

    output = Path(os.environ['OPT_LIVE_OUTPUT'])
    assert not output.exists(), 'Never overwrite or automatically repeat a live sample'
    profile_name = os.getenv('JOB_AGENT_LIVE_LIMITS', 'baseline')
    limits = PROFILES[profile_name]
    flags = {**FLAGS, 'JOB_AGENT_TASK_MAX_PROVIDER_ATTEMPTS': str(limits['provider_attempts']),
             'JOB_AGENT_TASK_MAX_INPUT_TOKENS': str(limits['input_tokens']),
             'JOB_AGENT_LINKEDIN_MAX_CALLS': str(limits['linkedin_calls'])}
    output.mkdir(parents=True)
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    config = load_mcp_runtime_config()
    assert len(config.servers) == 1 and config.servers[0].server_id == 'linkedin'
    allowed = {'search_jobs', 'get_job_details', 'close_session'}
    assert config.servers[0].tool_allowlist == allowed
    manager = McpToolManager(config=config, registry=ToolRegistry())
    c = career
    from eregion.runtime.career import qualification
    from eregion.domain.search_qualification import judge
    monkeypatch.setattr(qualification,'judge',judge)  # Explicit live opt-in replaces the fixture's scripted IO.
    c.runtime.mcp_manager = manager
    c.repository.change(c.state['task_id'], status='cancelled', pause_requested=True)
    c.applications.repository.save_candidate(
        {'first_name':'Synthetic','last_name':SCENARIOS[SCENARIO]['applicant'],'email':SCENARIOS[SCENARIO]['applicant']+'@example.com'},
        resume=render_letter_pdf(APPLICANTS[SCENARIOS[SCENARIO]['applicant']]), name='synthetic.pdf', extracted=True)
    c.harness.discovery = JobDiscovery()
    c.harness.analysis._fit_service = FitAnalysisService(jev_enabled=True,
        jev_config=replace(Settings.load(cache_path=output/'fit.sqlite3'), resolve_unknowns=False))
    service = CareerService(c.runtime, c.applications, harness=c.harness, start_alert_scheduler=False)
    service.loop.budget = 60
    task = None
    (output/'invocation.json').write_text(json.dumps({
        'head':subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip(),
        'goal':GOAL, 'scenario':SCENARIO, 'acceptance_version':ACCEPTANCE_VERSION, 'flags':flags, 'limits_profile':profile_name, 'limits':limits,
        **({} if ACCEPTANCE_VERSION in OPEN_VERSIONS else {'minimum_qualified':10, 'target':15}),
        'mcp_call_limit':limits['linkedin_calls'], 'deadline_seconds':limits['deadline_seconds'], 'synthetic_candidate':True,
        'audit_budget':{'provider_attempts':6,'input_tokens':200000},
        'cost_usd':None, 'cost_note':'Provider billing unavailable; attempt and token caps enforced.'
    }, indent=2), encoding='utf-8')
    try:
        manager.start()
        assert 'mcp__linkedin__search_jobs' in manager.public_tool_names
        task = service.start(GOAL, str(uuid4()))
        try:
            settle_live(service, task['run_id'], output, SCENARIOS[SCENARIO]['applicant'], timeout=limits['deadline_seconds'],semantic_review=True,
                        limits={'input_tokens':limits['input_tokens']}, acceptance_version=ACCEPTANCE_VERSION)
        except AssertionError:
            if not (output/'acceptance.json').exists():
                raise
        acceptance = json.loads((output/'acceptance.json').read_text(encoding='utf-8'))
        if ACCEPTANCE_VERSION not in OPEN_VERSIONS:
            # Historical versions only; v2 has no count threshold.
            if acceptance['qualified_count'] < 10:
                acceptance['failures'].append('fewer_than_ten_verified_scored_qualifying_recommendations')
            acceptance['limits']['minimum_qualified'] = 10
        acceptance['status'] = 'failed' if acceptance['failures'] else 'passed'
        (output/'batch-acceptance.json').write_text(json.dumps(acceptance,indent=2),encoding='utf-8')
        assert acceptance['status'] == 'passed', acceptance
    finally:
        if task:
            from sqlalchemy import select
            from eregion.runtime.provider_usage import ProviderUsageRow
            with c.repository.sessions() as db:
                rows = db.scalars(select(ProviderUsageRow).where(ProviderUsageRow.task_id == task['run_id']))
                (output/'provider-ledger.json').write_text(json.dumps(
                    [json.loads(row.details_json) for row in rows], indent=2), encoding='utf-8')
            state = service.repository.get(task['run_id'])
            ledger = ExternalToolLedger(c.repository.sessions).summary(task['run_id'])
            (output/'sources.json').write_text(json.dumps({
                'linkedin':state.get('linkedin_mcp_state'), 'external_tools':ledger},indent=2,default=str),encoding='utf-8')
            # Preserve the isolated test DB before pytest retention removes it.
            import sqlite3
            source=Path(c.repository.sessions.kw['bind'].url.database)
            with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as original, sqlite3.connect(output/'career.sqlite3') as saved:
                original.backup(saved)
        service.close()
        try:
            manager.call_tool('linkedin','close_session',{},timeout_seconds=15)
        except Exception:
            pass
        manager.stop()
