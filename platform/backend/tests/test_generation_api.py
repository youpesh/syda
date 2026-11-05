import csv
import io
import uuid
import zipfile
import pytest

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import database, job_queue, job_runner
from app.api import generation
from app.main import app
from app.models.entities import JobRecord
from app.models.user import User
from app.api.auth import require_authenticated


_TEST_USER = User(
    id=uuid.uuid4(),
    email="syda-test@example.com",
    hashed_password="test-only",
    is_active=True,
    is_superuser=False,
    is_verified=True,
    display_name="Syda Test",
)


@pytest.mark.parametrize("amount,expected_status", [(0, "failed"), (25, "complete")])
def test_worker_enforces_causal_gate_and_retains_report(monkeypatch, tmp_path, amount, expected_status):
    scenario = _scenario(record_count=3, paths=[{
        "name": "paid", "steps": ["Parent", "Child"], "weight": 1,
        "overrides": {"Parent": {"status": "paid", "amount": amount}},
    }])
    scenario["schemas"]["Parent"].update({"status": "text", "amount": "float"})
    scenario["checks"] = [{
        "name": "positive_payment<script>", "kind": "semantic_invariant", "table": "Parent",
        "condition": "status == 'paid'", "assertion": "amount > 0",
    }]
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/generate", json={"scenario": scenario})
        assert response.status_code == 200
        job_id = response.json()["jobId"]
        job = client.get(f"/api/status/{job_id}").json()
        assert job["status"] == expected_status
        report = client.get(f"/api/evaluation/{job_id}/report")
        assert report.status_code == 200
        html_report = client.get(f"/api/evaluation/{job_id}/report?format=html")
        assert html_report.status_code == 200
        if expected_status == "failed":
            assert job["stats"]["evaluationResult"] == "fail"
            assert job["downloadUrl"] is None
            assert not client.get(f"/api/jobs/{job_id}").json()["filesAvailable"]
            assert report.json()["violations"][0]["rule"] == "positive_payment<script>"
            assert "positive_payment&lt;script&gt;" in html_report.text
            assert "positive_payment<script>" not in html_report.text
            assert client.get(f"/api/download/{job_id}").status_code == 409
            assert client.get(f"/api/preview/{job_id}").status_code == 409
            assert not list((tmp_path / "jobs").rglob("*.csv"))
            with database.SessionLocal() as db:
                assert db.get(JobRecord, job_id).attempt_count == 1
        else:
            assert report.json()["violations"] == []
            assert job["stats"]["evaluationResult"] == "partial"
            assert client.get(f"/api/download/{job_id}").status_code == 200


def _scenario(record_count: int = 11, paths: list[dict] | None = None) -> dict:
    scenario = {
        "title": "MVP generation test",
        "description": "A parent-child dataset",
        "recordCount": record_count,
        "secondaryMetric": {"label": "Tables", "value": "2"},
        "workflow": ["Parent", "Child"],
        "rules": ["Every child references a parent"],
        "schemas": {
            "Parent": {
                "parent_id": {"type": "integer", "constraints": {"primary_key": True}},
                "name": "text",
            },
            "Child": {
                "child_id": {"type": "integer", "constraints": {"primary_key": True}},
                "parent_id": "foreign_key",
                "__foreign_keys__": {"parent_id": "Parent.parent_id"},
            },
        },
    }
    if paths is not None:
        scenario["paths"] = paths
    return scenario


def _client(monkeypatch, tmp_path) -> TestClient:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", testing_session)
    monkeypatch.setattr(generation, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(job_runner, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(job_queue, "DATA_DIR", tmp_path / "jobs")
    # Exercise real worker execution synchronously; broker transport has a separate test.
    monkeypatch.setattr(generation, "publish_job", lambda job_id, **kw: job_runner.execute_job(job_id))
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    app.dependency_overrides.clear()
    app.dependency_overrides[require_authenticated] = lambda: _TEST_USER
    return TestClient(app)


def test_generation_reports_and_downloads_the_rows_it_created(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        created = client.post("/api/generate", json={"scenario": _scenario()})
        assert created.status_code == 200

        job_id = created.json()["jobId"]
        status = client.get(f"/api/status/{job_id}")
        assert status.status_code == 200
        result = status.json()
        assert result["status"] == "complete"
        stats = result["stats"]
        assert stats["causalIntegrity"] == "Not evaluated"
        assert stats["referentialIntegrity"] == "100.0%"
        assert stats["compliance"] == "Not evaluated"
        assert stats["recordsGenerated"] == 22
        assert stats["flaggedRecords"] == 0
        assert stats["tableRowCounts"] == {"Parent": 11, "Child": 11}
        assert stats["pathCounts"] == {"default": 11}
        assert stats["evaluationResult"] == "partial"
        assert {metric["key"] for metric in stats["evaluationMetrics"]} == {
            "statistical_similarity",
            "semantic_correctness",
            "causal_consistency",
            "scenario_coverage",
            "privacy_leakage",
            "referential_integrity",
            "declared_rules",
        }
        declared_rules = next(
            metric for metric in stats["evaluationMetrics"]
            if metric["key"] == "declared_rules"
        )
        assert declared_rules["status"] == "not_evaluated"

        download = client.get(result["downloadUrl"])
        assert download.status_code == 200
        assert download.headers["content-type"] == "application/zip"
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            row_counts = []
            for filename in archive.namelist():
                rows = list(csv.reader(io.StringIO(archive.read(filename).decode())))
                row_counts.append(len(rows) - 1)
        assert sorted(row_counts) == [11, 11]
        assert sum(row_counts) == result["stats"]["recordsGenerated"]

        preview = client.get(f"/api/preview/{job_id}?limit=2")
        assert preview.status_code == 200
        assert sorted(
            table["rowCount"] for table in preview.json()["tables"].values()
        ) == [11, 11]
        assert all(
            len(table["rows"]) == 2 for table in preview.json()["tables"].values()
        )

        json_download = client.get(f"/api/download/{job_id}?format=json")
        assert json_download.status_code == 200
        assert json_download.headers["content-type"] == "application/zip"

        report = client.get(f"/api/evaluation/{job_id}/report?format=json")
        assert report.status_code == 200
        assert report.json()["result"] == "partial"
        assert len(report.json()["metrics"]) == 7

        html_report = client.get(f"/api/evaluation/{job_id}/report?format=html")
        assert html_report.status_code == 200
        assert "Syda evaluation report" in html_report.text


def test_generation_honors_weighted_scenario_paths(monkeypatch, tmp_path):
    paths = [
        {
            "name": "complete",
            "steps": ["Parent", "Child"],
            "weight": 0.5,
            "overrides": {},
        },
        {
            "name": "edge",
            "steps": ["Parent", "Child"],
            "weight": 0.25,
            "overrides": {},
        },
        {
            "name": "failure",
            "steps": ["Parent"],
            "weight": 0.25,
            "overrides": {},
        },
    ]
    with _client(monkeypatch, tmp_path) as client:
        created = client.post(
            "/api/generate",
            json={"scenario": _scenario(record_count=8, paths=paths)},
        )
        result = client.get(f"/api/status/{created.json()['jobId']}").json()

        assert result["status"] == "complete"
        assert result["stats"]["tableRowCounts"] == {"Parent": 8, "Child": 6}
        assert result["stats"]["pathCounts"] == {
            "complete": 4,
            "edge": 2,
            "failure": 2,
        }

        download = client.get(result["downloadUrl"])
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            tables = {
                filename.rsplit("/", 1)[-1]
                .removesuffix(".csv")
                .lower(): list(
                    csv.DictReader(io.StringIO(archive.read(filename).decode()))
                )
                for filename in archive.namelist()
            }

        parent_rows = tables["parent"]
        child_rows = tables["child"]
        assert {row["scenario_path"] for row in parent_rows} == {
            "complete",
            "edge",
            "failure",
        }
        assert {row["scenario_path"] for row in child_rows} == {"complete", "edge"}
        assert len({row["scenario_instance_id"] for row in parent_rows}) == 8

        parent_instance_by_id = {
            row["parent_id"]: row["scenario_instance_id"] for row in parent_rows
        }
        assert all(
            parent_instance_by_id[row["parent_id"]] == row["scenario_instance_id"]
            for row in child_rows
        )


def test_intentionally_absent_table_does_not_reject_valid_path(monkeypatch, tmp_path):
    scenario = _scenario(record_count=3, paths=[{
        "name": "cancelled", "steps": ["Parent"], "weight": 1,
        "overrides": {"Parent": {"name": "cancelled"}},
    }])
    scenario["checks"] = [{
        "name": "cancelled_has_no_child", "kind": "absence_by_value",
        "source": "Parent.name", "value": "cancelled", "absent_table": "Child",
    }]
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/generate", json={"scenario": scenario})
        assert response.status_code == 200
        job_id = response.json()["jobId"]
        job = client.get(f"/api/status/{job_id}").json()
        assert job["status"] == "complete"
        assert job["stats"]["tableRowCounts"] == {"Parent": 3, "Child": 0}
        assert job["stats"]["violations"] == []
        assert client.get(f"/api/download/{job_id}").status_code == 200


def test_generation_estimate_discloses_offline_fallback(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/estimate", json={"scenario": _scenario()})
        assert response.status_code == 200
        assert response.json() == {
            "provider": "Offline",
            "model": "Local schema synthesizer",
            "estimatedInputTokens": 0,
            "estimatedOutputTokens": 0,
            "estimatedCostUsd": 0.0,
            "note": "Local generation selected; no model API calls are estimated.",
        }


def test_validation_distinguishes_executable_checks_from_text_guidance(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/api/validate", json={"scenario": _scenario()})
        assert response.status_code == 200
        assert response.json()["executable_checks_count"] == 0
        assert response.json()["guidance_rules_count"] == 1


def test_generation_estimate_counts_only_unconstrained_fields(monkeypatch, tmp_path):
    paths = [
        {
            "name": "complete",
            "steps": ["Parent", "Child"],
            "weight": 0.5,
            "overrides": {},
        },
        {
            "name": "failure",
            "steps": ["Parent"],
            "weight": 0.5,
            "overrides": {},
        },
    ]
    with _client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr(
            generation,
            "resolve_provider",
            lambda _db, _user_id: {
                "id": "openai",
                "name": "OpenAI",
                "model": "gpt-4o-mini",
                "agent_model": "openai:gpt-4o-mini",
                "key": "test-only",
                "base_url": None,
            },
        )
        response = client.post(
            "/api/estimate",
            json={"scenario": _scenario(record_count=8, paths=paths)},
        )

        assert response.status_code == 200
        estimate = response.json()
        assert estimate["provider"] == "OpenAI"
        assert estimate["estimatedInputTokens"] == 100
        assert estimate["estimatedOutputTokens"] == 192


def test_generation_returns_actionable_error_when_database_is_unavailable(
    monkeypatch, tmp_path
):
    class FailingSession:
        def get(self, *args):
            raise SQLAlchemyError("database unavailable")

        def add(self, _record):
            return None

        def commit(self):
            raise SQLAlchemyError("database unavailable")

        def rollback(self):
            return None

    def failing_db():
        yield FailingSession()

    with _client(monkeypatch, tmp_path) as client:
        app.dependency_overrides[generation.get_db] = failing_db
        response = client.post("/api/generate", json={"scenario": _scenario()})
        assert response.status_code == 503
        assert response.json()["detail"] == (
            "The generation database is unavailable. Restart the backend or check DATABASE_URL."
        )
        app.dependency_overrides.clear()


def test_completed_job_without_files_returns_404(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        with database.SessionLocal() as session:
            session.add(
                JobRecord(
                    id="missing-files",
                    status="complete",
                    progress=100,
                    current_stage="Complete",
                    scenario=_scenario(),
                    user_id=_TEST_USER.id,
                )
            )
            session.commit()

        response = client.get("/api/download/missing-files")
        assert response.status_code == 404
        assert response.json()["detail"] == "Generated dataset files are unavailable"


def test_incomplete_job_can_be_polled_but_not_downloaded(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        with database.SessionLocal() as session:
            session.add(
                JobRecord(
                    id="still-running",
                    status="generating",
                    progress=55,
                    current_stage="Generating records",
                    scenario=_scenario(),
                    user_id=_TEST_USER.id,
                )
            )
            session.commit()

        status = client.get("/api/status/still-running")
        assert status.status_code == 200
        assert status.json()["status"] == "generating"

        download = client.get("/api/download/still-running")
        assert download.status_code == 409


def test_invalid_path_returns_actionable_estimate_and_validation_errors(monkeypatch, tmp_path):
    scenario = _scenario(paths=[{"name": "holiday", "steps": ["Child"], "weight": 1}])
    expected = "Scenario path 'holiday' includes 'Child' without required parent step 'Parent'."
    with _client(monkeypatch, tmp_path) as client:
        estimate = client.post("/api/estimate", json={"scenario": scenario})
        assert estimate.status_code == 422
        assert estimate.json()["detail"] == expected

        validation = client.post("/api/validate", json={"scenario": scenario})
        assert validation.status_code == 200
        assert validation.json()["valid"] is False
        assert validation.json()["schema_errors"] == [f"Scenario workflow: {expected}"]


def test_deleting_saved_scenario_preserves_generated_dataset(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        saved = client.post("/api/scenarios", json=_scenario())
        assert saved.status_code == 201
        scenario_id = saved.json()["id"]
        job = client.post("/api/generate", json={"scenario": _scenario(), "scenarioId": scenario_id})
        assert job.status_code == 200
        job_id = job.json()["jobId"]

        deleted = client.delete(f"/api/scenarios/{scenario_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/scenarios/{scenario_id}").status_code == 404
        assert all(item["id"] != scenario_id for item in client.get("/api/scenarios").json())
        assert client.get(f"/api/status/{job_id}").json()["status"] == "complete"
        assert client.get(f"/api/download/{job_id}").status_code == 200


def test_deleting_chat_removes_its_draft_and_preserves_saved_scenario(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        saved = client.post("/api/scenarios", json=_scenario())
        scenario_id = saved.json()["id"]
        conversation = client.post("/api/conversations", json={"title": "Insurance claims chat"})
        assert conversation.status_code == 201
        conversation_id = conversation.json()["id"]
        updated = client.put(f"/api/conversations/{conversation_id}", json={
            "messages": [{"id": "reply", "role": "assistant", "parts": [{"type": "text", "text": "A draft"}]}],
            "scenarioDraft": _scenario(),
        })
        assert updated.status_code == 200

        deleted = client.delete(f"/api/conversations/{conversation_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
        assert all(item["id"] != conversation_id for item in client.get("/api/conversations").json())
        assert client.get(f"/api/scenarios/{scenario_id}").status_code == 200


def test_cancel_generation_job(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        # Do not run synchronously, leave in queued state
        monkeypatch.setattr(generation, "publish_job", lambda job_id, **kw: True)
        job = client.post("/api/generate", json={"scenario": _scenario()})
        assert job.status_code == 200
        job_id = job.json()["jobId"]

        cancel_resp = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        status = client.get(f"/api/jobs/{job_id}").json()
        assert status["status"] == "cancelled"
        assert "cancelled" in status["currentStage"].lower()

        # Cannot cancel an already cancelled job
        repeat = client.post(f"/api/jobs/{job_id}/cancel")
        assert repeat.status_code == 400


def _document_scenario(output: str = "html", name: str = "Note") -> dict:
    scenario = _scenario(record_count=3)
    scenario["workflow"].append(name)
    scenario["schemas"][name] = {
        "__template__": True,
        "__template_source__": "<p>{{ name }}</p><p>{{ name.__class__.__mro__ }}</p>",
        "__output_file_type__": output,
        "__foreign_keys__": {"parent_id": "Parent.parent_id", "name": "Parent.name"},
        "note_id": {"type": "integer", "constraints": {"primary_key": True}},
        "parent_id": "foreign_key",
        "name": "text",
    }
    return scenario


def test_linked_documents_are_generated_previewed_and_downloaded(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        job_id = client.post("/api/generate", json={"scenario": _document_scenario()}).json()["jobId"]
        assert client.get(f"/api/status/{job_id}").json()["status"] == "complete"

        documents = client.get(f"/api/preview/{job_id}").json()["documents"]
        assert [doc["name"] for doc in documents] == ["document_1.html", "document_2.html", "document_3.html"]
        parent_names = {row["name"] for row in client.get(f"/api/preview/{job_id}").json()["tables"]["parent"]["rows"]}

        inline = client.get(documents[0]["url"])
        assert inline.status_code == 200
        assert inline.headers["content-security-policy"] == "sandbox"
        assert inline.headers["content-disposition"].startswith("inline")
        assert any(name in inline.text for name in parent_names)  # linked to its parent row
        assert "<class" not in inline.text  # sandboxed Jinja
        assert client.get(documents[0]["url"] + "?download=true").headers["content-disposition"].startswith("attachment")
        assert client.get(f"/api/documents/{job_id}/..").status_code == 404
        assert client.get(f"/api/documents/{job_id}/Note/..%2F..%2Fjob.db").status_code == 404

        for format in ("csv", "json"):
            with zipfile.ZipFile(io.BytesIO(client.get(f"/api/download/{job_id}?format={format}").content)) as archive:
                assert "Note/document_3.html" in archive.namelist()

        other = User(id=uuid.uuid4(), email="other@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        app.dependency_overrides[require_authenticated] = lambda: other
        assert client.get(documents[0]["url"]).status_code == 404
        assert client.get(f"/api/preview/{job_id}").status_code == 404


def test_linked_pdf_documents_render_with_weasyprint(monkeypatch, tmp_path):
    try:
        import weasyprint  # noqa: F401
    except OSError:
        pytest.skip("WeasyPrint system libraries (pango) are not installed")
    with _client(monkeypatch, tmp_path) as client:
        job_id = client.post("/api/generate", json={"scenario": _document_scenario("pdf")}).json()["jobId"]
        documents = client.get(f"/api/preview/{job_id}").json()["documents"]
        assert [doc["name"] for doc in documents] == ["document_1.pdf", "document_2.pdf", "document_3.pdf"]
        pdf = client.get(documents[0]["url"])
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content.startswith(b"%PDF")


def test_document_table_names_and_outputs_are_validated(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as client:
        for scenario in (_document_scenario(name="../Note"), _document_scenario(output="docx")):
            assert client.post("/api/generate", json={"scenario": scenario}).status_code == 422
