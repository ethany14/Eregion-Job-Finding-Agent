"""Local browser application API; construction does not open a browser."""
from threading import Lock
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from eregion.runtime.auto_apply.contracts import ReplyQuestion, ContinueApplication, StartApplication, VersionRequest, ChatRequest, ResumeProfileRequest, PreparationMessage, AccountCredentials, AccountMode, SavedCandidateRequest, MemoryUpdate, StandardAnswers
from eregion.runtime.auto_apply.loop import ApplicationLoop
from eregion.runtime.auto_apply.public import public
from eregion.runtime.auto_apply.repository import ApplyConflict, ApplyNotFound, ApplyRepository, DuplicateApplication

router = APIRouter(prefix="/api/auto-apply", tags=["automatic applications"])
_lock = Lock()


def _execution(runtime):
    """Share the runtime's ExecutionService when it has one (unified loop M5)."""
    execution = getattr(runtime, "execution", None)
    return {"execution": execution} if execution is not None else {}


def restore_applications(application):
    """Resume durable work on server startup, without waiting for an open UI."""
    runtime = application.state.session_runtime
    if runtime is None or getattr(runtime, "database", None) is None:
        return
    with _lock:
        if getattr(application.state, "auto_apply", None) is None:
            application.state.auto_apply = ApplicationLoop(ApplyRepository(runtime.database.session_factory), **_execution(runtime))


def service(request: Request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.url.netloc:
        raise HTTPException(403, "Open application automation from this local workspace.")
    with _lock:
        existing = getattr(request.app.state, "auto_apply", None)
        if existing is None:
            runtime = request.app.state.session_runtime
            if runtime is None:
                raise HTTPException(503, "The local workspace is not ready.")
            existing = ApplicationLoop(ApplyRepository(runtime.database.session_factory), **_execution(runtime))
            request.app.state.auto_apply = existing
        return existing


def invoke(action):
    try:
        return public(action())
    except ApplyNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except DuplicateApplication as exc:
        raise HTTPException(409, {"message": str(exc), "existing_run_id": exc.run_id}) from exc
    except ApplyConflict as exc:
        raise HTTPException(409, str(exc)) from exc


@router.get("")
def list_attempts(loop=Depends(service)):
    return {"attempts": [public(state) for state in loop.repository.list()]}


@router.post("", status_code=202)
def start_attempt(body: StartApplication, loop=Depends(service)):
    return invoke(lambda: loop.start(body))


@router.post("/resume-profile")
def resume_profile(body: ResumeProfileRequest, loop=Depends(service)):
    from eregion.runtime.auto_apply.resume_profile import extract_profile
    try:
        result=extract_profile(body.resume_base64)
        saved=loop.repository.saved_candidate()
        profile=result['profile']
        if saved['resume_base64']==body.resume_base64:
            profile={**profile,**{k:v for k,v in saved['profile'].items() if v and k not in {'education','experience'}},
                'education':profile.get('education') or saved['profile'].get('education',[]),
                'experience':profile.get('experience') or saved['profile'].get('experience',[])}
        result['profile']=profile
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/candidate")
def saved_candidate(response: Response, loop=Depends(service)):
    response.headers['Cache-Control']='no-store'
    return loop.repository.saved_candidate()


@router.post("/candidate")
def save_candidate(body: SavedCandidateRequest, loop=Depends(service)):
    import base64
    return {'profile':loop.repository.save_candidate(body.profile.model_dump(),
        resume=base64.b64decode(body.resume_base64) if body.resume_base64 else None,
        name=body.resume_name if body.resume_base64 else None,extracted=body.extracted)}


@router.delete("/candidate")
def clear_candidate(loop=Depends(service)):
    loop.repository.clear_candidate()
    return {'removed':True}


@router.get("/standard-answers")
def standard_answers(response: Response, loop=Depends(service)):
    response.headers['Cache-Control']='no-store'
    return {'answers':loop.repository.standard_answers(loop.repository.saved_candidate()['profile'])}


@router.put("/standard-answers")
def save_standard_answers(body: StandardAnswers, loop=Depends(service)):
    try:
        with loop.lock:
            saved = loop.repository.save_standard_answers(loop.repository.saved_candidate()['profile'], body.answers)
    except ApplyConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'answers':saved}


@router.post("/{run_id}/chat", status_code=202)
def chat(run_id: str, body: ChatRequest, loop=Depends(service)):
    return invoke(lambda: loop.chat(run_id, body))


@router.post("/{run_id}/resume", status_code=202)
def resume_attempt(run_id: str, body: VersionRequest, loop=Depends(service)):
    return invoke(lambda: loop.resume(run_id, body.expected_version))


@router.post("/prepare-message")
def prepare_message(body: PreparationMessage, loop=Depends(service)):
    from eregion.runtime.auto_apply.resume_profile import extract_chat_profile
    return extract_chat_profile(body.text, field=body.field)


@router.get("/{run_id}/preview")
def preview(run_id: str, loop=Depends(service)):
    try:
        content = loop.preview(run_id)
    except ApplyNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    return Response(content=content or b"", media_type="image/jpeg", status_code=200 if content else 204,
        headers={"Cache-Control": "no-store"})


class BrowserWindow(BaseModel):
    model_config=ConfigDict(extra='forbid')
    visible: bool
    expected_version: int=Field(ge=1)


@router.post('/{run_id}/browser-control')
async def browser_control(run_id: str, request: Request, loop=Depends(service)):
    from .browser_control import read_control_request
    from fastapi.responses import JSONResponse
    body = await read_control_request(request)
    # Keep browser-worker waits off the event loop so the chat stays responsive.
    from starlette.concurrency import run_in_threadpool
    result = await run_in_threadpool(lambda: invoke(lambda: loop.browser_control(run_id, body)))
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/{run_id}/browser-window')
def browser_window(run_id: str,body: BrowserWindow,loop=Depends(service)):
    return invoke(lambda:loop.set_browser_visible(run_id,body.visible,body.expected_version))


@router.get("/{run_id}/memory")
def memory(run_id: str, loop=Depends(service)):
    return invoke(lambda: {"memories": loop.repository.memories(loop.repository.get(run_id)["profile"])})


@router.delete("/{run_id}/memory/{memory_id}")
def forget(run_id: str, memory_id: str, loop=Depends(service)):
    def action():
        with loop.lock:
            loop.repository.forget(loop.repository.get(run_id)["profile"], memory_id)
        return {"forgotten": True}
    return invoke(action)


@router.patch("/{run_id}/memory/{memory_id}")
def update_memory(run_id: str, memory_id: str, body: MemoryUpdate, loop=Depends(service)):
    def action():
        with loop.lock:
            state = loop.repository.get(run_id)
            record = loop.repository.update_memory(state["profile"], memory_id,
                value=body.value, scope=body.scope, url=state.get("url"))
        return {"memory": record}
    return invoke(action)


@router.get("/{run_id}")
def get_attempt(run_id: str, loop=Depends(service)):
    from eregion.runtime.provider_usage import UsageLedger
    return invoke(lambda: {**loop.repository.get(run_id),
        'usage_summary': UsageLedger(loop.repository.sessions).summary(run_ids=[run_id])})


@router.post("/{run_id}/reply", status_code=202)
def reply_question(run_id: str, body: ReplyQuestion, loop=Depends(service)):
    return invoke(lambda: loop.reply_question(run_id, body))


@router.post("/{run_id}/continue", status_code=202)
def continue_attempt(run_id: str, body: ContinueApplication, loop=Depends(service)):
    return invoke(lambda: loop.continue_run(run_id, body))


@router.post("/{run_id}/cancel")
def cancel_attempt(run_id: str, body: VersionRequest, loop=Depends(service)):
    return invoke(lambda: loop.cancel(run_id, body.expected_version))


@router.post("/{run_id}/account", status_code=202)
async def account(run_id: str, request: Request, loop=Depends(service)):
    # FastAPI's default validation responses echo invalid input. Validate this
    # secret-bearing request explicitly and return a constant error instead.
    try:
        body = AccountCredentials.model_validate(await request.json())
    except (ValueError, TypeError):
        raise HTTPException(422, "Check the account details and try again.") from None
    return invoke(lambda: loop.account(run_id, body))


@router.post("/{run_id}/account-mode", status_code=202)
def account_mode(run_id: str, body: AccountMode, loop=Depends(service)):
    return invoke(lambda: loop.account(run_id, body, switch=True))
