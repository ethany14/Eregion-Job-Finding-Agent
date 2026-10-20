from copy import deepcopy
from types import SimpleNamespace
import time
from test_apply_experience_context import session
from eregion.runtime.auto_apply.runner import ApplicationHost
from eregion.runtime.auto_apply.agent import BROWSER_NAMES


def test_form_tools_load_browser_group_only_on_model_request():
    browser, s = session()
    browser.page_state=lambda:{'stage':'application','url':'https://example.test','heading':'Form','controls':[],'text':''}
    host=ApplicationHost(s,SimpleNamespace(timeout=45),active=lambda:True,reserve=lambda _:True,
                         report=lambda *_:None,decisions=SimpleNamespace(enabled=False))
    try:
        host.deadline=time.monotonic()+120
        host._prepare_page=s.observe
        turn=host.observe(); names={t['function']['name'] for t in turn.tools}
        assert not names & BROWSER_NAMES
        assert {'record_answers','resolve_field','click_page_action','ask_user','enable_browser_tools'} <= names
        # P4: record tools are for record pages; this form has none, and the observation names them.
        assert 'apply_record' not in names and 'apply_record' in turn.snapshot['browser_tools']['form_tools_not_offered_here']
        before=deepcopy(browser.fields); decisions=deepcopy(s.state.get('field_decisions'))
        s.execute('enable_browser_tools',{})
        assert browser.fields==before and s.state.get('field_decisions')==decisions
        expanded={t['function']['name'] for t in host.observe().tools}
        assert 'browser_read' in expanded and 'browser_click' in expanded and 'apply_record' in expanded
        assert 'enable_browser_tools' not in expanded
        assert not any(n.startswith('computer_') for n in expanded)
        s.state['browser_tools_requested']=False
        browser.page_state=lambda:{'stage':'job_page','url':'https://example.test','heading':'Job','controls':[],'text':''}
        assert 'browser_read' in {t['function']['name'] for t in host.observe().tools}
    finally:host.requests.close()
