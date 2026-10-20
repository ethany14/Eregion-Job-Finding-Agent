"""Workday-style pickers that filter on single words: one search call runs the words too (2026-10-20)."""
from eregion.runtime.auto_apply.agent import AgentTools, fallback_words, shared_word_options
from test_auto_apply import field

CATALOG = ['Accounting', 'Actuarial Science', 'Aerospace Engineering', 'Computer and Information Science',
           'Computer Systems Analysis', 'Electrical Engineering', 'Industrial Engineering', 'Management Information Systems']


class WordFilterBrowser:
    """A multi-word query leaves the full list (as Adobe's major picker did); one word filters."""

    def __init__(self):
        self.fields = [{**field('text:education--fieldOfStudy:1', label='Field of Study', required=False),
                        'dom_id': 'education-1--fieldOfStudy', 'choice_control': True}]
        self.queries = []
        self.option_search_evidence = {}

    def observe(self):
        return [dict(f) for f in self.fields]

    def field(self, key):
        return next(dict(f) for f in self.fields if f['key'] == key)

    def inspect_options(self, key, query=None):
        self.queries.append(query)
        words = (query or '').split()
        hits = CATALOG if len(words) != 1 else [c for c in CATALOG if words[0].casefold() in c.casefold()]
        return [{'label': c, 'value': c} for c in hits]


def tools(fact='Computer Science'):
    browser = WordFilterBrowser()
    state = {'profile': {'education': [{'id': 'b', 'school': 'School B', 'field_of_study': fact}], 'experience': []},
             'answers': {}, 'education_bindings': {}}
    session = AgentTools(browser, state, autofill=lambda: {}, active=lambda: True)
    session.observe()
    return browser, session


def test_helpers_pick_distinctive_words_and_rank_shared_options():
    assert fallback_words('Computer and Information Science') == ['Computer', 'Information', 'Science']
    assert fallback_words('Physics') == []
    ranked = shared_word_options('Computer Science', [{'label': c} for c in CATALOG])
    assert ranked[0] == 'Computer and Information Science' and 'Accounting' not in ranked


def test_the_broader_option_of_the_same_discipline_ranks_first_among_equals():
    # The owner's rule (2026-10-20): Systems Engineering -> Engineering; Computer Science ->
    # Computer and Information Science. Ranking is a hint; the model saves the decision.
    options = [{'label': c} for c in ['Aerospace Engineering', 'Industrial Engineering', 'Computer Systems Analysis', 'Engineering']]
    assert shared_word_options('Systems Engineering', options)[0] == 'Engineering'
    from eregion.runtime.auto_apply.prompt import SYSTEM
    assert 'Systems Engineering -> Engineering' in SYSTEM and 'Computer and Information Science' in SYSTEM


def test_one_search_call_runs_the_words_when_the_phrase_has_no_literal_match(monkeypatch):
    browser, session = tools()
    monkeypatch.setattr(session, 'search_source', lambda field, ref: 'Computer Science')
    result = session.execute('search_options', {'observation_id': session.observation_id,
                                                'field_key': 'text:education--fieldOfStudy:1',
                                                'source_ref': 'education:b:field_of_study'})
    assert browser.queries[1:] == ['Computer', 'Science']  # after the phrase, in the same call
    assert all(item['automatic'] for item in result['additional_searches'])
    assert result['options_sharing_words'][0] == 'Computer and Information Science'
    assert 'unresolved' in result  # no literal match: the equivalence decision stays the model's
    assert not session.state.get('field_decisions')


def test_a_literal_match_runs_no_extra_searches(monkeypatch):
    browser, session = tools('Accounting')
    monkeypatch.setattr(session, 'search_source', lambda field, ref: 'Accounting')
    result = session.execute('search_options', {'observation_id': session.observation_id,
                                                'field_key': 'text:education--fieldOfStudy:1',
                                                'source_ref': 'education:b:field_of_study', 'query': 'Accounting'})
    assert browser.queries == ['Accounting'] and 'additional_searches' not in result


def test_an_identical_search_on_the_same_page_reuses_its_results(monkeypatch):
    browser, session = tools()
    monkeypatch.setattr(session, 'search_source', lambda field, ref: 'Computer Science')
    args = {'observation_id': session.observation_id, 'field_key': 'text:education--fieldOfStudy:1',
            'source_ref': 'education:b:field_of_study'}
    first = session.execute('search_options', dict(args))
    driven = list(browser.queries)
    session.observe()
    again = session.execute('search_options', {**args, 'observation_id': session.observation_id})
    assert browser.queries == driven  # the picker was not driven again
    assert again['reused_results']['queries'] == ['Computer Science', 'Computer', 'Science']
    assert again['options_sharing_words'] == first['options_sharing_words']


def test_a_changed_field_value_runs_the_search_again(monkeypatch):
    browser, session = tools()
    monkeypatch.setattr(session, 'search_source', lambda field, ref: 'Computer Science')
    args = {'field_key': 'text:education--fieldOfStudy:1', 'source_ref': 'education:b:field_of_study'}
    session.execute('search_options', {**args, 'observation_id': session.observation_id})
    browser.fields[0]['value'] = 'Accounting'  # something was selected meanwhile
    count = len(browser.queries)
    session.observe()
    result = session.execute('search_options', {**args, 'observation_id': session.observation_id})
    assert len(browser.queries) > count and 'reused_results' not in result
