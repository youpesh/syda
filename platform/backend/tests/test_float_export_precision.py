"""Generated unique floats must survive the production CSV-to-database path."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import create_engine

from app.api import connections
from app.models.entities import JobRecord
from app.services.generation import _LocalTableGenerator


def test_connection_export_preserves_unique_adjacent_floats(tmp_path, monkeypatch):
    schemas = {
        "Measurement": {
            "reading": {
                "type": "float",
                "constraints": {"unique": True, "min": 0.001, "max": 0.009},
            }
        }
    }
    frame = _LocalTableGenerator().generate_for_schemas(
        schemas, sample_sizes={"Measurement": 10}
    )["Measurement"]
    expected = sorted(frame["reading"].tolist())
    assert len(set(expected)) == 10

    user = SimpleNamespace(id=uuid.uuid4())
    job = JobRecord(
        id="float-precision-job",
        user_id=user.id,
        status="complete",
        scenario={"schemas": schemas},
    )
    database = MagicMock()
    database.query.return_value.filter.return_value.first.return_value = job
    directory = tmp_path / job.id
    directory.mkdir()
    frame.to_csv(directory / "measurement.csv", index=False)

    engine = create_engine(f"sqlite:///{tmp_path / 'destination.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            'CREATE TABLE "Measurement" (reading REAL NOT NULL UNIQUE)'
        )
    monkeypatch.setattr(connections, "DATA_DIR", tmp_path)
    monkeypatch.setattr(connections, "connection_engine", lambda *args, **kwargs: engine)
    request = connections.ConnectionExportRequest(
        dialect="PostgreSQL",
        host="localhost",
        port=5432,
        database="test",
        username="user",
        password="test-only",
        jobId=job.id,
    )

    try:
        result = connections.export_connection(request, db=database, user=user)
        assert result["totalRows"] == 10
        assert result["tablesExported"] == ["Measurement"]
        with engine.connect() as connection:
            actual = connection.exec_driver_sql(
                'SELECT reading FROM "Measurement" ORDER BY reading'
            ).scalars().all()
        assert actual == expected
    finally:
        engine.dispose()
