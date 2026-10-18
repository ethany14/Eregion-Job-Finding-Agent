"""Build the API runtime using the persisted custom agent loop."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from eregion.api.db import create_database, upgrade_database, require_current_database
from eregion.api.repositories.run_repository import RunRepository
from eregion.api.services.run_service import RunService
from eregion.domain.workflow.handlers import JobAgentStepHandler
from eregion.domain.workflow.analysis import AnalysisWorkflow
from eregion.domain.workflow.repository import StateRepository
from eregion.domain.results import public_result


def create_run_service(*, database_url: str | None = None,
                       result_serializer: Callable[[dict[str, Any]], dict[str, Any]] = public_result,
                       migrate_schema: bool = False,
                       custom_handler: Any = None) -> RunService:
    """Create the only supported runtime; historical projections remain readable."""
    if migrate_schema:
        if database_url is None:
            raise ValueError("Explicit migration requires an explicit database URL.")
        upgrade_database(database_url)
    else:
        require_current_database(database_url)
    database = create_database(database_url)
    try:
        loop = AnalysisWorkflow(repository=StateRepository(database.session_factory),
            handler=custom_handler if custom_handler is not None else JobAgentStepHandler(),
            result_projector=result_serializer)
        return RunService(repository=RunRepository(database.session_factory),
            loop=loop, resources=(database,))
    except BaseException:
        database.close()
        raise
