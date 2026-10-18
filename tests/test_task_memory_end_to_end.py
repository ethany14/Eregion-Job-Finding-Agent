"""Real CareerHarness + TaskLoop with a scripted model; no paid evaluation."""
import json
from types import SimpleNamespace as NS
from eregion.runtime.memory.policy import LocalOwnerResolver
from eregion.runtime.memory.access import MemoryAccessResolver
from eregion.runtime.memory.repository import MemoryRepository
from eregion.runtime.career.repository import TaskRepository
from eregion.runtime.task_memory.service import TaskMemoryService
from eregion.runtime.task_memory.types import TaskMemoryConfig
from eregion.runtime.tasks.loop import TaskLoop
from test_career_flow import career, prepare, ToolModel, settled


def install(c, app, mode='on'):
    c.runtime.owner_resolver=LocalOwnerResolver()
    c.runtime.memory_access=MemoryAccessResolver(c.runtime.owner_resolver)
    c.runtime.memories=MemoryRepository(c.runtime.database.session_factory)
    service=TaskMemoryService(c.runtime,TaskMemoryConfig(mode,frozenset({app})))
    c.runtime.task_memory=service
    c.repository=TaskRepository(c.runtime.database.session_factory,task_memory=service.repository)
    c.harness.repository=c.repository
    return service


def test_actual_loop_recovers_cross_conversation_sources_and_material_versions(career):
    c=career; job,pack=prepare(c); app=job['application_id']; install(c,app)
    prior,_=c.repository.create('Keep the original project names.','prior',context={'application_id':app})
    for n in range(151):c.repository.append(prior['task_id'],role='assistant',text=f'Progress {n}')
    c.repository.change(prior['task_id'], status='completed')
    c.repository.change(c.state['task_id'], status='completed')
    # Reconstruct the service/repository from the same durable DB, as after a restart.
    install(c,app)
    observed=[]
    def decide(snapshot):
        observed.append(snapshot)
        memory=snapshot['task_memory']
        assert memory['application_id']==app
        assert any(e.get('text')=='Keep the original project names.' for e in memory['entries'])
        assert any(e['kind']=='material_item' for e in memory['entries'])
        assert snapshot['current_request']['message']['text']=='What was my earlier instruction?'
        return 'finish_task', {'text':'Your earlier instruction was to keep the original project names.'}
    model=ToolModel(decide)
    loop=TaskLoop(c.repository,c.harness,model_factory=lambda:model)
    try:
        state=loop.start('What was my earlier instruction?','resume',context={'application_id':app})
        done=settled(c.repository,state['task_id'])
        assert done['status']=='completed' and model.calls==1
        assert [h['tool'] for h in done['history']]==['finish_task']
        assert observed
    finally:loop.close()


def test_shadow_and_off_do_not_inject_into_real_harness(career):
    c=career
    app=c.runtime.workspace.save_workspace(cleaned_job_description='Python required',
        source_url='https://example.com/job',title='Engineer').application.application_id
    svc=install(c,app,'shadow')
    state,_=c.repository.create('Explain this job','shadow',context={'application_id':app})
    assert 'task_memory' not in c.harness.observe(state)
    assert svc.repository.version(app)>0
    c.runtime.task_memory=TaskMemoryService(c.runtime,TaskMemoryConfig())
    assert 'task_memory' not in c.harness.observe(state)
