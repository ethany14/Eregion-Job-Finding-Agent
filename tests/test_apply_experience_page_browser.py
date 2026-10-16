"""Real Chrome with synthetic full-page controls and explicit scripted decisions, no LLM/network."""
import os
from pathlib import Path
import pytest
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.autofill import autofill
from eregion.runtime.auto_apply.browser import BrowserBlocked
from test_auto_apply_workday import WorkdayBrowser, URL

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Offline Chrome opt-in')

class PageBrowser(WorkdayBrowser):
    def _guard_request(self,route):
        if route.request.is_navigation_request():
            route.fulfill(content_type='text/html',body=(Path(__file__).parent/'fixtures/auto_apply/experience_page.html').read_text())
        else:route.abort()

def test_two_work_and_education_records_reach_next_with_saved_decisions(monkeypatch):
    monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.fill_options',lambda *a,**k:pytest.fail('No paid models'))
    b=PageBrowser(URL)
    state={'automation_mode':'hybrid','answers':{},'profile':{'experience':[
        {'id':'one','title':'Research','company':'University','start_date':'Aug 2026','current':'Present'},
        {'id':'two','title':'Intern','company':'Employer','start_date':'Jul 2022','end_date':'Aug 2022'}],
        'education':[{'id':'a','school':'School A','degree':'Masters','field_of_study':'Systems Engineering'},
                     {'id':'b','school':'School B','degree':'Bachelors','field_of_study':'Computer Science'}]}}
    s=AgentTools(b,state,autofill=lambda:autofill(b,state,lambda:b''),active=lambda:True)
    def act(name,**args):
        s.observe();return s.execute(name,{'observation_id':s.observation_id,**args})
    def key(dom):
        s.observe();return next(f['key'] for f in s.fields if f.get('dom_id')==dom)
    try:
        # Fixture setup uses the actual Add Another controls; tool processing uses production code.
        b.page.locator('#addWork').click();b.page.locator('#addSchool').click()
        for n,record in [(1,'one'),(2,'two')]:
            prefix=f'workExperience-{n}--'
            components={'jobTitle':'title','companyName':'company','startDate-dateSectionMonth-input':'start_month','startDate-dateSectionYear-input':'start_year'}
            if n==2:components.update({'endDate-dateSectionMonth-input':'end_month','endDate-dateSectionYear-input':'end_year'})
            bindings=[{'field_key':key(prefix+suffix),'component':component} for suffix,component in components.items()]
            act('bind_experience',experience_id=record,fields=bindings)
            result=act('resolve_field',field_key=key(prefix+'current'),mode='equivalent',value='true' if n==1 else 'false',
                source_refs=[f'experience:{record}:'+('current' if n==1 else 'end_date')],reason='Scripted decision from saved employment evidence.')
            assert result['verified']
            # Skip semantic fast-options; none of these fields require them.
            s.fast_choices=None
            s._host_literal_autofill=True
            result=s.execute('autofill',{'field_keys':[x['field_key'] for x in bindings]})
            s._host_literal_autofill=False
            assert not result['failed'],result
        for n,record,degree in [(1,'a','Masters'),(2,'b','Bachelors')]:
            prefix=f'education-{n}--'
            act('bind_education',education_id=record,fields=[{'field_key':key(prefix+'schoolName'),'component':'school'},
                {'field_key':key(prefix+'degree'),'component':'degree'}])
            act('type_from_source',field_key=key(prefix+'schoolName'),source_ref=f'education:{record}:school')
            act('resolve_field',field_key=key(prefix+'degree'),mode='equivalent',value=degree,source_refs=[f'education:{record}:degree'],reason='Exact degree option.')
            act('resolve_field',field_key=key(prefix+'fieldOfStudy'),mode='leave_empty',value='',source_refs=[],reason='Scripted optional-field decision for this integration test.')
        assert b.page.locator('[id="workExperience-1--startDate-dateSectionYear-input"]').input_value()=='2026'
        assert b.page.locator('[id="workExperience-2--endDate-dateSectionYear-input"]').input_value()=='2022'
        assert len(state['field_decisions'])>=6
        assert b.page.locator('form').evaluate('(e)=>e.checkValidity()')
        b.page.get_by_role('button',name='Next',exact=True).click()
        assert b.page.get_by_role('heading',name='Application Questions').count()==1
        assert b.page.evaluate('window.nextCount===1 && window.submitCount===0')
    finally:b.close()

def test_plain_date_inspection_does_not_click_or_open_options():
    b=PageBrowser(URL)
    try:
        f=next(f for f in b.observe() if 'dateSectionMonth' in f['key'])
        b._close_other_menu=lambda key:pytest.fail('Invalid option inspection must not close another menu')
        b.page.evaluate('window.clicks=0;document.addEventListener("click",()=>window.clicks++)')
        with pytest.raises(BrowserBlocked,match='not an option control'):b.inspect_options(f['key'])
        assert b.page.evaluate('window.clicks')==0
    finally:b.close()


@pytest.mark.parametrize('path',['fill','fill_plain','type_field'])
def test_corrupted_year_is_repaired_through_all_write_paths(path):
    b=PageBrowser(URL)
    try:
        fields=b.observe()
        month=next(f for f in fields if 'startDate-dateSectionMonth' in f['key'])
        year=next(f for f in fields if 'startDate-dateSectionYear' in f['key'])
        write={'fill':b.fill,'fill_plain':b.fill_plain,'type_field':lambda f,v:b.type_field(f['key'],v)}[path]
        write(month,'08');write(year,'2026')
        assert b.page.locator('[id="workExperience-1--startDate-dateSectionMonth-input"]').input_value()=='8'
        assert b.page.locator('[id="workExperience-1--startDate-dateSectionYear-input"]').input_value()=='2026'
    finally:b.close()


def test_sibling_corruption_is_not_reported_as_success():
    b=PageBrowser(URL)
    try:
        month=next(f for f in b.observe() if 'startDate-dateSectionMonth' in f['key'])
        b.page.locator('[id="workExperience-1--startDate-dateSectionMonth-input"]').evaluate('''e=>e.addEventListener('blur',()=>{
            document.getElementById('workExperience-1--startDate-dateSectionYear-input').value='9999';
        })''')
        with pytest.raises(BrowserBlocked) as error:b.fill_plain(month,'08')
        assert error.value.code=='date_group_changed'
    finally:b.close()


def test_batched_turns_fill_four_records_and_reach_next_through_the_loop(monkeypatch):
    """Experience-page test 2: the real model needed 12 rounds and never pressed Next. Through the
    production loop, three scripted turns (writes + one page action each) complete the page.
    Proves tools and flow only; the decisions here are scripted, not a model's."""
    import json
    from langchain_core.messages import AIMessage, HumanMessage
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.fill_options', lambda *a, **k: [])
    b = PageBrowser(URL)
    state = {'automation_mode': 'hybrid', 'answers': {}, 'status': 'running', 'chat_revision': 1, 'messages': [], 'profile': {'experience': [
        {'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present'},
        {'id': 'two', 'title': 'Intern', 'company': 'Employer', 'start_date': 'Jul 2022', 'end_date': 'Aug 2022'}],
        'education': [{'id': 'a', 'school': 'School A', 'degree': 'Masters', 'field_of_study': 'Systems Engineering'},
                      {'id': 'b', 'school': 'School B', 'degree': 'Bachelors', 'field_of_study': 'Computer Science'}]}}
    work = {'jobTitle': 'title', 'companyName': 'company', 'startDate-dateSectionMonth-input': 'start_month', 'startDate-dateSectionYear-input': 'start_year'}

    def k(snap, part, n):
        return next(f['key'] for f in snap['fields'] if part in f['key'] and f['key'].endswith(f':{n}'))

    def control(snap, label):
        return next(c['id'] for c in snap['page']['controls'] if c['label'].startswith(label))

    def turn1(s):
        jobs = [{'field_key': k(s, p, 1), 'component': c} for p, c in work.items()]
        school = [{'field_key': k(s, 'schoolName', 1), 'component': 'school'}, {'field_key': k(s, 'fieldOfStudy', 1), 'component': 'field_of_study'}]
        return [('bind_experience', {'experience_id': 'one', 'fields': jobs}),
                ('bind_education', {'education_id': 'a', 'fields': [*school, {'field_key': k(s, 'degree', 1), 'component': 'degree'}]}),
                ('resolve_field', {'field_key': k(s, 'current', 1), 'mode': 'equivalent', 'value': 'true', 'source_refs': ['experience:one:current'], 'reason': 'Scripted.'}),
                ('resolve_field', {'field_key': k(s, 'degree', 1), 'mode': 'equivalent', 'value': 'Masters', 'source_refs': ['education:a:degree'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in jobs + school]}),
                ('click_page_action', {'control_id': control(s, 'Add Another Work')})]

    def turn2(s):
        jobs = [{'field_key': k(s, p, 2), 'component': c} for p, c in {**work, 'endDate-dateSectionMonth-input': 'end_month', 'endDate-dateSectionYear-input': 'end_year'}.items()]
        return [('bind_experience', {'experience_id': 'two', 'fields': jobs}),
                ('resolve_field', {'field_key': k(s, 'current', 2), 'mode': 'equivalent', 'value': 'false', 'source_refs': ['experience:two:end_date'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in jobs]}),
                ('click_page_action', {'control_id': control(s, 'Add Another Education')})]

    def turn3(s):
        school = [{'field_key': k(s, 'schoolName', 2), 'component': 'school'}, {'field_key': k(s, 'fieldOfStudy', 2), 'component': 'field_of_study'}]
        return [('bind_education', {'education_id': 'b', 'fields': [*school, {'field_key': k(s, 'degree', 2), 'component': 'degree'}]}),
                ('resolve_field', {'field_key': k(s, 'degree', 2), 'mode': 'equivalent', 'value': 'Bachelors', 'source_refs': ['education:b:degree'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in school]}),
                ('click_page_action', {'control_id': control(s, 'Next')})]

    class Batches:
        def __init__(self):
            self.turns, self.results = iter([turn1, turn2, turn3]), []

        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            self.results.append(snap.get('last_tool_result'))
            calls = next(self.turns)(snap)
            for name, args in calls:
                if name not in {'autofill'}:
                    args.setdefault('observation_id', snap['observation_id'])
            return AIMessage(content='', tool_calls=[{'id': f'{len(self.results)}-{i}', 'name': n, 'args': a} for i, (n, a) in enumerate(calls)])

    model = Batches()
    done = lambda: b.page.get_by_role('heading', name='Application Questions').count() > 0
    try:
        outcome = HybridApplicationAgent(model_factory=lambda: model).run(b, state, autofill=lambda: autofill(b, state, lambda: b''),
            active=lambda: not done(), reserve=lambda kind: True, report=lambda *a: None)
        assert done(), (outcome, len(model.results), [h.get("tool") for h in state.get("decision_history", [])][-12:], [(h.get("result") or {}).get("error") for h in state.get("decision_history", [])][-12:])
        assert len(model.results) == 3
        assert b.page.evaluate('window.nextCount===1 && window.submitCount===0')
        assert {k: d['value'] for k, d in state['field_decisions'].items() if 'current' in k} == {
            'checkbox:workExperience--current:on:1': True, 'checkbox:workExperience--current:on:2': False}
    finally:
        b.close()
