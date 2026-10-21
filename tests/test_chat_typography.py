"""Readability and safe Markdown rendering in the real chat, without model calls."""
import os

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from eregion.api.main import create_app

pytestmark=pytest.mark.skipif(os.getenv('JOB_AGENT_BROWSER_TESTS')!='1',reason='Opt-in offline Chrome integration.')

REPLY='''I am **Northstar**, your AI task partner for your job search. Here is how I can help:

### 1. Job discovery & fit analysis

* **Find opportunities:** Search company postings for roles that match your background and interests.
* **Match & gap scoring:** Compare job requirements with your resume and identify gaps to address.

### 2. Tailored resumes & cover letters

* **Evidence-grounded drafting:** Highlight relevant projects and metrics without inventing claims.
* **Iterative revisions:** Refine sections, bullet points, and tone from your feedback.

### 3. Application assistance

Help prepare applications and keep your review and submission in the conversation.

### 4. Interview preparation

Practice job-specific questions and get clear, focused feedback.

---

### Next steps for you

For the **AI Deployment Analyst** position at **Silna**, we can:

1. Tailor your resume around `JobHunterAgent` and your Python/SQL experience.
2. Add any relevant details you provide about document parsing.
3. Start a practice interview about agentic AI.

中文也能清晰显示：**先选一个目标**，我们再继续。

> Your original resume remains available for future applications.

| Next step | What you receive | Your review |
| --- | --- | --- |
| Compare fit | Evidence and gaps | Review the findings |
| Prepare materials | Resume and letter | Approve each version |

```python
print("Ready for the next step")
```

[View the job](https://example.com/jobs/123?a=1&b=2)
'''


@pytest.mark.parametrize('width',[1440,390])
def test_chat_markdown_readability_copy_and_untrusted_content(tmp_path,width):
    item={'run_id':'typography-test','career':True,'title':'Career overview','status':'needs_input',
        'version':1,'messages':[{'id':'u1','role':'user','text':'How can you help?'},
            {'id':'a1','role':'assistant','text':REPLY}],
        'input_request':{'id':'a1','text':REPLY,'field_keys':[],'fields':[]},
        'fields':[],'actions':[],'review_fields':[],'pending_fields':[],
        'jobs':[],'materials':[],'interviews':[],'task_error':None,'message':'Reply in chat.'}
    errors,requests=[],[]
    with TestClient(create_app(run_service=object()),base_url='http://localhost') as client,sync_playwright() as playwright:
        browser=playwright.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(locale='en-US',viewport={'width':width,'height':1000},device_scale_factor=1)
        page.add_init_script("Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.copiedReply=text}}})")
        page.on('pageerror',lambda error:errors.append(str(error)))
        def handle(route):
            request=route.request;requests.append(request.url)
            if request.url=='http://localhost/api/career/tasks':
                route.fulfill(json={'attempts':[item]});return
            if request.url=='http://localhost/api/auto-apply/candidate':
                route.fulfill(json={'profile':{},'resume_base64':None,'needs_extraction':False});return
            if request.url.startswith('http://localhost/api/applications/overview'):
                route.fulfill(json={'applications':[],'funnel':[],'weekly':[],'totals':{}});return
            if request.url.startswith('http://localhost/'):
                response=client.get(request.url)
                route.fulfill(status=response.status_code,body=response.content,
                    headers={'content-type':response.headers.get('content-type','text/plain')});return
            route.abort()
        page.route('**/*',handle)
        page.goto('http://localhost/apply')
        if width<760:page.locator('#history-toggle').click()
        page.locator('#attempts button').first.click()
        body=page.locator('.message.assistant .message-body')
        expect(body.locator('h4')).to_have_count(5)
        expect(body.locator('ol>li')).to_have_count(3)
        expect(body.locator('strong').first).to_have_text('Northstar')
        expect(body.locator('code').first).to_have_text('JobHunterAgent')
        expect(body.locator('table')).to_have_count(1)
        expect(page.locator('#conversation-actions .message-body')).to_have_count(0)
        expect(page.locator('#conversation-actions')).to_contain_text('Reply in the chat below.')
        assert '###' not in body.inner_text() and '**Northstar**' not in body.inner_text()
        link=body.get_by_role('link',name='View the job')
        assert link.get_attribute('href')=='https://example.com/jobs/123?a=1&b=2'
        assert link.get_attribute('rel')=='noopener noreferrer'
        assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
        if width<760:assert body.locator('.message-table').evaluate('(n)=>n.scrollWidth>n.clientWidth')
        page.get_by_role('button',name='Copy reply').click()
        page.wait_for_function("window.copiedReply?.includes('Next steps for you')")
        assert '**' not in page.evaluate('window.copiedReply')
        page.locator('#chat-scroll').evaluate('(n)=>{n.style.scrollBehavior="auto";n.scrollTop=0}')
        page.screenshot(path=str(tmp_path/f'chat-typography-{width}.png'))

        hostile='''<script>window.unsafeMarkdown=true</script>

<img src="https://untrusted.invalid/html-pixel" onerror="window.unsafeMarkdown=true">

![tracking](https://untrusted.invalid/markdown-pixel)

[bad](javascript:alert(1)) [encoded](&#106;avascript:alert(1))

**Safe &amp; visible** and `x < y`.
'''
        item['messages']=[{'id':'unsafe','role':'assistant','text':hostile}]
        item['input_request']=None
        item['version'] += 1  # The API increments version when messages change.
        page.route('http://localhost/api/career/tasks/typography-test',lambda route:route.fulfill(json=item))
        expect(body).to_contain_text('Safe & visible',timeout=6000)
        assert page.evaluate('window.unsafeMarkdown') is None
        expect(body.locator('script,img,iframe,object,svg')).to_have_count(0)
        assert not any('untrusted.invalid' in url for url in requests)
        assert not any((href or '').lower().startswith(('javascript:','data:')) for href in body.locator('a').evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("href"))'))
        assert errors==[]
        browser.close()
