from copy import deepcopy
from eregion.runtime.auto_apply.observation import observation_messages
from test_apply_experience_context import session

def test_progress_chatter_does_not_evict_user_evidence_from_real_observation():
    b,s=session()
    first={"id":"original","role":"user","text":"I need sponsorship; I will relocate at my own expense."}
    s.state["messages"]=[first]+[{"id":str(i),"role":"assistant","text":"Working"} for i in range(30)]
    original=deepcopy(s.state["messages"])
    actual=s.observe()["user_messages"]
    assert actual[0]==first and len(actual)==5
    assert s.state["messages"]==original

def test_order_and_question_identity_survive_projection():
    messages=[{"id":"first","role":"user","text":"Old fact"},
       {"id":"q","role":"assistant","text":"Questions","question_keys":["one","two"]},
       {"id":"reply","role":"user","text":"1. Yes 2. No","in_reply_to":"q"},
       *[{"id":str(i),"role":"assistant","text":"Working"} for i in range(25)],
       {"id":"correction","role":"user","text":"Correction: second answer is Yes."}]
    result=observation_messages(messages)
    assert result[0]==messages[0] and result[1]==messages[1] and result[2]==messages[2]
    assert result[-1]==messages[-1]
    assert len(result)==7

def test_initial_request_and_recent_users_have_bounded_progress_window():
    rows=[{"id":str(i),"role":"user","text":str(i)} for i in range(100)]
    result=observation_messages(rows)
    assert result==[rows[0],*rows[-12:]]
