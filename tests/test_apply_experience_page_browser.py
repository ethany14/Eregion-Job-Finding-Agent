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
    dynamic = False
    def _guard_request(self,route):
        if route.request.is_navigation_request():
            body=(Path(__file__).parent/'fixtures/auto_apply/experience_page.html').read_text()
            if self.dynamic:
                script=(Path(__file__).parent/'fixtures/auto_apply/experience_dynamic.js').read_text()
                body=body.replace('</body>', '<script>'+script+'</script></body>')
            route.fulfill(content_type='text/html',body=body)
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


@pytest.mark.parametrize('dynamic,early_next,compound', [(False,False,False), (True,False,False), (True,True,False), (True,False,True)])
def test_batched_turns_fill_four_records_and_reach_next_through_the_loop(monkeypatch, dynamic, early_next, compound):
    """Experience-page test 2: the real model needed 12 rounds and never pressed Next. Through the
    production loop, three scripted turns (writes + one page action each) complete the page.
    Proves tools and flow only; the decisions here are scripted, not a model's."""
    import json
    from copy import deepcopy
    from langchain_core.messages import AIMessage, HumanMessage
    from eregion.runtime.auto_apply.agent import HybridApplicationAgent
    if not compound:
        monkeypatch.setattr('eregion.runtime.auto_apply.fast_options.fill_options', lambda *a, **k: [])
    class NoPaidDecisions:
        enabled, timeout = True, 10
        def evaluate(self, *args, **kwargs):
            pytest.fail('The scripted record plan must not need another semantic provider.')
    class ScenarioBrowser(PageBrowser):
        def observe(self):
            fields = super().observe()
            self.metric_fields = deepcopy(fields)
            return fields
    ScenarioBrowser.dynamic = dynamic
    b = ScenarioBrowser(URL)
    b.page.evaluate('''() => document.addEventListener('submit', () => {
        window.valuesBeforeNext = Object.fromEntries([...document.querySelectorAll('input[id], select[id]')]
            .map(e => [e.id, e.type === 'checkbox' ? e.checked : e.value]));
        window.majorsBeforeNext = [...document.querySelectorAll('[data-automation-id="selectedItem"]')].map(e => e.textContent);
    }, true)''')
    state = {'automation_mode': 'hybrid', 'answers': {}, 'status': 'running', 'chat_revision': 1, 'messages': [], 'profile': {'experience': [
        {'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present'},
        {'id': 'two', 'title': 'Intern', 'company': 'Employer', 'start_date': 'Jul 2022', 'end_date': 'Aug 2022'}],
        'education': [{'id': 'a', 'school': 'School A', 'degree': 'Masters', 'field_of_study': 'Systems Engineering'},
                      {'id': 'b', 'school': 'School B', 'degree': 'Bachelors', 'field_of_study': 'Computer Science'}]}}
    work = {'jobTitle': 'title', 'companyName': 'company', 'startDate-dateSectionMonth-input': 'start_month', 'startDate-dateSectionYear-input': 'start_year'}

    def k(snap, part, n):
        return next(f['key'] for f in snap['fields'] + snap.get('field_index', []) if part in f['key'] and f['key'].endswith(f':{n}'))

    def control(snap, label):
        return next(c['id'] for c in snap['page']['controls'] if c['label'].startswith(label))

    def turn1(s):
        jobs = [{'field_key': k(s, p, 1), 'component': c} for p, c in work.items()]
        school = [{'field_key': k(s, 'schoolName', 1), 'component': 'school'}, {'field_key': k(s, 'fieldOfStudy', 1), 'component': 'field_of_study'}]
        calls = [('bind_experience', {'experience_id': 'one', 'fields': jobs}),
                ('bind_education', {'education_id': 'a', 'fields': [*school, {'field_key': k(s, 'degree', 1), 'component': 'degree'}]}),
                ('resolve_field', {'field_key': k(s, 'current', 1), 'mode': 'equivalent', 'value': 'true', 'source_refs': ['experience:one:current'], 'reason': 'Scripted.'}),
                ('resolve_field', {'field_key': k(s, 'degree', 1), 'mode': 'equivalent', 'value': 'Masters', 'source_refs': ['education:a:degree'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in jobs + school]}),
                ('click_page_action', {'control_id': control(s, 'Add Another Work')})]
        if dynamic:
            calls.insert(4, ('resolve_field', {'field_key': k(s, 'fieldOfStudy', 1), 'mode': 'leave_empty',
                'value': '', 'source_refs': ['education:a:field_of_study'], 'reason': 'Scripted: no reliable equivalent in this fixture.'}))
            calls[-2][1]['field_keys'].remove(k(s, 'fieldOfStudy', 1))
        if early_next:
            calls[-1][1]['control_id'] = control(s, 'Next')
        return calls

    def turn2(s):
        jobs = [{'field_key': k(s, p, 2), 'component': c} for p, c in {**work, 'endDate-dateSectionMonth-input': 'end_month', 'endDate-dateSectionYear-input': 'end_year'}.items()]
        return [('bind_experience', {'experience_id': 'two', 'fields': jobs}),
                ('resolve_field', {'field_key': k(s, 'current', 2), 'mode': 'equivalent', 'value': 'false', 'source_refs': ['experience:two:end_date'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in jobs]}),
                ('click_page_action', {'control_id': control(s, 'Add Another Education')})]

    def turn3(s):
        school = [{'field_key': k(s, 'schoolName', 2), 'component': 'school'}, {'field_key': k(s, 'fieldOfStudy', 2), 'component': 'field_of_study'}]
        calls = [('bind_education', {'education_id': 'b', 'fields': [*school, {'field_key': k(s, 'degree', 2), 'component': 'degree'}]}),
                ('resolve_field', {'field_key': k(s, 'degree', 2), 'mode': 'equivalent', 'value': 'Bachelors', 'source_refs': ['education:b:degree'], 'reason': 'Scripted.'}),
                ('autofill', {'field_keys': [x['field_key'] for x in school]}),
                ('click_page_action', {'control_id': control(s, 'Next')})]
        if dynamic:
            calls.pop();calls[-1][1]['field_keys'].remove(k(s, 'fieldOfStudy', 2))
        return calls

    def inspect_major(s):
        return [('inspect_field', {'field_key': k(s, 'fieldOfStudy', 2)})]

    def inspect_first_major(s):
        return [('inspect_field', {'field_key': k(s, 'fieldOfStudy', 1)})]

    def finish_major(s):
        return [('resolve_field', {'field_key': k(s, 'fieldOfStudy', 2), 'mode': 'equivalent',
            'value': 'Computer and Information Science', 'source_refs': ['education:b:field_of_study'],
            'reason': 'Scripted semantic mapping for tool integration, not a real model judgment.'}),
            ('click_page_action', {'control_id': control(s, 'Next')})]

    def record_call(ref, calls):
        plan = {'record': ref, 'bindings': [], 'decisions': [], 'fill_fields': []}
        for name, args in calls:
            if name.startswith('bind_'): plan['bindings'].extend(args['fields'])
            elif name == 'resolve_field': plan['decisions'].append(args)
            elif name == 'autofill': plan['fill_fields'] = args['field_keys']
            elif name == 'click_page_action': plan['then_control_id'] = args['control_id']
            else: pytest.fail('Unexpected record action')
        return [('apply_record', plan)]

    def work_one(s):
        calls = turn1(s)
        keys = [k(s, p, 1) for p in work]
        return record_call('work:one', [calls[0], calls[2], ('autofill', {'field_keys': keys}), calls[-1]])

    def work_two(s):
        return record_call('work:two', turn2(s)[:-1])

    def education_one(s):
        calls = turn1(s)
        return record_call('education:a', [calls[1], calls[3], calls[4],
            ('autofill', {'field_keys': [k(s, 'schoolName', 1)]}),
            ('click_page_action', {'control_id': control(s, 'Add Another Education')})])

    def education_two(s):
        calls = turn3(s)
        return record_call('education:b', [calls[0], calls[1], *finish_major(s)[:1], calls[2], finish_major(s)[1]])

    class Batches:
        def __init__(self):
            self.turns, self.results = iter(([inspect_first_major] if dynamic else []) + [turn1, turn2, turn3] + ([inspect_major, finish_major] if dynamic else [])), []
            if compound:
                self.turns = iter([inspect_first_major, work_one, work_two, education_one, inspect_major, education_two])
            self.context_sizes = []

        def invoke(self, messages, tools):
            snap = json.loads(next(m.content for m in reversed(messages) if isinstance(m, HumanMessage) and isinstance(m.content, str)))
            from eregion.runtime.auto_apply.observation import focus_observation
            measure = lambda x: len(json.dumps(x, ensure_ascii=False).encode('utf-8'))
            focused = focus_observation(deepcopy(snap), {**state, 'observation_scope': [snap['fields'][0]['key']],
                'observation_scope_page': [snap['page']['url'], snap['page']['heading']]}, deepcopy(b.metric_fields))
            self.context_sizes.append({'current_observation_bytes': measure(snap), 'scoped_observation_bytes': measure(focused),
                'fields_bytes': measure(snap['fields']), 'facts_bytes': measure(snap['facts']),
                'history_bytes': measure(snap['decision_history']), 'raw_page_text_bytes': len(snap['page'].get('text', '').encode('utf-8')),
                'tools_bytes': measure(tools), 'system_bytes': sum(len(str(m.content).encode('utf-8')) for m in messages if m.type == 'system')})
            self.results.append(snap.get('last_tool_result'))
            if early_next and len(self.results) == 3:
                assert any('work:two' in b for b in snap['record_blockers'])
                assert any('education:b' in b for b in snap['record_blockers'])
                return AIMessage(content='', tool_calls=[{'id':'stop','name':'ask_user',
                    'args':{'text':'Stopping this deliberately incomplete test.', 'field_keys':[]}}])
            calls = next(self.turns)(snap)
            for name, args in calls:
                if name not in {'autofill'}:
                    args.setdefault('observation_id', snap['observation_id'])
            return AIMessage(content='', tool_calls=[{'id': f'{len(self.results)}-{i}', 'name': n, 'args': a} for i, (n, a) in enumerate(calls)])

    model = Batches()
    done = lambda: b.page.evaluate('window.nextCount > 0')
    try:
        outcome = HybridApplicationAgent(model_factory=lambda: model, decisions=NoPaidDecisions() if compound else None).run(b, state, autofill=lambda: autofill(b, state, lambda: b''),
            active=lambda: not done(), reserve=lambda kind: True, report=lambda *a: None)
        if early_next:
            assert not done() and outcome['reason'] == 'needs_input'
            assert b.page.evaluate('window.nextCount===0 && window.submitCount===0')
            return
        assert done(), (outcome, len(model.results), [h.get("tool") for h in state.get("decision_history", [])][-12:], [(h.get("result") or {}).get("error") for h in state.get("decision_history", [])][-12:])
        assert len(model.results) == (6 if dynamic else 3)
        assert any(h['tool'] == 'click_page_action' and h['result'].get('label') == 'Next' for h in state['decision_history'])
        assert not any(h['tool'] == 'host:auto_advance' for h in state['decision_history'])
        assert b.page.evaluate('window.nextCount===1 && window.submitCount===0')
        actual = b.page.evaluate('window.valuesBeforeNext')
        assert {key: actual[key] for key in ['workExperience-1--jobTitle', 'workExperience-2--jobTitle',
            'workExperience-1--companyName', 'workExperience-2--companyName',
            'education-1--schoolName', 'education-2--schoolName', 'education-1--degree', 'education-2--degree']} == {
            'workExperience-1--jobTitle': 'Research', 'workExperience-2--jobTitle': 'Intern',
            'workExperience-1--companyName': 'University', 'workExperience-2--companyName': 'Employer',
            'education-1--schoolName': 'School A', 'education-2--schoolName': 'School B',
            'education-1--degree': 'Masters', 'education-2--degree': 'Bachelors'}
        for prefix, month, year in [('workExperience-1--startDate', '8', '2026'),
                                    ('workExperience-2--startDate', '7', '2022'), ('workExperience-2--endDate', '8', '2022')]:
            assert actual[prefix + '-dateSectionMonth-input'] == month
            assert actual[prefix + '-dateSectionYear-input'] == year
        assert actual['workExperience-1--current'] is True and actual['workExperience-2--current'] is False
        if dynamic:
            assert actual['education-1--fieldOfStudy'] == ''
            assert b.page.evaluate('window.majorsBeforeNext') == ['Computer and Information Science']
        assert set(state['record_completion']) == {'work:one', 'work:two', 'education:a', 'education:b'}
        assert {k: d['value'] for k, d in state['field_decisions'].items() if 'current' in k} == {
            'checkbox:workExperience--current:on:1': True, 'checkbox:workExperience--current:on:2': False}
    finally:
        print('CONTEXT_BYTES', json.dumps({'dynamic': dynamic, 'early_next': early_next, 'turns': model.context_sizes}))
        b.close()


def test_native_validation_refusing_next_is_not_success():
    b = PageBrowser(URL)
    try:
        action = next(c for c in b.page_state()['controls'] if c['kind'] == 'next')
        result = b.click_page_action(action)
        assert result['executed'] and not result['verified'] and not result['navigated']
        assert b.page.evaluate('window.nextCount') == 0
    finally:
        b.close()


@pytest.mark.parametrize('advance_step', [False, True])
def test_next_waits_for_step_evidence_not_just_new_fields(advance_step):
    class DynamicBrowser(PageBrowser): dynamic = True
    b = DynamicBrowser(URL)
    try:
        b.page.evaluate('''advance => {
            document.querySelector('form').noValidate = true;
            document.querySelectorAll('input, select').forEach(e => e.required=false);
            document.querySelector('form').onsubmit = e => {
                e.preventDefault();
                setTimeout(() => {
                    const field = document.createElement('input'); field.id='new-conditional'; document.querySelector('form').append(field);
                    if (advance) document.querySelector('[aria-current="step"]').textContent='current step 4 of 7';
                }, 500);
            };
        }''', advance_step)
        action = next(c for c in b.page_state()['controls'] if c['kind'] == 'next')
        result = b.click_page_action(action)
        assert result['verified'] is advance_step
        assert result['navigated'] is advance_step
    finally:
        b.close()


def test_scoped_autofill_handles_month_discarded_until_year_is_complete():
    from eregion.runtime.auto_apply.autofill import autofill
    b=PageBrowser(URL)
    try:
        b.page.evaluate("""() => {
          document.getElementById("addWork").click();
          const m=document.querySelector('[id="workExperience-2--startDate-dateSectionMonth-input"]');
          const y=document.querySelector('[id="workExperience-2--startDate-dateSectionYear-input"]');
          m.value=''; y.value='';
          m.addEventListener('blur',()=>{if(y.value.length!==4)m.value='';});
        }""")
        fields=b.observe()
        m=next(f for f in fields if f.get('dom_id')=='workExperience-2--startDate-dateSectionMonth-input')
        y=next(f for f in fields if f.get('dom_id')=='workExperience-2--startDate-dateSectionYear-input')
        state={'automation_mode':'hybrid','profile':{'experience':[{'id':'two','company':'Example','title':'Research','start_date':'Aug 2026'}]},'answers':{},'autofill_scope':[m['key'],y['key']]}
        session=AgentTools(b,state,autofill=lambda:{},active=lambda:True)
        session.observe()
        session.execute('bind_experience',{'observation_id':session.observation_id,'experience_id':'two','fields':[
            {'field_key':m['key'],'component':'start_month'},{'field_key':y['key'],'component':'start_year'}]})
        result=autofill(b,state,lambda:b'')
        assert not result['failed'], result
        values={f['key']:f['value'] for f in b.observe()}
        assert values[m['key']]=='8' and values[y['key']]=='2026'
        assert set(result['filled'])=={m['key'],y['key']}
    finally:b.close()


def test_dynamic_preselection_is_unresolved_until_model_decides(monkeypatch):
    monkeypatch.setenv('TYPESAFE_ENABLED','false')
    class PreselectedBrowser(PageBrowser): dynamic=True
    b=PreselectedBrowser(URL)
    # PageBrowser opens lazily through AgentTools, like other whole-page cases.
    state={'profile':{'education':[{'id':'b','school':'School B','degree':'Bachelors','field_of_study':'Computer Science'}]},'answers':{}}
    try:
        s=AgentTools(b,state,autofill=lambda:{},active=lambda:True)
        s.observe();b.page.locator('#addSchool').click();s.observe()
        f=next(x for x in s.fields if x.get('dom_id')=='education-2--fieldOfStudy')
        assert f['value_committed'] and f['value']=='Computer and Information Science'
        s.execute('bind_education',{'observation_id':s.observation_id,'education_id':'b','fields':[{'field_key':f['key'],'component':'field_of_study'}]})
        obs=s.observe();item=next(x for x in obs['fields'] if x['key']==f['key'])
        assert item['mapping']=='undecided' and not item['write_verified']
        assert 'already an observed option' in item['selected_option_decision']
        result=s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':f['key'],'mode':'equivalent','value':f['value'],
            'source_refs':['education:b:field_of_study'],'reason':'Scripted semantic decision for tool validation only.'})
        assert result['verified'] and s.state['field_decisions'][f['key']]['mode']=='equivalent'
    finally:b.close()


def test_model_optional_omission_clears_only_its_preselected_workday_tag():
    class Dynamic(PageBrowser): dynamic=True
    b=Dynamic(URL)
    try:
        b.page.locator('#addSchool').click()
        b.page.evaluate("""()=>{const shell=document.querySelector('#education-1--fieldOfStudy').closest('[data-automation-id="multiSelectContainer"]');
            const tag=document.createElement('div');tag.dataset.automationId='selectedItem';tag.textContent='Keep other record';shell.append(tag);}""")
        state={'profile':{},'answers':{}}
        s=AgentTools(b,state,autofill=lambda:{},active=lambda:True);s.observe()
        target=next(x for x in s.fields if x.get('dom_id')=='education-2--fieldOfStudy')
        assert target['value']=='Computer and Information Science'
        result=s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':target['key'],
            'mode':'leave_empty','value':'','source_refs':[],'reason':'Scripted optional omission; not a semantic mapping test.'})
        assert result['verified'] and result['left_empty']
        fields=b.observe()
        assert next(x for x in fields if x.get('dom_id')=='education-2--fieldOfStudy')['value']==''
        assert next(x for x in fields if x.get('dom_id')=='education-1--fieldOfStudy')['value']=='Keep other record'
        assert state['field_decisions'][target['key']]['mode']=='leave_empty'
        assert b.page.evaluate('window.nextCount')==0
    finally:b.close()

def test_optional_clear_without_observed_remove_control_fails_closed():
    class Dynamic(PageBrowser): dynamic=True
    b=Dynamic(URL)
    try:
        b.page.locator('#addSchool').click()
        b.page.locator('[data-automation-id="DELETE_charm"]').evaluate_all('(els)=>els.forEach(e=>e.remove())')
        target=next(x for x in b.observe() if x.get('dom_id')=='education-2--fieldOfStudy')
        with pytest.raises(BrowserBlocked,match='no observed removal'):
            b.clear_optional_field(target['key'])
        assert b.field(target['key'])['value']=='Computer and Information Science'
        assert b.page.evaluate('window.nextCount')==0
    finally:b.close()


def test_omission_after_an_uncommitted_search_clears_the_typed_text():
    # Dynamic-19: the model searched "Systems", then decided to omit the Cornell major; the typed
    # text stayed in the picker input because the observed value excludes uncommitted search text.
    class Dynamic(PageBrowser): dynamic=True
    b=Dynamic(URL)
    try:
        state={'profile':{},'answers':{}}
        s=AgentTools(b,state,autofill=lambda:{},active=lambda:True);s.observe()
        target=next(x for x in s.fields if x.get('dom_id')=='education-1--fieldOfStudy')
        b.inspect_options(target['key'],query='Systems')
        s.observe()
        typed=next(x for x in s.fields if x['key']==target['key'])
        assert typed['value']=='' and typed.get('search_text')=='Systems'
        assert b.locate(typed).input_value()=='Systems'
        result=s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':target['key'],
            'mode':'leave_empty','value':'','source_refs':[],'reason':'Scripted optional omission; not a semantic test.'})
        assert result['verified'] and result['left_empty']
        assert b.locate(b.field(target['key'])).input_value()==''
        assert b.page.locator('[data-automation-id="selectedItem"]').count()==0
        assert b.page.evaluate('window.nextCount')==0
    finally:b.close()
