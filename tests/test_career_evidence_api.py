from fastapi.testclient import TestClient

from eregion.api.main import create_app
from eregion.api.session_dependencies import create_session_runtime


def test_evidence_api_local_lifecycle_and_safe_projection(tmp_path):
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'evidence-api.db').as_posix()}",
        model=object(),
    )
    try:
        with TestClient(create_app(run_service=object(), session_runtime=runtime),
                        raise_server_exceptions=False) as client:
            body = {
                "category": "experience", "source_type": "user_attested",
                "claim_text": "Built Python APIs.", "exact_quote": "Built Python APIs.",
                "source_reference": "C:\\private\\resume.txt",
            }
            created = client.post("/api/evidence/candidates", json=body)
            assert created.status_code == 200, created.text
            item = created.json()
            assert item["status"] == "candidate"
            assert "source_reference" not in item["current"]
            assert "C:\\private" not in created.text
            path = f"/api/evidence/{item['evidence_id']}"
            assert client.get(path).status_code == 200
            assert len(client.get(path + "/versions").json()["versions"]) == 1
            assert len(client.get(path + "/events").json()["events"]) == 1
            assert client.post(path + "/confirm", json={"expected_version": 999}).status_code == 409
            confirmed = client.post(path + "/confirm", json={"expected_version": item["version"]})
            assert confirmed.status_code == 200
            assert confirmed.json()["status"] == "confirmed"
            assert client.post(path + "/reject", json={"expected_version": confirmed.json()["version"]}).status_code == 409
            assert client.get("/api/evidence?status=confirmed").json()["items"][0]["evidence_id"] == item["evidence_id"]
            assert client.get("/api/evidence?status=candidate").json()["items"] == []
            assert client.post("/api/evidence/candidates", json={
                **body, "source_type": "resume",
            }).status_code == 422
            assert client.get("/api/evidence/missing").status_code == 404
    finally:
        runtime.close()


def test_application_evidence_link_api_requires_confirmed_version(tmp_path):
    runtime = create_session_runtime(migrate_schema=True,
        database_url=f"sqlite:///{(tmp_path / 'evidence-link-api.db').as_posix()}",
        model=object(),
    )
    try:
        job = runtime.workspace.create_or_find_job(
            cleaned_job_description="Python engineer.", title="Engineer", company="Example")
        app = runtime.workspace.create_application(job_id=job.job.job_id,
            snapshot_id=job.snapshot.snapshot_id)
        with TestClient(create_app(run_service=object(), session_runtime=runtime),
                        raise_server_exceptions=False) as client:
            item = client.post("/api/evidence/candidates", json={
                "category": "skill", "source_type": "user_attested",
                "claim_text": "Used Python.", "exact_quote": "Used Python.",
            }).json()
            path = f"/api/applications/{app.application_id}/evidence-links"
            request = {"evidence_id": item["evidence_id"],
                       "expected_version": item["version"]}
            assert client.post(path, json=request).status_code == 409
            confirmed = client.post(f"/api/evidence/{item['evidence_id']}/confirm",
                json={"expected_version": item["version"]}).json()
            request["expected_version"] = confirmed["version"]
            linked = client.post(path, json=request)
            assert linked.status_code == 200, linked.text
            link_id = linked.json()["link_id"]
            assert client.get(f"/api/applications/{app.application_id}/evidence").json()["links"][0]["link_id"] == link_id
            assert client.delete(f"{path}/{link_id}", params={"expected_version": confirmed["version"] + 1}).status_code == 200
            assert client.get(f"/api/applications/{app.application_id}/evidence").json()["links"] == []
    finally:
        runtime.close()
