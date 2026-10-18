"""E1.1 document uploads: durable per-application records, strict readback, no blind appends.

Intel-34 (2026-10-17): Workday appends every upload and Review listed resume.pdf three times;
the scan merged the names, and a non-empty control counted as a verified upload."""
import os
import pytest

from eregion.runtime.auto_apply.autofill import autofill, upload_plan, pending_documents
from eregion.runtime.auto_apply.agent import AgentTools
from eregion.runtime.auto_apply.browser import BrowserBlocked

RESUME, TAILORED = b'%PDF-1 resume v1', b'%PDF-1 resume v2'


class UploadBrowser:
    """A Workday-like appending upload control (or a single-file input) with observable cards."""
    def __init__(self, cards=(), appends=True, complete_late=False):
        self.cards, self.appends, self.uploads, self.complete_late = list(cards), appends, 0, complete_late
        self.pending_card = None

    def field(self):
        return {'id':'r','key':'file:Resume/CV:1','kind':'file','label':'Resume/CV','required':True,
                'value':'; '.join(dict.fromkeys(self.cards)),'attachments':list(self.cards),'appends':self.appends,
                'invalid':False,'options':[],'errors':[]}

    def observe(self):
        if self.pending_card:
            self.cards.append(self.pending_card); self.pending_card = None
        return [self.field()]

    def barrier(self):return None

    def fill(self, field, value):
        self.uploads += 1
        name = field['upload_filename']
        if not self.appends:
            self.cards = [name]
        elif self.complete_late:
            # The site's success card appears only after our wait gave up.
            self.pending_card = name
            raise BrowserBlocked('Upload did not confirm in time.', code='dom_interaction_failed')
        else:
            self.cards.append(name)


def state(**extra):
    return {'profile':{}, 'answers':{}, 'document_refresh':{'resume':True}, 'automation_mode':'hybrid',
            'material_refs':{'tailored_resume':{'pack_id':'p','item_id':'i','version':1}}, **extra}


def run(browser, data, current=RESUME):
    return autofill(browser, data, lambda: current)


def test_upload_is_recorded_and_consecutive_autofills_do_not_append_again():
    browser, data = UploadBrowser(), state()
    first = run(browser, data)
    assert first['filled'] == ['file:Resume/CV:1'] and browser.cards == ['resume.pdf']
    record = data['document_uploads']['resume']
    assert record['status'] == 'verified' and record['before'] == 0 and record['after'] == 1
    assert record['material_ref'] == {'pack_id':'p','item_id':'i','version':1}
    data['document_refresh']['resume'] = True  # e.g. a later step re-requests the same material
    run(browser, data); run(browser, data)
    assert browser.uploads == 1 and browser.cards == ['resume.pdf']
    assert pending_documents(data, [browser.field()]) == []


def test_a_new_control_showing_the_recorded_upload_is_not_uploaded_again():
    # Workday carries the step-1 upload into My Experience's Resume/CV control (re-render / new page).
    data = state(); run(UploadBrowser(), data)
    data['document_refresh']['resume'] = True
    carried = UploadBrowser(cards=['resume.pdf'])
    run(carried, data)
    assert carried.uploads == 0 and data['document_refresh']['resume'] is False


def test_restart_uses_the_durable_record_not_browser_memory():
    import json
    data = state(); run(UploadBrowser(), data)
    restored = json.loads(json.dumps(data))  # what the repository persists across a restart
    restored['document_refresh']['resume'] = True
    browser = UploadBrowser(cards=['resume.pdf'])
    run(browser, restored)
    assert browser.uploads == 0


def test_an_upload_that_completes_after_the_wait_is_recognized_not_repeated():
    browser, data = UploadBrowser(complete_late=True), state()
    result = run(browser, data)
    assert browser.uploads == 1 and data['document_uploads']['resume']['status'] in {'attempted', 'verified'}
    if data['document_uploads']['resume']['status'] == 'attempted':
        assert 'file:Resume/CV:1' in result['failed']
    run(browser, data)
    assert browser.uploads == 1 and browser.cards == ['resume.pdf']
    assert data['document_uploads']['resume']['status'] == 'verified'


def test_new_material_version_is_held_on_an_appending_control_with_the_old_copy():
    data = state(); browser = UploadBrowser(); run(browser, data)
    data['document_refresh']['resume'] = True  # replace_documents after a tailored resume changed
    result = run(browser, data, current=TAILORED)
    assert browser.uploads == 1 and browser.cards == ['resume.pdf']
    assert result['failed']['file:Resume/CV:1'] == 'document_existing_unverified'
    assert 'older version' in result['failure_details']['file:Resume/CV:1']['error']
    assert pending_documents(data, [browser.field()]) == ['resume']  # still owed, not silently done


def test_same_name_different_content_is_not_treated_as_uploaded():
    data = state(document_uploads={'resume':{'fingerprint':'0'*64,'filename':'resume.pdf','status':'verified','before':0}})
    plan = upload_plan(UploadBrowser(cards=['resume.pdf']).field(), data, 'resume', RESUME)
    assert plan['action'] == 'hold'


def test_unknown_existing_attachment_is_held_until_the_user_decides():
    browser, data = UploadBrowser(cards=['My CV 2025.pdf']), state()
    result = run(browser, data)
    assert browser.uploads == 0 and result['failed']['file:Resume/CV:1'] == 'document_existing_unverified'
    assert 'did not upload' in result['failure_details']['file:Resume/CV:1']['error']
    # The user's literal confirmation lets the model keep it; autofill records that without uploading.
    data['messages'] = [{'id':'m1','role':'user','text':'Keep my CV that is already attached.'}]
    tools = AgentTools(browser, data, autofill=lambda: {}, active=lambda: True)
    tools.observe()
    with pytest.raises(BrowserBlocked, match='literal quote'):
        tools.execute('bind_document', {'observation_id':tools.observation_id,'field_key':'file:Resume/CV:1',
            'document':'resume','keep_existing':{'message_id':'m1','quote':'replace it'}})
    kept = tools.execute('bind_document', {'observation_id':tools.observation_id,'field_key':'file:Resume/CV:1',
        'document':'resume','keep_existing':{'message_id':'m1','quote':'Keep my CV that is already attached'}})
    assert kept['kept'] == ['My CV 2025.pdf']
    run(browser, data)
    assert browser.uploads == 0 and data['document_uploads']['resume']['status'] == 'kept_by_user'
    assert pending_documents(data, [browser.field()]) == []


def test_a_single_file_input_is_replaced_and_verified_by_its_own_file():
    browser, data = UploadBrowser(cards=['resume.pdf'], appends=False), state(
        document_uploads={'resume':{'fingerprint':'0'*64,'filename':'resume.pdf','status':'verified','before':0}})
    result = run(browser, data, current=TAILORED)
    assert browser.uploads == 1 and result['filled'] == ['file:Resume/CV:1']
    assert data['document_uploads']['resume']['status'] == 'verified'


def test_a_vanished_control_is_not_upload_evidence():
    class Vanishing(UploadBrowser):
        def observe(self):
            return [] if self.uploads else [self.field()]
    browser, data = Vanishing(), state()
    result = run(browser, data)
    assert data['document_uploads']['resume']['status'] == 'attempted'
    assert 'file:Resume/CV:1' in result['failed']


@pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Offline Chrome opt-in')
def test_chrome_scan_reports_every_card_and_autofill_appends_once():
    from test_auto_apply_browser import FixtureBrowser
    b = FixtureBrowser('https://example.wd5.myworkdayjobs.com/en-US/External/job/x/apply')
    try:
        b.page.set_content('''<form><div data-automation-id="attachments-FileUpload"><label for="r">Resume/CV</label>
          <input type="file" id="r" multiple onchange="window.uploads=(window.uploads||0)+1;for(const f of this.files){
            const item=document.createElement('div');item.setAttribute('data-automation-id','file-upload-item');
            item.innerHTML='<span data-automation-id=&quot;file-upload-item-name&quot;>'+f.name+'</span><span data-automation-id=&quot;file-upload-successful&quot;>ok</span>';
            this.parentElement.append(item)}"></div>
          <div data-automation-id="file-upload-item"><span data-automation-id="file-upload-item-name">resume.pdf</span>
            <span data-automation-id="file-upload-successful">ok</span></div></form>''')
        # A card outside the control does not belong to it; the scan reads cards within the control.
        field = b.observe()[0]
        assert field['appends'] is True and field['attachments'] == []
        data = state()
        autofill(b, data, lambda: RESUME); autofill(b, data, lambda: RESUME)
        field = b.observe()[0]
        assert b.page.evaluate('window.uploads') == 1 and field['attachments'] == ['resume.pdf']
        assert data['document_uploads']['resume']['status'] == 'verified'
    finally:
        b.close()
