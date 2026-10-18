"""Page actions re-identify their control after writes renumber the page (Adobe-35/36)."""
import os
import pytest

from eregion.runtime.auto_apply.browser import BrowserBlocked

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')

PAGE = '''<form id="application"><h2>My Experience</h2><label for="t">Job Title*</label><input id="t" required>
<div id="extra"></div><button type="button" data-automation-id="pageFooterNextButton"
 onclick="window.nexts=(window.nexts||0)+1;document.querySelector('h2').textContent='Application Questions'">Next</button></form>'''


@pytest.fixture
def browser():
    from test_auto_apply_browser import FixtureBrowser
    b = FixtureBrowser('https://example.wd5.myworkdayjobs.com/en-US/External/job/x/apply')
    b.page.set_content(PAGE)
    try:
        yield b
    finally:
        b.close()


def next_control(b):
    return next(c for c in b.page_state()['controls'] if c['label'] == 'Next')


def test_next_still_resolves_after_a_write_renumbers_controls(browser):
    observed = next_control(browser)
    # A write reveals a new button before Next (e.g. a row's Delete), shifting the index.
    browser.page.evaluate("document.getElementById('extra').innerHTML='<button type=button>Delete</button>'")
    assert next_control(browser)['id'] != observed['id']
    browser.click_page_action(observed)
    assert browser.page.evaluate('window.nexts') == 1


def test_ambiguous_or_missing_controls_require_a_new_observation(browser):
    observed = next_control(browser)
    browser.page.evaluate("""document.getElementById('extra').innerHTML=
        '<button type=button data-automation-id=pageFooterNextButton>Next</button>'""")
    with pytest.raises(BrowserBlocked, match='2 matching controls') as error:
        browser.click_page_action({**observed, 'id': 'ns-action-99'})
    assert error.value.code == 'stale_observation'
    browser.page.evaluate("document.querySelectorAll('button').forEach(b=>b.remove())")
    with pytest.raises(BrowserBlocked, match='no matching control'):
        browser.click_page_action(observed)
    assert not browser.page.evaluate('window.nexts')


def test_submit_controls_stay_blocked(browser):
    with pytest.raises(BrowserBlocked, match='final submission review'):
        browser.click_page_action({**next_control(browser), 'kind': 'submit'})
