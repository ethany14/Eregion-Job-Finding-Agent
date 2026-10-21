"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager
from collections.abc import Callable
from typing import Any

import os

import anyio
from fastapi import FastAPI, Request
from starlette.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from eregion.paths import PROJECT_ROOT
from urllib.parse import urlencode

from eregion.api.error_handlers import install_error_handlers
from eregion.api.context_routes import router as context_router
from eregion.api.evidence_routes import router as evidence_router
from eregion.api.interview_routes import router as interview_router
from eregion.api.pack_routes import router as pack_router
from eregion.api.agent_task_routes import router as agent_task_router
from eregion.api.multi_agent_routes import router as multi_agent_router
from eregion.api.mock_interview_routes import router as mock_interview_router
from eregion.api.routes.runs import router as runs_router
from eregion.api.session_dependencies import SessionRuntime, create_session_runtime
from eregion.api.session_routes import router as sessions_router
from eregion.api.workspace_routes import router as workspace_router
from eregion.api.feedback_routes import router as feedback_router
from eregion.api.assistant_routes import router as assistant_router
from eregion.api.services.run_service import RunService
from eregion.api.resume_routes import router as resume_router
from eregion.api.auto_apply_routes import router as auto_apply_router
from eregion.api.career_routes import router as career_router, groups_router as career_groups_router
from eregion.api.material_routes import router as material_router
from eregion.api.job_alert_routes import router as job_alert_router
from eregion.api.model_settings_routes import router as model_settings_router


LOCAL_HOSTS = ("localhost", "127.0.0.1", "testserver")


def allowed_hosts() -> list[str]:
    """Loopback names by default; JOB_AGENT_ALLOWED_HOSTS adds comma-separated names."""
    extra = [host.strip() for host in os.getenv("JOB_AGENT_ALLOWED_HOSTS", "").split(",") if host.strip()]
    return [*LOCAL_HOSTS, *extra]


def chat_redirect(request: Request) -> RedirectResponse:
    # Keep saved-job and browser links useful without carrying old view settings.
    query = urlencode({key: request.query_params[key] for key in ("application", "url")
                       if request.query_params.get(key)})
    return RedirectResponse("/apply" + (f"?{query}" if query else ""),
                            headers={"Cache-Control": "no-store"})


class ChatAssets(StaticFiles):
    async def get_response(self, path, scope):
        # HTML is served only by the chat route, including on case-insensitive Windows.
        if Path(path).suffix.lower() in {".html", ".htm"}:
            return chat_redirect(Request(scope))
        response = await super().get_response(path, scope)
        # Modules import each other without version tags (i18n.js -> i18n-zh.js); revalidate so an
        # updated script is never mixed with a heuristically cached older one. Unchanged files are a 304.
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    run_service: RunService | None = None,
    *,
    session_runtime: SessionRuntime | None = None,
    session_runtime_factory: Callable[[], SessionRuntime] | None = None,
    fit_analysis_service: Any | None = None,
) -> FastAPI:
    factory = session_runtime_factory
    if factory is None and run_service is None and session_runtime is None:
        factory = create_session_runtime

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        created_runtime = None
        if (
            application.state.session_runtime is None
            and factory is not None
            and not application.dependency_overrides
        ):
            if factory is create_session_runtime:
                # Switches saved from the workspace that their readers take from the process environment.
                from eregion.domain.model_settings import load_process_switches
                load_process_switches()
            created_runtime = await anyio.to_thread.run_sync(factory)
            application.state.session_runtime = created_runtime
        try:
            from eregion.api.auto_apply_routes import restore_applications
            await anyio.to_thread.run_sync(restore_applications, application)
            from eregion.api.career_routes import restore_tasks
            await anyio.to_thread.run_sync(restore_tasks, application)
            yield
        finally:
            career = getattr(application.state, 'career_agent', None)
            if career is not None:
                # False means interview work outlived its drain wait and the career
                # resources were deliberately kept open; retry once before the
                # runtime (and database) close, otherwise leave them and say so.
                if await anyio.to_thread.run_sync(career.close) is False and                         await anyio.to_thread.run_sync(career.close) is False:
                    logging.getLogger(__name__).warning(
                        "Career resources left open at shutdown: interview work did not finish.")
                application.state.career_agent = None
            automation = getattr(application.state, "auto_apply", None)
            if automation is not None:
                await anyio.to_thread.run_sync(automation.close)
                application.state.auto_apply = None
            if run_service is None and application.state.run_service is not None:
                await anyio.to_thread.run_sync(application.state.run_service.close)
                application.state.run_service = None
            if created_runtime is not None:
                await anyio.to_thread.run_sync(created_runtime.close)
                application.state.session_runtime = None

    application = FastAPI(
        title="eregion API",
        version="0.5.0",
        description=(
            "Evidence-grounded resume tailoring and persistent local agent sessions. "
            "Session access currently assumes a trusted single-user local deployment."
        ),
        lifespan=lifespan,
    )
    application.state.run_service = run_service
    application.state.session_runtime = session_runtime
    application.state.fit_analysis_service = fit_analysis_service
    application.include_router(runs_router)
    application.include_router(sessions_router)
    application.include_router(context_router)
    application.include_router(evidence_router)
    application.include_router(interview_router)
    application.include_router(pack_router)
    application.include_router(agent_task_router)
    application.include_router(multi_agent_router)
    application.include_router(mock_interview_router)
    application.include_router(workspace_router)
    application.include_router(feedback_router)
    application.include_router(assistant_router)
    application.include_router(resume_router)
    application.include_router(auto_apply_router)
    application.include_router(career_router)
    application.include_router(career_groups_router)
    application.include_router(material_router)
    application.include_router(job_alert_router)
    application.include_router(model_settings_router)
    from eregion.api.skill_evolution_routes import router as skill_evolution_router
    application.include_router(skill_evolution_router)
    install_error_handlers(application)
    @application.middleware('http')
    async def provider_usage_context(request, call_next):
        from eregion.runtime.provider_usage import UsageLedger, usage_scope
        from uuid import uuid4
        runtime = application.state.session_runtime
        database = getattr(runtime, 'database', None)
        with usage_scope(ledger=UsageLedger(database.session_factory) if database else None,
                         request_id=str(uuid4()), purpose='api'):
            return await call_next(request)
    # DNS rebinding sends a page's own hostname to this local server. Reject
    # every Host except loopback names so remote pages cannot read candidate
    # data or drive applications through it.
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())

    web_root = PROJECT_ROOT / "frontend/web_app"
    if web_root.exists():
        application.mount("/web-assets", ChatAssets(directory=web_root), name="web-assets")

        @application.get("/apply", include_in_schema=False)
        async def auto_apply_page() -> FileResponse:
            return FileResponse(web_root / "apply.html", headers={"Cache-Control": "no-store"})

        @application.get("/", include_in_schema=False)
        @application.get("/app", include_in_schema=False)
        @application.get("/app/", include_in_schema=False)
        @application.get("/index.html", include_in_schema=False)
        async def web_app(request: Request) -> RedirectResponse:
            return chat_redirect(request)

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, Any]:
        runtime = application.state.session_runtime
        mcp = (
            runtime.mcp_health().model_dump(mode="json")
            if runtime is not None
            else {
                "configured_servers": 0,
                "ready_servers": 0,
                "failed_optional_servers": 0,
                "registered_tools": 0,
                "servers": [],
            }
        )
        return {
            "status": "degraded" if mcp["failed_optional_servers"] else "ok",
            "mcp": mcp,
        }

    return application


app = create_app()
