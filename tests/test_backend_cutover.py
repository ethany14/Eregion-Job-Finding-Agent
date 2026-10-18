"""Custom-only execution, including read-only historical run compatibility."""
import asyncio
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from eregion.api.db import create_database
from eregion.api.main import create_app
from eregion.api.repositories.run_repository import RunRepository
from eregion.api.runtime import create_run_service
from eregion.api.schemas.runs import CreateRunRequest
from runtime_helpers import CompleteFakeHandler, sqlite_url


def test_factory_only_creates_custom_runs_without_checkpoint_files(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_AGENT_BACKEND", "langgraph")
    checkpoint = tmp_path / "unused.sqlite"
    monkeypatch.setenv("JOB_AGENT_CHECKPOINT_PATH", str(checkpoint))
    service = create_run_service(migrate_schema=True, database_url=sqlite_url(tmp_path / "runs.sqlite"),
                                 custom_handler=CompleteFakeHandler())
    try:
        result = asyncio.run(service.create_run(CreateRunRequest(
            resume_text="Built Python APIs.", job_description="Python")))
        assert result.status == "awaiting_review"
        assert service._repository.get(result.run_id).backend == "custom"
        assert not checkpoint.exists()
    finally:
        service.close()


@pytest.mark.parametrize("backend", ["langgraph", "unknown"])
def test_historical_results_are_readable_but_review_is_rejected(tmp_path, backend):
    url = sqlite_url(tmp_path / "runs.sqlite")
    service = create_run_service(migrate_schema=True, database_url=url, custom_handler=CompleteFakeHandler())
    database = create_database(url)
    repository = RunRepository(database.session_factory)
    repository.create(run_id="historical", thread_id="historical", resume_text="R",
                      job_description="J", backend=backend)
    repository.update("historical", status="awaiting_review", result={"saved": True}, error_message=None)
    try:
        with TestClient(create_app(service)) as client:
            stored = client.get("/runs/historical")
            review = client.post("/runs/historical/review", json={"approved": True})
        assert stored.status_code == 200
        assert stored.json()["result"] == {"saved": True}
        assert review.status_code == 409
        assert "read-only" in review.json()["detail"]
        row = repository.get("historical")
        assert row.backend == backend
        assert row.status == "awaiting_review"
        assert row.result == {"saved": True}
    finally:
        database.close()
        service.close()


def test_runtime_and_evaluations_import_with_langgraph_unavailable():
    root = Path(__file__).resolve().parents[1]
    code = """
import sys
import importlib.abc
class BlockGraph(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'langgraph' or fullname.startswith('langgraph.'):
            raise ModuleNotFoundError('Retired dependency is unavailable', name=fullname)
sys.meta_path.insert(0, BlockGraph())
import eregion.api.main, eregion.api.runtime, main
import evals.run_evals, evals.run_ablation, evals.run_stability_evals
import evals.run_requirement_intelligence_v3
assert not any(name == 'langgraph' or name.startswith('langgraph.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=root,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
