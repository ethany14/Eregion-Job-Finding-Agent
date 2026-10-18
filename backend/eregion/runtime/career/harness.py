"""Career tools reuse existing workspace, evidence, writing, browser and interview services."""
import asyncio
import base64
import hashlib
import logging
import json
import re
from uuid import uuid4

from eregion.runtime.tasks.loop import TEXT, function
from eregion.runtime.auto_apply.contracts import ChatRequest, StartApplication
from eregion.runtime.auto_apply.repository import DuplicateApplication, ApplyConflict
from eregion.runtime.application_pack.rendering import render_cover_letter
from eregion.runtime.workspace.types import ArtifactType
from eregion.domain.rendering import render_tailored_resume_text
from eregion.domain.pdf_rendering import render_tailored_resume_pdf
from .discovery import JobDiscovery
from .prompt import SYSTEM, APPLICATION_INSTRUCTION
from eregion.runtime.browser.tools import BROWSER_TOOLS, BROWSER_NAMES, VISUAL_NAMES
from .alerts import AlertConfig, job_key
from .search_filters import FILTER_SCHEMA, FILTER_FIELDS, effective_filters, filter_cards
from .prompt import RECOMMENDATION_RULE, FINAL_FORMAT


APP = {'application_id':TEXT}
ITEM = {'pack_id':TEXT,'item_id':TEXT}
# One recommendation contract (docs/specs/tiered-recommendations-20261013.md).
# The model chooses the tier; code checks only structure, citations and identity.
TIERS = {'priority':'Priority match','stretch':'Worth trying (known gaps)','to_confirm':'Needs your confirmation'}
JEV_STAGE_REQUESTS = 8          # measured: one shared request per Jev stage per batch
MAIN_ROUTE_REQUESTS_PER_JOB = 5 # measured without Jev (claude-main-fit-probe-03)
TOOLS = [
    function('dismiss_candidate','Exclude a retrieved but unsuitable posting for this user turn without paid scoring. Quote its exact title, location or JD and explain the mismatch with the user request. This is a model decision, not proof the vacancy is closed.',
        {'url':TEXT,'quote':{'type':'string','minLength':1,'maxLength':600},'reason':{'type':'string','minLength':1,'maxLength':600}}),
    function('configure_job_alert','Create or update a daily autonomous job recommendation schedule explicitly requested in the cited user message. Timezone is required; never guess it. This schedules research only, not applications.',
        {'message_id':TEXT,'alert_id':TEXT,'config':AlertConfig.model_json_schema()},['message_id','config']),
    function('set_job_alert_enabled','Pause or resume a daily search at the user\'s explicit request.',
        {'message_id':TEXT,'alert_id':TEXT,'enabled':{'type':'boolean'}}),
    function('recommend_job','Present an actually read and scored job as an opportunity in one tier: priority, stretch or '
        'to_confirm (see the tier rule). evidence: exact quotes from this posting supporting the reason. gaps: each '
        'known skill/experience gap including required ones, with how hard it makes the application. to_confirm: '
        'every personal fact the posting requires that the resume does not state (GPA, nationality, age, visa/program status, license, graduation date or cohort, start date). next_steps: '
        'concrete actions. Also adds it to the digest when this task was started by a schedule.',
        {**APP,'tier':{'type':'string','enum':list(TIERS)},'reason':TEXT,
         'evidence':{'type':'array','minItems':1,'maxItems':4,'items':{'type':'string','maxLength':400}},
         'gaps':{'type':'array','maxItems':10,'items':{'type':'string','maxLength':300}},
         'to_confirm':{'type':'array','maxItems':8,'items':{'type':'string','maxLength':300}},
         'next_steps':{'type':'array','minItems':1,'maxItems':5,'items':{'type':'string','maxLength':200}},
         'caveats':TEXT,'conditions_reviewed':{'type':'boolean','description':'For priority only: true after checking that the resume or the user states every posting condition on the candidate (enrollment, graduation date or cohort, availability or start date, authorization).'}},
        ['application_id','tier','reason','evidence','gaps','to_confirm','next_steps']),
    function('search_jobs','API-first discovery through configured LinkedIn MCP and public company boards. Returns observed posting cards; process candidate_queue.next_unread with verify_and_score_jobs. Search again for new boards only after actionable candidates are processed. Optional boards contain platform and company board token.',
        {**FILTER_SCHEMA,'query':TEXT,'location':TEXT,'use_directory':{'type':'boolean','description':'Select fresh verified boards by role and location; prefer after blocked requested sources.'},'boards':{'type':'array','maxItems':5,'items':{'type':'object',
            'properties':{'platform':{'type':'string','enum':['greenhouse','lever','ashby','workday']},'company':TEXT,'host':TEXT,'site':TEXT},
            'required':['platform','company'],'additionalProperties':False}},'limit':{'type':'integer','minimum':1,'maximum':20},
            'pages':{'type':'integer','minimum':1,'maximum':8,'description':'LinkedIn result pages for this query (about 10 cards and 9 s each). '
                     'search_coverage reports for each query the provider total and cards seen, so you can judge whether deeper pages are worth it.'}},['query']),
    function('screen_jobs','Quickly rank a batch of actually observed job links against a short search query. Use after browser search to choose which descriptions to read and score. Advisory only; cannot replace score_job or establish eligibility. Unknowns remain available to you.',
        {'urls':{'type':'array','minItems':1,'maxItems':20,'items':TEXT},'query':TEXT}),
    function('read_job','Read and cache the canonical posting via configured LinkedIn MCP or public APIs. Prefer verify_and_score_jobs for a batch. Browser detail regions are a fallback; supply observed region_ref and observation_id for split pages. A cached body needs no repeated read.',
        {'url':TEXT,'region_ref':TEXT,'observation_id':TEXT},['url']),
    function('import_job','Save a retrieved job. Only for a pasted JD, supply message_id and its literal job_text excerpt (never the task instruction). Returns application_id.',
        {'url':TEXT,'message_id':TEXT,'job_text':TEXT,'title':TEXT,'company':TEXT,'location':TEXT},['url']),
    function('list_jobs','Inspect saved jobs from the old workspace as well as this task.',{'query':TEXT},[]),
    function('inspect_job','Read a saved job, score, gaps and linked artifacts. With requirement_ids (from decision_brief), '
        'return only those requirements: full posting text, level, status, reason and resume evidence.',
        {**APP,'requirement_ids':{'type':'array','maxItems':20,'uniqueItems':True,'items':TEXT}}),
    function('score_job','Run the existing resume/JD match analysis and persist the actual score and requirement gaps.',APP),
    function('score_jobs','Compare up to sixteen imported jobs against the current resume in one batch. Uses shared Jev context when enabled; preserves uncertainty and separate results.',
        {'application_ids':{'type':'array','minItems':1,'maxItems':16,'uniqueItems':True,'items':TEXT}},['application_ids']),
    function('generate_materials','Prepare verified drafts, reusing saved drafts for unchanged inputs. Only when the user explicitly requests a fresh/new set, use fresh=true with their message_id; preserves old documents. Use revise_material for feedback on an existing document.',
        {**APP,'artifacts':{'type':'array','minItems':1,'maxItems':2,'items':{'type':'string','enum':['tailored_resume','cover_letter']}},
            'fresh':{'type':'boolean'},'message_id':TEXT},['application_id','artifacts']),
    function('inspect_materials','Read the current revisions, contents, verification and approval state.',{'pack_id':TEXT}),
    function('revise_material','Revise an existing document using the user feedback in the cited message.',{**ITEM,'message_id':TEXT}),
    function('remember_profile','Extract contact/profile corrections from an actual user message into reusable candidate details.',{'message_id':TEXT}),
    function('remember_evidence','Save a career fact explicitly supplied by the user, quoting their exact words. Use it for grounded materials.',
        {'message_id':TEXT,'quote':TEXT,'application_id':TEXT},['message_id','quote']),
    function('start_application','Open/focus an application using this job\'s current approved documents. Omit pack_id to use explicit per-document choices across packs. Optional pack_id must not conflict with an explicit choice; missing document types use valid explicit choices. Original explicitly selects the saved PDF. Cannot submit.',
        {**APP,'application_ids':{'type':'array','items':TEXT,'minItems':1,'maxItems':4},'pack_id':TEXT,'resume_source':{'type':'string','enum':['auto','original','tailored']}},[]),
    function('open_application_url','Open one url or up to four urls with the saved original resume. Use urls for apply-to-both requests. Returns actual starts, reuse and failures; never claim more applications than the returned results. Cannot submit.',{'url':TEXT,'urls':{'type':'array','items':TEXT,'minItems':1,'maxItems':4}},[]),
    function('inspect_application','Read the live browser progress, outstanding question and verified outcome. Observed answers are the actual review outcome. include_fields returns compact controls; request field_key only to investigate one original control. Do not reread the entire form to report its outcome. Wait while the worker is running.',{'run_id':TEXT,'include_fields':{'type':'boolean'},'field_key':TEXT},['run_id']),
    function('reply_application','Forward a user message verbatim to the linked application agent. Never invent an answer.',
        {'run_id':TEXT,'message_id':TEXT}),
    function('start_interview','Start job-specific practice using the existing mock interviewer; show its questions in this chat.',
        {**APP,'mode':{'type':'string','enum':['mixed','recruiter_screen','behavioral','project_deep_dive','role_specific']},
            'question_count':{'type':'integer','minimum':3,'maximum':12}},['application_id']),
    function('inspect_interview','Read the interview question, feedback and report.',{'interview_id':TEXT}),
    function('answer_interview','Send the user\'s original answer to the exact observed interview question.',
        {'interview_id':TEXT,'question_id':TEXT,'message_id':TEXT}),
    function('control_interview','Skip, end or resume an interview at the user\'s request. Cite the request message.',
        {'interview_id':TEXT,'action':{'type':'string','enum':['skip','end','resume']},'message_id':TEXT}),
]
from . import source_mode
from .decisions import TOOL as DECIDE_JOB_TOOL

# API-first: a directory result with fewer jobs than this gets one Treg LinkedIn supplement.
TREG_SUPPLEMENT_BELOW = 3
TOOLS.append(DECIDE_JOB_TOOL)


class CareerHarness:
    # Bound replay cost as well as live observation size. Complete source data
    # remains in ContextArchive; permission facts/live controls stay protected.
    request_chars = 48000
    tools, prompt = [*TOOLS,*BROWSER_TOOLS],SYSTEM
    # These reads do not alter browser observation tokens or external state.
    batchable_tools = frozenset({'list_jobs', 'inspect_job', 'inspect_materials', 'inspect_interview'})
    # Independent per-job outcomes the prompt already invites in one response; each
    # call still re-reads state and runs its own gates. Not inspection tools for waits.
    batch_decision_tools = frozenset({'recommend_job', 'decide_job', 'dismiss_candidate', 'read_context'})

    def stable_tools(self, state):
        # A new user revision can change task kind; browser and progress cannot.
        return self.available_tools(state, stable=True)

    def available_tools(self,state, *, stable=False):
        from eregion.runtime.fast_decisions import configured_decisions
        from .search_recovery import search_request
        tools=[t for t in self.tools if stable or state.get('visual_fallback') or t['function']['name'] not in VISUAL_NAMES]
        from eregion.runtime.request_options import enabled
        if enabled('VERIFY_AND_SCORE', default=self.api_first()):
            from .workflows import TOOL
            from .batch_workflow import TOOL as BATCH_TOOL
            tools = [*tools, TOOL, BATCH_TOOL]
        # Offer operations for resources that actually exist in this process.
        # browser_open remains available for independent research at any time.
        if not stable and (not self._browsing or not self._browsing.inspect(state['task_id'])):
            tools=[t for t in tools if t['function']['name'] not in BROWSER_NAMES or t['function']['name']=='browser_open']
        latest=next((m for m in reversed(state.get('messages',[])) if m.get('role')=='user'),None)
        children=[self.applications.repository.get(rid) for rid in state.get('application_runs',[])]
        if not stable and latest and children and all(any(m.get('id')==latest['id'] for m in child.get('messages',[])) for child in children):
            tools=[t for t in tools if t['function']['name']!='reply_application']
        if not stable and not configured_decisions().enabled:
            tools = [t for t in tools if t['function']['name'] != 'screen_jobs']
        if self.recommendation_run(state):
            allowed=BROWSER_NAMES|{'dismiss_candidate','search_jobs','screen_jobs','read_job','import_job','verify_and_score','verify_and_score_jobs','inspect_job','score_job','score_jobs','recommend_job','decide_job'}
            return [t for t in tools if t['function']['name'] in allowed]
        latest_text=latest['text'] if latest else state.get('goal','')
        positive=re.sub(r'\b(?:do not|don.t|never)\b[^.!?]*','',latest_text,flags=re.I)
        if search_request(state) and not re.search(r'\b(?:apply|submit|interview|tailor|letter|schedule|alert|write|draft|prepare|materials?|rewrite|update|create)\b',positive,re.I):
            allowed=BROWSER_NAMES|{'dismiss_candidate','search_jobs','screen_jobs','read_job','import_job','verify_and_score','verify_and_score_jobs','list_jobs','inspect_job','score_job','score_jobs','recommend_job','decide_job'}
            tools=[t for t in tools if t['function']['name'] in allowed]
        return tools

    def api_first(self):
        """The single source-order switch for prompt, snapshot, tools and routing."""
        from .treg_config import settings
        return str(settings()['JOB_AGENT_JOB_API_FIRST']).strip().lower() in {'1','true','yes','on'}

    def _treg_config(self):
        from .treg_config import TregConfigError, load
        try:
            return load()
        except TregConfigError:
            return None  # invalid Treg settings block Treg only

    def _source_scope(self,state):
        from .search_policy import source_scope
        latest=next((m.get('text','') for m in reversed(state.get('messages',[])) if m.get('role')=='user'),state.get('goal',''))
        return source_scope(latest)

    def _treg(self):
        """Lazily built fixed-endpoint client; None when Treg cannot be used (other sources keep working)."""
        cfg=self._treg_config()
        if not cfg or cfg.usable():
            return None
        if getattr(self,'_treg_client',None) is None:
            from .external_tool_usage import ExternalToolLedger
            from .treg_client import TregClient
            self._treg_client=TregClient(cfg,ExternalToolLedger(self.repository.sessions))
        return self._treg_client

    def _treg_search(self,task_id,state,args):
        from .treg_jobs import SEARCH_ENDPOINTS, normalize_search, search_body
        from .treg_client import TregUnavailable
        client=self._treg()
        provider=client.config.search_provider if client else None
        if not client or not provider:
            return {'jobs':[],'error':'LinkedIn search through Treg is not enabled or no provider is selected.',
                'error_code':'treg_unavailable','no_new_evidence':True,
                'next':'Disclose the limitation, or use the browser on LinkedIn if the user allows it.'}
        filters=effective_filters(state,args)
        body=search_body(provider,str(args['query']),args.get('location') or None, **filters,
            limit=min(args.get('limit',10),client.config.max_results))
        from .source_health import user_turn
        turn=user_turn(state)
        cache_key=hashlib.sha256(json.dumps([provider,body],sort_keys=True).encode()).hexdigest()
        cache=state.get('treg_search_cache',{})
        cached=cache.get('results',{}).get(cache_key) if turn and cache.get('user_turn_id')==turn else None
        if cached is not None:
            return {**cached,'no_new_evidence':True,'cached':True}
        try:
            call=client.call(SEARCH_ENDPOINTS[provider],body,scope=task_id,task_id=task_id,
                revision=str(state.get('chat_revision')))
        except TregUnavailable as exc:
            return {'jobs':[],'error':f'Treg is unavailable: {exc.reason}.','error_code':'treg_unavailable','no_new_evidence':True}
        info={'provider':provider,'endpoint':SEARCH_ENDPOINTS[provider],'outcome':call.outcome,
              'charged_micro':call.charged_micro,'cache':call.cache,
              'applied_filters':{k:v for k,v in filters.items() if provider=='anyapi' or k=='posted'},
              'unapplied_filters':[k for k in filters if provider=='crawl4ai' and k!='posted']}
        if call.outcome!='succeeded' or call.quarantined:
            return {'jobs':[],'treg':info,'error':'Treg search did not return usable results.','error_code':'treg_failed',
                'no_new_evidence':True}
        cfg=client.config
        jobs,detail=normalize_search(provider,call.body,max_results=min(args.get('limit',10),cfg.max_results))
        jobs,detail['local_filter']=filter_cards(jobs,state)
        if detail.get('source_blocked') and not jobs:
            return {'jobs':[],'treg':{**info,**detail},'error':'The LinkedIn source reported it was blocked.',
                'error_code':'treg_source_blocked','no_new_evidence':True,
                'next':'Treat LinkedIn as a blocked source this turn; use other sources and disclose the limitation.'}
        note='LinkedIn search cards are candidates only: read each posting before qualifying, importing or scoring it.'
        if detail.get('partial') or detail.get('source_blocked'):
            note+=' The provider reported partial or blocked coverage; disclose it and do not treat these results as complete.'
        result={'jobs':jobs,'treg':{**info,**detail},'note':note}
        def remember(s):
            candidates={job_key(j['url']):j for j in s.get('search_candidates',{}).values() if j.get('url')}
            candidates.update({job_key(j['url']):j for j in jobs})
            old=s.get('treg_search_cache',{})
            results=old.get('results',{}) if old.get('user_turn_id')==turn else {}
            return {'search_candidates':dict(list(candidates.items())[-60:]),
                'treg_search_cache':{'user_turn_id':turn,'results':dict(list({**results,cache_key:result}.items())[-16:])}}
        self.repository.change(task_id,remember)
        return result

    def _treg_scrape(self,task_id,state,url):
        """Complete JD text through Treg for a public non-ATS URL, or None to use the existing path."""
        from urllib.parse import urlsplit
        host=(urlsplit(url).hostname or '').lower()
        ats=('greenhouse.io','lever.co','ashbyhq.com','myworkdayjobs.com')
        client=self._treg()
        if not client or not client.config.scrape_enabled or any(host==h or host.endswith('.'+h) for h in ats):
            return None
        from .treg_jobs import SCRAPE_ENDPOINT, assess_scrape
        from .treg_client import TregUnavailable
        candidate=next((c for c in state.get('search_candidates',{}).values() if c.get('url') and job_key(c['url'])==job_key(url)),{})
        try:
            call=client.call(SCRAPE_ENDPOINT,{'url':url,'format':'md'},scope=task_id,task_id=task_id,
                revision=str(state.get('chat_revision')))
        except TregUnavailable:
            return None
        if call.outcome!='succeeded' or call.quarantined:
            return None
        verdict=assess_scrape(candidate,call.body)
        # Keep why a paid scrape was or was not accepted (B6 run-2 could not be attributed).
        record={'url':url,'status':verdict['status'],'failed_checks':sorted(k for k,v in verdict['checks'].items() if not v),
                'text_len':verdict['text_len'],'raw_len':verdict['raw_len'],'operation_id':call.operation_id}
        self.repository.change(task_id,lambda s:{'treg_scrape_verdicts':[*s.get('treg_scrape_verdicts',[]),record][-20:]})
        if verdict.get('closed_signal'):
            # The posting says it is closed: report that instead of re-reading it elsewhere.
            return {'closed':True,'url':url,'source':'treg:crawl4ai.web.scrape'}
        if not verdict['complete']:
            return None  # not proven to be this complete JD: keep the existing read path
        return {'url':url,'title':candidate.get('title'),'company':candidate.get('company') or '',
            'location':candidate.get('location') or '','description':verdict['description_text'],
            'source':'treg:crawl4ai.web.scrape','content_hash':verdict['content_hash']}

    def terminal_delivery(self,state):
        from .decisions import terminal_delivery
        return terminal_delivery(state)

    def recommendation_run(self,state):
        run_id=(state.get('initial_context') or {}).get('recommendation_run_id')
        if run_id and not state.get('chat_revision') and getattr(self,'alerts',None):
            run=self.alerts.get_run(run_id)
            if run['status'] in {'pending','running'}:return run
        return None

    def __init__(self, runtime, applications, repository, *, discovery=None, fit_service=None):
        self.runtime,self.applications,self.repository = runtime,applications,repository
        self.discovery = discovery or JobDiscovery()
        from eregion.api.services.workspace_service import WorkspaceAnalysisService
        if fit_service is None and getattr(runtime, 'database', None):
            from eregion.runtime.analysis_cache import AnalysisCache
            from eregion.runtime.memory.policy import LocalOwnerResolver
            from eregion.api.services.fit_analysis_service import FitAnalysisService
            fit_service = FitAnalysisService(analysis_cache=AnalysisCache(runtime.database.session_factory,
                owner=(getattr(runtime, 'owner_resolver', None) or LocalOwnerResolver()).resolve().owner_id))
        self.analysis = WorkspaceAnalysisService(runtime.workspace,fit_service,evidence=runtime.evidence)
        self._resume_cache = (None,'')
        self._resume_issue = None
        self._browsing = None
        from .interview_queue import InterviewQueue
        self.interview_inputs = InterviewQueue(self)

    @property
    def browsing(self):
        if self._browsing is None:
            from eregion.runtime.browser.manager import BrowsingManager
            self._browsing = BrowsingManager()
        return self._browsing

    def close(self):
        # Idempotent; one failing resource must not leave the others open. The
        # browser manager closes its pages on its own worker thread.
        treg, self._treg_client = getattr(self, '_treg_client', None), None
        browsing = self._browsing
        for close in (getattr(self.discovery, 'close', None), self.interview_inputs.close,
                      treg.close if treg else None, browsing.close if browsing else None):
            if close is None:continue
            try:close()
            except Exception:logging.getLogger(__name__).warning('Career resource close failed.',exc_info=True)

    def candidate(self):
        saved = self.applications.repository.saved_candidate()
        if saved.get('resume_base64'):
            from eregion.runtime.resumes.pdf_text import extract_pdf_text
            digest = hashlib.sha256(saved['resume_base64'].encode()).hexdigest()
            if self._resume_cache[0] != digest:
                try:
                    text,_ = extract_pdf_text(base64.b64decode(saved['resume_base64']))
                    self._resume_issue = None
                except ValueError:
                    text = ''
                    self._resume_issue = 'The saved PDF could not be read. Ask for a selectable-text PDF before scoring or writing.'
                self._resume_cache = (digest,text)
            text = self._resume_cache[1]
        else:
            try:text = self.runtime.resumes.default(self.runtime.owner_resolver.resolve().owner_id).extracted_text
            except (LookupError,AttributeError):text = ''
        return saved,text

    def observe(self,state):
        from .search_recovery import recovery
        from .search_policy import constraints
        from .source_health import coverage, projection
        self.sync_submissions(state)
        saved,text = self.candidate()
        recommendation=self.recommendation_run(state)
        latest=next((m['text'] for m in reversed(state.get('messages',[])) if m.get('role')=='user'),state['goal'])
        # Index existing resources rather than duplicating every score, field,
        # document and interview inside each observation.
        from .decisions import statuses
        decision_rows=statuses(state)
        known={'request':latest,'jobs':list(state['jobs']),
            'scored_not_recommended':[{**j,**decision_rows.get(j['application_id'],{})} for j in state.get('scored_job_evidence',{}).values()
                if decision_rows.get(j['application_id'],{}).get('decision_status')!='recommended'],
            'materials':list(state['packs']), 'applications':list(state['application_runs']),
            'interviews':list(state['interviews']),
            'recent_outcomes':[{'tool':h['tool'],**{k:h.get('result',{})[k] for k in
                ('no_new_evidence','error','error_code','blocked','url','next') if isinstance(h.get('result'),dict) and k in h['result']}}
                for h in state.get('history',[])[-4:]],
            'instruction':'Saved results by ID; scored is not recommended. Every scored job needs an outcome (decision_status): '
                'recommend_job with its tier if it meets the requested role/location/seniority, or decide_job with the specific '
                'exclusion or missing evidence; searching waits while any is pending. '+RECOMMENDATION_RULE+
                ' Use exact qualification_evidence; never guess years or auto-recommend unverified/'
                'ineligible jobs. Final results list each read/scored URL and actual score.'}

        from .candidate_health import active_rejections
        known['excluded_candidates']=active_rejections(state)
        source=state.get('linkedin_mcp_state') or {}
        if source:
            log=source.get('log',[])
            known['linkedin_source_facts']={
                'calls_used':source.get('calls_used',0),'call_limit':source.get('call_limit',26),'stop_reason':source.get('stopped'),
                'successful_searches':sum(r.get('tool')=='search_jobs' and r.get('outcome')=='ok' for r in log),
                'successful_details':sum(r.get('tool')=='get_job_details' and r.get('outcome')=='ok' for r in log),
                'instruction':'A later source stop or exhausted budget does not erase earlier successful results. Report both accurately.'}
        from .candidate_queue import enabled as candidate_batches, view as candidate_view
        search_view=recovery(state,saved_urls=[self.runtime.workspace.application_job(a).canonical_url for a in state['jobs']])
        task_memory = getattr(self.runtime, 'task_memory', None)
        return {**(task_memory.context(state) if task_memory else {}),
            'candidate':saved.get('profile',{}),'resume_text':text[:40000],
            'search_recovery':search_view,
            # Coverage, yield and remaining budget sit beside the queue: they are what a
            # continue-or-stop choice rests on.
            'search_coverage':{**coverage(state),'budget_remaining':self.search_budget(state)},
            **({'candidate_queue':candidate_view(state,search_view)} if candidate_batches() and search_view else {}),
            **source_mode.snapshot_policy(api_first:=self.api_first(),self._source_scope(state) if api_first else None),
            'known_results':known,
            'scheduled_search':recommendation,
            # What the final answer must restate stays inline: identity, URL, score and the
            # saved tier, gaps, items to confirm and next steps (stop replay v3: without them
            # the model re-read every archived job to write the reply). The list is never
            # archived (request_context); free-form reason/caveat prose stays in state.
            'recommended_jobs':[{k:r[k] for k in ('application_id','title','company','url','match_score',
                                                  'score_range','tier','gaps','to_confirm','next_steps') if k in r}
                                for r in state.get('recommendations',[])],
            'previous_recommendations':self.alerts.recommended() if recommendation else [],
            'job_alerts':self.alerts.list() if getattr(self,'alerts',None) and not recommendation else [],
            'browser':state.get('browser'),
            'research_pages': [{k: (v[:400] if k == 'excerpt' else [{a:b for a,b in link.items() if a in {'url','label'}} for link in v[:6]] if k == 'links' else v) for k,v in page.items() if k in {'url','title','blocked','links','excerpt'}} for page in state.get('research_pages', [])],
            'search_source_health':projection(state),
            'search_constraints':constraints(state),
            'visual_fallback':bool(state.get('visual_fallback')),
            'resume_issue':self._resume_issue if saved.get('resume_base64') else None,
            'has_original_pdf':bool(saved.get('resume_base64')),
            'jobs':[self.job_summary(a) for a in state['jobs']],
            'materials':[self.materials(p,detail=False) for p in state['packs']],
            'applications':[self.application(r) for r in state['application_runs']],
            'material_choices':{a:self.material_choices(a) for a in state['jobs']},
            'capability_versions':{'application':{r:self.wait_token({'id':r}) for r in state['application_runs']}},
            'interviews':[self.interview(i) for i in state['interviews']]}

    def search_budget(self,state):
        """Remaining paid work for this task, as facts for the model's continue/stop choice."""
        from eregion.runtime.provider_usage import task_limits, UsageLedger
        limits=task_limits();used=UsageLedger(self.repository.sessions).summary(task_id=state['task_id']) if state.get('task_id') else {}
        source=state.get('linkedin_mcp_state') or {}
        return {'provider_attempts':(limits['provider_attempts']-used.get('calls',0)) if limits.get('provider_attempts') else None,
                'input_tokens':(limits['input_tokens']-used.get('budget_input_tokens',used.get('input_tokens',0)))
                               if limits.get('input_tokens') else None,
                'linkedin_calls':(source['call_limit']-source.get('calls_used',0)) if source.get('call_limit') else None}

    def job_summary(self,application_id,*,detail=False):
        rt = self.runtime
        app = rt.workspace.get_application(application_id)
        job = rt.workspace.application_job(application_id)
        artifacts = rt.workspace.list_artifacts(application_id)
        attached = [e.occurred_at for e in rt.workspace.list_events(application_id)
                    if e.event_type.value == 'snapshot_attached']
        boundary = max(attached) if attached else app.created_at
        match = next((a for a in reversed(artifacts) if a.artifact_type==ArtifactType.MATCH_REPORT
                      and a.created_at >= boundary),None)
        result = {'application_id':application_id,'title':job.title,'company':job.company,'url':job.canonical_url,
            'location':job.location,'status':app.status.value,'match_score':match.content.get('overall_score') if match else None,
            'result_at':(match.created_at if match else app.created_at).isoformat()}
        from .presentation import display_label
        result['status_label']=display_label(app.status.value)
        if match:
            parsed=next((a for a in reversed(artifacts) if a.artifact_type==ArtifactType.JOB_ANALYSIS
                and a.created_at>=boundary),None)
            requirements=parsed.content.get('requirements',[]) if parsed else []
            result.update(self.requirement_summary(match.content,requirements))
            result['match_overview'] = self.match_overview(match.content,requirements)
            overview=result['match_overview']
            result['decision_brief']=self.decision_brief(match.content,overview)
            result.update(score_range=match.content.get('score_range'),
                analysis_status=match.content.get('analysis_status','completed'),
                analysis_status_label=display_label(match.content.get('analysis_status','completed')),
                score_algorithm=match.content.get('algorithm','legacy'),
                score_valid_until=match.content.get('valid_until'))
        if detail:
            result.update(description=rt.workspace.current_snapshot(application_id).cleaned_job_description,
                match_report=match.content if match else None)
        return result

    @staticmethod
    def recommendation_record(args,job,retrieved,qualification,interval):
        """Structure and citation checks only; the tier and its meaning are the model's."""
        from eregion.domain.jev_fit.resolve import locate
        tier=args.get('tier')
        if tier not in TIERS:raise ValueError('Choose tier: priority, stretch or to_confirm.')
        reason=args.get('reason')
        if not isinstance(reason,str) or not 1<=len(reason.strip())<=2000:raise ValueError('Give a concise recommendation reason.')
        lists={k:[str(v).strip() for v in (args.get(k) or []) if str(v).strip()] for k in ('evidence','gaps','to_confirm','next_steps')}
        # A tier is defined by what it discloses: a stretch names its gaps, a to_confirm
        # opportunity names what the user must confirm. Contents stay the model's.
        if tier=='stretch' and not lists['gaps']:raise ValueError('A stretch opportunity must list its specific gaps.')
        if tier=='to_confirm' and not lists['to_confirm']:raise ValueError('A to_confirm opportunity must list what the user must confirm.')
        if not lists['next_steps']:raise ValueError('Give at least one concrete next step.')
        near=[c for c in (qualification.get('near_misses') or [])]
        if near and tier=='priority':
            raise ValueError('This opening is not confirmed inside the requested '+' and '.join(c['field'] for c in near)
                             +' (near or unknown); present it as to_confirm or stretch and state that, never as priority.')
        conditions=[c['quote'] for c in (qualification.get('candidate_conditions') or [])]
        if conditions and tier=='priority' and not lists['to_confirm'] and args.get('conditions_reviewed') is not True:
            # One explicit check, judged by the model: intern postings were placed as priority while the
            # resume stated no availability or start date (live-set-03, user labels to_confirm).
            raise ValueError('This posting states conditions on the candidate: '+json.dumps(conditions[:6],ensure_ascii=False)
                             +'. Check each against the resume and the facts the user stated. If any is not stated there '
                             '(for example availability, start date, cohort or graduation window), present the job as '
                             'to_confirm and list it; if all are stated, resubmit priority with conditions_reviewed=true '
                             'and say in the reason how they are met.')
        sources={'title':retrieved.get('title') or '','location':retrieved.get('location') or '',
                 'description':retrieved.get('description') or ''}
        evidence=[]
        for quote in lists['evidence'][:4]:
            found=None
            for name,text in sources.items():
                span=locate(quote,text)
                if span is not None:found=(name,span);break
            if not found:raise ValueError('Each evidence item must be an exact quote from this posting: '+quote[:120])
            name,(a,b)=found;evidence.append({'source':name,'quote':sources[name][a:b]})
        score=job['match_score']
        if score is not None:
            meaning=('Fit estimate 0-100 of the saved resume against this posting'
                     +(' (Jev; unresolved items carry probability-weighted credit)' if job.get('score_algorithm')=='jev-only' else '')
                     +'. It is not proof of eligibility.')
        else:
            meaning='No point score: unresolved requirements leave a bounded interval.' if interval else 'Score unavailable.'
        payload={k:job[k] for k in ('application_id','title','company','location','url','match_score')}|{
            'tier':tier,'tier_label':TIERS[tier],'reason':reason.strip(),'evidence':evidence,
            'qualification_evidence':{f:(c.get('quote') or '')[:300] for f,c in (qualification.get('checks') or {}).items()},
            'gaps':lists['gaps'],'to_confirm':lists['to_confirm'],'next_steps':lists['next_steps'],
            'request_near_misses':qualification.get('near_misses') or [],
            'score_meaning':meaning,'verified_at':retrieved.get('read_at'),'caveats':str(args.get('caveats') or '')[:2000]}
        if score is None:
            payload.update(score_range=interval,analysis_status=job.get('analysis_status'))
        return payload

    BRIEF_ITEMS = 10
    BRIEF_TEXT = 140

    @classmethod
    def decision_brief(cls,report,overview):
        """Everything a recommend/exclude/needs-evidence decision needs, without paging.

        Each item keeps its stable requirement ID, level and the scorer's status; text is
        the posting's own requirement, shortened only with an explicit flag. Totals and the
        omitted count make any cap visible; inspect_job(requirement_ids=...) reads exact
        items. Statuses are reported, never converted into a verdict here.
        """
        matches=[m for m in report.get('matches',[]) if isinstance(m,dict)]
        def item(m):
            text=str(m.get('job_skill') or m.get('display_name') or '')
            row={'id':m.get('requirement_id'),'level':m.get('requirement_level'),'status':m.get('match_status'),
                 'requirement':text[:cls.BRIEF_TEXT]}
            if len(text)>cls.BRIEF_TEXT:row['text_truncated']=True
            return row
        def listed(rows):
            return {'total':len(rows),'items':[item(m) for m in rows[:cls.BRIEF_ITEMS]],
                    'omitted':max(0,len(rows)-cls.BRIEF_ITEMS)}
        unresolved=[m for m in matches if m.get('requirement_level')=='required'
                    and m.get('match_status') in {'missing','partial','unknown'}]
        # Missing first, then partial, then unknown: the order a reader checks gaps.
        unresolved.sort(key=lambda m:['missing','partial','unknown'].index(m['match_status']))
        confirm=[m for m in matches if m.get('match_status')=='needs_confirmation']
        counts={}
        for m in matches:
            key=f"{m.get('requirement_level') or 'unspecified'}:{m.get('match_status')}"
            counts[key]=counts.get(key,0)+1
        return {'confirmed_ineligible':any(m.get('match_status')=='confirmed_condition' and m.get('condition_satisfied') is False
                                          for m in matches),
                'counts':counts,
                'strengths':[r['requirement'][:100] for r in overview['matched'][:3]],
                'required_unresolved':listed(unresolved),
                'needs_confirmation':listed(confirm),
                'note':'Scoring is complete. Statuses come from the scorer: partial means some supporting evidence, not '
                       'automatically disqualifying; unknown means the scorer did not decide, so compare that requirement with '
                       'the resume yourself. Use these items to choose the tier (priority, stretch, to_confirm) and to name '
                       'the gaps that matter most and the items to confirm. For exact text or resume evidence of any item (listed, truncated '
                       'or omitted) call inspect_job with requirement_ids.'}

    def requirement_details(self,application_id,requirement_ids):
        report=(self.job_summary(application_id,detail=True).get('match_report') or {})
        wanted=list(dict.fromkeys(str(i) for i in requirement_ids))[:20]
        found={m.get('requirement_id'):m for m in report.get('matches',[]) if isinstance(m,dict)}
        def evidence(m):
            rows=[str(e) for e in (m.get('resume_evidence') or [])][:2]
            return [r[:600] for r in rows]
        return {'application_id':application_id,
                'requirements':[{'id':i,'level':found[i].get('requirement_level'),'status':found[i].get('match_status'),
                                 'requirement':found[i].get('job_skill'),'reason':found[i].get('match_reason'),
                                 'resume_evidence':evidence(found[i]),
                                 **({'condition_satisfied':found[i].get('condition_satisfied')}
                                    if found[i].get('match_status')=='confirmed_condition' else {})}
                                for i in wanted if i in found],
                'unknown_ids':[i for i in wanted if i not in found]}

    @staticmethod
    def match_overview(report,requirements=()):
        from .presentation import display_label
        names={r['requirement_id']:r.get('display_name') or r.get('canonical_name') for r in requirements if r.get('requirement_id')}
        matches = [m for m in report.get('matches', []) if isinstance(m, dict)]
        result = {'explanation': report.get('explanation', ''),
                  'score_range':report.get('score_range'),
                  'analysis_status':report.get('analysis_status','completed'),
                  'analysis_status_label':display_label(report.get('analysis_status','completed')),
                  'detail_hint': 'Scoring has finished. Report the observed score or interval and unresolved items; do not repeat scoring merely because uncertainty remains. Full citations remain in match_report.'}
        for status in ('matched', 'partial', 'missing', 'needs_confirmation', 'unknown', 'confirmed_condition'):
            rows = [m for m in matches if m.get('match_status') == status]
            result[status] = [{'requirement': display_label(names.get(m.get('requirement_id')) or m.get('display_name') or m.get('job_skill')),
                              'display_name':display_label(names.get(m.get('requirement_id')) or m.get('display_name') or m.get('job_skill')),
                              'status_label':display_label(status),'level': m.get('requirement_level'),
                              **({'satisfied':m.get('condition_satisfied')} if status=='confirmed_condition' else {})}
                              for m in (rows[:8] if status == 'matched' else rows)]
        return result

    @staticmethod
    def requirement_summary(report,requirements=()):
        """Counts by match status and the first few gaps, for the job card in chat."""
        matches = [m for m in report.get('matches') or [] if isinstance(m,dict)]
        counts = {status:sum(m.get('match_status')==status for m in matches)
            for status in ('matched','partial','missing','needs_confirmation','unknown','confirmed_condition')}
        missing = [*(report.get('missing_required_requirements') or []),*(report.get('missing_preferred_requirements') or [])]
        from .presentation import display_label
        names={r.get('canonical_name'):r.get('display_name') for r in requirements if r.get('display_name')}
        gaps = list(dict.fromkeys(display_label(names.get(m.get('canonical_name')) or m.get('display_name') or m.get('canonical_name')) for m in missing
            if isinstance(m,dict) and str(m.get('canonical_name') or '').strip()))[:3]
        return {'requirement_counts':counts if matches else None,'gaps':gaps}

    def materials(self,pack_id,*,detail=True):
        pack = self.runtime.pack_workflow.refresh_staleness(pack_id)
        items = []
        for item in self.runtime.packs.items(pack_id):
            if not detail:
                value = item.model_dump(mode='json')
                items.append({k:value[k] for k in ('pack_item_id','artifact_type','status','version','revision_count','verification') if k in value})
                continue
            content = self.runtime.packs.artifact(pack_id,item.pack_item_id) if item.status.value in {'awaiting_review', 'approved'} else None
            text = (render_tailored_resume_text(content) if item.artifact_type=='tailored_resume'
                else render_cover_letter(content) if item.artifact_type=='cover_letter' else json.dumps(content)) if content else ''
            items.append({**item.model_dump(mode='json'),'text':text})
        overview = [{**{k:item.get(k) for k in ('pack_item_id','artifact_type','status','version')},
            'verification':item.get('verification'),
            'document_text_path':['items', str(index), 'text'] if detail and item.get('text') else None}
            for index,item in enumerate(items)]
        result = {'pack_id':pack_id,'application_id':pack.application_id,'status':pack.status.value,'version':pack.version,'items':items,
            'material_overview':overview,
            'detail_hint':'Generation status and verification are in material_overview. To inspect wording, read document_text_path directly; do not page through all item metadata. Awaiting review means generation is finished and the user reviews next. Complete source/verification evidence remains in items.'}
        if not detail:result['content_available_via'] = 'inspect_materials'
        return result

    def material_choices(self, application_id):
        from eregion.runtime.application_pack.selection import current_materials, latest_materials
        rt = self.runtime
        latest = {i.pack_item_id for p,i in latest_materials(rt.workspace,rt.packs,rt.pack_workflow,application_id)}
        return [{'pack_id':p.pack_id,'item_id':i.pack_item_id,'type':i.artifact_type,
            'status':i.status.value,'version':i.version,'verified':i.verification.passed if i.verification else None,
            'has_artifact':bool(i.artifact_id),'is_latest_attempt':i.pack_item_id in latest,
            'requires_explicit_selection':i.pack_item_id not in latest}
            for p,i in current_materials(rt.workspace,rt.packs,rt.pack_workflow,application_id)]

    def application(self,run_id,*,include_fields=False,field_key=None):
        state = self.applications.repository.get(run_id)
        delivered=[m['id'] for m in state.get('messages',[]) if m.get('role')=='user'][-8:]
        if state['status'] in {'queued','running','submitting'} and not include_fields and not field_key:
            return {k:state.get(k) for k in ('run_id','status','version','material_refs')} | {
                'working':True,'delivered_user_message_ids':delivered,
                'next':'The browser worker is still executing. Use wait_for_result(kind=application,id=run_id) or do independent work. Fields from earlier pages are not a final result. Use inspect_application(field_key=...) only to investigate a specific working control.'}
        result = {k:state.get(k) for k in ('run_id','url','status','message','input_request','pending_fields',
            'fields','messages','account_request','confirmation','pause_requested','material_refs','agent_reason') if k!='messages'}
        result['version'] = state.get('version')
        result['delivered_user_message_ids'] = delivered
        controls = state.get('review_fields',[]) if state['status']=='awaiting_review' else state.get('fields',[])
        # Review values are a result, not a navigation index into DOM metadata.
        # Preserve the complete original through a targeted read when it matters.
        compact = []
        for field in controls:
            if field.get('kind')=='password':continue
            answer = {k:field.get(k) for k in ('key','label','value')}
            if field.get('invalid') or field.get('error'):
                answer.update({k:field[k] for k in ('invalid','error') if k in field})
            if len(str(answer.get('value') or ''))>300:
                answer['value']={'filled':True,'characters':len(str(answer['value'])),
                    'detail':'Read this field_key for its complete value.'}
            compact.append(answer)
        if state['status']=='awaiting_review':
            result['observed_answers'] = compact
            result['documents']=[{'label':f.get('label'),'filename':f.get('value')}
                for f in state.get('fields',[]) if f.get('kind')=='file']
            result.pop('fields',None)
            result.pop('agent_reason',None)
            result.pop('account_request',None)
            result['detail_hint'] = ('observed_answers contains the actual reviewed controls and is sufficient to report the outcome. '
                'User facts and intended answers are not proof of what the website contains. '
                'If a specific value is unclear, inspect field_key for that original control, not the whole archive. '
                'awaiting_review means prepared for the user to review; it does not mean submitted.')
        elif include_fields:
            result['fields']=compact
        else:
            # W6 review: the raw field list (DOM metadata, every option) was ~19k characters and
            # the career agent paged through it 6 times. By default only what blocks progress.
            result.pop('fields',None)
            result['blocking_fields']=[{k:f.get(k) for k in ('key','label','kind','invalid','error') if f.get(k) not in (None,'',False)}
                for f in controls if (f.get('required') and f.get('value') in (None,'',False)) or f.get('invalid')][:20]
            result['field_count']=len(controls)
            if state['status']=='needs_input':
                # Model replay: the woken career agent read this view, not the stop notice. No
                # blocking (required) field does not mean every optional field is settled.
                from eregion.runtime.auto_apply.autofill import desired_answer, matches
                answers_state={'answers':{},'profile':{},**state}
                searches=state.get('option_search_log',{})
                def unresolved(f,wanted):
                    # Career replay: the agent asked whether to use "Systems Engineering" without
                    # knowing the site lists no such option. Say why each field is unresolved.
                    log=searches.get(f['key']) or {}
                    seen=[str(o) for o in log.get('seen',[])]
                    entry={'key':f['key'],'label':f.get('label'),'target':wanted,
                           **({'selected':f['value']} if f.get('value') else {})}
                    if f.get('value'):entry['reason']='selected_value_differs_from_fact'
                    elif log:entry['reason']=('no_matching_option_in_searches' if str(wanted).casefold() not in
                        {x.casefold() for x in seen} else 'matching_option_seen_not_selected')
                    else:entry['reason']='not_filled'
                    if log:entry['search']={'queries':log.get('queries',[]),'candidates_seen':len(seen),
                        'target_seen':str(wanted).casefold() in {x.casefold() for x in seen},
                        **({'source':log['source']} if log.get('source') else {})}
                    return entry
                result['unresolved_optional_fields']=[unresolved(f,wanted) for f in controls
                    if f.get('kind') and not f.get('required') and f.get('kind')!='file'
                    and (wanted:=desired_answer(f,answers_state)) is not None and not matches(f,wanted)][:20]
                result['budget']=self.task_budget(state.get('origin_task_id'))
                if state.get('agent_reason') in {'page_budget','application_budget','agent_limit','no_progress'}:
                    result['next']=("Stopped on a limit: page_budget is this page's allowance, application_budget the whole "
                        "application's; budget is the task's remaining provider allowance. Tell the user the reason, the "
                        "blocking and unresolved fields (with their reasons and search evidence) and the remaining budget, then "
                        "ask how to proceed. Do not re-read all fields or search jobs. Nothing is running: any text you send must "
                        "not say the application is continuing, being pushed toward review or being prepared.")
        if field_key:
            field = next((f for f in controls if f.get('key')==field_key),None)
            if field is None:raise ValueError('Unknown field_key. Use a key from the current application observation.')
            if field.get('kind')=='password' or state.get('account_request'):
                raise ValueError('Account controls are available only through the secure interface.')
            result={'run_id':run_id,'status':state['status'],'version':state.get('version'),
                'control':field,'detail_hint':'This is the requested original control, with its complete value and constraints.'}
        if state.get('account_request'):
            account = state['account_request']
            result['account_request'] = {k:account.get(k) for k in ('origin','mode','stage')}
            result['account_request']['consent_count'] = len(account.get('consents',[]))
            result['fields'] = [] # Account fields/page prose belong to the secure UI.
        if state['status']=='needs_input' and not include_fields and state.get('input_request'):
            result.pop('fields',None)
            result['detail_hint']='Forward this outstanding question to the user. If an actual user message already answers it, forward that message once. Detailed controls remain available with include_fields=true.'
        return result

    def interview(self,identifier):
        view = self.runtime.mock_interviewer.view(identifier)
        view.update(mock_interview_id=identifier,status=view['interview']['status'],
            version=view['interview']['version'],current_question=view.get('question'))
        if view.get('report_available'):view['report'] = self.runtime.mock_interviewer.interviews.report(identifier)
        if view['interview']['status'] == 'failed':
            failure = view.get('failure') or {}
            view.update(error='Interview processing failed; the answer remains saved.',
                error_code=failure.get('error_code', 'interview_failed'),
                next=failure.get('next', 'Inspect the failure and retry only the failed stage.'))
        return view

    def route_user_control(self,state,message):
        """Queue interview answers and explicit controls before model routing."""
        routed=self.interview_inputs.route(state,message) or self.route_application_reply(state,message)
        if routed:return routed
        text=message['text'].strip().rstrip('.!。！').casefold()
        if text not in {'end interview','end the interview','end this interview',
                        'finish interview','finish the interview','end this practice and show my report',
                        '结束面试','结束这次面试','结束模拟面试'}:
            return None
        context=message.get('context') or {}
        identifiers=[context['interview_id']] if context.get('interview_id') else state.get('interviews',[])
        active=[i for i in identifiers if self.runtime.mock_interviewer.interviews.get(i).status.value
                not in {'completed','cancelled'}]
        if len(active)!=1:return None
        result=self.execute(state['task_id'],'control_interview',
            {'interview_id':active[0],'action':'end','message_id':message['id']},call_id='direct:'+message['id'])
        return {'text':'The interview has ended. Your saved answers and report are available below.',
                'tool':'control_interview','result':result}

    def require_link(self,state,collection,identifier):
        if identifier not in state.get(collection,[]):raise ValueError('This result does not belong to this task. Inspect or import it first.')

    def link_job_pack(self, state, pack_id):
        if pack_id in state.get('packs', []): return
        pack = self.runtime.packs.get(pack_id)
        self.require_link(state, 'jobs', pack.application_id)
        self.repository.link(state['task_id'], 'packs', pack_id)

    @staticmethod
    def user_message(state,identifier):
        message = next((m for m in state['messages'] if m['id']==identifier and m['role']=='user'),None)
        if not message:
            entry=next((e for e in state.get('interview_queue',[]) if e['message_id']==identifier),None)
            if entry:message={'id':identifier,'role':'user','text':entry['text'],'at':entry['at'],
                'context':{'interview_id':entry['interview_id']}}
        if not message:raise ValueError('Cite an actual user message from this conversation.')
        return message

    @staticmethod
    def key(state,action,args):
        message = next((m['id'] for m in reversed(state['messages']) if m['role']=='user'),'')
        return hashlib.sha256(json.dumps([state['task_id'],message,action,args],sort_keys=True).encode()).hexdigest()

    def analysis_fingerprint(self,resume_text,snapshot_id):
        inputs = [resume_text,snapshot_id,self.analysis.cache_signature(),self.fit_facts()]
        return hashlib.sha256(json.dumps(inputs).encode()).hexdigest()

    def save_scored_evidence(self,task_id,identifiers):
        results={identifier:{k:v for k,v in self.job_summary(identifier).items() if k in {
            'application_id','title','company','url','location','match_score','score_range','qualification_evidence','decision_brief'}}
            for identifier in identifiers}
        from .qualification import assess
        state=self.repository.get(task_id)
        for identifier,row in results.items():
            q=assess(state,self.job_summary(identifier,detail=True))
            row['qualification_evidence']=[{'field':field,**quote}
                for field,item in q['checks'].items() for quote in item.get('evidence',[])][:8]
            # The posting's own conditions on the candidate (GPA, nationality, cohort...),
            # quoted by the qualification model, so tiering never depends on what the
            # fit scorer happened to mark as unresolved.
            row['posting_conditions']=q.get('candidate_conditions',[])
            # Where the opening sits just outside the requested level or location (near miss):
            # the user decides, so the difference is stated and the job is never a priority match.
            row['request_near_misses']=q.get('near_misses',[])
        self.repository.change(task_id,lambda s:{'scored_job_evidence':{**s.get('scored_job_evidence',{}),**results}})

    def fit_facts(self):
        profile=self.applications.repository.saved_candidate().get('profile',{})
        # Location is explicit saved data, never a proxy for citizenship/permission.
        return {k:profile[k]for k in ('city','country')if profile.get(k)} or None

    def execute(self,task_id,name,args,*,call_id):
        if name == 'verify_and_score_jobs':
            from eregion.runtime.request_options import enabled
            if not enabled('VERIFY_AND_SCORE',default=self.api_first()):raise ValueError('verify_and_score_jobs is disabled.')
            from .batch_workflow import run
            return run(self,task_id,args,call_id)
        if name == 'dismiss_candidate':
            from .candidate_health import dismiss
            return dismiss(self.repository, task_id, args,
                           self._browsing.inspect(task_id) if self._browsing else None)
        if name == 'verify_and_score':
            from eregion.runtime.request_options import enabled
            if not enabled('VERIFY_AND_SCORE', default=self.api_first()):
                raise ValueError('verify_and_score is disabled.')
            from .workflows import run
            return run(self, task_id, args, call_id)
        acquisition=name in BROWSER_NAMES or name in {'read_job','import_job'}
        if acquisition:
            from .source_health import browser_preflight, record_failure
            state=self.repository.get(task_id)
            observed=self._browsing.inspect(task_id) if self._browsing else None
            # Importing a captured same-posting body is local, like a literal
            # user quote. Inner candidate preflight and posting validation still
            # run. A read, an uncached target, or another identity needs access.
            cached_import=name=='import_job' and any(j.get('url') and j.get('description')
                and job_key(j['url'])==job_key(args.get('url','')) for j in state.get('retrieved_jobs',{}).values())
            if not (name=='import_job' and args.get('job_text') or cached_import):
                browser_preflight(state,name,args,observed)
        try:return self._execute(task_id,name,args,call_id=call_id)
        except Exception as exc:
            if acquisition:
                record_failure(self.repository,task_id,args.get('url') or (observed or {}).get('url'),exc,observed)
            raise

    def _execute(self,task_id,name,args,*,call_id):
        state = self.repository.get(task_id)
        from .candidate_health import preflight
        if name in {'browser_open','browser_read','read_job','import_job'}:
            observed=self._browsing.inspect(task_id) if self._browsing else None
            preflight(state,name,args,observed)
        rt = self.runtime
        key = self.key(state,name,args)
        recommendation=self.recommendation_run(state)
        if recommendation and name not in {t['function']['name'] for t in self.available_tools(state)}:
            raise ValueError('This unattended task is authorized for job research and recommendations only.')
        if name in {'configure_job_alert','set_job_alert_enabled'}:
            self.user_message(state,args['message_id'])
            if not getattr(self,'alerts',None):raise ValueError('Scheduled searches are unavailable in this runtime.')
            identifier=args.get('alert_id')
            old=self.alerts.get(identifier) if identifier else None
            if name=='set_job_alert_enabled':
                if type(args['enabled']) is not bool:raise ValueError('Use a boolean enabled setting.')
                config={k:old[k] for k in AlertConfig.model_fields if k in old}|{'enabled':args['enabled']}
            else:config=args['config']
            return self.alerts.save(config,request_key=key,alert_id=identifier,expected_version=old['version'] if old else None)
        if name=='decide_job':
            from .decisions import decide
            return decide(self.repository,task_id,state,args)
        if name in {'search_jobs','screen_jobs'}:
            from .decisions import gate
            blocked=gate(state,name,args)
            if blocked:return blocked
            from .candidate_queue import enabled as candidate_batches, gate as candidate_gate
            if candidate_batches():
                from .search_recovery import recovery
                blocked=candidate_gate(state,recovery(state,saved_urls=[self.runtime.workspace.application_job(a).canonical_url
                    for a in state['jobs']]),name)
                if blocked:return blocked
        if name=='recommend_job':
            identifier=args['application_id'];self.require_link(state,'jobs',identifier)
            saved_rec=next((r for r in state.get('recommendations',[]) if r.get('application_id')==identifier),None)
            if (saved_rec and saved_rec.get('match_score')==self.job_summary(identifier)['match_score']
                    and saved_rec.get('tier')==args.get('tier')):
                return {'recommended':True,'already_recommended':True,'no_new_evidence':True,
                        'recommendation':{k:saved_rec[k] for k in ('application_id','title','url','match_score') if k in saved_rec},
                        'next':'Already saved with this score; do not repeat it. Continue with other candidates or finish.'}
            _,resume_text=self.candidate()
            application=rt.workspace.get_application(identifier)
            fingerprint=self.analysis_fingerprint(resume_text,application.current_snapshot_id)
            if not resume_text or state.get('analyses',{}).get(identifier)!=fingerprint:
                raise ValueError('Score this job against the current resume first.')
            job=self.job_summary(identifier,detail=True)
            if job['status'] in {'applied','interviewing','offer','rejected','withdrawn','archived'}:
                raise ValueError('This job is already applied to or closed; look for a new opportunity.')
            interval=job.get('score_range')
            if job['match_score'] is None and not (isinstance(interval,list) and len(interval)==2
                    and all(type(v) in (int,float) and 0<=v<=100 for v in interval) and interval[0]<=interval[1]):
                raise ValueError('This job has no verified match score or bounded score interval.')
            if job.get('score_algorithm')=='jev-only':
                import time
                if (job.get('score_valid_until')or 0)<=time.time():raise ValueError('This score has expired. Score the job again before recommending it.')
            if any(m.get('match_status')=='confirmed_condition' and m.get('condition_satisfied')is False
                   for m in (job.get('match_report')or {}).get('matches',[])):
                raise ValueError('A confirmed personal condition is not satisfied; this job cannot be recommended as eligible.')
            retrieved=next((j for j in state.get('retrieved_jobs',{}).values() if j.get('url') and job_key(j['url'])==job_key(job['url'])),None)
            if not retrieved:raise ValueError('Read the current posting before recommending it.')
            from .qualification import record
            qualification=record(self.repository,task_id,retrieved)
            if not qualification['qualified']:
                return {'error':'Requested search constraints are not verified; this job cannot count as a recommendation.',
                        'error_code':'qualification_needed','qualification':qualification,
                        'next':'Use decide_job with needs_evidence or exclude. Do not claim it meets the requested conditions.'}
            payload=self.recommendation_record(args,job,retrieved,qualification,interval)
            if recommendation:
                result=self.alerts.add(recommendation['run_id'],payload)
                if not result['added']:return result
                payload=result['recommendation']
            self.repository.change(task_id,lambda s:{'recommendations':[
                *[j for j in s.get('recommendations',[]) if job_key(j['url'])!=job_key(payload['url'])],payload][-30:]})
            return {'recommended':True,'recommendation':payload}
        if name in BROWSER_NAMES:
            if name in VISUAL_NAMES and not state.get('visual_fallback'):
                raise ValueError('Enable Visual browser backup in Your details before sending screenshots to the configured model.')
            revision=state['chat_revision']
            def active():
                current=self.repository.get(task_id)
                return current['chat_revision']==revision and not current.get('pause_requested') and current['status'] not in {'cancelled','completed'}
            result=self.browsing.call(task_id,name,args,visual=bool(state.get('visual_fallback')),active=active)
            from .source_health import record_browser
            record_browser(self.repository,task_id,result)
            summary={k:result.get(k) for k in ('url','heading','title','blocked','tabs','observation_id','page_evidence_hash','job_detail')}
            summary['regions']=[{k:r[k] for k in ('ref','label','tag','text_length') if k in r}
                for r in result.get('regions',[])[:6]]
            from eregion.runtime.browser.evidence import research_page
            page = research_page(result)
            def remember(current):
                from .search_recovery import candidates_from_page, search_request
                candidates={**current.get('observed_job_candidates',{}),
                    **candidates_from_page(result,search_request(current))}
                pages = list(current.get('research_pages', []))
                prior = next((p for p in pages if p.get('url') == page['url']), None)
                if not page['links'] and prior:
                    page['links'] = prior.get('links', [])
                pages = [p for p in pages if p.get('url') != page['url']]
                return {'browser': summary, 'active_browser': 'research', 'research_pages': [*pages, page][-8:],
                    'observed_job_candidates':dict(list(candidates.items())[-60:])}
            self.repository.change(task_id, remember)
            return result
        if name=='search_jobs':
            from urllib.parse import urlsplit
            from .source_health import blocked_hosts as persistent_blocked_hosts
            blocked_hosts=persistent_blocked_hosts(state)
            filters=effective_filters(state,args)
            # pages is a LinkedIn depth choice; board APIs never receive it
            # (acceptance tiered-v3-01: a TypeError here ended the search).
            search_args={k:v for k,v in args.items() if k not in FILTER_FIELDS and k!='pages'}
            if blocked_hosts:search_args['excluded_hosts']=blocked_hosts
            use_directory = search_args.pop('use_directory', False)
            if use_directory and args.get('boards'): raise ValueError('Choose directory selection or explicit boards.')
            treg_cfg,source_restriction=self._treg_config(),self._source_scope(state)
            api_first=self.api_first()
            if api_first and source_restriction=='linkedin':
                # The user asked for LinkedIn only: never traverse other boards.
                from .linkedin_runtime import search as linkedin_search
                linked=linkedin_search(self,task_id,state,args)
                if linked.get('status')=='unavailable' and self._treg():
                    return self._treg_search(task_id,state,args)
                return linked
            if api_first and not args.get('boards') and not use_directory:
                use_directory=True  # API first: verified company boards before any browser attempt
            if use_directory: search_args.pop('boards', None)
            health=state.get('search_source_health') or {}
            from .source_health import user_turn
            turn=user_turn(state)
            sources=dict(health.get('sources',{})) if health.get('user_turn_id')==turn else {}
            exhausted=[source for source,item in sources.items() if item.get('blocked') or item.get('empty_count',0)>=2 or item.get('error_count',0)>=2 or item.get('http_status')==404]
            if exhausted:search_args['excluded_sources']=exhausted
            # Directory search pages through matching boards: a repeat in the same user
            # turn covers the next boards for this role/location instead of the same five.
            from .board_directory import role_families
            directory_key=json.dumps([sorted(role_families(str(args['query']))),
                str(args.get('location','')).strip().casefold()])
            progress=health.get('directory_searched',{}) if health.get('user_turn_id')==turn else {}
            searched=list(progress.get(directory_key,[])) if use_directory else []
            if use_directory: search_args['skip']=searched
            # A presentation-only limit change must not refetch the same
            # exhausted result list. A larger limit can expand a genuinely
            # truncated list; changed source health/new user input can retry.
            request_key=hashlib.sha256(json.dumps({
                'query':str(args['query']).strip().casefold(),
                'location':str(args.get('location','')).strip().casefold(),
                'boards':args.get('boards'), 'use_directory':use_directory, 'excluded_hosts':blocked_hosts,
                'excluded_sources':sorted(exhausted),'directory_searched':searched,'filters':filters},sort_keys=True).encode()).hexdigest()
            attempts=state.get('search_request_cache',{})
            attempts=dict(attempts.get('requests',{})) if attempts.get('user_turn_id')==turn else {}
            previous=attempts.get(request_key)
            if previous and (args.get('limit',10)<=previous['limit'] or len(previous['result'].get('jobs',[]))<previous['limit']):
                return {**previous['result'],'no_new_evidence':True,
                    'progress_feedback':'This search/source configuration already returned this list in the current user turn. No HTTP repeated. Verify a healthy observed posting, or discover a genuinely different query/source. Cached snippets are not verified vacancies.'}
            revision = state['chat_revision']
            # Polled per downloaded chunk and limiter wait; loading the task state each
            # time would cost hundreds of DB reads per large board. Re-check at most 4x/s.
            checked = {'at': float('-inf'), 'value': False}
            def cancelled():
                import time as _time
                if checked['value'] or _time.monotonic() - checked['at'] < .25:
                    return checked['value']
                current = self.repository.get(task_id)
                checked['at'] = _time.monotonic()
                checked['value'] = bool(current['chat_revision'] != revision or current.get('pause_requested')
                                        or current['status'] in {'cancelled','completed'})
                return checked['value']
            from contextlib import nullcontext
            scope = self.discovery.cancellation(cancelled) if isinstance(self.discovery, JobDiscovery) else nullcontext()
            with scope:
                if use_directory:
                    result = self.discovery.search_directory(**search_args, cancelled=cancelled)
                else:
                    result = self.discovery.search(**search_args)
                    if args.get('boards'): result['board_verification'] = 'unverified explicit tokens; directory verification not established'

            if api_first:
                result['jobs'],result['local_filter']=filter_cards(result.get('jobs',[]),state)
                result['unapplied_board_filters']=list(filters)
            if api_first and source_restriction!='no_linkedin':
                from .linkedin_runtime import search as linkedin_search
                linked=linkedin_search(self,task_id,self.repository.get(task_id),args)
                result['jobs']=[*result.get('jobs',[]),*linked.get('jobs',[])]
                result['linkedin_mcp']={k:v for k,v in linked.items() if k!='jobs'}
            if blocked_hosts:result['excluded_blocked_hosts']=blocked_hosts
            # API-first: thin directory coverage (fewer than TREG_SUPPLEMENT_BELOW jobs) gets one
            # Treg LinkedIn supplement per query/location per user turn, merged and deduped.
            supplement_key=json.dumps([str(args['query']).strip().casefold(),str(args.get('location','')).strip().casefold(),filters],sort_keys=True)
            supplemented=list(health.get('treg_supplemented',[])) if health.get('user_turn_id')==turn else []
            if (api_first and use_directory and len(result.get('jobs') or [])<TREG_SUPPLEMENT_BELOW
                    and source_restriction!='no_linkedin' and self._treg()
                    and result.get('linkedin_mcp',{}).get('status')!='blocked' and supplement_key not in supplemented):
                supplement=self._treg_search(task_id,state,args)
                supplemented.append(supplement_key)
                if supplement.get('jobs'):
                    result['jobs']=[*(result.get('jobs') or []),*supplement['jobs']]
                result['treg_supplement']={k:v for k,v in supplement.items() if k!='jobs'}
            # One malformed posting URL from a board must not fail the whole search.
            valid=[]
            for job in result.get('jobs') or []:
                try:job_key(job.get('url') or '')
                except ValueError:continue
                valid.append(job)
            if len(valid)!=len(result.get('jobs') or []):
                result['dropped_invalid_urls']=len(result.get('jobs') or [])-len(valid)
            result['jobs']=list({job_key(j['url']):j for j in valid}.values())
            if api_first:
                result['jobs'],_=filter_cards(result['jobs'],state)
            jobs = result.get('jobs', [])
            for error in result.get('errors',[]):
                source=error.get('source')
                if not source:continue
                old=sources.get(source,{})
                sources[source]={**old,'blocked':bool(error.get('blocked')),
                    'empty_count':old.get('empty_count',0)+int(error.get('error')=='no_extracted_results'),
                    'error':error.get('error'),'last_query':args['query']}
            for job in jobs:
                if job.get('source_engine'):sources.pop(job['source_engine'],None)
            # Keep the entire turn's exclusion table. Truncating to eight lets
            # old blocked tenants re-enter after other boards were attempted.
            from .source_health import record_board_outcomes
            sources, history = record_board_outcomes(sources, health.get('history', {}), result, turn)
            if use_directory:
                progress={**progress,directory_key:list(dict.fromkeys([*searched,*result.get('directory_searched',[])]))}
            health={'revision':state['chat_revision'],'user_turn_id':turn,'sources':sources,'history':history,
                    'directory_searched':progress,'treg_supplemented':supplemented}
            attempts[request_key]={'limit':args.get('limit',10),'query':args['query'],'location':args.get('location'),'result':result}
            def remember_search(s):
                # Preserve navigation URLs but key storage by posting identity;
                # tracking-only variants must not evict genuinely new targets.
                candidates={job_key(j['url']):j for j in s.get('search_candidates',{}).values() if j.get('url')}
                candidates.update({job_key(j['url']):j for j in jobs})
                return {'search_source_health':health,'search_candidates':dict(list(candidates.items())[-100:]),
                    'search_request_cache':{'user_turn_id':turn,'requests':dict(list(attempts.items())[-32:])}}
            self.repository.change(task_id,remember_search)
            from eregion.runtime.fast_decisions import configured_decisions
            if jobs and configured_decisions().enabled:
                screened = self.screen(task_id, state, jobs, args['query'] + ' ' + args.get('location', ''))
                result.update(screened)
            return result
        if name=='screen_jobs':
            candidates = {job_key(j['url']):j for j in [*state.get('search_candidates',{}).values(),
                *state.get('retrieved_jobs',{}).values()] if j.get('url')}
            observed = self._browsing.inspect(task_id) if self._browsing else None
            if observed and not observed.get('blocked'):
                for control in observed.get('controls', []):
                    url = control.get('href')
                    if url and url.startswith('https://'):
                        candidates.setdefault(job_key(url), {'url':url,'title':control.get('label',''),
                                                   'context':control.get('context','')})
            urls = list(dict.fromkeys(args['urls']))
            if not 1 <= len(urls) <= 20 or any(job_key(url) not in candidates for url in urls):
                raise ValueError('Screen only links from observed browser/search results or retrieved jobs.')
            return self.screen(task_id, state, [candidates[job_key(url)] for url in urls], args['query'])
        if name=='read_job':
            # Persist attempted identities even when the read rejects a blocked or
            # ambiguous page, so completion recovery cannot force an endless retry.
            self.repository.change(task_id,lambda s:{'job_read_attempts':dict(list({
                **s.get('job_read_attempts',{}),job_key(args['url']):state['chat_revision']}.items())[-100:])})
            observed = self._browsing.inspect(task_id) if self._browsing else None
            from eregion.runtime.auto_apply.contracts import public_https_url
            url=public_https_url(args['url'])
            same_job=bool(observed and job_key(url)==job_key(observed['url']))
            treg_cfg=self._treg_config()
            api_first=self.api_first()
            from .source_health import user_turn
            turn=user_turn(state)
            cached=next((j for j in state.get('retrieved_jobs',{}).values()
                if j.get('url') and job_key(j['url'])==job_key(url)),None)
            if (api_first and not args.get('region_ref') and turn and cached
                and cached.get('source') in {'treg:crawl4ai.web.scrape','linkedin_mcp'}
                and state.get('successful_read_turns',{}).get(job_key(url))==turn):
                from .posting_brief import with_brief
                return {**with_brief(cached),'cached':True,'no_new_evidence':True}
            if args.get('region_ref') or (same_job and not api_first):
                # Capture the live, rendered description when a site requires JS.
                # The model cannot supply or invent this source text.
                read_args={k:args[k] for k in ('region_ref','observation_id') if k in args}
                read_args['job_only']=True
                first=self.browsing.call(task_id,'browser_read',read_args)
                from .source_health import record_browser
                record_browser(self.repository,task_id,first)
                if first.get('blocked'):raise ValueError('Finish the account/verification step before reading a public job description.')
                if first.get('body_scope')=='unbounded':
                    raise ValueError('No single bounded description. Select this posting\'s description region; whole-page text cannot establish its requirements.')
                linked=any(c.get('href') and c['href'].startswith('https://') and job_key(c['href'])==job_key(url)
                    for c in first.get('controls',[]))
                if job_key(url)!=job_key(first['url']) and not (args.get('region_ref') and linked):
                    raise ValueError('The selected panel does not identify this job URL. Open that posting or select its actual detail panel.')
                pieces=[first.get('text','')]
                latest=first
                for offset in range(7000,min(first.get('text_length',0),35000),7000):
                    page_args={'text_offset':offset,'job_only':True}
                    if args.get('region_ref'):page_args.update(region_ref=args['region_ref'],observation_id=latest['observation_id'])
                    part=self.browsing.call(task_id,'browser_read',page_args)
                    record_browser(self.repository,task_id,part)
                    if part.get('blocked') or part['url']!=first['url'] or part.get('primary_heading')!=first.get('primary_heading'):
                        raise ValueError('The page changed while reading. Inspect it again.')
                    pieces.append(part.get('text',''))
                    latest=part
                # These are character slices of one string, sometimes inside a word.
                description='\n'.join(filter(None,[first.get('primary_heading'),first.get('job_company'),
                    first.get('job_location'),first.get('job_header_text'),''.join(pieces).strip()]))
                if len(description)<100:raise ValueError('This panel has too little job detail. Open the posting or expand its description before reading it.')
                job={'url':url,'title':first.get('primary_heading') or first.get('title'),
                    'company':first.get('job_company',''),'location':first.get('job_location',''),
                    'description':description,'source':'rendered_browser','page_url':first['url']}
            else:
                job=None
                from .linkedin_runtime import available as linkedin_available, invoke as linkedin_invoke
                identity=job_key(url)
                if api_first and identity.startswith('linkedin:') and (state.get('linkedin_mcp_state') or {}).get('stopped'):
                    raise ValueError('LinkedIn MCP source is stopped; resolve its session before another source retry.')
                if api_first and identity.startswith('linkedin:') and linkedin_available(self):
                    detail=linkedin_invoke(self,task_id,state,'details',{'job_id':identity.split(':',1)[1]})
                    if detail['status']!='complete_jd':
                        raise ValueError('LinkedIn MCP posting is '+detail['status']+'; no automatic paid or browser retry.')
                    candidate=next((j for j in state.get('search_candidates',{}).values()
                        if j.get('url') and job_key(j['url'])==identity),{})
                    job={'url':detail['url'],'title':detail.get('title') or candidate.get('title') or '',
                         'company':detail.get('company') or candidate.get('company') or '',
                         'location':detail.get('location') or candidate.get('location') or '',
                         'metadata_evidence':detail.get('metadata_evidence',{}),
                         'discovery_card':{k:candidate.get(k) for k in ('title','company','location')},
                         'description':detail['description'],'source':'linkedin_mcp',
                         'raw_posting':detail.get('raw_posting'),'posting_scope':detail.get('posting_scope')}
                if job is None:job=self._treg_scrape(task_id,state,url) if api_first else None
                if job and job.get('closed'):
                    raise ValueError('This posting reports it is closed or no longer accepting applications; '
                                     'do not import or recommend it.')
                if job is None:job = self.discovery.job(args['url'])
            from .candidate_health import validate_and_record
            validate_and_record(self.repository,task_id,job,observed)
            from datetime import UTC, datetime
            # Verification time for the recommendation record (not part of the body hash).
            job={**job,'read_at':job.get('read_at') or datetime.now(UTC).isoformat()}
            self.repository.change(task_id,lambda s:{'retrieved_jobs':dict(list({**s.get('retrieved_jobs',{}),job['url']:job,args['url']:job}.items())[-100:])})
            if job.get('source') in {'treg:crawl4ai.web.scrape','linkedin_mcp'} and turn:
                self.repository.change(task_id,lambda s:{'successful_read_turns':dict(list({
                    **s.get('successful_read_turns',{}),job_key(url):turn}.items())[-100:])})
            from .posting_brief import with_brief
            return with_brief(job)
        if name=='import_job':
            retrieved = next((job for job in state.get('retrieved_jobs', {}).values()
                              if job.get('url') and job_key(job['url']) == job_key(args['url'])), None)
            quote = None
            if args.get('job_text'):
                message = self.user_message(state, args.get('message_id'))
                quote = args['job_text'].strip()
                if not quote or quote not in message['text']:
                    raise ValueError('job_text must be a literal job-description excerpt from the cited user message.')
            if retrieved:
                # Captured same-posting evidence has precedence, even if a tool
                # mistakenly quotes the user's task instruction as job_text.
                job = retrieved
            elif quote:
                source_key = hashlib.sha256((message['id'] + ':' + quote).encode()).hexdigest()
                checked = state.get('job_source_checks', {}).get(source_key)
                if checked is None:
                    from eregion.runtime.semantics import SemanticDecisions
                    from eregion.domain.model import shared_model
                    checked = SemanticDecisions(model=shared_model(purpose='fast')).pasted_job_text(
                        message['text'], quote)
                    self.repository.change(task_id, lambda current: {'job_source_checks': dict(list({
                        **current.get('job_source_checks', {}), source_key: checked}.items())[-16:])})
                if not checked:
                    raise ValueError('The quote is not identifiable as a pasted job description. Read the posting URL or ask the user for the actual JD; task instructions and search preferences are not job evidence.')
                job = {'url':args['url'] or None, 'description':quote, 'source':'user_job_quote',
                       **{k:args.get(k,'') for k in ('title','company','location')}}
            elif args.get('message_id'):
                raise ValueError('A message ID alone does not identify job text. Use read_job for the posting URL, or provide job_text quoting the pasted JD; never import the task instruction.')
            else:
                job = self.discovery.job(args['url'])
            if job.get('source')=='rendered_browser':
                # Metadata must be present in the captured panel as well. Applied before the
                # title check, so a captured page without title metadata can be imported.
                normalize=lambda value:re.sub(r'\s+',' ',str(value)).strip().casefold()
                evidence=normalize(job['description'])
                job={**job,**{key:args[key] for key in ('title','company','location')
                    if args.get(key) and normalize(args[key]) in evidence}}
            if job.get('source') in {'rendered_browser','user_job_quote'} and not str(job.get('title') or '').strip():
                # A missing title in text the user or page supplied is not a reason to reject the posting.
                raise ValueError('Give the posting title: pass title exactly as it appears in the job text.')
            from .candidate_health import validate_and_record
            validate_and_record(self.repository,task_id,job,self._browsing.inspect(task_id) if self._browsing else None)
            if not job.get('description'):raise ValueError('Read or supply the job description first.')
            resolved = rt.workspace.save_workspace(cleaned_job_description=job['description'],source_url=job['url'],
                title=job.get('title'),company=job.get('company'),location=job.get('location'),
                extraction_metadata={'source':job.get('source','career_retrieval')})
            self.repository.link(task_id,'jobs',resolved.application.application_id)
            # A direct browser flow can acquire its real description later.
            if job['url']:
                from eregion.runtime.auto_apply.contracts import posting
                for run_id in state['application_runs']:
                    child=self.applications.repository.get(run_id)
                    if posting(child['url'])[1]==posting(job['url'])[1]:
                        self.repository.change(task_id,lambda s:{'application_links':{**s.get('application_links',{}),
                            run_id:{'application_id':resolved.application.application_id,'materials':child.get('material_refs',{})}}})
            return self.job_summary(resolved.application.application_id,detail=True)
        if name=='list_jobs':
            jobs = rt.workspace.list_applications(search=args.get('query'),limit=30)
            for app in jobs:self.repository.link(task_id,'jobs',app.application_id)
            return {'jobs':[self.job_summary(a.application_id) for a in jobs]}
        if 'application_id' in args:self.require_link(state,'jobs',args['application_id'])
        if 'pack_id' in args and args['pack_id']:self.link_job_pack(state,args['pack_id'])
        if 'run_id' in args:
            self.require_link(state,'application_runs',args['run_id'])
            if name!='inspect_application':self.require_application_owner(state,self.applications.repository.get(args['run_id']))
        if 'interview_id' in args:self.require_link(state,'interviews',args['interview_id'])
        if name=='inspect_job' and args.get('requirement_ids'):
            # Exact read by stable requirement ID: no full report, no materials.
            return self.requirement_details(args['application_id'],args['requirement_ids'])
        if name=='inspect_job':
            # A new conversation can discover a saved job's existing materials.
            # Linking them here keeps subsequent inspect/revise calls scoped to
            # this verified job instead of forcing another generation.
            packs = rt.packs.list_for_application(args['application_id'])
            for pack in packs:self.repository.link(task_id,'packs',pack.pack_id)
            return {'material_choices':self.material_choices(args['application_id']),
                    'materials':[self.materials(pack.pack_id,detail=False) for pack in packs],
                    **self.job_summary(args['application_id'],detail=True)}
        if name=='score_jobs':
            identifiers=args['application_ids']
            if not identifiers or len(identifiers)>16 or len(set(identifiers))!=len(identifiers):
                raise ValueError('Choose one to sixteen unique imported jobs.')
            for identifier in identifiers:self.require_link(state,'jobs',identifier)
            versions={a:rt.workspace.get_application(a).version for a in identifiers}
            from .qualification import record_many
            qualifications=record_many(self.repository,task_id,[self.job_summary(a,detail=True) for a in identifiers])
            if any(not q['qualified'] for q in qualifications):
                return {'error':'Verify requested constraints before paid batch scoring.','error_code':'qualification_needed','qualifications':qualifications}
            _,text=self.candidate()
            if not text:raise ValueError('Attach a resume in this chat before comparing fit.')
            identifiers,deferred=self.affordable_scoring(task_id,identifiers)
            asyncio.run(self.analysis.analyze_many(identifiers,resume_text=text,candidate_facts=self.fit_facts()))
            fingerprints={a:self.analysis_fingerprint(text,rt.workspace.get_application(a).current_snapshot_id)for a in identifiers}
            self.repository.change(task_id,lambda s:{'analyses':{**s.get('analyses',{}),**fingerprints}})
            self.save_scored_evidence(task_id,identifiers)
            evidence=self.repository.get(task_id).get('scored_job_evidence',{})
            return {'jobs':[{**self.job_summary(a,detail=True),'posting_conditions':evidence.get(a,{}).get('posting_conditions',[]),
                             'request_near_misses':evidence.get(a,{}).get('request_near_misses',[])}
                            for a in identifiers],
                    **({'unscored_for_budget':deferred,'budget_note':'Imported and qualified but not scored: the '
                        'remaining provider attempts could not complete their scoring. Not recommendations.'} if deferred else {}),
                    'no_new_evidence':all(rt.workspace.get_application(a).version==versions[a] for a in identifiers)}
        if name=='score_job':
            from .qualification import record
            qualification=record(self.repository,task_id,self.job_summary(args['application_id'],detail=True))
            if not qualification['qualified']:
                return {'error':'Verify requested constraints before paid scoring.','error_code':'qualification_needed','qualification':qualification}
            saved,text = self.candidate()
            if not text:raise ValueError('Attach a resume in this chat before comparing fit.')
            app = rt.workspace.get_application(args['application_id'])
            fingerprint = self.analysis_fingerprint(text,app.current_snapshot_id)
            if (self.analysis.uses_jev() or state.get('analyses',{}).get(app.application_id)!=fingerprint
                    or self.job_summary(app.application_id)['match_score'] is None):
                asyncio.run(self.analysis.analyze(app.application_id,snapshot_id=app.current_snapshot_id,
                    resume_text=text,expected_version=app.version,candidate_facts=self.fit_facts()))
                self.repository.change(task_id,lambda s:{'analyses':{**s.get('analyses',{}),app.application_id:fingerprint}})
            self.save_scored_evidence(task_id,[app.application_id])
            return self.job_summary(app.application_id,detail=True)
        if name=='generate_materials':
            artifacts = args['artifacts']
            if not artifacts or len(artifacts)>2 or set(artifacts)-{'tailored_resume','cover_letter'}:raise ValueError('Choose resume and/or cover letter.')
            app = rt.workspace.get_application(args['application_id'])
            fresh = args.get('fresh', False)
            if fresh:
                message = self.user_message(state, args.get('message_id'))
                latest = next(m for m in reversed(state['messages']) if m['role']=='user')
                if message['id'] != latest['id']: raise ValueError('A fresh set requires the current user request, not an older instruction.')
            # One generation request per job/user turn, independent of tool
            # argument ordering or splitting resume and letter into two calls.
            request_key = self.key(state, name, {'application_id':app.application_id,'fresh':fresh})
            previous = {p.pack_id for p in rt.packs.list_for_application(app.application_id)}
            pack = rt.pack_workflow.create(app.application_id,expected_version=app.version,
                idempotency_key=request_key,scope_to_inputs=not fresh)
            self.repository.link(task_id,'packs',pack.pack_id)
            rt.pack_workflow.generate_many(pack.pack_id, artifacts, idempotency_key=request_key)
            result = self.materials(pack.pack_id)
            if not fresh and pack.pack_id in previous:
                result['progress_feedback'] = ('Reused saved drafts for identical source inputs; no fresh set was generated. '
                    'If the latest user explicitly requested a new set, call generate_materials with fresh=true and their message_id. '
                    'Otherwise inspect these drafts or return their observed status. Repeating this call will reuse them again.')
            return result
        if name=='inspect_materials':return self.materials(args['pack_id'])
        if name=='revise_material':
            message = self.user_message(state,args['message_id'])
            item = rt.packs.item(args['pack_id'],args['item_id'])
            rt.pack_workflow.regenerate(args['pack_id'],item.pack_item_id,expected_version=item.version,
                idempotency_key=key,feedback=message['text'])
            return self.materials(args['pack_id'])
        if name=='remember_profile':
            from eregion.runtime.auto_apply.resume_profile import extract_chat_profile
            message = self.user_message(state,args['message_id'])
            saved = self.applications.repository.saved_candidate()
            fingerprint = hashlib.sha256((message['id'] + ':' + message['text']).encode()).hexdigest()
            if fingerprint in state.get('profile_updates', {}):
                return {'profile':saved['profile'],'already_processed':True,
                    'progress_feedback':'This user message has already been applied to the reusable profile. Use current details; do not parse or apply the same message again.'}
            parsed = extract_chat_profile(message['text'], current_profile=saved['profile'])
            self.applications.repository.save_candidate({**saved['profile'],**parsed.get('profile',{})})
            if not parsed.get('retryable'):
                self.repository.change(task_id, lambda latest: {'profile_updates':dict(list({
                    **latest.get('profile_updates',{}),fingerprint:{'message_id':message['id'],'changed_fields':list(parsed.get('profile',{}))}}.items())[-40:])})
            return {'profile':self.applications.repository.saved_candidate()['profile'], 'message':parsed.get('message')}
        if name=='start_application':return self.start_application(state,args,key)
        if name=='open_application_url':return self.open_application_url(state,args,key)
        if name=='remember_evidence':
            message = self.user_message(state,args['message_id'])
            quote = args['quote'].strip()
            if not quote or len(quote)>4000 or quote not in message['text']:
                raise ValueError('Quote the user\'s exact career fact. Do not add or infer facts.')
            previous = state.get('saved_evidence',{}).get(key)
            if previous:return {'evidence_id':previous,'saved':True}
            evidence = rt.evidence.create_candidate(category='experience',claim_text=quote,
                source_type='user_attested',exact_quote=quote)
            confirmed = rt.evidence.confirm(evidence.evidence_id,evidence.version)
            if args.get('application_id'):
                rt.evidence.link_to_application(confirmed.evidence_id,args['application_id'],expected_version=confirmed.version)
            self.repository.change(task_id,lambda s:{'saved_evidence':{**s.get('saved_evidence',{}),key:confirmed.evidence_id}})
            return {'evidence_id':confirmed.evidence_id,'saved':True,'quote':quote}
        if name=='inspect_application':
            self.repository.change(task_id,active_application_run=args['run_id'],active_browser='application')
            result=self.application(args['run_id'],include_fields=args.get('include_fields',False),field_key=args.get('field_key'))
            # Rereading a page that waits on the user (sign-in, verification, a question) within one
            # turn cannot change it; say so once instead of letting the turn poll the same state.
            seen=[state.get('chat_revision',0),result.get('version')]
            if result.get('status')=='needs_input' and (state.get('inspected_waiting') or {}).get(args['run_id'])==seen:
                result={**result,'unchanged_since_last_inspection':True,
                    'next':'Nothing changed since your last inspection in this turn; the application is waiting for the user. Do not inspect again; ask or report once and end the turn.'}
            if result.get('status')=='needs_input':
                self.repository.change(task_id,lambda s:{'inspected_waiting':{**(s.get('inspected_waiting') or {}),args['run_id']:seen}})
            return result
        if name=='reply_application':
            message = self.user_message(state,args['message_id'])
            previous = self.applications.repository.get(args['run_id'])
            duplicate = any(m.get('id') == message['id'] for m in previous.get('messages', []))
            if duplicate:
                return {k:previous[k] for k in ('run_id','status','version')} | {
                    'reply_delivered':True,'already_delivered':True,'message_id':message['id'],'no_new_evidence':True,
                    'next':'This exact message was already delivered; no action or verification was repeated. Use the live application outcome. Wait if it is working; if it is awaiting_review, report the verified review result.'}
            self.applications.chat(args['run_id'],ChatRequest(text=message['text'],message_id=message['id'],
                visual_fallback=state['visual_fallback']))
            current = self.application(args['run_id'])
            if current['status'] in {'queued','running','submitting'}:
                return {k:current[k] for k in ('run_id','status','version')} | {
                    'reply_delivered':True,'message_id':message['id'],'no_new_evidence':duplicate,
                    'next':'The exact answer is delivered and the browser worker is processing it. Wait with wait_for_result(kind=application,id=run_id), or do independent work. Do not forward it again or page through its working form.'}
            return current
        if name=='start_interview':
            interview = rt.mock_interviewer.start(args['application_id'],mode=args.get('mode','mixed'),
                target_question_count=args.get('question_count',5),idempotency_key=key)
            self.repository.link(task_id,'interviews',interview.mock_interview_id)
            return self.interview(interview.mock_interview_id)
        if name=='inspect_interview':return self.interview(args['interview_id'])
        if name in {'answer_interview','control_interview'}:
            message = self.user_message(state,args['message_id'])
            current = rt.mock_interviewer.interviews.get(args['interview_id'])
            if name=='answer_interview':
                answer_key=f'career:{message["id"]}:{args["question_id"]}'
                previous=rt.mock_interviewer.interviews.answer_by_key(current.mock_interview_id,answer_key)
                if previous and previous.original_text!=message['text']:
                    raise ValueError('The saved interview answer differs from this message.')
                from .interview_queue import pending_answers
                pending=pending_answers(self,state,current.mock_interview_id)
                if not previous and (not pending or pending[0]['id']!=message['id']):
                    raise ValueError('Consume the earlier interview answer first, in arrival order (FIFO).')
                if not previous:
                    if current.current_question_id!=args['question_id']:raise ValueError('The interview question changed. Inspect it before sending this answer.')
                rt.mock_interviewer.answer_deferred(current.mock_interview_id,message['text'],
                    question_id=args['question_id'],idempotency_key=answer_key,
                    dispatch=lambda:self.interview_inputs.start_feedback(task_id,current.mock_interview_id))
                self.repository.change(task_id,lambda s:{'interview_answer_receipts':{
                    **s.get('interview_answer_receipts',{}),current.mock_interview_id:list(dict.fromkeys([
                        *s.get('interview_answer_receipts',{}).get(current.mock_interview_id,[]),message['id']]))}})
            else:
                if args['action'] not in {'skip','end','resume'}:raise ValueError('Unsupported interview control.')
                if args['action']=='end':
                    from .interview_queue import pending_answers
                    if pending_answers(self,state,current.mock_interview_id,before_message_id=message['id']):
                        raise ValueError('Save earlier pending interview answers before ending the interview.')
                with rt.mock_interviewer.transition_lock(current.mock_interview_id):
                    current=rt.mock_interviewer.interviews.get(current.mock_interview_id)
                    if args['action']=='resume' and rt.mock_interviewer.interviews.feedback_jobs(current.mock_interview_id):
                        self.interview_inputs.retry(task_id,current.mock_interview_id)
                        if current.status.value=='planning':rt.mock_interviewer._ask(current)
                    else:getattr(rt.mock_interviewer,args['action'])(current.mock_interview_id,expected_version=current.version,idempotency_key=key)
            return self.interview(current.mock_interview_id)
        raise ValueError('Unknown career capability.')

    def screen(self, task_id, state, jobs, query):
        from eregion.runtime.career.screening import screen_jobs
        from eregion.runtime.fast_decisions import FastDecisions, configured_decisions
        if not isinstance(query, str) or not 0 < len(query) <= 3000:
            raise ValueError('Use a concise search preference query.')
        revision = state.get('chat_revision', 0)
        service = configured_decisions()
        # An auxiliary budget per user turn prevents a cheap tool becoming an
        # unlimited provider loop. It does not consume the main reasoning budget.
        allowed = [False]
        def reserve(current):
            count = current.get('fast_screen_calls', 0) if current.get('fast_screen_revision') == revision else 0
            if (current.get('chat_revision', 0) != revision or current.get('pause_requested')
                    or current.get('status') in {'completed','cancelled'} or count >= 20):
                return {}
            allowed[0] = True
            return {'fast_screen_revision': revision, 'fast_screen_calls': count + 1}
        if service.enabled:
            self.repository.change(task_id, reserve)
            if not allowed[0]:
                service = FastDecisions()
        result = screen_jobs(jobs, query, decisions=service)
        latest = self.repository.get(task_id)
        if latest.get('chat_revision', 0) != revision or latest.get('pause_requested'):
            return {'stale': True, 'next': 'Read the latest user request before screening again.'}
        self.repository.change(task_id, lambda s: {'fast_decisions': [
            *s.get('fast_decisions', []), {'purpose':'job_screening', **result['fast_decision']}][-30:]})
        return result

    @staticmethod
    def application_instruction(state):
        latest = next((m['text'] for m in reversed(state.get('messages', [])) if m.get('role') == 'user'), '')
        # Preserve the entire 4,000-character user message. Appending boilerplate
        # would exceed the worker contract, while slicing could sever a negation.
        return latest or APPLICATION_INSTRUCTION

    def open_application_url(self,state,args,key):
        from eregion.runtime.auto_apply.contracts import posting
        if 'urls' in args:
            if 'url' in args:raise ValueError('Supply url or urls, not both.')
            urls=args['urls']
            if not isinstance(urls,list) or not 1<=len(urls)<=4:raise ValueError('Supply one to four application URLs.')
            unique={posting(url)[1]:url for url in urls}
            before={r['run_id'] for r in self.applications.repository.list()}
            results=[]
            for index,url in enumerate(unique.values()):
                try:
                    run=self.open_application_url(self.repository.get(state['task_id']),{'url':url},f'{key}:{index}')
                    results.append({'url':url,'started':run['run_id'] not in before,'application':run})
                    before.add(run['run_id'])
                except (ValueError,ApplyConflict) as exc:
                    results.append({'url':url,'started':False,'error':str(exc)})
            return {'requested_count':len(unique),'started_count':sum(r['started'] for r in results),
                'opened_or_reused_count':len({r['application']['run_id'] for r in results if 'application' in r}),
                'resolved_request_count':sum('application' in r for r in results),'results':results,
                'detail_hint':'These are actual durable attempts. A failed entry did not start; awaiting_review is not submitted.'}
        if not args.get('url'):raise ValueError('Supply url or urls.')
        saved,_ = self.candidate()
        if not saved.get('resume_base64'):raise ValueError('Upload a resume PDF before opening this application.')
        request = StartApplication(url=args['url'],profile=saved['profile'],resume_base64=saved['resume_base64'],
            idempotency_key=key,save_as_default=False,auto_submit=False,automation_mode='hybrid',
            consent_policy='ask',visual_fallback=state['visual_fallback'],initial_message=self.application_instruction(state))
        try:run=self.applications.start(request,material_refs={},origin_task_id=state['task_id'])
        except DuplicateApplication as exc:
            run=self.applications.repository.get(exc.run_id)
            run=self.require_application_owner(state,run,adopt=True)
            if run['status'] in {'cancelled','failed'}:run=self.applications.resume(run['run_id'],run['version'])
        self.repository.link(state['task_id'],'application_runs',run['run_id'],active_application_run=run['run_id'],active_browser='application')
        refs=run.get('material_refs',{})
        application_id=next((a for a in state['jobs'] if self.runtime.workspace.application_job(a).canonical_url
            and posting(self.runtime.workspace.application_job(a).canonical_url)[1]==posting(run['url'])[1]),None)
        if refs:
            application_id=self.runtime.packs.get(next(iter(refs.values()))['pack_id']).application_id
            self.repository.link(state['task_id'],'jobs',application_id)
            for ref in refs.values():self.repository.link(state['task_id'],'packs',ref['pack_id'])
        self.repository.change(state['task_id'],lambda s:{'application_links':{**s.get('application_links',{}),
            run['run_id']:{'application_id':application_id,'materials':refs}}})
        latest=self.repository.get(state['task_id'])
        if latest['status']=='cancelled' and run['status'] not in {'submitted','submitting','submission_unknown','cancelled','failed'}:
            self.applications.cancel(run['run_id'],self.applications.repository.get(run['run_id'])['version'])
        elif latest.get('pause_requested'):self.control(latest,'pause')
        return {**self.application(run['run_id']),'note':'No fit score or job description was invented. This starts only the browser application.'}

    def material_selections_for(self, state, application_id):
        scoped = state.get('material_selections_by_application', {})
        if application_id in scoped:
            return {kind:choice for kind,choice in scoped[application_id].items()
                    if kind in {'tailored_resume','cover_letter'}}
        result = {}
        # Migrate old conversations by the immutable pack->application relationship.
        for kind, choice in state.get('material_selections', {}).items():
            if kind not in {'tailored_resume','cover_letter'} or not choice: continue
            try:pack = self.runtime.packs.get(choice['pack_id'])
            except (KeyError, LookupError):
                raise ValueError('The explicitly selected document is unavailable. Review/select the intended current version before upload.') from None
            if pack.application_id == application_id: result[kind] = choice
        return result

    def start_application(self,state,args,key):
        if 'application_ids' in args:
            if args.get('application_id') or args.get('pack_id'):raise ValueError('Use application_ids without a single application_id or shared pack_id. Each job uses its own reviewed materials.')
            ids=args['application_ids']
            if not isinstance(ids,list) or not 1<=len(ids)<=4:raise ValueError('Supply one to four saved job IDs.')
            ids=list(dict.fromkeys(ids))
            for app_id in ids:self.runtime.workspace.get_application(app_id)
            before={r['run_id'] for r in self.applications.repository.list()}
            results=[]
            for index,app_id in enumerate(ids):
                try:
                    run=self.start_application(self.repository.get(state['task_id']),
                        {'application_id':app_id,'resume_source':args.get('resume_source','auto')},f'{key}:{index}')
                    results.append({'application_id':app_id,'started':run['run_id'] not in before,'application':run})
                    before.add(run['run_id'])
                except (ValueError,ApplyConflict) as exc:results.append({'application_id':app_id,'started':False,'error':str(exc)})
            return {'requested_count':len(ids),'started_count':sum(r['started'] for r in results),
                'opened_or_reused_count':len({r['application']['run_id'] for r in results if 'application' in r}),
                'resolved_request_count':sum('application' in r for r in results),'results':results}
        if not args.get('application_id'):raise ValueError('Supply application_id or application_ids.')
        rt = self.runtime
        app = rt.workspace.get_application(args['application_id'])
        job = rt.workspace.application_job(app.application_id)
        if not job.canonical_url:raise ValueError('This saved job needs a public application URL.')
        saved,_ = self.candidate()
        encoded, cover = saved.get('resume_base64'),None
        material_refs = {}
        source = args.get('resume_source','auto')
        if source not in {'auto','original','tailored'}:raise ValueError('Choose auto, original or tailored materials.')
        if source=='original' and args.get('pack_id'):raise ValueError('Choose a pack or the original resume, not both.')
        from eregion.runtime.application_pack.selection import latest_materials
        latest = latest_materials(rt.workspace, rt.packs, rt.pack_workflow, app.application_id)
        selected = []
        choices = self.material_selections_for(state, app.application_id)
        if args.get('pack_id'):
            pack = rt.pack_workflow.refresh_staleness(args['pack_id'])
            if pack.application_id!=app.application_id or pack.snapshot_id!=app.current_snapshot_id or pack.status.value=='stale':
                raise ValueError('Use current materials for this exact job.')
            selected = [(pack, item) for item in rt.packs.items(pack.pack_id)]
            if choices:
                from eregion.runtime.application_pack.selection import current_materials, reviewed_selections
                pinned = reviewed_selections(latest,
                    current_materials(rt.workspace, rt.packs, rt.pack_workflow, app.application_id),
                    choices)
                for kind, choice in choices.items():
                    if not choice: continue
                    expected = next(((p.pack_id, i.pack_item_id, i.version) for p, i in pinned if i.artifact_type == kind), None)
                    actual = next(((p.pack_id, i.pack_item_id, i.version) for p, i in selected if i.artifact_type == kind), None)
                    if actual is None and expected is not None:
                        # A resume-only pack is compatible with a cover explicitly
                        # selected from another pack. Never infer an older choice.
                        selected.append(next((p,i) for p,i in pinned if i.artifact_type == kind))
                    elif actual != expected:
                        raise ValueError('This pack conflicts with the explicitly selected document version. Omit pack_id to use the reviewed per-document choices, or review a different version before upload.')
        elif source != 'original':
            from eregion.runtime.application_pack.selection import current_materials, reviewed_selections
            selected = reviewed_selections(latest,
                current_materials(rt.workspace, rt.packs, rt.pack_workflow, app.application_id),
                choices)
        if source == 'tailored' and not any(i.artifact_type == 'tailored_resume' for p, i in selected):
            raise ValueError('Generate and review this job\'s tailored resume first.')
        if selected:
            for pack, item in selected:
                self.repository.link(state['task_id'], 'packs', pack.pack_id)
                if item.artifact_type not in {'tailored_resume','cover_letter'}:continue
                if item.status.value!='approved' or not item.artifact_id:
                    raise ValueError('The generated documents need review in this chat before being used for an application.')
                newest = next((i for p, i in latest if (i.artifact_type, i.source_question) == (item.artifact_type, item.source_question)), item)
                approval = choices.get(item.artifact_type, {})
                if newest.pack_item_id != item.pack_item_id and approval != {
                        'pack_id': pack.pack_id, 'item_id': item.pack_item_id, 'version': item.version,
                        'superseding_item_id': newest.pack_item_id, 'superseding_version': newest.version}:
                    raise ValueError('A newer material attempt exists. The older successful revision remains available; explicitly review/select it in this conversation before upload.')
                content = rt.packs.artifact(pack.pack_id,item.pack_item_id)
                if item.artifact_type=='tailored_resume':encoded = base64.b64encode(render_tailored_resume_pdf(content)).decode()
                else:cover = render_cover_letter(content)
                material_refs[item.artifact_type] = {'pack_id':pack.pack_id,'item_id':item.pack_item_id,'version':item.version}
        if not encoded:raise ValueError('Upload the original resume PDF, or approve a tailored resume first.')
        request = StartApplication(url=job.canonical_url,profile=saved['profile'],resume_base64=encoded,
            idempotency_key=key,automation_mode='hybrid',auto_submit=False,consent_policy='ask',
            save_as_default=False,cover_letter=cover,visual_fallback=state['visual_fallback'],
            initial_message=self.application_instruction(state))
        try:run = self.applications.start(request,material_refs=material_refs,origin_task_id=state['task_id'])
        except DuplicateApplication as exc:
            run = self.applications.repository.get(exc.run_id)
            run = self.require_application_owner(state,run,adopt=True)
            if run['status'] in {'submitted','submitting','submission_unknown'}:
                # Never relabel an earlier submission as having used the new pack.
                self.repository.link(state['task_id'],'application_runs',run['run_id'],active_application_run=run['run_id'],active_browser='application')
                self.repository.change(state['task_id'],lambda s:{'application_links':{**s.get('application_links',{}),
                    run['run_id']:{'application_id':app.application_id,'materials':run.get('material_refs',{})}}})
                return {**self.application(run['run_id']),'note':'This job already has a submission. No new materials were uploaded.'}
            latest_user = next((m for m in reversed(state.get('messages', [])) if m.get('role') == 'user'), {})
            message_id = 'handoff:' + hashlib.sha256((state['task_id'] + ':' + latest_user.get('id', '')).encode()).hexdigest()[:48]
            instruction = self.application_instruction(state)
            previous = run.get('material_refs',{})
            # Pinned versions preserve identity across older non-deterministic PDF exports.
            same_resume = (previous.get('tailored_resume')==material_refs.get('tailored_resume')
                if material_refs.get('tailored_resume') else self.applications.repository.resume(run['run_id'])==base64.b64decode(encoded))
            if not same_resume or previous!=material_refs or run.get('cover_letter')!=cover or run['profile']!=request.profile.model_dump():
                run = self.applications.replace_materials(run['run_id'],base64.b64decode(encoded),cover,
                    profile=request.profile.model_dump(),material_refs=material_refs,
                    initial_message=instruction,message_id=message_id)
            elif run['status'] in {'cancelled','failed'}:
                run = self.applications.resume(run['run_id'],run['version'],initial_message=instruction,message_id=message_id)
            elif not any(m.get('id') == message_id for m in run.get('messages', [])):
                last_user = next((m for m in reversed(run.get('messages', [])) if m.get('role') == 'user'), {})
                if last_user.get('text') != instruction:
                    run = self.applications.chat(run['run_id'], ChatRequest(text=instruction,message_id=message_id))
        self.repository.link(state['task_id'],'application_runs',run['run_id'],active_application_run=run['run_id'],active_browser='application')
        self.repository.change(state['task_id'],lambda s:{'application_links':{**s.get('application_links',{}),
            run['run_id']:{'application_id':app.application_id,'materials':material_refs}}})
        latest = self.repository.get(state['task_id'])
        if latest['status']=='cancelled' and run['status'] not in {'submitted','submitting','submission_unknown','cancelled','failed'}:
            self.applications.cancel(run['run_id'],self.applications.repository.get(run['run_id'])['version'])
        elif latest.get('pause_requested'):
            self.control(latest,'pause')
        return self.application(run['run_id'])

    def sync_submissions(self,state):
        for run_id,link in state.get('application_links',{}).items():
            if not link.get('application_id'):continue
            child = self.applications.repository.get(run_id)
            if child['status']=='submitted' and child.get('confirmation'):
                from eregion.runtime.workspace.errors import StaleApplicationError
                try:
                    self.runtime.workspace.record_browser_submission(link['application_id'],run_id=run_id,
                        confirmation=child['confirmation'])
                except StaleApplicationError:
                    if not self.runtime.workspace.get_application(link['application_id']).applied_at:raise

    def wait_after_repeated_read(self, state, tool):
        # Preserve autonomous independent work. Park only after a repeated,
        # unchanged inspection while a linked capability is actually working.
        if tool not in self.batchable_tools | {'read_context', 'inspect_application', 'reply_application'}:
            return None
        for run_id in reversed(state.get('application_runs', [])):
            if self.applications.repository.get(run_id)['status'] in {'queued','running','submitting'}:
                return {'kind': 'application', 'id': run_id}
        return None

    def validate_wait(self,state,args):
        if args['kind']!='application':raise ValueError('Only browser applications currently run in the background; other tools return their result directly.')
        self.require_link(state,'application_runs',args['id'])

    def capability_finished(self,args):
        """Final application states never change again; waiting on them would never wake."""
        if args.get('kind')!='application':return False
        return self.applications.repository.get(args['id'])['status'] in {'submitted','submission_unknown','failed','cancelled'}

    def task_budget(self,task_id):
        """Used and remaining provider attempts / input tokens for the task (None when uncapped)."""
        from eregion.runtime.provider_usage import task_limits, UsageLedger
        limits=task_limits()
        if not task_id or not limits:return None
        used=UsageLedger(self.repository.sessions).summary(task_id=task_id)
        result={'provider_attempts_used':used['calls'],'input_tokens_used':used['budget_input_tokens']}
        if 'provider_attempts' in limits:result['provider_attempts_left']=max(0,limits['provider_attempts']-used['calls'])
        if 'input_tokens' in limits:result['input_tokens_left']=max(0,limits['input_tokens']-used['budget_input_tokens'])
        return result

    def capability_stopped(self,args):
        """An application paused for input never wakes a wait by itself (Veeva pilot: a
        model-timeout pause left the task waiting while the user's "continue" went nowhere)."""
        if args.get('kind')!='application':return None
        child=self.applications.repository.get(args['id'])
        if child['status'] in {'queued','running','submitting'} or self.capability_finished(args):return None
        view=self.application(args['id'])
        result={'changed':True,'status':child['status'],'message':child.get('message'),'agent_reason':child.get('agent_reason'),
            'blocking_fields':view.get('blocking_fields',[]),'input_request':child.get('input_request'),
            'budget':self.task_budget(child.get('origin_task_id'))}
        if child.get('input_request'):
            result['next']=("The application asks the user the input_request question. Ask the user unless their latest "
                            "message already answers it; then forward that message with reply_application.")
        elif child.get('agent_reason') in self.LIMIT_REASONS:
            # Adobe re-test: after a page-budget stop the career agent spent 10 calls re-inspecting
            # the full field list and searching jobs, and never told the user what was blocking.
            result['next']=("The application stopped on a limit and needs the user's decision. Tell the user what blocks "
                            "it (message, blocking_fields) and the remaining budget, and ask whether to continue. Do not inspect it again, search "
                            "jobs or open the posting unless the user asks; if you must investigate, say which "
                            "uncertainty you are resolving.")
        else:
            result['next']=("The application is stopped, not running. Forward the user's latest message with "
                            'reply_application to resume it, or ask the user for what it needs.')
        return result

    LIMIT_REASONS = {'page_budget','application_budget','agent_limit','no_progress'}

    def hold_wait(self,state):
        """An application stopped on a question or account form that it already shows the user
        (with its own answer form). Re-asking it cost three parent calls per question (Intel-32)."""
        wait = state.get('wait') or {}
        if wait.get('kind')!='application':return False
        child = self.applications.repository.get(wait['id'])
        return (child['status']=='needs_input' and not child.get('pause_requested') and not child.get('paused_for_control')
                and bool(child.get('input_request') or child.get('account_request'))
                and child.get('agent_reason') not in self.LIMIT_REASONS)

    def route_application_reply(self,state,message):
        """Answers sent with the application's own form were delivered already; wait for it."""
        run_id = (message.get('context') or {}).get('delivered_to_application')
        if not run_id or run_id not in state.get('application_runs',[]):return None
        if self.applications.repository.get(run_id)['status'] not in {'queued','running'}:return None
        return {'tool':'reply_application','result':{'run_id':run_id,'reply_delivered':True,'direct':True},
                'wait':{'kind':'application','id':run_id}}

    def wake(self,state):
        wait = state.get('wait') or {}
        if wait.get('kind')=='application':
            child = self.applications.repository.get(wait['id'])
            if child['status'] in {'submitted','submission_unknown','failed','cancelled'}:
                return True  # Final: its token will never change, so release the wait now.
            return child['status'] not in {'queued','running','submitting'} and self.wait_token(wait)!=wait.get('token')
        return False

    def wait_token(self,args):
        child = self.applications.repository.get(args['id'])
        account = child.get('account_request') or {}
        return [child['status'],child.get('chat_revision'),(child.get('input_request') or {}).get('id'),child.get('review_digest'),
            [account.get(k) for k in ('origin','mode','stage')],
            [c['key'] for c in account.get('consents',[])],
            child.get('message') if child['status']=='needs_input' else None]

    def affordable_scoring(self,task_id,identifiers):
        """Start only a scoring batch the task's remaining attempts can finish.

        A Jev batch costs about one extraction per job plus shared stage requests; the
        main-model route about five requests per job. A batch cut off midway spends
        its extraction requests and yields no score, so the batch is sized first.
        """
        from eregion.runtime.provider_usage import task_limits, UsageLedger, REPLY_RESERVE, ReplyBudgetReserved
        cap=task_limits().get('provider_attempts')
        if not cap or cap<=2*REPLY_RESERVE:return identifiers,[]
        room=cap-REPLY_RESERVE-UsageLedger(self.repository.sessions).summary(task_id=task_id)['calls']
        fits=room-JEV_STAGE_REQUESTS if self.analysis.uses_jev() else room//MAIN_ROUTE_REQUESTS_PER_JOB
        if fits<1:raise ReplyBudgetReserved('reply_reserve')
        return identifiers[:fits],identifiers[fits:]

    def validate_finish(self,state):
        for run_id in state['application_runs']:
            child = self.applications.repository.get(run_id)
            if child['status'] in {'queued','running','submitting'}:raise ValueError('A linked application is still working. Wait for its result or continue independent work.')
        from .search_recovery import recovery
        pending=recovery(state)
        remaining=state.get('model_budget',60)-state.get('model_calls',0)+state.get('budget_base',0)
        # The task provider-attempt cap is shared with tool work; when its reply
        # reserve is reached no candidate can be read or scored anyway.
        from eregion.runtime.provider_usage import task_limits, UsageLedger, REPLY_RESERVE
        cap=task_limits().get('provider_attempts')
        if cap and cap>2*REPLY_RESERVE and state.get('task_id'):
            remaining=min(remaining,cap-UsageLedger(self.repository.sessions).summary(task_id=state['task_id'])['calls'])
        if (pending and pending['requested_count'] and pending['recommended_count']<pending['requested_count']
                and pending['pending_candidates'] and remaining>3):
            raise ValueError('The requested search is incomplete and observed posting links remain unread with '
                f'{remaining} decisions available. Open a different pending candidate, verify and score it if eligible '
                '(a shortfall is reported honestly; never recommend a mandatory-gap job to close it). '
                'Do not claim the budget is exhausted. Pending links: '+str(pending['pending_candidates'][:3]))

    def validate_stop(self,state,reason):
        """Structure only: a turn that searched records a short reason for stopping."""
        from .search_recovery import search_request
        searched=any(h.get('tool')=='search_jobs' and h.get('chat_revision',state.get('chat_revision',0))==state.get('chat_revision',0)
                     for h in state.get('history',[]))
        if search_request(state) and searched and not (isinstance(reason,str) and reason.strip()):
            raise ValueError('Give stop_reason: one short sentence on why searching stops now, from search_coverage, '
                             'candidate quality, failures and budget_remaining.')

    def validate_completion(self,state,text):
        from .source_health import validate_coverage
        validate_coverage(state,text)
        from .presentation import validate_final_language
        validate_final_language(state,text)
        from .search_recovery import search_request
        from .reply_evidence import reply_job_evidence
        if not search_request(state):return
        observed=list(state.get('scored_job_evidence',{}).values())
        if self is not None:
            observed=[self.job_summary(identifier) for identifier in state.get('jobs',[]) if identifier in state.get('analyses',{})]
        if state.get('analyses') and not observed and not re.search(r'https://[^\s]+',text):
            raise ValueError('Final read/scored search results must include the observed job URL and score, even when no recommendation was saved.')
        claimed=re.search(r'\b(\d+)\s+(?:verified\s+match(?:es)?|recommendations?|recommended\s+(?:jobs|roles))\b',text,re.I)
        if claimed and int(claimed[1])>len(state.get('recommendations',[])):
            raise ValueError('The final verified match/recommendation count exceeds persisted recommendations. Use recommend_job for a verified eligible scored vacancy; otherwise label it as only read/scored and disclose that it is not recommended.')
        urls=set()
        for url in re.findall(r'https://[^\s<>"\]]+',text):
            try:urls.add(job_key(url.rstrip('.,;!?)')))
            except ValueError:continue
        evidence={job_key(r['url']):r for r in [*observed,*state.get('recommendations',[])] if r.get('url')}
        missing=[r['url'] for r in evidence.values() if job_key(r['url']) not in urls]
        if missing:raise ValueError('Final search results must include each saved recommendation URL and its observed score, with risks '
            '(scored jobs that were excluded or need evidence are listed too, marked as such). Missing URLs: '+str(missing))
        # The reply uses the same tier contract as the saved records (labels only;
        # whether a tier is right is the model's and the audit's judgment).
        # A label matches whatever punctuation or spacing separates its words
        # ('Worth trying - known gaps'); replay tiered-v3-01 lost two rounds to a dash.
        def label_pattern(label):
            return re.compile(r'[^0-9a-z]+'.join(re.escape(w) for w in re.findall(r'[0-9a-z]+',label.casefold())),re.I)
        absent=[TIERS[t] for t in dict.fromkeys(r.get('tier') for r in state.get('recommendations',[]))
                if t in TIERS and not label_pattern(TIERS[t]).search(text)]
        if absent:raise ValueError('Group the recommendations under their tier labels with counts; missing labels: '
                                   +', '.join(absent)+'. '+FINAL_FORMAT)
        # Structure only: each saved recommendation appears under its own saved tier label
        # (acceptance tiered-v2-01 listed a to_confirm job under Priority match).
        # Only a heading-shaped line counts ('### Worth trying (known gaps) (2)'); the same words
        # inside an entry ('- Needs your confirmation: GPA ...') are not a section (tiered-v3-04).
        def heading(match):
            line_start=text.rfind('\n',0,match.start())+1
            line_end=text.find('\n',match.end());line_end=len(text) if line_end<0 else line_end
            before=text[line_start:match.start()];rest=re.sub(r'\([^)]*\)','',text[match.end():line_end])
            return not re.search(r'[0-9A-Za-z]',before.replace('#','')) and not re.search(r'[A-Za-z:]',rest)
        labels=sorted((m.start(),tier) for tier,label in TIERS.items() for m in label_pattern(label).finditer(text) if heading(m))
        placed={}
        for match in re.finditer(r'https://[^\s<>"\]]+',text):
            try:key=job_key(match[0].rstrip('.,;!?)'))
            except ValueError:continue
            under=next((tier for start,tier in reversed(labels) if start<match.start()),None)
            placed.setdefault(key,set()).add(under)
        misplaced=[f"{r['url']} (saved as {TIERS[r['tier']]})" for r in state.get('recommendations',[])
                   if r.get('url') and r.get('tier') in TIERS and r['tier'] not in placed.get(job_key(r['url']),set())]
        if misplaced:raise ValueError('List each recommendation under its saved tier label; misplaced: '
                                      +'; '.join(misplaced)+'. '+FINAL_FORMAT)
        scores={}
        for url,values in reply_job_evidence(text).items():
            entry=scores.setdefault(job_key(url),{'scores':[],'ranges':[]})
            for kind in entry:entry[kind].extend(values[kind])
        for recommendation in evidence.values():
            values=scores.get(job_key(recommendation['url']),{'scores':[],'ranges':[]})
            expected=recommendation.get('match_score')
            if expected is None:
                interval=recommendation.get('score_range')
                valid_interval=(isinstance(interval,list) and len(interval)==2
                    and all(type(v) in (int,float) and 0<=v<=100 for v in interval) and interval[0]<=interval[1])
                if not valid_interval or values['scores'] or not values['ranges'] or any(r!=interval for r in values['ranges']):
                    raise ValueError('Final score interval is missing or differs from the saved observed interval '
                        f'for {recommendation["url"]}: expected {interval}. Keep unresolved qualifications explicit; do not invent a point score.')
            elif (type(expected) not in (int,float) or not values['scores'] or values['ranges']
                    or any(value!=expected for value in values['scores'])):
                raise ValueError('Final recommendation score is missing or differs from the saved observed score '
                    f'for {recommendation["url"]}: expected {expected}. Label it explicitly in that job entry, e.g. '
                    f'"Score {expected}" (a bare bolded number is not read as a score); salary/age/ID numbers are not scores.')

    def validate_reply(self,state,text):
        """Fence common plural completion claims against the request's actual URLs."""
        from eregion.runtime.auto_apply.contracts import posting
        latest=next((m['text'] for m in reversed(state.get('messages',[])) if m.get('role')=='user'),'')
        requested=set()
        for url in re.findall(r'https://[^\s<>"\]]+',latest):
            try:requested.add(posting(url.rstrip('.,;!?)'))[1])
            except ValueError:continue
        if len(requested)<2:return
        runs=[self.applications.repository.get(r) for r in state['application_runs']]
        states={posting(r['url'])[1]:r['status'] for r in runs}
        allowed={'ready':{'awaiting_review'},'submitted':{'submitted'},
            'opened':{'running','needs_input','awaiting_review','submitting','submitted','submission_unknown'},
            'started':{'queued','running','needs_input','awaiting_review','submitting','submitted','submission_unknown'}}
        aliases={'launched':'started','打开':'opened','开始':'started','提交':'submitted','准备':'ready'}
        # A PDF failure elsewhere in the reply cannot negate an application claim.
        # Only a negation attached to this quantified clause/action is exempt.
        for clause in re.split(r'[.!?;\n。！？；]|\b(?:but|however|although)\b|但是|不过',text,flags=re.I):
            plural=re.search(r'\b(?:both|all)\b|两个|两份|全部',clause,re.I)
            if not plural:continue
            if re.search(r'\b(?:not\s+(?:both|all)|only one|neither)\b|不是全部|并非全部|只有一个',clause,re.I):continue
            for action in re.finditer(r'\b(?:opened|started|launched|submitted|ready)\b|打开|开始|提交|准备',clause,re.I):
                prefix=clause[:action.start()]
                if re.search(r'\b(?:not|never|cannot|can.t|couldn.t|unable to)\s+(?:\w+\s+){0,3}$|(?:没有|未能|尚未|未|不).{0,5}$',prefix,re.I):continue
                kind=aliases.get(action.group().casefold(),action.group().casefold())
                valid={identity for identity,status in states.items() if status in allowed[kind]}
                if not requested<=valid:
                    raise ValueError(f'Only {len(requested & valid)} of {len(requested)} requested applications have a confirmed {kind} state. Report each actual state; unrelated failures do not exempt this claim.')

    def control(self,state,action):
        if action not in {'pause','resume'}:return
        for run_id in state['application_runs']:
            child = self.applications.repository.get(run_id)
            try:self.require_application_owner(state,child)
            except ValueError:continue
            if child['status'] in {'submitted','submission_unknown','cancelled','failed','submitting'}:continue
            if action=='resume' and not child.get('pause_requested'):continue
            self.applications.chat(run_id,ChatRequest(text='pause' if action=='pause' else 'continue',
                message_id=str(uuid4()),visual_fallback=state['visual_fallback']))

    def owner_active(self,task_id):
        """An owner that was deleted, cancelled or moved to Trash no longer holds the run."""
        try:
            owner,meta=self.repository.get(task_id),self.repository.metadata(task_id)
        except LookupError:
            return False
        return owner['status']!='cancelled' and not meta.get('trashed_at')

    def require_application_owner(self,state,run,*,adopt=False):
        """Refuse cross-conversation writes; optionally adopt a run whose owner is gone.

        Adoption is only offered when a conversation explicitly reopens the same
        job, and never while the abandoned run is still executing.
        """
        owner=run.get('origin_task_id')
        if owner and owner!=state['task_id']:
            if self.owner_active(owner):
                raise ValueError('This application is already owned by another conversation. Continue it in that conversation; no documents or browser state were changed.')
            return self.adopt_application(state,run,adopt)
        if not owner:
            linked=[s['task_id'] for s in self.repository.list() if run['run_id'] in s.get('application_runs',[])]
            others=[task for task in linked if task!=state['task_id'] and self.owner_active(task)]
            if others:
                raise ValueError('This older application is linked to another conversation. Its execution ownership is ambiguous; no documents or browser state were changed.')
            if any(task!=state['task_id'] for task in linked):
                return self.adopt_application(state,run,adopt)
        return run

    def adopt_application(self,state,run,adopt):
        if not adopt:
            raise ValueError('This application belonged to a conversation that was deleted or cancelled. Reopen the job from this conversation to take it over.')
        if run['status'] in {'queued','running','submitting'}:
            raise ValueError('The earlier attempt for this job is still finishing. Wait a moment, then reopen the job here.')
        return self.applications.repository.save(run,origin_task_id=state['task_id'])
