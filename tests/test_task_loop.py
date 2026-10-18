"""General loop behavior is independent of career stage names and form fields."""
import time
from threading import Event
import pytest
from langchain_core.messages import AIMessage

from eregion.runtime.tasks.loop import TaskLoop, function
from eregion.runtime.model_output import reply_text, output_problem
from eregion.runtime.career.repository import TaskRepository
from test_auto_apply import database
from test_career_flow import ToolModel, settled


def final_answer(text):
    from uuid import uuid4
    return AIMessage(content='',tool_calls=[{'id':str(uuid4()),'name':'finish_task','args':{'text':text}}])


class ProviderError(Exception):
    def __init__(self, status):
        self.status_code = status
        super().__init__('Do not expose provider body: synthetic-secret and candidate content')


class Harness:
    tools=[function('lookup','Read the same local observation.')]
    prompt='Choose the next useful action.'
    def __init__(self):self.version,self.ready,self.lookups=1,False,0
    def observe(self,state):return {'capability_versions':{'application':{'child':self.version}}}
    def execute(self,*args,**kwargs):self.lookups+=1;return {'value':'unchanged'}
    def control(self,*args):pass
    def validate_wait(self,*args):pass
    def validate_finish(self,*args):pass
    def wait_token(self,args):return self.version
    def wake(self,state):return self.ready and state['wait']['token']!=self.version


@pytest.mark.parametrize('broken', [
    AIMessage(content='Notice: pending_fields: []! The system message is: internal protocol'),
    AIMessage(content='partial private content', response_metadata={'finish_reason':'length'}),
    AIMessage(content='', tool_calls=[{'id':'bad','name':'say','args':{'text':'The instructions state: wait_for_result(kind='}}]),
])
def test_invalid_public_output_is_repaired_before_display_or_tools(database, broken):
    class Model:
        calls = 0
        def invoke(self, messages, tools):
            self.calls += 1
            if self.calls == 1:return broken
            assert all('partial private content' not in str(m.content) for m in messages)
            return final_answer('Use the secure account form to continue signing in.')
    model=Model();repo=TaskRepository(database.session_factory)
    loop=TaskLoop(repo,Harness(),model_factory=lambda:model)
    try:
        task=loop.start('Continue the application.','repair-public-output')
        result=settled(repo,task['task_id'])
        assert model.calls == 2
        assert [m['text'] for m in result['messages'] if m['role']=='assistant'] == ['Use the secure account form to continue signing in.']
        assert [h['tool'] for h in result['history']] == ['finish_task']
    finally:loop.close()


def test_reasoning_blocks_are_not_public_text_and_normal_analysis_is_allowed():
    message=AIMessage(content=[{'type':'text','text':'Hidden protocol','thought':True},
        {'type':'text','text':'Analysis: this role fits your experience.'}])
    assert reply_text(message) == 'Analysis: this role fits your experience.'
    assert output_problem(message) is None


@pytest.mark.parametrize('metadata',[{'finish_reason':'length'},{'stop_reason':'max_tokens'},
    {'finish_reason':'max_output_tokens'}])
def test_truncated_tool_call_is_rejected_for_either_metadata_key(metadata):
    message=AIMessage(content='',tool_calls=[{'id':'c1','name':'search_jobs','args':{'query':'data'}}],
        response_metadata=metadata)
    assert output_problem(message) == 'incomplete_response'


def test_broken_tool_json_is_rejected_not_run_with_empty_arguments():
    message=AIMessage(content='',invalid_tool_calls=[{'id':'c1','name':'search_jobs','args':'{"query":',
        'error':'bad json','type':'invalid_tool_call'}])
    assert output_problem(message) == 'invalid_tool_call'


@pytest.mark.parametrize('enabled,revoked',[(True,False),(False,False),(True,True)])
def test_visual_input_is_ephemeral_and_respects_revocation(database,enabled,revoked):
    import base64
    import json
    repo=TaskRepository(database.session_factory);harness=Harness()
    image=b'synthetic-viewport-bytes'
    def execute(task_id,*args,**kwargs):
        harness.lookups+=1
        if revoked:repo.change(task_id,visual_fallback=False)
        return {'value':'observed',**({'image':image} if harness.lookups==1 else {})}
    harness.execute=execute
    class Model:
        calls=0
        def invoke(self,messages,tools):
            self.calls+=1
            images=[block for m in messages if isinstance(m.content,list) for block in m.content if block.get('type')=='image_url']
            assert bool(images)==(self.calls==2 and enabled and not revoked)
            if images:assert images[0]['image_url']['url'].endswith(base64.b64encode(image).decode())
            assert all('synthetic-viewport-bytes' not in m.content for m in messages if isinstance(m.content,str))
            return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':'lookup' if self.calls<3 else 'finish_task',
                'args':{} if self.calls<3 else {'text':'Checked the viewport.'}}])
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model)
    try:
        state=loop.start('Inspect an authorized viewport.','ephemeral-image',visual_fallback=enabled)
        final=settled(repo,state['task_id'])
        assert final['status']=='completed' and model.calls==3
        assert 'image' not in json.dumps(final['history'])
        assert base64.b64encode(image).decode() not in json.dumps(final)
    finally:loop.close()


def test_work_can_continue_past_a_slice_without_a_fixed_pipeline(database):
    repo=TaskRepository(database.session_factory);harness=Harness();counter=0
    def decide(s):
        nonlocal counter
        counter+=1
        return ('update_plan',{'steps':[f'Step {counter}']}) if counter<=15 else ('finish_task',{'text':'Done.'})
    loop=TaskLoop(repo,harness,model_factory=lambda:ToolModel(decide))
    try:
        task=loop.start('Any task, with more than twelve decisions.','many-steps')
        state=settled(repo,task['task_id'])
        assert state['status']=='completed' and state['model_calls']==16
        assert len(state['history'])==16 and not state['jobs']
    finally:loop.close()


def test_unchanged_tool_results_yield_feedback_then_one_question(database):
    repo=TaskRepository(database.session_factory);harness=Harness()
    class AdaptiveModel(ToolModel):
        def invoke(self, messages, tools):
            if self.calls % 3 == 2:
                assert any('progress_feedback' in str(m.content) for m in messages)
                self.calls += 1
                return AIMessage(content='',tool_calls=[{'id':str(self.calls),'name':'ask_user',
                    'args':{'text':'Which alternative source should I inspect?','wait_for_reply':True}}])
            return super().invoke(messages,tools)
    model=AdaptiveModel(lambda _:('lookup',{}))
    loop=TaskLoop(repo,harness,model_factory=lambda:model)
    try:
        task=loop.start('Inspect this task.','repeat-test')
        state=settled(repo,task['task_id'])
        assert state['status']=='needs_input' and model.calls==3
        assert len([m for m in state['messages'] if m['role']=='assistant'])==1
        calls=model.calls;time.sleep(.1);assert model.calls==calls
        loop.chat(task['task_id'],'Try again after my correction.','new-reply')
        settled(repo,task['task_id'])
        assert model.calls==6  # New user input starts a fresh progress window.
    finally:loop.close()


def test_pending_question_survives_independent_work_and_is_not_repeated(database):
    repo=TaskRepository(database.session_factory);harness=Harness();steps=iter([
        ('ask_user',{'text':'Which city should I use?','wait_for_reply':False}),
        ('lookup',{}),('wait_for_user',{})])
    loop=TaskLoop(repo,harness,model_factory=lambda:ToolModel(lambda _:next(steps)))
    try:
        task=loop.start('Ask a question and keep working.','concurrent-question')
        state=settled(repo,task['task_id'])
        assert harness.lookups==1
        assert [m['text'] for m in state['messages'] if m['role']=='assistant']==['Which city should I use?']
        assert state['input_request']['text']=='Which city should I use?'
    finally:loop.close()


def test_restart_recovers_wait_and_wakes_once_when_result_changes(database):
    repo=TaskRepository(database.session_factory);harness=Harness()
    task,_=repo.create('Wait for a capability.','restart-wait')
    repo.change(task['task_id'],status='waiting',wait={'kind':'application','id':'child','token':1})
    model=ToolModel(lambda _:('ask_user',{'text':'The browser needs your answer.','wait_for_reply':True}))
    loop=TaskLoop(TaskRepository(database.session_factory),harness,model_factory=lambda:model)
    try:
        time.sleep(.55);assert model.calls==0
        harness.version,harness.ready=2,True
        state=settled(repo,task['task_id'],('needs_input',))
        assert state['input_request'] and model.calls==1
        time.sleep(.55);assert model.calls==1
    finally:loop.close()


def test_result_arriving_during_model_decision_cannot_be_lost(database):
    repo=TaskRepository(database.session_factory);harness=Harness();entered,release=Event(),Event()
    def decide(s):
        if not entered.is_set():
            entered.set();assert release.wait(5)
            return 'wait_for_result',{'kind':'application','id':'child'}
        return 'finish_task',{'text':'I read the new result.'}
    loop=TaskLoop(repo,harness,model_factory=lambda:ToolModel(decide))
    try:
        task=loop.start('Wait without losing events.','result-race')
        assert entered.wait(5);harness.version,harness.ready=2,True;release.set()
        assert settled(repo,task['task_id'])['status']=='completed'
    finally:release.set();loop.close()


def test_stale_free_text_cannot_overwrite_a_pause(database):
    from langchain_core.messages import AIMessage
    repo=TaskRepository(database.session_factory);entered,release=Event(),Event()
    class Model:
        def invoke(self,*args):
            entered.set();assert release.wait(5);return AIMessage(content='An obsolete question.')
    loop=TaskLoop(repo,Harness(),model_factory=Model)
    try:
        task=loop.start('Start work.','pause-race')
        assert entered.wait(5);loop.chat(task['task_id'],'pause','pause-once');release.set()
        state=settled(repo,task['task_id']);time.sleep(.1)
        assert state['pause_requested']
        assert not any(m['text']=='An obsolete question.' for m in repo.get(task['task_id'])['messages'])
    finally:release.set();loop.close()


def test_transient_model_failures_retry_then_deliver_plain_chat_once(database):
    repo=TaskRepository(database.session_factory);harness=Harness()
    class Model:
        calls=0
        def invoke(self,*args):
            self.calls+=1
            if self.calls<3:raise ProviderError(503)
            return final_answer('Hi! What would you like help with?')
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model,retry_delays=(0,0))
    try:
        task=loop.start('hi','hello-retry')
        state=settled(repo,task['task_id'])
        assert model.calls==state['model_calls']==3
        assert not state['error'] and state['model_activity'] is None
        assert [m['text'] for m in state['messages'] if m['role']=='assistant']==['Hi! What would you like help with?']
        assert harness.lookups==0
    finally:loop.close()


@pytest.mark.parametrize('status,calls',[(503,3),(429,3),(401,1),(400,1)])
def test_model_failure_is_bounded_specific_redacted_and_recoverable(database,caplog,status,calls):
    import json
    repo=TaskRepository(database.session_factory)
    class Model:
        calls=0
        recover=False
        def invoke(self,*args):
            self.calls+=1
            if not self.recover:raise ProviderError(status)
            return final_answer('I can respond again.')
    model=Model();loop=TaskLoop(repo,Harness(),model_factory=lambda:model,retry_delays=(0,0))
    try:
        task=loop.start('hi','provider-failure')
        state=settled(repo,task['task_id'])
        assert state['error']['status']==status and state['error']['stage']=='model_request'
        assert model.calls==calls and state['model_calls']==calls
        assert len([m for m in state['messages'] if m['role']=='assistant'])==1
        assert 'synthetic-secret' not in json.dumps(state)+caplog.text
        if status==503:assert 'overloaded (HTTP 503)' in state['messages'][-1]['text']
        model.recover=True
        loop.chat(task['task_id'],'Try again','retry-provider')
        state=settled(repo,task['task_id'])
        assert state['error'] is None and state['messages'][-1]['text']=='I can respond again.'
    finally:loop.close()


def test_model_retries_do_not_replay_completed_tools(database):
    repo=TaskRepository(database.session_factory);harness=Harness()
    class Model:
        calls=0
        def invoke(self,*args):
            self.calls+=1
            if self.calls==1:return AIMessage(content='',tool_calls=[{'name':'lookup','args':{},'id':'lookup-once'}])
            if self.calls==2:raise ProviderError(503)
            return final_answer('The lookup completed.')
    model=Model();loop=TaskLoop(repo,harness,model_factory=lambda:model,retry_delays=(0,0))
    try:
        task=loop.start('Inspect.','retry-after-tool')
        state=settled(repo,task['task_id'])
        assert model.calls==3 and harness.lookups==1
        assert [h['tool'] for h in state['history']]==['lookup','finish_task']
    finally:loop.close()


def test_every_retry_consumes_budget(database):
    repo=TaskRepository(database.session_factory)
    class Model:
        def invoke(self,*args):raise ProviderError(503)
    loop=TaskLoop(repo,Harness(),model_factory=Model,budget=2,retry_delays=(0,0))
    try:
        task=loop.start('hi','retry-budget')
        state=settled(repo,task['task_id'])
        assert state['model_calls']==2 and state['model_activity'] is None
        assert 'Send continue' in state['messages'][-1]['text']
    finally:loop.close()


def test_stale_failure_cannot_overwrite_new_user_message(database):
    repo=TaskRepository(database.session_factory);entered,release=Event(),Event()
    class Model:
        calls=0
        def invoke(self,*args):
            self.calls+=1
            if self.calls==1:
                entered.set();assert release.wait(5);raise ProviderError(503)
            return final_answer('Responding to your latest message.')
    model=Model();loop=TaskLoop(repo,Harness(),model_factory=lambda:model,retry_delays=())
    try:
        task=loop.start('hi','failure-message-race')
        assert entered.wait(5)
        loop.chat(task['task_id'],'Can you hear me?','newest-message');release.set()
        state=settled(repo,task['task_id'])
        assert state['error'] is None and model.calls==2
        assert [m['text'] for m in state['messages'] if m['role']=='assistant']==['Responding to your latest message.']
    finally:release.set();loop.close()


def test_pause_cancels_retry_backoff(database):
    repo=TaskRepository(database.session_factory)
    class Model:
        calls=0
        def invoke(self,*args):self.calls+=1;raise ProviderError(503)
    model=Model();loop=TaskLoop(repo,Harness(),model_factory=lambda:model,retry_delays=(10,10))
    try:
        task=loop.start('hi','pause-retry')
        deadline=time.monotonic()+5
        while (repo.get(task['task_id']).get('model_activity') or {}).get('phase')!='retrying':
            assert time.monotonic()<deadline;time.sleep(.01)
        loop.chat(task['task_id'],'pause','pause-model-retry')
        state=settled(repo,task['task_id']);time.sleep(.2)
        assert state['pause_requested'] and state['model_activity'] is None and model.calls==1
        assert not any(m['role']=='assistant' for m in state['messages'])
    finally:loop.close()


def test_text_with_tool_call_is_visible_before_slow_tool_finishes_and_context_is_not_duplicated(database):
    import json
    from langchain_core.messages import HumanMessage, ToolMessage
    entered,release=Event(),Event()
    marker='synthetic-context-'+'x'*12000
    class SlowHarness(Harness):
        def observe(self,state):return {**super().observe(state),'long_context':marker}
        def execute(self,*args,**kwargs):
            entered.set();assert release.wait(5)
            return {'value':'lookup-result'}
    class Model:
        requests=[]
        def invoke(self,messages,tools):
            self.requests.append(messages)
            if len(self.requests)==1:
                return AIMessage(content='I am checking that now.',tool_calls=[{'name':'lookup','args':{},'id':'slow-lookup'}])
            return final_answer('The check is complete.')
    repo=TaskRepository(database.session_factory);model=Model()
    loop=TaskLoop(repo,SlowHarness(),model_factory=lambda:model)
    try:
        task=loop.start('Check this.','tool-progress')
        assert entered.wait(5)
        state=repo.get(task['task_id'])
        assert state['messages'][-1]['text']=='I am checking that now.'
        assert state['tool_activity']['tool']=='lookup'
        release.set();state=settled(repo,task['task_id'])
        assert state['tool_activity'] is None
        assert [t['stage'] for t in state['timings']]==['observation','model','tool','observation','model','tool']
        assert all(t['duration_seconds']>=0 for t in state['timings'])
        assert len(model.requests)==2
        request=model.requests[1]
        assert sum(marker in m.content for m in request)==1
        assert any(isinstance(m,ToolMessage) and 'lookup-result' in m.content for m in request)
        assert isinstance(request[1],HumanMessage)
        assert json.loads(request[-1].content)['long_context']==marker
    finally:release.set();loop.close()


def test_a_message_during_a_pause_is_kept_and_answered_with_the_pause_state(database):
    # §13: while the user controlled the browser, a long message with answers got no reply
    # and the task stayed paused, which looked like the agent ignoring it.
    from langchain_core.messages import AIMessage
    repo=TaskRepository(database.session_factory);entered,release=Event(),Event()
    class Model:
        def invoke(self,*args):
            entered.set();assert release.wait(5);return AIMessage(content='Working.')
    loop=TaskLoop(repo,Harness(),model_factory=Model)
    try:
        task=loop.start('Start work.','pause-reply')
        assert entered.wait(5);loop.chat(task['task_id'],'pause','pause-1');release.set()
        settled(repo,task['task_id'])
        loop.chat(task['task_id'],'My Cornell start was Aug 2026. Use that for the form.','held-1')
        state=repo.get(task['task_id'])
        assert state['pause_requested'] and state['status']=='needs_input'
        assert any(m['role']=='user' and m['id']=='held-1' for m in state['messages'])
        assert 'Paused' in state['messages'][-1]['text'] and state['messages'][-1]['role']=='assistant'
    finally:release.set();loop.close()


def test_a_capability_asking_the_user_itself_keeps_the_wait_without_a_model_turn(database):
    # Intel-32: each application question woke the parent to re-ask it (three calls each).
    class Holding(Harness):
        held=True
        def hold_wait(self,state):return self.held
    repo=TaskRepository(database.session_factory);harness=Holding()
    task,_=repo.create('Wait for a capability.','hold-wait')
    repo.change(task['task_id'],status='waiting',wait={'kind':'application','id':'child','token':1})
    model=ToolModel(lambda _:('finish_task',{'text':'The application is ready for review.'}))
    loop=TaskLoop(TaskRepository(database.session_factory),harness,model_factory=lambda:model)
    try:
        harness.version,harness.ready=2,True
        time.sleep(1.2)
        state=repo.get(task['task_id'])
        assert model.calls==0 and state['status']=='waiting' and state['wait']['token']==2
        harness.held,harness.version=False,3
        assert settled(repo,task['task_id'],('completed',))['status']=='completed' and model.calls==1
    finally:loop.close()


def test_a_reply_routed_to_its_capability_waits_without_a_model_turn(database):
    class Routing(Harness):
        def route_user_control(self,state,message):
            return {'tool':'reply_application','result':{'reply_delivered':True},'wait':{'kind':'application','id':'child'}}
    repo=TaskRepository(database.session_factory)
    model=ToolModel(lambda _:('finish_task',{'text':'unused'}))
    loop=TaskLoop(repo,Routing(),model_factory=lambda:model)
    try:
        task,_=repo.create('Apply.','route-wait')
        state=loop.chat(task['task_id'],'1 Yes, 2 No','m1')
        assert state['status']=='waiting' and state['wait']=={'kind':'application','id':'child','token':1}
        assert state['history'][-1]['tool']=='reply_application'
        time.sleep(.6);assert model.calls==0
    finally:loop.close()
