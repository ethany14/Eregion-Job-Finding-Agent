from eregion.runtime.career.search_filters import effective_filters, filter_cards

def test_filters_and_unknowns():
    state={'goal':'Find junior data analyst jobs in New York'}
    filters=effective_filters(state,{'work_type':'remote','posted':'week'})
    assert filters['posted']=='week' and filters['work_type']=='remote' and filters['experience_level']=='entry'
    jobs=[{'title':t,'url':str(i)} for i,t in enumerate(['Senior Data Analyst','Junior Data Analyst','Data Analyst','Graduate Data Analyst'])]
    kept,info=filter_cards(jobs,state)
    # Cards are only ordered; the model rejects a level mismatch after reading the posting.
    assert sorted(j['url'] for j in kept)==sorted(j['url'] for j in jobs) and info['excluded_count']==0
    assert sorted(j['url'] for j in filter_cards(jobs,{'goal':'Find data analyst jobs'})[0])==sorted(j['url'] for j in jobs)
