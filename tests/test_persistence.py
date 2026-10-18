import asyncio
import pytest
from fastapi.testclient import TestClient
from eregion.api.db import create_database
from eregion.api.main import create_app
from eregion.api.repositories.run_repository import RunRepository
from eregion.api.runtime import create_run_service
from eregion.api.schemas.runs import CreateRunRequest, ReviewRequest
from eregion.api.services.run_service import SAFE_RUN_ERROR, InvalidRunStateError, RunNotFoundError
from runtime_helpers import CompleteFakeHandler, FailingHandler, sqlite_url


def new_service(tmp_path):
    return create_run_service(migrate_schema=True, database_url=sqlite_url(tmp_path / "runs.sqlite"),
                              custom_handler=CompleteFakeHandler())


def create_paused_run(service) -> str:
    created = asyncio.run(
        service.create_run(
            CreateRunRequest(
                resume_text="Built Python APIs.",
                job_description="Requires Python.",
            )
        )
    )
    assert created.status == "awaiting_review"
    return created.run_id


def test_created_run_is_queryable_from_database(tmp_path):
    service = new_service(tmp_path)
    try:
        run_id = create_paused_run(service)
        database = create_database(sqlite_url(tmp_path / "runs.sqlite"))
        try:
            row = RunRepository(database.session_factory).get(run_id)
        finally:
            database.close()
        assert row is not None
        assert row.run_id == row.thread_id == run_id
        assert row.status == "awaiting_review"
        assert row.result["revision_count"] == 0
    finally:
        service.close()


def test_repository_none_result_preserves_latest_stable_projection(tmp_path):
    database = create_database(
        sqlite_url(tmp_path / "projection.sqlite"), create_schema_for_tests=True
    )
    repository = RunRepository(database.session_factory)
    try:
        repository.create(
            run_id="projection-run",
            thread_id="projection-run",
            resume_text="Resume",
            job_description="Job",
        )
        repository.update(
            "projection-run",
            status="awaiting_review",
            result={"tailored_resume": "stable"},
            error_message=None,
        )
        revising = repository.update(
            "projection-run",
            status="revising",
            result=None,
            error_message=None,
        )
        assert revising.status == "revising"
        assert revising.result == {"tailored_resume": "stable"}

        failed = repository.update(
            "projection-run",
            status="failed",
            result=None,
            error_message=SAFE_RUN_ERROR,
        )
        assert failed.status == "failed"
        assert failed.result == {"tailored_resume": "stable"}
    finally:
        database.close()


def test_new_application_instance_reads_and_approves_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        with TestClient(create_app(restarted_service)) as client:
            stored = client.get(f"/runs/{run_id}")
            approved = client.post(
                f"/runs/{run_id}/review", json={"approved": True}
            )
        assert stored.status_code == 200
        assert stored.json()["status"] == "awaiting_review"
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
    finally:
        restarted_service.close()


def test_reject_with_feedback_resumes_custom_state_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        reviewed = asyncio.run(
            restarted_service.review_run(
                run_id,
                ReviewRequest(approved=False, feedback="Shorten the summary."),
            )
        )
        assert reviewed.status == "awaiting_review"
        assert reviewed.result["revision_count"] == 1
    finally:
        restarted_service.close()


def test_missing_run_is_404_with_new_application(tmp_path):
    service = new_service(tmp_path)
    try:
        with TestClient(create_app(service)) as client:
            response = client.get("/runs/missing")
        assert response.status_code == 404
    finally:
        service.close()


def test_completed_run_cannot_be_reviewed_again_after_restart(tmp_path):
    first_service = new_service(tmp_path)
    run_id = create_paused_run(first_service)
    asyncio.run(
        first_service.review_run(run_id, ReviewRequest(approved=True, feedback=None))
    )
    first_service.close()

    restarted_service = new_service(tmp_path)
    try:
        with pytest.raises(InvalidRunStateError, match="not awaiting review"):
            asyncio.run(
                restarted_service.review_run(
                    run_id, ReviewRequest(approved=True, feedback=None)
                )
            )
    finally:
        restarted_service.close()


def test_failed_status_is_persisted_without_internal_error_details(tmp_path):
    service = create_run_service(migrate_schema=True,
        database_url=sqlite_url(tmp_path / "runs.sqlite"),
        custom_handler=FailingHandler())
    run_id = asyncio.run(
        service.create_run(
            CreateRunRequest(resume_text="Resume", job_description="Job")
        )
    ).run_id
    service.close()

    restarted_service = new_service(tmp_path)
    try:
        stored = asyncio.run(restarted_service.get_run(run_id))
        assert stored.status == "failed"
        assert stored.error == SAFE_RUN_ERROR
        assert "secret" not in stored.error
        with pytest.raises(InvalidRunStateError):
            asyncio.run(
                restarted_service.review_run(
                    run_id, ReviewRequest(approved=True, feedback=None)
                )
            )
    finally:
        restarted_service.close()


def test_repository_missing_run_raises_service_not_found(tmp_path):
    service = new_service(tmp_path)
    try:
        with pytest.raises(RunNotFoundError):
            asyncio.run(service.get_run("missing"))
    finally:
        service.close()
