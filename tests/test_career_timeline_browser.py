"""The actual chat DOM keeps old result cards above later messages."""
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

pytestmark = pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS') != '1', reason='Opt-in offline Chrome')
WEB = Path(__file__).resolve().parents[1] / 'frontend/web_app'


@pytest.mark.parametrize('theme', ['light', 'night'])
@pytest.mark.parametrize('width', [1440, 390])
def test_result_cards_are_chronological_after_reply_poll_and_reload(tmp_path, theme, width):
    def at(minute): return f'2026-10-09T10:{minute:02d}:00Z'
    item = {'run_id': 'timeline', 'career': True, 'title': 'Career timeline', 'status': 'completed',
        'version': 1, 'fields': [], 'review_fields': [], 'pending_fields': [], 'message': '',
        'actions': [{'at': at(4), 'tool': 'score_job', 'outcome': 'completed'}],
        'jobs': [{'application_id': 'job', 'title': 'AI Deployment Analyst', 'company': 'Example',
            'match_score': 82, 'status': 'needs_evidence', 'result_at': at(1).removesuffix('Z')}],
        'materials': [{'pack_id': 'pack', 'status': 'awaiting_review', 'items': [
            {'pack_item_id': 'resume', 'artifact_type': 'tailored_resume', 'status': 'awaiting_review',
                'version': 1, 'text': 'Synthetic resume text', 'created_at': at(3)}]}],
        'interviews': [],
        'usage_summary': {'calls': 3, 'input_tokens': 12345, 'output_tokens': 600, 'unknown_usage_calls': 1,
            'discarded': 1, 'groups': [{'purpose': 'prepare_materials:ResumeSelection', 'model': 'example-model',
                'calls': 3, 'input_tokens': 12345, 'output_tokens': 600, 'unknown_usage_calls': 1}]},
        'messages': [{'id': 'u1', 'role': 'user', 'text': 'Review this job.', 'at': at(0)},
            {'id': 'a1', 'role': 'assistant', 'text': 'The match score is ready.', 'at': at(2)},
            {'id': 'a2', 'role': 'assistant', 'text': 'Your resume is ready.', 'at': at(4)},
            {'id': 'u2', 'role': 'user', 'text': 'What should I do next?', 'at': at(5)},
            {'id': 'a3', 'role': 'assistant', 'text': 'Latest reply remains below your earlier results.', 'at': at(6)}]}
    errors, sent = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 900}, timezone_id='America/New_York', locale='en-US')
        page.add_init_script(f"localStorage.setItem('eregion.theme','{theme}')")
        page.on('pageerror', lambda error: errors.append(str(error)))

        def route(r):
            u = urlsplit(r.request.url)
            if u.netloc != 'localhost': return r.abort()
            if u.path == '/api/career/tasks': return r.fulfill(json={'attempts': [] if u.query else [item], 'groups': []})
            if u.path == '/api/career/tasks/timeline': return r.fulfill(json=item)
            if u.path == '/api/career/tasks/timeline/chat':
                data = r.request.post_data_json
                sent.append(data)
                item['messages'].extend([
                    {'id': data['message_id'], 'role': 'user', 'text': data['text'], 'at': at(8)},
                    {'id': 'a4', 'role': 'assistant', 'text': 'New response below the cards.', 'at': at(9)}])
                return r.fulfill(json=item)
            if u.path == '/api/auto-apply/candidate': return r.fulfill(json={'profile': {}, 'resume_base64': None, 'needs_extraction': False})
            if u.path.startswith('/api/'): return r.fulfill(json={'applications': [], 'totals': {}, 'digests': [], 'schedules': []})
            target = WEB / ('apply.html' if u.path == '/apply' else u.path.removeprefix('/').removeprefix('web-assets/'))
            if target.is_file(): return r.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(target)[0] or 'text/plain')
            return r.fulfill(status=404)

        page.route('**/*', route)

        def open_chat():
            page.goto('http://localhost/apply')
            if width < 760: page.locator('#history-toggle').click()
            page.locator('#attempts button').first.click()
            expect(page.locator('[data-message-id="a3"]')).to_be_visible()

        def order():
            return page.locator('#agent-messages').evaluate('(root)=>[...root.children].map(n=>n.dataset.messageId||n.dataset.resultKey)')

        open_chat()
        assert order() == ['u1', 'job:job', 'a1', 'material:resume', 'a2', 'u2', 'a3']
        assert not page.locator('#agent-trace').evaluate('(n)=>n.open')
        page.locator('.doc-text summary').click()
        # An unrelated poll must keep the document's expansion and DOM position.
        page.wait_for_timeout(2200)
        assert page.locator('.doc-text').evaluate('(n)=>n.open')
        page.locator('#chat-text').fill('Continue with the next step.')
        page.locator('#send-message').click()
        expect(page.locator('[data-message-id="a4"]')).to_contain_text('New response below the cards.')
        assert order()[-2:] == [sent[0]['message_id'], 'a4']
        expect(page.locator('[data-message-id="a4"]')).to_be_in_viewport()
        assert page.locator('#career-results .career-material').count() == 1
        assert page.locator('.doc-text').evaluate('(n)=>n.open')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'timeline-{theme}-{width}.png'), full_page=True)
        page.locator('#browser-toggle').click()
        expect(page.locator('#perf-summary')).to_contain_text('12,345 input / 600 output tokens reported')
        page.locator('#usage-details summary').click()
        expect(page.locator('#usage-details')).to_contain_text('prepare materials:ResumeSelection')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'usage-{theme}-{width}.png'), full_page=True)
        page.locator('#browser-close').click()
        open_chat()
        assert order()[-2:] == [sent[0]['message_id'], 'a4']
        page.locator('.career-job button', has_text='Prepare materials').click()
        expect(page.locator('#send-message')).to_be_enabled()
        assert sent[-1]['context'] == {'application_id': 'job'}
        assert not errors
        browser.close()
