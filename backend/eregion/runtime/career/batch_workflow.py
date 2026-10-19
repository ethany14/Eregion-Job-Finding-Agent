"""Bounded sequential acquisition + existing shared scorer, inside the task owner."""
import httpx
from eregion.runtime.tasks.loop import function, TEXT
from eregion.runtime.core.engine import LoopStop
from eregion.runtime.provider_usage import usage_scope, TaskProviderBudgetExceeded
from .alerts import job_key
from .qualification import record_many,request_hash,body_hash
from .linkedin_source import LinkedInSourceStopped

TOOL=function('verify_and_score_jobs',
    'Process 1-20 observed posting URLs together: read/reuse, qualify against the user request, import qualified jobs, '
    'then score qualified jobs in shared groups of at most sixteen. Unknown/mismatched jobs stay deferred without paid scoring. '
    'Never recommends; decide the returned scored jobs separately. Prefer this for multi-job searches.',
    {'urls':{'type':'array','minItems':1,'maxItems':20,'uniqueItems':True,'items':TEXT}})


def processed(state):
    """Postings finished for the current request: judged a clear mismatch, scored, or dismissed."""
    from .candidate_health import active_rejections
    fingerprint=request_hash(state)
    bodies={job_key(j['url']):j for j in state.get('retrieved_jobs',{}).values() if j.get('url')}
    scored={job_key(e['url']) for e in (state.get('scored_job_evidence') or {}).values() if e.get('url')}
    done={}
    for key,row in state.get('candidate_qualifications',{}).items():
        if row.get('request_hash')!=fingerprint or key not in bodies or row.get('body_hash')!=body_hash(bodies[key]):continue
        if not row['qualified']:done[key]={'outcome':'not_shown','unmet':[f for f,c in row['checks'].items() if c['status']=='unmet']}
        elif key in scored:done[key]={'outcome':'scored'}
    for r in active_rejections(state):done[job_key(r['url'])]={'outcome':'dismissed'}
    return done


def run(harness,task_id,args,call_id):
    repo=harness.repository;initial=repo.get(task_id);revision=initial['chat_revision']
    urls=args['urls'];identities=[job_key(u) for u in urls]
    if not 1<=len(urls)<=20 or len(set(identities))!=len(urls):raise ValueError('Choose 1-20 distinct observed postings.')
    observed={job_key(j['url']) for kind in ('search_candidates','observed_job_candidates','retrieved_jobs')
              for j in initial.get(kind,{}).values() if j.get('url')}
    if any(key not in observed for key in identities):raise ValueError('Use URLs from observed search results or saved retrieved postings.')
    # Postings already finished for this request are skipped and reported, not redone: a thin
    # market used to end at the no-progress guard after the model resubmitted them (live-set-02).
    done=processed(initial)
    skipped=[{'url':u,**done[job_key(u)]} for u in urls if job_key(u) in done]
    if skipped:
        urls=[u for u in urls if job_key(u) not in done];identities=[job_key(u) for u in urls]
    if not urls:
        return {'jobs':[],'candidates':[],'qualified_count':0,'already_processed':skipped,'no_new_evidence':True,
                'next':'All of these postings were already processed for this request; do not resubmit them. '
                       'Process candidate_queue.next_unread, search for different postings, or finish with the saved results.'}
    key=harness.key(initial,'verify_and_score_jobs',{'urls':sorted(identities)})
    journal=dict(initial.get('batch_verifications',{}).get(key,{}));items=[];apps=[];changed=False;sequence=0
    def check():
        s=repo.get(task_id)
        if s['chat_revision']!=revision or s.get('pause_requested') or s['status'] in {'cancelled','completed'}:
            raise LoopStop('steered')
    def save(**kwargs):
        check();journal.update(kwargs,items=list(items))
        repo.change(task_id,lambda s:{'batch_verifications':{**s.get('batch_verifications',{}),key:dict(journal)}})
    def invoke(name,arguments):
        nonlocal sequence
        sequence+=1
        check();save(pending=name)
        value=harness.execute(task_id,name,arguments,call_id=f'{call_id}:{name}:{sequence}')
        check();return value
    try:
        with usage_scope(provider_guard=check):
            ready=[];ready_items=[]
            for url in urls:
                check();state=repo.get(task_id)
                job=next((j for j in state.get('retrieved_jobs',{}).values() if j.get('url') and job_key(j['url'])==job_key(url)),None)
                try:
                    new_read=job is None
                    job=job or invoke('read_job',{'url':url})
                    if job.get('error'):items.append({'url':url,'status':'read_failed','error':job['error']});continue
                    from .candidate_health import validate_and_record
                    validate_and_record(repo,task_id,job)
                    if new_read:changed=True
                    ready.append(job)
                    item={'url':url,'title':job.get('title'),'status':'read'}
                    items.append(item);ready_items.append(item)
                    save(pending=None)
                except (ValueError,LookupError,httpx.HTTPError) as exc:
                    # One posting's source failure (e.g. a board API 404) stays with that
                    # posting; it used to fail the whole batch (tiered-v2 acceptance 01).
                    items.append({'url':url,'status':'source_error','error':(str(exc) or type(exc).__name__)[:400]});save(pending=None)
                except LinkedInSourceStopped as exc:
                    # The source (e.g. its call budget) stopped: keep processing postings
                    # already read instead of discarding the whole batch.
                    items.append({'url':url,'status':'source_stopped','error':exc.reason});save(pending=None)
            check();save(pending='qualify_jobs')
            qualifications=record_many(repo,task_id,ready)
            for job,qualification,item in zip(ready,qualifications,ready_items):
                check();url=job['url']
                previous=initial.get('candidate_qualifications',{}).get(job_key(url),{})
                if any(previous.get(k)!=qualification.get(k) for k in
                       ('request_hash','body_hash','checks','qualified')):changed=True
                item.update(qualification=qualification,status='needs_evidence')
                if not qualification['qualified']:save(pending=None);continue
                try:
                    imported=invoke('import_job',{'url':url})
                    if imported.get('error'):item.update(status='import_failed',error=imported['error']);continue
                    apps.append(imported['application_id'])
                    if apps[-1] not in initial.get('jobs',[]):changed=True
                    item.update(application_id=apps[-1],status='imported')
                    save(pending=None)
                except (ValueError,LookupError) as exc:
                    item.update(status='import_failed',error=str(exc)[:400]);save(pending=None)
            scored,unscored,interrupted,notes=[],[],[],[]
            for start in range(0,len(apps),16):
                group=apps[start:start+16]
                result=invoke('score_jobs',{'application_ids':group})
                if result.get('error'):
                    # Earlier groups' scores and every read, qualification and import are saved;
                    # this group stays imported but unscored (never a recommendation).
                    pending=[a for a in apps[start:] if a not in {j.get('application_id') for j in scored}]
                    save(pending=None,failure=result)
                    return {**result,'jobs':scored,'candidates':items,'unscored_for_budget':pending,
                            'budget_note':'Imported and qualified but not scored. Not recommendations; paid scoring '
                                          'decisions already made are cached and are not paid again on a retry.'}
                scored.extend(result['jobs'])
                unscored.extend(result.get('unscored_for_budget') or [])
                interrupted.extend(result.get('interrupted_chunk') or [])
                if result.get('budget_note'):notes.append(result['budget_note'])
                if not result.get('no_new_evidence'):changed=True
            save(pending=None,completed=True,failure=None)
            return {'jobs':scored,'candidates':items,'qualified_count':len(apps),
                    **({'already_processed':skipped} if skipped else {}),
                    **({'unscored_for_budget':unscored,'budget_note':notes[-1] if notes else 'Imported and qualified but not '
                        'scored: the remaining budget could not finish them. Not recommendations.'} if unscored else {}),
                    **({'interrupted_chunk':interrupted} if interrupted else {}),
                    'no_new_evidence':not changed,
                    'next':'Decide each scored job. Deferred jobs are not quota matches; discover different postings when no qualified pending candidates remain.'}
    except TaskProviderBudgetExceeded:
        save(failure='provider_budget');raise
