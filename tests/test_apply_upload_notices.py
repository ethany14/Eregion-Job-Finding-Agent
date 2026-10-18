"""Browser regression for Adobe-11 success alerts and delayed Next, no provider/site."""
import os
import pytest
from test_auto_apply_workday import WorkdayBrowser, URL

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Offline Chrome opt-in')
HTML="""<html><body><main><div aria-current="step">current step 3 of 7</div><form>
<div data-automation-id="attachments-FileUpload"><label for="cv">Resume/CV</label><input id="cv" type="file">
<div data-automation-id="file-upload-item"><span data-automation-id="file-upload-item-name">resume.pdf</span>
<span data-automation-id="file-upload-successful">Attached</span></div>
<div role="alert">resume.pdf successfully uploaded</div></div>
<button type="button" onclick="setTimeout(()=>document.querySelector('[aria-current]').textContent='current step 4 of 7',3100)">Next</button>
</form></main></body></html>"""
class UploadBrowser(WorkdayBrowser):
    def _guard_request(self,route):
        if route.request.is_navigation_request():route.fulfill(content_type='text/html',body=HTML)
        else:route.abort()


def test_successful_upload_alert_is_not_invalid_and_next_waits_for_real_step():
    b=UploadBrowser(URL)
    try:
        f=b.observe()[0]
        assert f['value']=='resume.pdf' and not f['invalid'] and not f['errors']
        page=b.page_state();assert not page['errors']
        result=b.click_page_action(next(c for c in page['controls'] if c['kind']=='next'))
        assert result['verified'] and result['step_before']=='step:3' and result['step_after']=='step:4'
    finally:b.close()


@pytest.mark.parametrize('mode',['real_error','aria_invalid','no_success_card'])
def test_success_does_not_hide_real_errors_or_unverified_upload(mode):
    b=UploadBrowser(URL)
    try:
        if mode=='real_error':
            b.page.evaluate("document.querySelector('[data-automation-id=attachments-FileUpload]').insertAdjacentHTML('beforeend','<div data-automation-id=errorMessage>File rejected</div>')")
        elif mode=='aria_invalid':b.page.locator('#cv').evaluate("e=>e.setAttribute('aria-invalid','true')")
        else:b.page.locator('[data-automation-id=file-upload-successful]').evaluate('e=>e.remove()')
        f=b.observe()[0];assert f['invalid']
        if mode!='aria_invalid':assert b.page_state()['errors']
        if mode=='real_error':assert f['errors']==['File rejected']
    finally:b.close()
