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


# V4: the providers' published test keys (no real challenge; tokens are dummies that real sites reject).
# Turnstile: https://developers.cloudflare.com/turnstile/troubleshooting/testing/
# reCAPTCHA: https://developers.google.com/recaptcha/docs/faq   hCaptcha: https://docs.hcaptcha.com/#integration-testing-test-keys
TURNSTILE = '<script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script><div class="cf-turnstile" data-sitekey="{key}"></div>'
OFFICIAL = {
    'turnstile_pass': TURNSTILE.format(key='1x00000000000000000000AA'),
    'turnstile_fail': TURNSTILE.format(key='2x00000000000000000000AB'),
    'turnstile_invisible_pass': TURNSTILE.format(key='1x00000000000000000000BB'),
    'turnstile_interactive': TURNSTILE.format(key='3x00000000000000000000FF'),
    'hcaptcha_test': '<script src="https://js.hcaptcha.com/1/api.js" async defer></script><div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001"></div>',
    'recaptcha_test': '<script src="https://www.google.com/recaptcha/api.js" async defer></script><div class="g-recaptcha" data-sitekey="6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"></div>',
}
for name, widget in OFFICIAL.items():
    PAGES[name] = page(FORM.replace('<button type="button"', widget + '<button type="button"', 1))
WIDGET_HOSTS = ('challenges.cloudflare.com', 'hcaptcha.com', 'www.google.com', 'www.gstatic.com', 'www.recaptcha.net')


class OfficialWidgetBrowser(ScenarioBrowser):
    """As ScenarioBrowser, but the official widget providers' own hosts load from the network (V4 only)."""
    requests = []

    def _guard_request(self, route):
        host = (route.request.url.split('/')[2] if '://' in route.request.url else '').split(':')[0]
        if any(host == h or host.endswith('.' + h) for h in WIDGET_HOSTS):
            type(self).requests.append(host)
            route.continue_()
        else:
            super()._guard_request(route)


# Provider-widget boundaries without the network: a Turnstile-shaped frame served by the local route.
PROVIDER_FRAME_URL = 'https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/test'
FRAMES[PROVIDER_FRAME_URL] = '<p>widget</p>'


def widget(response='', *, visible=True, frame=True, notice=''):
    shown = 'width:300px;height:65px' if visible else 'display:none'
    return (f'<div class="cf-turnstile">' + (f'<iframe src="{PROVIDER_FRAME_URL}" style="{shown}"></iframe>' if frame else '') +
            f'<input type="hidden" name="cf-turnstile-response" value="{response}"></div>' + notice)


WITH_WIDGET = lambda body, **kw: page(FORM.replace('<button type="button"', widget(**kw) + '<button type="button"', 1) + body)
PAGES.update({
    'widget_completed': WITH_WIDGET('', response='dummy-response'),
    'widget_unfinished': WITH_WIDGET(''),                       # interactive, or an error callback left it empty
    'widget_rejected': WITH_WIDGET('', response='dummy-response', notice='<div role="alert">Verification failed. Please try again.</div>'),
    'widget_invisible_idle': WITH_WIDGET('', visible=False),
    'widget_invisible_completed': WITH_WIDGET('', visible=False, response='dummy-response'),
    'widget_load_failed': WITH_WIDGET('', frame=False),       # the provider script never rendered a frame
    'widget_completed_error': page('<h1>Something went wrong</h1>' + widget(response='dummy-response'), title='Error'),
})
