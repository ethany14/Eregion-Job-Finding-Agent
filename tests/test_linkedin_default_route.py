import json
from pathlib import Path
import pytest
from eregion.runtime.career.linkedin_source import search_cards, LinkedInSourceStopped
from eregion.runtime.mcp.config import load_mcp_runtime_config
from test_career_flow import career,execute
from test_integrated_p3 import Discovery,ask
from test_linkedin_mcp_t0 import FakeLinkedIn,manager_with,result,DETAIL,SEARCH


def test_real_cards_are_bound_and_ambiguous_fields_stay_unknown():
    payload=json.loads((Path(__file__).parent/'fixtures/linkedin-mcp-search.json').read_text())
    cards=search_cards(payload)
    assert cards['4460477929']=={'title':'Junior Data Analyst','company':'Calibrate','location':'United States (Remote)'}
    payload['sections']['search_results']='Junior Data Analyst\nPromoted\nNew York, NY'
    assert search_cards(payload)['4460477929']['company'] is None
    payload['references']['search_results'][1]['url']='https://evil.example/jobs/view/4460477929/'
    assert '4460477929' not in search_cards(payload)


def setup(c,monkeypatch,script):
    monkeypatch.setenv('JOB_AGENT_JOB_API_FIRST','1')
    c.harness.discovery=Discovery(directory=[{'url':'https://boards.greenhouse.io/x/jobs/1','title':'Junior Data Analyst'}])
    fake=FakeLinkedIn(script);manager=manager_with(fake);c.runtime.mcp_manager=manager
    return fake,manager


def test_default_combines_sources_reuses_reads_and_never_spends(career,monkeypatch):
    c=career
    search={**SEARCH,'references':{'search_results':[{'kind':'job','url':'/jobs/view/4475055219/','text':'Junior Data Analyst'}]},
        'sections':{'search_results':'Junior Data Analyst\nExample\nNew York, NY'}}
    fake,manager=setup(c,monkeypatch,{'search_jobs':result(search),'get_job_details':result(DETAIL)})
    try:
        ask(c,'Find junior data analyst jobs in New York')
        found=execute(c,'search_jobs',query='junior data analyst',location='New York')
        assert any(j['source']=='linkedin_mcp' for j in found['jobs'] if 'source' in j)
        assert c.harness.discovery.calls[0][0]=='directory'
        url=DETAIL['url'];first=execute(c,'read_job',url=url);second=execute(c,'read_job',url=url)
        assert first['source']=='linkedin_mcp' and first['title']=='Junior Data Analyst'
        assert second['cached'] and second['no_new_evidence']
        assert [name for name,_,_ in fake.calls].count('get_job_details')==1
        assert c.repository.get(c.state['task_id'])['linkedin_mcp_state']['calls_used']==2
    finally:manager.stop()


def test_auth_stop_persists_and_does_not_use_browser(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{'search_jobs':result(error='Sign in required')})
    try:
        ask(c,'Only search LinkedIn for junior data analyst jobs')
        a=execute(c,'search_jobs',query='junior data analyst');b=execute(c,'search_jobs',query='data analyst')
        assert a['error_code']==b['error_code']=='auth_required' and len(fake.calls)==1
        assert c.harness.discovery.calls==[]
    finally:manager.stop()


def test_cancelled_task_never_dispatches_mcp(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{})
    try:
        ask(c,'Only search LinkedIn for jobs');c.repository.change(c.state['task_id'],status='cancelled')
        with pytest.raises(LinkedInSourceStopped):execute(c,'search_jobs',query='data analyst')
        assert not fake.calls
    finally:manager.stop()


def test_config_file_can_preserve_large_detail_evidence():
    config=load_mcp_runtime_config((Path(__file__).resolve().parents[1]/'docs/examples/linkedin-mcp.json'))
    assert config.servers[0].max_result_bytes==2000000


def test_pending_call_after_restart_requires_recovery(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{})
    try:
        ask(c,'Only search LinkedIn for jobs')
        c.repository.change(c.state['task_id'],linkedin_mcp_state={'calls_used':1,'stopped':None,'log':[{'outcome':'pending'}]})
        found=execute(c,'search_jobs',query='data analyst')
        assert found['error_code']=='pending_call_requires_recovery' and not fake.calls
        assert c.repository.get(c.state['task_id'])['linkedin_mcp_state']['calls_used']==1
    finally:manager.stop()


def test_explicit_empty_mcp_path_never_reads_project_env(monkeypatch):
    monkeypatch.setenv('MCP_CONFIG_PATH','')
    assert not load_mcp_runtime_config().servers


def test_mcp_no_linkedin_restriction_uses_only_boards(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{})
    try:
        ask(c,'Find data analyst jobs, no LinkedIn please')
        found=execute(c,'search_jobs',query='data analyst')
        assert found['jobs'] and not fake.calls
    finally:manager.stop()


def test_stopped_mcp_cannot_be_bypassed_when_manager_disappears(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{'search_jobs':result(error='Sign in required')})
    try:
        ask(c,'Only search LinkedIn for jobs')
        assert execute(c,'search_jobs',query='analyst')['error_code']=='auth_required'
        c.runtime.mcp_manager=None
        assert execute(c,'search_jobs',query='other analyst')['error_code']=='auth_required'
        with pytest.raises(ValueError,match='source is stopped'):
            execute(c,'read_job',url=DETAIL['url'])
    finally:manager.stop()


def test_larger_candidate_request_expands_pages_once(career,monkeypatch):
    c=career;fake,manager=setup(c,monkeypatch,{'search_jobs':result(SEARCH)})
    try:
        ask(c,'Only search LinkedIn for jobs')
        execute(c,'search_jobs',query='analyst',limit=10)
        execute(c,'search_jobs',query='analyst',limit=20)
        again=execute(c,'search_jobs',query='analyst',limit=15)
        assert [args['max_pages'] for _,args,_ in fake.calls]==[1,2] and again['cached']
    finally:manager.stop()
