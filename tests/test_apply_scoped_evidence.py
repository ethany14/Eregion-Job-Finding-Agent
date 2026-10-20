"""A scoped observation carries the evidence its fields depend on; everything else stays one call away."""
from eregion.runtime.auto_apply.agent import AgentTools
from test_auto_apply import FakeBrowser, field

PROFILE = {'first_name': 'Avery', 'email': 'avery@example.com',
           'experience': [{'id': 'one', 'title': 'Research', 'company': 'University', 'start_date': 'Aug 2026', 'current': 'Present'},
                          {'id': 'two', 'title': 'Intern', 'company': 'Employer', 'start_date': 'Jul 2022', 'end_date': 'Aug 2022'}],
           'education': [{'id': 'a', 'school': 'School A', 'degree': 'Masters'},
                         {'id': 'b', 'school': 'School B', 'degree': 'Bachelors'}]}
RESUME = 'Avery\nEXPERIENCE\nResearch at University. Built evaluation services.\n'


def row(kind, n, name, label, field_kind='text', required=True):
    item = field(f'{field_kind}:{kind}--{name}:{n}', kind=field_kind, label=label, required=required)
    item['dom_id'] = f'{kind}-{n}--{name}'
    return item


def page():
    return [row('workExperience', 1, 'jobTitle', 'Job Title'), row('workExperience', 1, 'companyName', 'Company'),
            row('workExperience', 2, 'jobTitle', 'Job Title'), row('workExperience', 2, 'companyName', 'Company'),
            row('education', 1, 'schoolName', 'School'), row('education', 2, 'schoolName', 'School'),
            field('textarea:why:1', kind='textarea', label='Why are you interested in this role?')]


def session(**state_changes):
    browser = FakeBrowser('')
    browser.fields = page()
    state = {'profile': PROFILE, 'answers': {'select:sponsorship:1': 'No', 'textarea:why:1': 'Because of the research mission.'},
             'resume_context': {'name': 'resume.pdf', 'text': RESUME}, 'cover_letter': 'Dear team, ...',
             'review_fields': [{'key': 'text:email:1', 'label': 'Email', 'value': 'avery@example.com', 'kind': 'email'}],
             'messages': [{'id': 'm1', 'role': 'user', 'text': 'No sponsorship needed.', 'bound_answers': {'select:sponsorship:1': 'No'}}],
             **state_changes}
    tools = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    tools.observe()
    return browser, tools


def focus(tools, keys):
    result = tools.execute('focus_fields', {'field_keys': keys})
    assert result['observation_scope'] == keys
    return tools.observe()


def refs(snapshot):
    return {f['ref'] for f in snapshot['facts']}


def test_full_page_keeps_every_fact():
    _, tools = session()
    snapshot = tools.observe()
    assert {'experience:one:title', 'experience:two:title', 'education:a:school', 'education:b:school',
            'answer:select:sponsorship:1', 'material:cover_letter'} <= refs(snapshot)
    assert 'facts_outside_scope' not in snapshot and snapshot['previous_page_answers']


def test_a_bound_record_keeps_its_facts_and_others_keep_an_identity_index():
    _, tools = session()
    bound = tools.execute('bind_experience', {'observation_id': tools.observation_id, 'experience_id': 'experience:one',
        'fields': [{'field_key': 'text:workExperience--jobTitle:1', 'component': 'title'},
                   {'field_key': 'text:workExperience--companyName:1', 'component': 'company'}]})
    assert not bound.get('error'), bound
    tools.observe()
    snapshot = focus(tools, ['text:workExperience--jobTitle:1'])
    kept = refs(snapshot)
    assert {'experience:one:title', 'experience:one:company', 'profile:first_name'} <= kept
    assert not any(r.startswith(('experience:two', 'education:')) for r in kept)
    index = {e['record']: e for e in snapshot['facts_outside_scope']['records']}
    assert index['experience:two'] == {'record': 'experience:two', 'title': 'Intern', 'company': 'Employer'}
    assert index['education:a'] == {'record': 'education:a', 'school': 'School A', 'degree': 'Masters'}
    # A user's bound answer always stays; an unrelated answer, materials and the resume wait for a wider scope.
    assert 'answer:select:sponsorship:1' in kept and 'answer:textarea:why:1' not in kept
    assert 'material:cover_letter' in snapshot['facts_outside_scope']['refs']
    assert 'text' not in snapshot['resume_context'] and snapshot['resume_context']['text_omitted']
    assert snapshot['previous_page_answers'] == [] and snapshot['previous_page_answers_outside_scope']['count'] == 1
    # The rest of the page stays visible as an index; nothing is dropped.
    assert {f['key'] for f in snapshot['field_index']} >= {'text:workExperience--jobTitle:2', 'text:education--schoolName:1'}


def test_an_unbound_record_row_keeps_every_record_of_its_kind():
    _, tools = session()
    kept = refs(focus(tools, ['text:workExperience--jobTitle:2']))
    assert {'experience:one:title', 'experience:two:title'} <= kept  # the model still chooses the record
    assert not any(r.startswith('education:') for r in kept)


def test_an_open_question_keeps_the_resume_materials_and_its_answer():
    _, tools = session()
    snapshot = focus(tools, ['textarea:why:1'])
    assert snapshot['resume_context']['text'] == RESUME
    assert {'material:cover_letter', 'answer:textarea:why:1'} <= refs(snapshot)


def test_cited_facts_of_a_decision_stay_in_scope():
    _, tools = session()
    bound = tools.execute('bind_education', {'observation_id': tools.observation_id, 'education_id': 'a',
        'fields': [{'field_key': 'text:education--schoolName:1', 'component': 'school'}]})
    assert not bound.get('error'), bound
    tools.observe()
    kept = refs(focus(tools, ['text:education--schoolName:1']))
    assert {'education:a:school', 'education:a:degree'} <= kept


def test_a_draft_from_an_earlier_page_does_not_resend_the_resume():
    browser, tools = session(answer_drafts={'textarea:essay:1': {'field': {'label': 'Earlier essay', 'kind': 'textarea'},
                                                                 'text': 'Draft from an earlier page.'}})
    browser.fields = [f for f in page() if f['kind'] != 'textarea']
    snapshot = tools.observe()
    assert 'text' not in snapshot['resume_context']
    browser.fields = page()
    assert tools.observe()['resume_context']['text'] == RESUME  # an open question on this page still has it
