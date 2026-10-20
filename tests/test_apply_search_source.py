from copy import deepcopy
import pytest
from test_auto_apply_agent import AgentTools, RepairBrowser, state
from test_auto_apply import field
from eregion.runtime.auto_apply.browser import BrowserBlocked


def setup():
    browser=RepairBrowser()
    browser.fields=[field('School'),field('Major','combobox')]
    for f in browser.fields:f['dom_id']='education-1--'+f['key']
    saved=state()
    saved['profile']['education']=[{'id':'a','school':'North','field_of_study':'Physics'}, {'id':'b','school':'South','field_of_study':'History'}]
    s=AgentTools(browser,saved,autofill=lambda:{},active=lambda:True);s.observe()
    return browser,s


def test_unbound_query_does_not_authorize_a_write_or_save_mapping():
    b,s=setup();before=deepcopy(s.state);f=s.fields[1]
    assert s.search_source(f,'education:a:field_of_study')=='Physics'
    assert s.state==before
    with pytest.raises(BrowserBlocked,match='Bind this field'):
        s.source(f,'education:a:field_of_study')


def test_query_keeps_bound_component_and_row_identity_guards():
    b,s=setup();school,major=s.fields
    s._bind_education({'education_id':'a','fields':[{'field_key':school['key'],'component':'school'}]})
    with pytest.raises(BrowserBlocked,match='another record'):
        s.search_source(major,'education:b:field_of_study')
    assert s.search_source(major,'education:a:field_of_study')=='Physics'
    s._bind_education({'education_id':'a','fields':[{'field_key':major['key'],'component':'field_of_study'}]})
    with pytest.raises(BrowserBlocked,match='already bound'):
        s.search_source(major,'education:b:field_of_study')


def test_missing_query_fact_rejected():
    b,s=setup()
    with pytest.raises(BrowserBlocked,match='not in the resume'):
        s.search_source(s.fields[1],'education:unknown:field_of_study')


@pytest.mark.parametrize('case', ['valid','required','nonempty','invalid','stale','no_decision'])
def test_unbound_optional_omission_satisfies_education_mapping_only_when_verified(case):
    from eregion.runtime.auto_apply.education import gaps
    from eregion.runtime.auto_apply.decisions import signature
    from eregion.runtime.auto_apply.contracts import candidate_answer
    b,s=setup();school,major=s.fields
    s.state['profile']['education']=s.state['profile']['education'][:1]
    s._bind_education({'education_id':'a','fields':[{'field_key':school['key'],'component':'school'}]})
    school['value']='North';major['required']=False
    s.state['field_decisions']={major['key']:{'field':signature(major),'mode':'leave_empty','value':None,'sources':{},
        'original_answer':candidate_answer(major,s.state['profile'],s.state['answers']), 'reason':'No evidenced equivalent in observed choices.'}}
    if case=='required':major['required']=True
    if case=='nonempty':major['value']='Unconfirmed option'
    if case=='invalid':major['invalid']=True
    if case=='stale':s.state['field_decisions'][major['key']]['field']['group']='Different row'
    if case=='no_decision':s.state['field_decisions']={}
    pending=gaps(s.state,s.fields,{})
    assert any('Map education field' in x for x in pending) is (case!='valid')


@pytest.mark.parametrize('tool', ['inspect_field','search_options'])
def test_option_response_keeps_committed_value_separate_from_empty_results(tool):
    b,s=setup();f=s.fields[1];f['value']='Site taxonomy label';f['value_committed']=True
    b.fields[1].update(value=f['value'],value_committed=True)
    b.inspect_options=lambda key,query=None: []
    b.field=lambda key: next(x for x in b.fields if x['key']==key)
    args={'field_key':f['key'],'observation_id':s.observation_id}
    if tool=='search_options':args['source_ref']='education:a:field_of_study'
    before=deepcopy(s.state.get('field_decisions',{}))
    result=s.execute(tool,args)
    assert result['options']==[] and result['total_options']==0
    assert result['current_selection']=={'value':'Site taxonomy label','committed':True,'saved_decision':None}
    assert s.state.get('field_decisions',{})==before
    assert not s.state.get('education_bindings')
    assert f['value']=='Site taxonomy label'


def test_uncommitted_query_is_not_reported_as_a_selected_value():
    b,s=setup();f=s.fields[1];f['value']='Search text';f['value_committed']=False
    assert s.selected_option_context(f)=={}



def test_repeated_context_projection_preserves_global_field_index():
    from eregion.runtime.auto_apply.observation import focus_observation
    fields=[{'key':'one','dom_id':'education-1--school','label':'School','kind':'text'},
            {'key':'two','dom_id':'education-2--school','label':'School','kind':'text'}]
    snap={'page':{'url':'u','heading':'h','text':'raw'},'fields':deepcopy(fields),
          'record_rows':[{'row_id':'education-1','field_keys':['one']},{'row_id':'education-2','field_keys':['two']}]}
    state={'observation_scope':['one'],'observation_scope_page':['u','h']}
    first=focus_observation(snap,state,fields)
    second=focus_observation(deepcopy(first),state,fields)
    assert second==first
    assert [f['key'] for f in second['field_index']]==['two']
    assert fields[1]['key']=='two'


def questionnaire_setup():
    b=RepairBrowser()
    b.fields=[field('Unrelated optional field'),field('Current enrollment status','combobox')]
    b.fields[1]['dom_id']='questionnaire--current-status'
    b.fields[1]['options']=[{'label':'Currently enrolled','value':'enrolled'}]
    saved=state(answers={},education_required=True)
    saved['profile']['education']=[{'id':'a','school':'North','degree':'Masters','end_date':'Expected May 2027'}]
    t=AgentTools(b,saved,autofill=lambda:{},active=lambda:True);t.observe()
    return b,t


def test_questionnaire_education_interpretation_requires_verification_without_row_binding():
    from eregion.runtime.auto_apply.decisions import active_decision
    from eregion.runtime.auto_apply.education import gaps
    b,t=questionnaire_setup();f=t.fields[1];seen=[]
    t.verify_answers=lambda items,context=None: seen.append(items) or [True]
    args={'field_key':f['key'],'observation_id':t.observation_id,'mode':'equivalent',
          'value':'Currently enrolled','source_refs':['education:a:end_date'],
          'reason':'Interpret the cited current program fact in this question context.'}
    with pytest.raises(BrowserBlocked,match='interpretation'):
        t.source(f,'education:a:end_date')
    result=t.execute('resolve_field',args)
    assert result['verified'] and len(seen)==1
    assert seen[0][0]['sources']=={'education:a:end_date':'Expected May 2027'}
    assert not t.state.get('education_bindings')
    current=b.observe()[1]
    assert active_decision(current,t.state)['source_context']=='question'
    assert gaps(t.state,b.observe(),{})==[]
    # Cache only the same evidence/context. Source changes invalidate saved decisions.
    t.execute('resolve_field',args)
    assert len(seen)==1
    t.state['profile']['education'][0]['end_date']='May 2025'
    assert active_decision(current,t.state) is None


@pytest.mark.parametrize('verdict', [False,None,'yes'])
def test_questionnaire_unsupported_or_unverified_evidence_never_writes(verdict):
    b,t=questionnaire_setup();f=t.fields[1]
    if verdict is not None:t.verify_answers=lambda items,context=None:[verdict]
    with pytest.raises(BrowserBlocked):
        t.execute('resolve_field',{'field_key':f['key'],'observation_id':t.observation_id,'mode':'equivalent',
            'value':'Currently enrolled','source_refs':['education:a:end_date'],'reason':'Proposed interpretation.'})
    assert b.fields[1]['value']=='' and not t.state.get('field_decisions')
    assert not t.state.get('education_bindings')


def test_questionnaire_verification_preserves_record_guards_and_explicit_answers():
    b,t=questionnaire_setup();f=t.fields[1];sources={'education:a:end_date':'Expected May 2027'}
    assert t._question_education_sources(f,sources)
    f['dom_id']='education-1--degree'
    assert not t._question_education_sources(f,sources)
    f['dom_id']='questionnaire--current-status';t.state['answers'][f['key']]='Not enrolled'
    assert not t._question_education_sources(f,sources)


def test_questionnaire_question_cannot_be_bound_as_an_education_row():
    # Questionnaire replay 01 (Adobe-21 page 2): binding "current status" to the Cornell row
    # skipped the evidence verification the questionnaire path requires.
    b,t=questionnaire_setup();f=t.fields[1]
    row=field('School','text');row['dom_id']='education-1--school';b.fields.append(row);t.observe()
    with pytest.raises(BrowserBlocked,match='questionnaire question'):
        t.execute('bind_education',{'observation_id':t.observation_id,'education_id':'a',
            'fields':[{'field_key':row['key'],'component':'school'},{'field_key':f['key'],'component':'graduation_status'}]})
    assert not t.state.get('education_bindings') and b.fields[1]['value']==''
    t.execute('bind_education',{'observation_id':t.observation_id,'education_id':'a','fields':[{'field_key':row['key'],'component':'school'}]})
    assert list(t.state['education_bindings'])==[row['key']]


def test_extra_queries_run_in_one_call_and_report_each_result():
    # Dynamic-19: "Systems Engineering", "Engineering" and "Systems" took three model rounds.
    b,s=setup();major=s.fields[1];seen=[]
    catalogue={'Physics':[],'Applied Physics':[{'label':'Applied Physics','value':'ap'}],'Science':[{'label':'Physical Science','value':'ps'},{'label':'Applied Physics','value':'ap'}]}
    b.inspect_options=lambda key,query=None:seen.append(query) or deepcopy(catalogue.get(query,[]))
    b.field=lambda key:next(f for f in b.fields if f['key']==key)
    result=s.execute('search_options',{'observation_id':s.observation_id,'field_key':major['key'],'source_ref':'education:a:field_of_study',
        'query':'Physics','query_reason':'Shorter and related terms for the cited major.','extra_queries':['Applied Physics','Science','Physics']})
    assert seen==['Physics','Applied Physics','Science']            # duplicate of the main query not repeated
    assert [x['query'] for x in result['additional_searches']]==['Applied Physics','Science']
    # Labels in options_sharing_words are listed there once (Adobe run 41); the count stays.
    assert result['options_sharing_words']==['Applied Physics']
    science=result['additional_searches'][1]
    assert science['options']==['Physical Science'] and science['in_options_sharing_words']==1 and science['total_options']==2
    assert [o['label'] for o in s.observed_options[major['key']]]==['Applied Physics','Physical Science']
    assert s.state['option_search_log'][major['key']]['queries']==['Physics','Applied Physics','Science']
    assert major['value']=='' and not s.state.get('field_decisions')      # searching decides nothing


def test_extra_queries_need_a_reason_and_are_bounded():
    b,s=setup();major=s.fields[1]
    b.inspect_options=lambda key,query=None:[]
    b.field=lambda key:next(f for f in b.fields if f['key']==key)
    for extra,reason in [(['a','b','c','d'],'r'),(['ok'],None),([''],'r')]:
        args={'observation_id':s.observation_id,'field_key':major['key'],'source_ref':'education:a:field_of_study','extra_queries':extra}
        if reason:args['query_reason']=reason
        with pytest.raises(BrowserBlocked):
            s.execute('search_options',args)


def test_degree_decision_may_cite_the_bound_record_school_but_not_another_record():
    # Adobe-36: resolve_field(Masters) for Cornell cited its degree and school and was refused.
    browser=RepairBrowser()
    browser.fields=[field('School'),field('Degree','combobox')]
    for f in browser.fields:f['dom_id']='education-1--'+f['key']
    browser.fields[1]['options']=[{'label':'Bachelors','value':'b'},{'label':'Masters','value':'m'}]
    saved=state()
    saved['profile']['education']=[{'id':'a','school':'Cornell University','degree':'M.Eng.'},{'id':'b','school':'Wisconsin','degree':'B.A.'}]
    s=AgentTools(browser,saved,autofill=lambda:{},active=lambda:True);s.observe()
    degree=s.fields[1]
    s._bind_education({'education_id':'a','fields':[{'field_key':degree['key'],'component':'degree'}]})
    s.observe()
    decide=lambda refs:s.execute('resolve_field',{'observation_id':s.observation_id,'field_key':degree['key'],'mode':'equivalent',
        'value':'Masters','source_refs':refs,'reason':'M.Eng. is a master\'s-level degree.'})
    with pytest.raises(BrowserBlocked,match='bound education record is a'):
        decide(['education:a:degree','education:b:degree'])
    with pytest.raises(BrowserBlocked,match='bound component education:a:degree'):
        decide(['education:a:school'])
    s.observe()
    result=decide(['education:a:degree','education:a:school'])
    assert result['verified'] and browser.fields[1]['value']=='Masters'
    from eregion.runtime.auto_apply.decisions import active_decision
    assert set(active_decision(browser.fields[1],s.state)['sources'])=={'education:a:degree','education:a:school'}
    s.state['profile']['education'][0]['school']='Cornell Tech'  # a cited fact changed: the decision retires
    assert active_decision(browser.fields[1],s.state) is None
