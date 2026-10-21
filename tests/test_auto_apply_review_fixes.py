"""Focused production-path checks; no pytest or external application submissions."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eregion.api.db import create_database
from eregion.runtime.auto_apply.agent import AgentTools, grounded
from eregion.runtime.auto_apply.browser import ApplicationBrowser, SCAN, BrowserBlocked
from eregion.runtime.auto_apply.matching import question_identity
from eregion.runtime.auto_apply.memory import compatible, same_employer
from eregion.runtime.auto_apply.models import ApplicationMemoryRow
from eregion.runtime.auto_apply.repository import ApplyRepository, ApplyConflict


def field(label='Desired salary', kind='text', key='q'):
    return {'key': key, 'label': label, 'kind': kind, 'required': True,
            'value': '', 'invalid': False, 'options': []}


class MemoryChecks(unittest.TestCase):
    def setUp(self):
        self.db = create_database('sqlite:///:memory:', create_schema_for_tests=True)
        self.repo = ApplyRepository(self.db.session_factory)
        self.profile = {'email': 'fixture@example.invalid'}

    def tearDown(self):
        self.db.close()

    def state(self, employer, question=None):
        return {'profile': self.profile, 'url': f'https://jobs.lever.co/{employer}/role',
                'fields': [question or field()], 'answers': {}, 'run_id': employer,
                'updated_at': '2026-10-09T00:00:00+00:00'}

    def recall(self, employer, question=None):
        state = self.state(employer, question)
        return self.repo.recalled_answers(state, state['fields'])[0]

    def test_employers_global_fallback_and_clearing(self):
        for employer, amount in [('a', '100000'), ('b', '120000')]:
            self.repo.remember(self.state(employer), {'q': amount}, scopes={'q': 'employer'})
        self.assertEqual(self.recall('a'), {'q': '100000'})
        self.assertEqual(self.recall('b'), {'q': '120000'})
        self.assertEqual(self.recall('c'), {})
        displayed = {i['topic']: i['value'] for i in self.repo.standard_answers(self.profile)}
        self.assertEqual(displayed['salary'], '')
        # Even an older client posting every displayed field cannot promote or delete local answers.
        self.repo.save_standard_answers(self.profile, displayed)
        self.assertEqual(len(self.repo.memories(self.profile)), 2)
        self.repo.save_standard_answers(self.profile, {'salary': '90000'})
        self.assertEqual(self.recall('a'), {'q': '100000'})
        self.assertEqual(self.recall('b'), {'q': '120000'})
        self.assertEqual(self.recall('c'), {'q': '90000'})
        self.repo.save_standard_answers(self.profile, {'salary': ''})
        self.assertEqual(len(self.repo.memories(self.profile)), 2)
        self.assertEqual(self.recall('c'), {})

    def test_scope_edits_keep_references_and_do_not_collide(self):
        for employer, amount in [('a', '100000'), ('b', '120000')]:
            self.repo.remember(self.state(employer), {'q': amount}, scopes={'q': 'employer'})
        memory = next(m for m in self.repo.memories(self.profile) if '/a/' in m['url'])
        self.repo.update_memory(self.profile, memory['memory_id'], scope='global')
        self.assertEqual(self.recall('c'), {'q': '100000'})
        self.assertEqual(self.recall('b'), {'q': '120000'})
        self.repo.remember(self.state('a'), {'q': '110000'}, scopes={'q': 'employer'})
        self.repo.update_memory(self.profile, memory['memory_id'], value='95000')
        self.assertEqual(self.recall('a'), {'q': '110000'})
        self.assertEqual(self.recall('c'), {'q': '95000'})
        self.repo.update_memory(self.profile, memory['memory_id'], scope='employer', url=self.state('b')['url'])
        self.assertEqual(self.recall('b'), {'q': '95000'})
        self.assertEqual(self.recall('a'), {'q': '110000'})
        self.assertEqual(self.recall('c'), {})
        self.assertEqual(len(self.repo.memories(self.profile)), 2)

    def test_legacy_memory_and_new_employer_records_coexist(self):
        record = {'memory_id': 'legacy', 'question': 'Desired salary', 'value': '80000',
                  'updated_at': '2020-01-01', 'source': 'user_answer', 'url': self.state('a')['url']}
        with self.db.session_factory.begin() as session:
            session.add(ApplicationMemoryRow(memory_id='legacy', owner_key=self.repo.owner_key(self.profile),
                                             record_json=json.dumps(record)))
        self.repo.remember(self.state('b'), {'q': '120000'}, scopes={'q': 'employer'})
        self.assertEqual(self.recall('a'), {'q': '80000'})
        self.assertEqual(self.recall('b'), {'q': '120000'})
        self.repo.save_standard_answers(self.profile, {'salary': '90000'})
        self.assertEqual(self.recall('a'), {'q': '90000'})
        self.assertEqual(self.recall('b'), {'q': '120000'})
        self.assertIn('legacy', [m['memory_id'] for m in self.repo.memories(self.profile)])

    def test_standard_scope_needs_an_employer(self):
        self.repo.save_standard_answers(self.profile, {'salary': '90000'})
        memory = self.repo.memories(self.profile)[0]
        with self.assertRaises(ApplyConflict):
            self.repo.update_memory(self.profile, memory['memory_id'], scope='employer')
        self.repo.update_memory(self.profile, memory['memory_id'], scope='employer', url=self.state('a')['url'])
        self.assertEqual(self.recall('a'), {'q': '90000'})
        self.assertEqual(self.recall('b'), {})

    def test_shared_ats_employer_namespaces(self):
        for domain in ['jobs.lever.co', 'jobs.ashbyhq.com', 'jobs.smartrecruiters.com', 'job-boards.greenhouse.io']:
            self.assertTrue(same_employer(f'https://{domain}/Acme/1', f'https://{domain}/acme/2'))
            self.assertFalse(same_employer(f'https://{domain}/Acme/1', f'https://{domain}/Globex/2'))
            self.assertFalse(same_employer(f'https://{domain}/', f'https://{domain}/'))
        self.assertTrue(same_employer('https://jobs.smartrecruiters.com/Acme/1', 'https://careers.smartrecruiters.com/Acme'))
        self.assertFalse(same_employer('https://a.wd5.myworkdayjobs.com/jobs', 'https://b.wd5.myworkdayjobs.com/jobs'))

    def test_aggregator_host_is_not_an_employer(self):
        self.assertFalse(same_employer('https://www.linkedin.com/jobs/view/1', 'https://www.linkedin.com/jobs/view/2'))

    def test_salary_currency_and_period(self):
        source = field('Expected salary (USD per year)')
        self.repo.remember(self.state('a', source), {'q': '100000'}, scopes={'q': 'global'})
        for label in ['Expected salary (EUR per year)', 'Expected salary (USD per month)',
                      'Expected hourly pay (USD)', 'Desired salary']:
            self.assertEqual(self.recall('b', field(label)), {}, label)
            self.assertFalse(compatible(self.repo.memories(self.profile)[0], field(label), self.state('b')), label)
        self.assertEqual(self.recall('b', field('Desired annual salary (USD)')), {'q': '100000'})
        self.assertNotEqual(question_identity('Expected salary (USD per year)'), question_identity('Expected salary (EUR per month)'))
        self.repo.save_standard_answers(self.profile, {'salary': '110000 USD per year'})
        self.assertEqual(self.recall('b', field('Expected salary (USD per year)')), {'q': '110000 USD per year'})
        self.assertEqual(self.recall('b', field('Expected salary (EUR per month)')), {})
        self.repo.save_standard_answers(self.profile, {'salary': ''})
        self.assertEqual(self.recall('b', source), {'q': '100000'})

    def test_generic_salary_does_not_assume_units(self):
        self.repo.save_standard_answers(self.profile, {'salary': '100000'})
        self.assertEqual(self.recall('b', field('Desired salary')), {'q': '100000'})
        self.assertEqual(self.recall('b', field('Expected salary (USD per year)')), {})

    def test_labeled_dollar_currency_disambiguates_a_bare_symbol(self):
        source = field('Desired annual salary (USD)')
        self.repo.remember(self.state('a', source), {'q': '$100000'}, scopes={'q': 'global'})
        self.assertEqual(self.recall('b', source), {'q': '$100000'})
        self.assertEqual(self.recall('b', field('Desired annual salary (CAD)')), {})


class GroundingChecks(unittest.TestCase):
    def test_semantic_verification_is_atomic_and_not_cached_across_values(self):
        question = field('Do you require sponsorship?', 'select')
        question['options'] = [{'label': v, 'value': v} for v in ['Yes', 'No']]
        state = {'profile': {}, 'answers': {}, 'answer_sources': {}, 'chat_revision': 0,
                 'messages': [{'id': 'u1', 'role': 'user', 'text': "I don't need sponsorship"}]}
        saved, calls = [], []
        tools = AgentTools(None, state, autofill=None, active=lambda: True,
                           bind_answers=lambda rows, revision: saved.extend(rows))
        tools.fields, tools.observation_id = [question], 'observation'
        def verify(items, *, context=None):
            calls.append(items)
            return [item['proposed'] == 'No' for item in items]
        tools.verify_answers = verify
        args = {'observation_id': 'observation', 'answers': [{'field_key': 'q', 'message_id': 'u1',
                'quote': "I don't need sponsorship", 'value': 'Yes'}]}
        with self.assertRaises(BrowserBlocked): tools._record_answers(args)
        self.assertEqual(saved, [])
        self.assertEqual(state['answers'], {})
        args['answers'][0]['value'] = 'No'
        tools._record_answers(args)
        tools._record_answers(args)
        self.assertEqual(state['answers'], {'q': 'No'})
        self.assertEqual(len(calls), 2)

    def test_bare_boolean_and_geographic_aliases_keep_fast_path(self):
        self.assertTrue(grounded(field(), 'Yes', 'Yes', []))
        self.assertFalse(grounded(field(), 'No', 'Yes', []))
        self.assertFalse(grounded(field(), "I don't need sponsorship", 'Yes', []))
        self.assertTrue(grounded(field('Country'), 'US', 'United States', []))
        self.assertTrue(grounded(field('Location (City)'), 'ithaca ny', 'Ithaca, New York, United States', []))


class ChromeChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        try:
            cls.chrome = cls.playwright.chromium.launch(channel='chrome', headless=True)
        except Exception as exc:
            cls.playwright.stop()
            if 'not found' in str(exc) or "doesn't exist" in str(exc):
                raise unittest.SkipTest('Chrome is not installed') from exc
            raise

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.playwright.stop()

    def setUp(self):
        self.page = self.chrome.new_page(locale='en-US')
        self.browser = ApplicationBrowser.__new__(ApplicationBrowser)
        self.browser.page = self.browser.scope = self.page
        self.browser._check_page = lambda: None
        self.browser.observe = lambda: self.page.evaluate(SCAN)

    def tearDown(self):
        self.page.close()

    def test_one_submit_click_then_confirmation(self):
        self.page.set_content('''<main><form id="application"
            onsubmit="event.preventDefault();window.submits=(window.submits||0)+1;document.querySelector('main').innerHTML='<h1>Your application has been submitted.</h1>'">
            <label for="n">First name</label><input id="n" required value="Fixture"><button type="submit">Submit application</button></form></main>''')
        self.assertIsNone(self.browser.confirmation(after_submit=True))
        self.browser.submit()
        self.assertIsNotNone(self.browser.wait_confirmation())
        self.assertEqual(self.page.evaluate('window.submits'), 1)

    def test_confirmation_negative_and_positive_pages(self):
        thank_you = '<p>Thank you for your application to Acme.</p>'
        filled = '<form id="application"><label for="n">First name</label><input id="n" required value="Fixture">'
        cases = [
            (thank_you + filled + '<button type="submit">Submit</button></form>', False),
            (thank_you + filled + '<div role="alert">The server could not save your application. Please try again.</div><button type="submit">Submit</button></form>', False),
            ('<div class="application-success">Your application has been submitted.</div>' + filled + '<button type="submit">Submit</button></form>', False),
            (thank_you + '<div role="alert">Please correct the information.</div>', False),
            ('<p>Your application was not successfully submitted.</p>', False),
            ('<p>Your application is complete and ready to submit.</p>', False),
            (thank_you + '<button disabled>Submit application</button>', False),
            (thank_you + '<form><input aria-label="Optional note"><button type="submit">Send</button></form>', False),
            ('<h1>Thanks for applying!</h1><p>We have received your application.</p>', True),
            ('<div id="application_confirmation">Your application has been submitted.</div>', True),
            ('<div role="alert">Your application has been submitted.</div>', True),
            ('<p>Your application has been submitted.</p><form role="search"><input type="search"><button type="submit">Search</button></form>', True),
        ]
        for html, expected in cases:
            with self.subTest(html=html[:90]):
                self.page.set_content('<main>' + html + '</main>')
                self.assertEqual(bool(self.browser.confirmation(after_submit=True)), expected)
        self.page.set_content('<main><h1>Thanks for applying!</h1></main>')
        self.assertIsNone(self.browser.confirmation())

    def test_success_url_without_a_receipt_is_not_confirmation(self):
        self.page.route('https://fixture.invalid/**', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<main>Loading...</main>'))
        self.page.goto('https://fixture.invalid/success')
        self.assertIsNone(self.browser.confirmation(after_submit=True))
        self.page.set_content('<main>We have received your application.</main>')
        self.assertIsNotNone(self.browser.confirmation(after_submit=True))

    def test_real_standard_answer_form_only_sends_edits(self):
        source = (ROOT / 'frontend/web_app/apply.js').read_text(encoding='utf-8')
        functions = source[source.index('async function loadStandardAnswers()'):source.index("$('#add-education').addEventListener")]
        self.page.set_content('<div id="standard-fields"></div>')
        self.page.evaluate('''() => {
            window.$ = s => document.querySelector(s);
            window.el = (tag, text) => {const e=document.createElement(tag);e.textContent=text||'';return e;};
            window.t = window.tServer = text => text;  // interface translation (i18n.js), English here
            window.requests=[];
            window.request=async(path,body,method)=>{
                if(method){window.requests.push({path,body,method});return {};}
                return {answers:[{topic:'salary',question:'Desired salary',kind:'text',value:''},
                    {topic:'referral_source',question:'How did you hear about us?',kind:'text',value:'LinkedIn'}]};
            };
        }''')
        self.page.add_script_tag(content=functions)
        self.page.evaluate('loadStandardAnswers()')
        self.page.evaluate('saveStandardAnswers()')
        self.assertEqual(self.page.evaluate('requests'), [])
        self.page.locator('[data-topic="referral_source"]').fill('Indeed')
        self.page.evaluate('saveStandardAnswers()')
        self.assertEqual(self.page.evaluate('requests[0].body'), {'answers': {'referral_source': 'Indeed'}})
        self.page.evaluate('saveStandardAnswers()')
        self.assertEqual(self.page.evaluate('requests.length'), 1)
        self.page.locator('[data-topic="referral_source"]').fill('')
        self.page.evaluate('saveStandardAnswers()')
        self.assertEqual(self.page.evaluate('requests[1].body'), {'answers': {'referral_source': ''}})


if __name__ == '__main__':
    unittest.main(verbosity=2)
