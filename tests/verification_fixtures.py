"""Synthetic verification pages served at fixed URLs by a local route (V0). No real challenge, no network.

Every page counts what the browser did to it: input events (writes), Next clicks, file uploads
and form submissions, so a test can prove nothing was filled, navigated, uploaded or submitted
while a person handles a verification. The challenge page also defines a site callback that
would submit the form when "solved"; the agent never calls it (a callback with side effects is
exactly why solving libraries are not wired in).
"""
from eregion.runtime.auto_apply.browser import ApplicationBrowser

HOST = 'https://example.wd5.myworkdayjobs.com'
JOB = HOST + '/en-US/External/job/New-York/Engineer_JR123'
APPLY = JOB + '/apply/applyManually'
OTHER_JOB = HOST + '/en-US/External/job/New-York/Analyst_JR999/apply/applyManually'
LOGIN = HOST + '/en-US/External/login'

COUNTERS = '''<script>
window.writes = window.writes || 0; window.nexts = window.nexts || 0; window.uploads = window.uploads || 0; window.submits = window.submits || 0;
document.addEventListener('input', () => window.writes++, true);
document.addEventListener('change', e => { if (e.target.type === 'file') window.uploads++; }, true);
document.addEventListener('submit', e => { e.preventDefault(); window.submits++; }, true);
</script>'''

FORM = '''<form id="application" data-automation-id="applyFlowPage"><h2 data-automation-id="pageHeaderTitle">My Information</h2>
<label for="first">First Name*</label><input id="first" name="legalName--firstName" required>
<label for="last">Last Name*</label><input id="last" name="legalName--lastName" required>
<label for="email">Email*</label><input id="email" type="email" name="email" required>
<button type="button" data-automation-id="pageFooterNextButton" onclick="window.nexts++">Next</button></form>'''


def page(body, title='Apply'):
    return f'<!doctype html><html><head><title>{title}</title>{COUNTERS}</head><body>{body}</body></html>'


CHALLENGE = ('<iframe title="Widget containing a security challenge" style="width:300px;height:80px" '
             'srcdoc="<p>Press and hold</p>"></iframe>'
             '<script>window.onChallengeSolved = () => document.getElementById("application").requestSubmit();</script>')

PAGES = {
    # Normal form plus passive widgets: a hidden reCAPTCHA iframe and its script. Not a challenge.
    'passive': page(FORM + '<script src="https://www.google.com/recaptcha/api.js"></script>'
                    '<iframe src="https://www.google.com/recaptcha/api2/anchor?k=TEST" style="display:none" title="reCAPTCHA"></iframe>'),
    'challenge': page(FORM + CHALLENGE),
    'datadome': page(FORM + '<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=TEST" style="width:400px;height:300px"></iframe>'),
    'interstitial': page('<h1>Checking your browser before accessing the site</h1><p>This may take a few seconds.</p>', title='Just a moment...'),
    'otp_email': page('<h2>Check your email</h2><p>We sent a code to your email address.</p>'
                      '<label for="code">Code</label><input id="code" autocomplete="one-time-code"><button type="button">Verify</button>'),
    'otp_split': page('<h2>Enter the code we sent to your phone</h2><div class="digits">' +
                      ''.join(f'<input aria-label="Digit {i}" maxlength="1" inputmode="numeric">' for i in range(1, 7)) + '</div>'),
    'otp_label': page('<form><label for="vc">Enter verification code</label><input id="vc" name="code"><button type="button">Continue</button></form>'),
    'otp_frame': page('<iframe title="Sign in" style="width:400px;height:200px" srcdoc="&lt;label for=c&gt;Security code&lt;/label&gt;'
                      '&lt;input id=c autocomplete=one-time-code&gt;"></iframe>'),
    'blank': page(''),
    'error': page('<h1>Something went wrong</h1><p>Please try again later.</p>', title='Error'),
    'login': page('<h2>Sign In</h2><label for="u">Email Address</label><input id="u" type="email" autocomplete="username">'
                  '<label for="p">Password</label><input id="p" type="password"><button type="button">Sign In</button>', title='Sign In'),
    'form': page(FORM),
}
# Subframe documents the routes serve (a real DataDome frame keeps its own URL).
FRAMES = {'https://geo.captcha-delivery.com/captcha/': '<p>Access is temporarily restricted.</p>'}


class ScenarioBrowser(ApplicationBrowser):
    """Serves PAGES[name] for the URL mapped in `urls`; every other request is aborted."""

    def __init__(self, url=JOB, *, scene='form', **kwargs):
        # The browser opens the posting's job page; the scene is what that page shows.
        self.urls = {JOB: scene, APPLY: scene, OTHER_JOB: 'form', LOGIN: 'login'}
        super().__init__(url, headless=True, **kwargs)

    def _guard_request(self, route):
        request = route.request
        url = request.url.split('?', 1)[0]
        if request.is_navigation_request() and request.frame == self.page.main_frame and url in self.urls:
            route.fulfill(content_type='text/html', body=PAGES[self.urls[url]])
        elif request.is_navigation_request() and url in FRAMES:
            route.fulfill(content_type='text/html', body=FRAMES[url])
        else:
            route.abort()

    def show(self, scene, url=JOB):
        """What the site shows next at `url` (a person solving, a redirect, an error page...)."""
        self.urls[url] = scene
        self.page.goto(url)

    def counters(self):
        return self.page.evaluate('({writes: window.writes || 0, nexts: window.nexts || 0, uploads: window.uploads || 0, submits: window.submits || 0})')
