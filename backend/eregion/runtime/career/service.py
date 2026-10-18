"""One conversational surface over task reasoning and the existing browser UI contract."""
import logging
from threading import Event, Thread

from eregion.runtime.tasks.loop import TaskLoop
from eregion.runtime.auto_apply.contracts import ContinueApplication, ReplyQuestion
from .repository import TaskRepository, now
from .harness import CareerHarness
from .prompt import APPLICATION_INSTRUCTION

# A conversation in one of these states may still be touched by the task loop.
BUSY_STATUSES = {'queued','running','waiting','submitting'}
TRASH_SWEEP_SECONDS = 24*60*60


class CareerService:
    def __init__(self,runtime,applications,*,model_factory=None,harness=None,start_alert_scheduler=True,execution=None):
        self.runtime,self.applications = runtime,applications
        task_memory = getattr(runtime, 'task_memory', None)
        self.repository = TaskRepository(runtime.database.session_factory,
            task_memory=task_memory.repository if task_memory else None)
        self.harness = harness or CareerHarness(runtime,applications,self.repository)
        from .alerts import AlertRepository
        from .alert_scheduler import AlertScheduler
        self.alerts=AlertRepository(runtime.database.session_factory)
        self.harness.alerts=self.alerts
        # One process-wide owner for background work (unified loop M5); the
        # composition root supplies it, otherwise this service keeps a private one.
        from eregion.runtime.execution import ExecutionService
        execution = execution or getattr(runtime,'execution',None)
        self._owns_execution = execution is None
        self.execution = execution or ExecutionService()
        self.loop = TaskLoop(self.repository,self.harness,execution=self.execution,
            **({'model_factory':model_factory} if model_factory else {}))
        inputs=getattr(self.harness,'interview_inputs',None)
        if inputs:
            inputs.bind(self.execution)
            self.execution.register_kind('interview_inputs',max_workers=4,recover=inputs.recover)
            self.execution.recover('interview_inputs')
        self.alert_scheduler=AlertScheduler(self,start=start_alert_scheduler)
        if start_alert_scheduler:
            self.execution.register_resource('career-job-alerts','Timer that enqueues due scheduled searches as career tasks.')
        # Trash empties itself after 30 days: once at startup, then daily, without an external scheduler.
        self._sweeping = Event()
        self.repository.purge_expired_trash()
        Thread(target=self._sweep_trash,daemon=True,name='career-trash').start()
        self.execution.register_resource('career-trash','Daily purge of expired trash; no task ownership.')

    def _sweep_trash(self):
        while not self._sweeping.wait(TRASH_SWEEP_SECONDS):
            try:self.repository.purge_expired_trash()
            except Exception:continue  # A locked database is retried on the next sweep.

    def view(self,task_id):
        from eregion.runtime.auto_apply.public import public
        from eregion.runtime.provider_usage import UsageLedger
        state = self.repository.get(task_id)
        self.harness.sync_submissions(state)
        child = self.applications.repository.get(state['active_application_run']) if state.get('active_application_run') else None
        messages = {m['id']:m for m in state['messages']}
        for linked_id in state['application_runs']:
            linked = child if child and child['run_id']==linked_id else self.applications.repository.get(linked_id)
            for m in linked.get('messages',[]):
                # This opening instruction was supplied by the capability, not typed by the user.
                if m['role']=='user' and m['text'].startswith(APPLICATION_INSTRUCTION):continue
                messages.setdefault(m['id'],{**m,'application_run_id':linked_id})
        base = public(child) if child else {'fields':[],'review_fields':[],'actions':[],'pending_fields':[]}
        status = state['status']
        if child and child['status'] in {'needs_input','awaiting_review','submitting'} and not state.get('pause_requested') and state['status']!='cancelled':
            status = child['status']
        own_question = state.get('input_request')
        child_wait = bool(child and child['status']=='needs_input')
        if own_question and own_question.get('application_run_id'):
            linked_id=own_question['application_run_id']
            if self.harness.wait_token({'id':linked_id})!=own_question.get('application_wait_token'):
                own_question=None
                self.repository.change(task_id,input_request=None)
        account_wait = bool(base.get('account_request')) and status == 'needs_input'
        failure = state.get('error')
        if failure and not isinstance(failure,dict):
            failure = {'message':'The previous model request failed. Try again to get an updated result or error status.',
                       'stage':'model_request','code':'model_error','retryable':True}
        activity = state.get('model_activity') or {}
        tool_activity = state.get('tool_activity') or {}
        result = {**base,'run_id':task_id,'career':True,'title':state['title'],'goal':state['goal'],
            'usage_summary':UsageLedger(self.repository.sessions).summary(task_id=task_id, run_ids=state['application_runs']),
            'browser':state.get('browser'),
            'preview_url':f'/api/career/tasks/{task_id}/browser/preview' if state.get('browser') and (not child or state.get('active_browser')=='research') else
                f'/api/auto-apply/{child["run_id"]}/preview' if child else None,
            'status':status,'task_status':state['status'],'task_version':state['version'],
            'version':child['version'] if child else state['version'],
            'application_run_id':child['run_id'] if child else None,'application_status':child['status'] if child else None,
            'can_reopen':False,'pause_requested':state.get('pause_requested'),
            'messages':sorted(messages.values(),key=lambda m:m.get('at','')),
            'task_plan':state['plan'],'visual_fallback':state['visual_fallback'],
            'input_request':None if account_wait else base.get('input_request') or own_question,
            'task_error':failure,'model_activity':activity or None,
            'tool_activity':tool_activity or None,'timings':state.get('timings',[]),
            'message':base.get('message') if child_wait else (tool_activity.get('message') or activity.get('message') or 'Working toward your goal.') if state['status'] in {'queued','running'} else
                (failure or {}).get('message') or (own_question or {}).get('text') or base.get('message','Ready for your next message.'),
            'actions':sorted([*state['actions'],*base.get('actions',[])],key=lambda a:a.get('at',''))[-100:],
            'jobs':[self.harness.job_summary(a) for a in state['jobs']],
            'recommendations':state.get('recommendations',[]),
            'materials':[self.harness.materials(p) for p in state['packs']],
            'interviews':[self.harness.interview(i) for i in state['interviews']],
            'created_at':state['created_at'],'updated_at':state['updated_at']}
        meta = self.repository.metadata(task_id)
        result.update(custom_title=bool(meta['title']),title=meta['title'] or state['title'],group_id=meta['group_id'],
            pinned_at=meta['pinned_at'],archived_at=meta['archived_at'],trashed_at=meta['trashed_at'])
        return result

    def list(self,view='active'):
        states = self.repository.list_view(view)
        attempts = [self.view(s['task_id']) for s in states]
        result = {'attempts':attempts,'groups':self.repository.groups()}
        if view!='active':return result
        # Browser attempts started outside a conversation stay visible in the main list.
        linked = {r for s in self.repository.list() for r in s['application_runs']}
        from eregion.runtime.auto_apply.public import public
        result['attempts'] += [public(s) for s in self.applications.repository.list()
            if s['run_id'] not in linked and not self._created_by_conversation(s)]
        return result

    @staticmethod
    def _created_by_conversation(run):
        # Runs a conversation started keep that origin after the conversation is deleted.
        return bool(run.get('origin_task_id')) or any(m['role']=='user' and APPLICATION_INSTRUCTION in m['text']
            for m in run.get('messages',[])[:3])

    def organise(self,task_id,changes):
        """Rename, pin, group, archive or trash a conversation without touching its task state."""
        update = {}
        if 'title' in changes:
            title = (changes['title'] or '').strip()
            update['title'] = title[:120] or None
        if 'group_id' in changes:update['group_id'] = changes['group_id'] or None
        if 'pinned' in changes:update['pinned_at'] = now() if changes['pinned'] else None
        if 'archived' in changes:update['archived_at'] = now() if changes['archived'] else None
        if 'trashed' in changes:
            if changes['trashed']:
                view = self.view(task_id)
                if view['task_status'] in BUSY_STATUSES or view['status'] in BUSY_STATUSES:
                    raise ValueError('Stop this conversation before moving it to Trash.')
                update['trashed_at'] = now()
            else:update['trashed_at'] = None
        self.repository.update_meta(task_id,**update)
        return self.view(task_id)

    def delete(self,task_id):
        # Only the conversation goes; linked applications, materials and interviews are separate records.
        manager = getattr(self.harness,'_browsing',None)
        self.repository.delete(task_id)
        if manager:manager.close_task(task_id)
        return {'deleted':task_id}

    def empty_trash(self):
        return {'deleted':self.repository.purge_trash()}

    def start(self,text,key,visual_fallback=False,application_id=None):
        context = None
        if application_id:
            self.runtime.workspace.get_application(application_id)
            context = {'application_id':application_id}
        state = self.loop.start(text,key,visual_fallback=visual_fallback,context=context)
        return self.view(state['task_id'])

    def chat(self,task_id,text,message_id,visual_fallback=None,context=None):
        meta = self.repository.metadata(task_id)
        if meta['trashed_at']:raise ValueError('Restore this conversation from Trash before continuing it.')
        if meta['archived_at']:self.repository.update_meta(task_id,archived_at=None)
        if context:
            state=self.repository.get(task_id)
            for key,collection in {'application_id':'jobs','pack_id':'packs','interview_id':'interviews'}.items():
                if context.get(key):self.harness.require_link(state,collection,context[key])
            if context.get('item_id'):
                if not context.get('pack_id'):raise ValueError('Select the document\'s material pack.')
                self.runtime.packs.item(context['pack_id'],context['item_id'])
        self.loop.chat(task_id,text,message_id,visual_fallback=visual_fallback,context=context)
        return self.view(task_id)

    def application_action(self,task_id,action,body):
        state = self.repository.get(task_id)
        run_id = body.get('application_run_id')
        if not run_id or run_id!=state.get('active_application_run'):
            raise ValueError('The active browser application changed. Refresh before replying or confirming.')
        clean = {k:v for k,v in body.items() if k!='application_run_id'}
        self.harness.require_application_owner(state,self.applications.repository.get(run_id))
        if action=='reply':
            self.applications.reply_question(run_id,ReplyQuestion.model_validate(clean))
        elif action=='continue':
            request = ContinueApplication.model_validate(clean)
            if request.confirm_submit:
                refs = state.get('application_links',{}).get(run_id,{}).get('materials',{})
                if refs!=self.applications.repository.get(run_id).get('material_refs',{}):
                    raise ValueError('Another conversation changed this application\'s documents. Inspect and update the materials before submitting.')
                for ref in refs.values():
                    pack = self.runtime.pack_workflow.refresh_staleness(ref['pack_id'])
                    item = self.runtime.packs.item(ref['pack_id'],ref['item_id'])
                    if pack.status.value=='stale' or item.status.value!='approved' or item.version!=ref['version']:
                        raise ValueError('The materials changed after the browser was filled. Update this application before confirming submission.')
            self.applications.continue_run(run_id,request)
        else:raise ValueError('Unsupported application action.')
        # Invalidate a parent decision computed before this explicit UI action.
        child = self.applications.repository.get(run_id)
        latest_user = next((m for m in reversed(child['messages']) if m['role']=='user'),None)
        event_id = (latest_user['id'] if latest_user and ('answers' in clean or action=='reply')
            else f'application-action:{run_id}:{action}:{clean.get("expected_version",0)}')
        direct = latest_user and ('answers' in clean or action=='reply')
        self.chat(task_id,latest_user['text'] if direct
            else 'I confirmed submission using the review button.' if clean.get('confirm_submit') else 'Continue this application.',event_id,
            context={'delivered_to_application':run_id} if direct else None)
        return self.view(task_id)

    def set_browser_visible(self,task_id,visible,version):
        with self.loop.lock:
            state=self.repository.get(task_id)
            if state['version']!=version:raise ValueError('The task changed. Refresh before switching browser mode.')
            child_id=state.get('active_application_run')
            research=bool(state.get('browser')) and (not child_id or state.get('active_browser')=='research')
            if not research and not child_id:raise ValueError('No browser is open for this conversation yet.')
            if child_id and self.applications.repository.get(child_id)['status']=='submitting':
                raise ValueError('Wait for submission to finish before switching browser mode.')
            state=self.repository.change(task_id,lambda s:{'chat_revision':s['chat_revision']+1},
                status='needs_input',pause_requested=True,wait=None,model_activity=None)
            self.harness.control(state,'pause')
        if research:
            from eregion.runtime.auto_apply.browser import BrowserBlocked
            manager=getattr(self.harness,'_browsing',None)
            if not manager:raise ValueError('No research browser is open. Resume the task first.')
            revision=state['chat_revision']
            def paused():
                current=self.repository.get(task_id)
                return current.get('pause_requested') and current['chat_revision']==revision
            try:observed=manager.set_visible(task_id,visible,active=paused)
            except BrowserBlocked as exc:raise ValueError(str(exc)) from None
            self.repository.change(task_id,browser={k:observed.get(k) for k in ('url','heading','title','blocked','tabs','observation_id')})
        else:
            child=self.applications.repository.get(child_id)
            self.applications.set_browser_visible(child_id,visible,child['version'])
        return self.view(task_id)

    def browser_control(self, task_id, request):
        from eregion.runtime.auto_apply.browser import BrowserBlocked
        with self.loop.lock:
            state = self.repository.get(task_id)
            child_id = state.get('active_application_run')
            research = bool(state.get('browser')) and (not child_id or state.get('active_browser') == 'research')
            if state['status'] == 'cancelled':
                raise ValueError('This task was cancelled.')
            if not research and not child_id:
                raise ValueError('No browser is open for this conversation yet.')
            if child_id and self.applications.repository.get(child_id)['status'] == 'submitting':
                raise ValueError('Wait for submission to finish before taking control.')
            if request.action == 'start':
                if state['version'] != request.expected_version:
                    raise ValueError('The task changed. Refresh before taking control.')
                state = self.repository.change(task_id, lambda s: {'chat_revision': s['chat_revision']+1},
                    status='needs_input', pause_requested=True, wait=None, model_activity=None)
                self.harness.control(state, 'pause')
            if not state.get('pause_requested'):
                raise ValueError('Pause the agent before controlling its browser.')
            revision = state['chat_revision']
        def paused():
            current = self.repository.get(task_id)
            return (current.get('pause_requested') and current['chat_revision'] == revision
                    and current['status'] != 'cancelled')
        scope = f'career:{task_id}:{revision}'
        try:
            if research:
                manager = getattr(self.harness, '_browsing', None)
                if not manager:
                    raise ValueError('No research browser is open. Resume the task first.')
                return manager.browser_control(task_id, request, scope=scope, active=paused)
            if not paused():
                raise ValueError('The task changed. Take control again.')
            if request.action == 'start':
                request = request.model_copy(update={'expected_version': self.applications.repository.get(child_id)['version']})
            return self.applications.browser_control(child_id, request, parent_scope=scope)
        except (ValueError, BrowserBlocked) as exc:
            raise ValueError(str(exc)) from None
        except Exception:
            raise ValueError('Browser control could not complete. Refresh the view before trying again.') from None

    def review_material(self,task_id,pack_id,item_id,version,approve):
        state = self.repository.get(task_id)
        self.harness.link_job_pack(state, pack_id)
        self.runtime.pack_workflow.refresh_staleness(pack_id)
        if self.runtime.packs.item(pack_id,item_id).version != version:
            raise ValueError('The document changed; review the current revision.')
        self.runtime.packs.review(pack_id,item_id,expected_version=version,approve=approve,
            idempotency_key=f'career-review:{task_id}:{item_id}:{version}:{approve}')
        item = self.runtime.packs.item(pack_id,item_id)
        if item.version != version and item.status.value != 'approved':
            raise ValueError('The document changed; review the current revision.')
        from eregion.runtime.application_pack.selection import latest_materials
        pack = self.runtime.packs.get(pack_id)
        newest = next((i for p, i in latest_materials(self.runtime.workspace, self.runtime.packs,
            self.runtime.pack_workflow, pack.application_id) if (i.artifact_type, i.source_question) ==
            (item.artifact_type, item.source_question)), item)
        selection = ({'pack_id': pack_id, 'item_id': item_id, 'version': item.version,
            'superseding_item_id': newest.pack_item_id, 'superseding_version': newest.version} if approve else {})
        current_choices = self.harness.material_selections_for(state, pack.application_id)
        self.repository.change(task_id, lambda s: {
            'material_selections': {**s.get('material_selections', {}), item.artifact_type:selection},
            'material_selections_by_application': {**s.get('material_selections_by_application', {}),
                pack.application_id:{**current_choices,item.artifact_type:selection}}})
        return self.chat(task_id,f'I {"approved" if approve else "rejected"} the displayed {item.artifact_type.replace("_"," ")} revision {version} for this job.',f'material-review:{item_id}:{version}:{approve}',context={'pack_id':pack_id,'item_id':item_id})

    def cancel(self,task_id):
        # Fence pending model decisions before draining any child browser work.
        with self.loop.lock:
            state = self.repository.change(task_id,lambda s:{'chat_revision':s['chat_revision']+1},
                status='cancelled',pause_requested=True,wait=None)
        for run_id in state['application_runs']:
            child = self.applications.repository.get(run_id)
            try:self.harness.require_application_owner(state,child)
            except ValueError:continue
            if child['status'] not in {'submitted','submission_unknown','cancelled','failed','submitting'}:
                self.applications.cancel(run_id,child['version'])
        manager=getattr(self.harness,'_browsing',None)
        if manager:manager.close_task(task_id)
        return self.view(task_id)

    def close(self):
        """Stop producers, drain career and interview work, then close the resources
        (discovery, Treg, browser) that work uses. Returns False, keeping those
        resources open, if interview work is still running after its bounded wait;
        calling close again finishes the shutdown once that work has exited."""
        if scheduler:=getattr(self,'alert_scheduler',None):scheduler.close()
        if sweeping:=getattr(self,'_sweeping',None):sweeping.set()
        self.loop.close()
        inputs=getattr(self.harness,'interview_inputs',None)
        if inputs is not None and inputs.close() is False:
            self.close_incomplete=True
            logging.getLogger(__name__).warning(
                'Career shutdown incomplete: interview work is still running; resources kept open.')
            return False
        self.close_incomplete=False
        if hasattr(self.harness,'close'):self.harness.close()
        if self._owns_execution:self.execution.close(wait=True)
        return True
