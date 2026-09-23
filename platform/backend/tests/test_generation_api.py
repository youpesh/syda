import csv
import io
import zipfile

from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import database
from app.api import generation
from app.main import app
from app.models.entities import JobRecord


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
    monkeypatch.setattr(generation, "SessionLocal", testing_session)
    monkeypatch.setattr(generation, "DATA_DIR", tmp_path / "jobs")
    for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    app.dependency_overrides.clear()
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
        }

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
        assert len(report.json()["metrics"]) == 6

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
            "note": "No LLM provider key is configured; generation will use the local fallback.",
        }


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
        monkeypatch.setenv("OPENAI_API_KEY", "estimate-only")
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
                )
            )
            session.commit()

        status = client.get("/api/status/still-running")
        assert status.status_code == 200
        assert status.json()["status"] == "generating"

        download = client.get("/api/download/still-running")
        assert download.status_code == 409
