"""The same research tools serve chat requests and durable daily triggers."""
from datetime import UTC, datetime
from uuid import uuid4
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient

from eregion.runtime.career.alerts import AlertConfig, AlertRepository, next_due, job_key
from eregion.runtime.career.alert_scheduler import AlertScheduler
from eregion.runtime.career.service import CareerService
from eregion.api.main import create_app
from test_auto_apply import database
from test_career_flow import career, ToolModel, settled, execute


def config(**changes):
    return AlertConfig(name='Daily Python roles',preferences='Python engineer, remote US',timezone='America/New_York',**changes).model_dump()


def test_schedule_timezones_dst_and_nonexistent_local_times():
    c=config()
    assert next_due(c,datetime(2026,10,8,12,59,tzinfo=UTC)).isoformat()=='2026-10-08T13:00:00+00:00'
    assert next_due(c,datetime(2026,11,2,12,59,tzinfo=UTC)).isoformat()=='2026-11-02T14:00:00+00:00'
    assert next_due(config(local_time='02:30'),datetime(2026,3,8,5,tzinfo=UTC)).isoformat()=='2026-03-08T07:30:00+00:00'
    assert next_due(config(local_time='01:30'),datetime(2026,11,1,6,tzinfo=UTC)).isoformat()=='2026-11-02T06:30:00+00:00'
    with pytest.raises(ValueError):AlertConfig(**{**config(),'timezone':'US/not-real'})


def test_due_claim_is_durable_idempotent_and_coalesces_missed_days(database):
    clock=[datetime(2026,10,8,12,59,tzinfo=UTC)]
    repo=AlertRepository(database.session_factory,clock=lambda:clock[0])
    alert=repo.save(config(),request_key='daily-test')
    assert repo.claim(alert['alert_id']) is None
    clock[0]=datetime(2026,10,12,15,tzinfo=UTC)
    run=repo.claim(alert['alert_id'])
    assert run['scheduled_for']=='2026-10-12T13:00:00+00:00'
    reloaded=AlertRepository(database.session_factory,clock=lambda:clock[0])
    assert reloaded.claim(alert['alert_id']) is None
    assert len(reloaded.active())==1
    assert reloaded.get(alert['alert_id'])['next_run_at']=='2026-10-13T13:00:00+00:00'
    assert reloaded.claim(alert['alert_id'],request_key='manual')==run
    reloaded.finish(run['run_id'],'completed','No new matches.')
    update=reloaded.get(alert['alert_id'])
    reloaded.save(config(enabled=False),alert_id=alert['alert_id'],expected_version=update['version'])
    clock[0]=datetime(2026,10,14,15,tzinfo=UTC)
    assert reloaded.claim(alert['alert_id']) is None


def test_verified_recommendations_deduplicate_tracking_urls_and_apply_threshold(database):
    repo=AlertRepository(database.session_factory)
    alert=repo.save(config(),request_key='threshold')
    run=repo.claim(alert['alert_id'],request_key='manual')
    payload={'url':'https://jobs.lever.co/company/abc123?source=linkedin','match_score':85,'application_id':'saved-job','reason':'Matches Python experience.'}
    assert repo.add(run['run_id'],payload)['added']
    assert not repo.add(run['run_id'],{**payload,'url':'https://jobs.lever.co/company/abc123/apply'})['added']
    with pytest.raises(ValueError,match='threshold'):repo.add(run['run_id'],{**payload,'url':'https://jobs.lever.co/company/other','match_score':50})
    repo.finish(run['run_id'],'completed','One new match.')
    next_run=repo.claim(alert['alert_id'],request_key='next-manual')
    assert not repo.add(next_run['run_id'],payload)['added']
    assert len(repo.feed()[1]['recommendations'])==1
    repo.mark_read(run['run_id']);assert repo.get_run(run['run_id'])['read_at']
    assert job_key('https://linkedin.com/jobs/search-results/?currentJobId=123&trackingId=x')==job_key('https://www.linkedin.com/jobs/view/title-123')


SEARCH_STEPS=['search_jobs','read_job','import_job','score_job','recommend_job']


def search_decision(s):
    # Progress is the furthest step this task has reached, as a model would read its replayed turns:
    # the snapshot's history is a bounded window that drops its oldest rows near its size budget.
    reached=max((SEARCH_STEPS.index(h['tool']) for h in s['history'] if h['tool'] in SEARCH_STEPS),default=-1)
    url='https://jobs.lever.co/example/abc123'
    if reached<0:return 'search_jobs',{'query':'Python engineer'}
    if reached<1:return 'read_job',{'url':url}
    if reached<2 or not s['jobs']:return 'import_job',{'url':url}
    job=s['jobs'][0]
    if reached<3:return 'score_job',{'application_id':job['application_id']}
    # The scripted posting's role and location are unconfirmed for the request: to_confirm, never priority.
    if reached<4:return 'recommend_job',{'application_id':job['application_id'],'tier':'to_confirm',
        'reason':'Your resume shows Python API experience.','evidence':[job['title']],'gaps':[],'to_confirm':['Role and location fit the request.'],
        'next_steps':['Apply with the saved resume.'],'caveats':'Confirm location and sponsorship.'}
    score=next((r.get('match_score') for r in s.get('recommendations',[])),job.get('match_score'))
    return 'finish_task',{'text':f'Needs your confirmation (1)\n- Python Engineer: {url} Score {score}. To confirm: role and '
                                  'location fit the request. Next step: apply with the saved resume.',
                          'stop_reason':'The single observed posting was processed; no other candidates were observed.'}


def start_service(c,decide=search_decision):
    c.repository.change(c.state['task_id'],status='cancelled',pause_requested=True)
    return CareerService(c.runtime,c.applications,harness=c.harness,model_factory=lambda:ToolModel(decide),start_alert_scheduler=False)


def test_on_demand_chat_search_recommends_without_creating_a_schedule(career):
    c=career;service=start_service(c)
    try:
        task=service.start('Find and recommend jobs for my resume.',str(uuid4()))
        state=settled(service.repository,task['run_id'])
        assert state['status']=='completed'
        assert not service.alerts.list() and not service.alerts.feed()
        view=service.view(task['run_id'])
        assert len(view['recommendations'])==1 and view['recommendations'][0]['match_score']==view['jobs'][0]['match_score']
        assert 'Python API' in view['recommendations'][0]['reason']
        assert 'configure_job_alert' not in [a['tool'] for a in state['actions']]
    finally:service.close()


def test_daily_trigger_uses_same_search_loop_and_creates_one_digest(career):
    c=career;service=start_service(c)
    try:
        alert=service.alerts.save(config(min_score=0),request_key='daily-flow')
        run=service.alert_scheduler.run_now(alert['alert_id'],'manual-trigger')
        settled(service.repository,run['task_id']);service.alert_scheduler.tick()
        digest=service.alerts.feed()[0]
        assert digest['status']=='completed' and len(digest['recommendations'])==1
        assert not c.browsers
        assert service.alert_scheduler.run_now(alert['alert_id'],'manual-trigger')['run_id']==run['run_id']
        assert len(service.alerts.feed())==1
        assert service.view(run['task_id'])['recommendations']
    finally:service.close()


def test_background_scope_does_not_take_away_interactive_capabilities(career):
    c=career;repo=AlertRepository(c.runtime.database.session_factory);c.harness.alerts=repo
    alert=repo.save(config(),request_key='scope');run=repo.claim(alert['alert_id'],request_key='one')
    state,_=c.repository.create('Scheduled research','scope-research',context={'recommendation_run_id':run['run_id']})
    repo.attach(run['run_id'],state['task_id'])
    tools={t['function']['name'] for t in c.harness.available_tools(state)}
    assert 'recommend_job' in tools and 'start_application' not in tools
    with pytest.raises(ValueError,match='research'):
        c.harness.execute(state['task_id'],'open_application_url',{'url':'https://example.com/job'},call_id='blocked')
    state=c.repository.change(state['task_id'],chat_revision=1)
    assert 'start_application' in {t['function']['name'] for t in c.harness.available_tools(state)}


def test_scheduler_recovers_claimed_run_after_restart_without_duplicate_task(career):
    c=career;service=start_service(c)
    try:
        # A crash after claiming a slot, before creating its conversation.
        service.alerts.clock=lambda:datetime(2026,10,9,1,tzinfo=UTC)
        alert=service.alerts.save(config(min_score=0),request_key='restart-search')
        run=service.alerts.claim(alert['alert_id'],request_key='before-restart')
        restored=AlertScheduler(service,start=False,clock=service.alerts.clock)
        restored.tick()
        attached=service.alerts.get_run(run['run_id'])
        assert attached['task_id']
        assert service.repository.metadata(attached['task_id'])['title'].endswith('2026-10-08')
        settled(service.repository,attached['task_id']);restored.tick();restored.tick()
        assert len(service.alerts.feed())==1
        assert service.alerts.feed()[0]['status']=='completed'
        assert len([s for s in service.repository.list() if s.get('initial_context',{}).get('recommendation_run_id')==run['run_id']])==1
    finally:service.close()


def test_recommendation_requires_score_for_current_resume_and_retrieved_posting(career):
    c=career
    url='https://jobs.lever.co/example/abc123'
    execute(c,'read_job',url=url)
    job=execute(c,'import_job',url=url)
    args={'application_id':job['application_id'],'tier':'priority','reason':'Matches Python experience.',
          'evidence':[job['title']],'gaps':[],'to_confirm':[],'next_steps':['Apply.'],'caveats':''}
    with pytest.raises(ValueError,match='current resume'):execute(c,'recommend_job',**args)
    execute(c,'score_job',application_id=job['application_id'])
    saved,text=c.harness.candidate()
    c.harness.candidate=lambda:(saved,text+'\nNew experience: SQL analytics.')
    with pytest.raises(ValueError,match='current resume'):execute(c,'recommend_job',**args)
    execute(c,'score_job',application_id=job['application_id'])
    c.repository.change(c.state['task_id'],retrieved_jobs={})
    with pytest.raises(ValueError,match='Read the current posting') as refused:execute(c,'recommend_job',**args)
    # The refusal names the tool that supplies the missing read, with the posting's URL.
    detail=refused.value.workflow_detail
    assert detail['error_code']=='posting_not_read' and 'read_job' in detail['next'] and url in detail['next']
    execute(c,'read_job',url=url)
    assert execute(c,'recommend_job',**args)['recommended']


def test_chat_can_explicitly_create_and_pause_schedule(career):
    c=career;c.harness.alerts=AlertRepository(c.runtime.database.session_factory)
    c.repository.append(c.state['task_id'],role='user',id='request-schedule',text='Recommend Python jobs every day at 9 AM New York time.')
    alert=execute(c,'configure_job_alert',message_id='request-schedule',config=config())
    assert execute(c,'configure_job_alert',message_id='request-schedule',config=config())['alert_id']==alert['alert_id']
    c.repository.append(c.state['task_id'],role='user',id='request-pause',text='Pause that daily search.')
    paused=execute(c,'set_job_alert_enabled',message_id='request-pause',alert_id=alert['alert_id'],enabled=False)
    assert not paused['enabled']
    with pytest.raises(ValueError,match='actual user'):execute(c,'configure_job_alert',message_id='invented',config=config())


def test_alert_api_configuration_validation_versioning_and_origin(career):
    c=career;service=start_service(c)
    app=create_app(run_service=object(),session_runtime=c.runtime);app.state.auto_apply=c.applications;app.state.career_agent=service
    with TestClient(app,base_url='http://localhost') as client:
        response=client.post('/api/career/alerts',json={'config':config(),'idempotency_key':'api-schedule'})
        assert response.status_code==201
        alert=response.json()
        assert client.get('/api/career/alerts').json()['schedules'][0]['alert_id']==alert['alert_id']
        assert client.put('/api/career/alerts/'+alert['alert_id'],json={'config':config(enabled=False),'expected_version':999}).status_code==409
        assert client.post('/api/career/alerts',json={'config':{**config(),'sites':['https://127.0.0.1/']},'idempotency_key':'private-url'}).status_code==422
        assert client.get('/api/career/alerts',headers={'origin':'https://another.example'}).status_code==403
