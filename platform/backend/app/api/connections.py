"""Authenticated saved profiles, schema inspection and direct dataset export. Passwords are never persisted."""
import uuid
from typing import Literal, Optional, List
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool
from sqlglot.errors import SqlglotError
from syda.db_schema_loader import DatabaseSchemaLoader
from .auth import require_authenticated
from ..database import get_db
from ..models.entities import ConnectionProfile, JobRecord
from ..models.user import User
from ..job_queue import DATA_DIR, output_directory
from ..services.ddl_schema import schemas_from_ddl

router = APIRouter(prefix="/connections", tags=["Connections"], dependencies=[Depends(require_authenticated)])

Dialect = Literal["PostgreSQL", "MySQL", "Oracle", "SQL Server"]
MAX_TABLES = 200
TOO_MANY_TABLES = f"This schema has more than {MAX_TABLES} tables. Narrow it to the tables needed for this scenario."

class ConnectionFields(BaseModel):
    dialect: Dialect
    host: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._:\-]+$")
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)

class ConnectionRequest(ConnectionFields):
    password: SecretStr

class ConnectionProfileRequest(ConnectionFields):
    name: str = Field(min_length=1, max_length=120)

class ConnectionExportRequest(ConnectionRequest):
    job_id: str = Field(alias="jobId")
    if_exists: Literal["append", "replace", "fail"] = Field(default="append", alias="ifExists")
    table_filter: Optional[List[str]] = Field(default=None, alias="tableFilter")

@router.get("/profiles")
def list_profiles(db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    rows = db.query(ConnectionProfile).filter(ConnectionProfile.user_id == user.id).order_by(ConnectionProfile.created_at).all()
    return [row.to_dict() for row in rows]

@router.put("/profiles/{profile_id}")
def save_profile(profile_id: uuid.UUID, request: ConnectionProfileRequest, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    # ponytail: PUT upserts with a client-generated id, so one endpoint covers create and edit.
    row = db.get(ConnectionProfile, str(profile_id))
    if row and row.user_id != user.id:
        raise HTTPException(404, "Connection not found")
    if not row:
        row = ConnectionProfile(id=str(profile_id), user_id=user.id)
        db.add(row)
    for key, value in request.model_dump().items():
        setattr(row, key, value)
    db.commit()
    return row.to_dict()

@router.delete("/profiles/{profile_id}", status_code=204)
def delete_profile(profile_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_authenticated)):
    deleted = db.query(ConnectionProfile).filter(ConnectionProfile.id == str(profile_id), ConnectionProfile.user_id == user.id).delete()
    db.commit()
    if not deleted:
        raise HTTPException(404, "Connection not found")

def connection_engine(request: ConnectionRequest, read_only: bool = True):
    driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql", "Oracle": "oracle+oracledb", "SQL Server": "mssql+pymssql"}[request.dialect]
    options = {"Oracle": {"tcp_connect_timeout": 10}, "SQL Server": {"login_timeout": 10, "timeout": 15 if read_only else 60}}.get(request.dialect, {"connect_timeout": 10})
    if request.dialect == "PostgreSQL":
        timeout = 15000 if read_only else 60000
        options["options"] = f"-c statement_timeout={timeout}" + (" -c default_transaction_read_only=on" if read_only else "")
    if request.dialect == "MySQL":
        timeout = 15 if read_only else 60
        options.update(read_timeout=timeout, write_timeout=timeout)
    url = URL.create(driver, username=request.username, password=request.password.get_secret_value(), host=request.host, port=request.port, database=request.database if request.dialect != "Oracle" else None, query={"service_name": request.database} if request.dialect == "Oracle" else {})
    return create_engine(url, connect_args=options, poolclass=NullPool)

@router.post("/inspect")
def inspect_connection(request: ConnectionRequest):
    engine = None
    try:
        engine = connection_engine(request, read_only=True)
        names = inspect(engine).get_table_names()
        if len(names) > MAX_TABLES:
            raise HTTPException(422, TOO_MANY_TABLES + " Use a database account scoped to those tables.")
        schemas = DatabaseSchemaLoader(engine).load_schemas(names)
        return {"schemas": schemas, "tableCount": len(schemas)}
    except (ImportError, ModuleNotFoundError):
        raise HTTPException(503, f"The {request.dialect} driver is not installed on the server. Ask the administrator to install the matching database driver, then retry.") from None
    except (SQLAlchemyError, pd.errors.DatabaseError):
        # Driver exceptions can contain connection strings and credentials.
        raise HTTPException(400, "Could not read the database schema. Check the host, port, database, credentials, network access, and schema permissions, then retry.") from None
    finally:
        if engine is not None:
            engine.dispose()

class DdlRequest(BaseModel):
    dialect: Dialect
    ddl: str = Field(min_length=1, max_length=2_000_000)

@router.post("/ddl")
def parse_ddl(request: DdlRequest):
    """Read table structure from a pasted DDL script: no network path to the database needed."""
    try:
        schemas = schemas_from_ddl(request.ddl, request.dialect)
    except SqlglotError:
        raise HTTPException(422, f"Could not read this script as {request.dialect} SQL. Check the database type and that the script is complete (no cut-off quotes).") from None
    if not schemas:
        raise HTTPException(422, f"No CREATE TABLE statements found. Paste table DDL and check that the database type ({request.dialect}) matches the script.")
    if len(schemas) > MAX_TABLES:
        raise HTTPException(422, TOO_MANY_TABLES)
    return {"schemas": schemas, "tableCount": len(schemas)}

@router.post("/export")
def export_connection(
    request: ConnectionExportRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_authenticated),
):
    job = db.query(JobRecord).filter(JobRecord.id == request.job_id, JobRecord.user_id == user.id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "complete":
        raise HTTPException(409, "Dataset generation is not complete yet")

    job_dir = output_directory(job, DATA_DIR)
    csv_files = sorted(job_dir.glob("*.csv")) if job_dir.exists() else []
    if not csv_files:
        raise HTTPException(404, "Generated dataset files are unavailable")

    data = {}
    schemas = (job.scenario or {}).get("schemas", {})
    schema_names = {name.lower(): name for name in schemas}
    for csv_file in csv_files:
        table_name = schema_names.get(csv_file.stem.lower(), csv_file.stem)
        if request.table_filter and table_name.lower() not in [t.lower() for t in request.table_filter]:
            continue
        try:
            frame = pd.read_csv(csv_file)
            declared_fields = schemas.get(table_name, {})
            # Provenance belongs to the run, not the destination schema.
            internal_columns = [name for name in ("scenario_instance_id", "scenario_path")
                                if name not in declared_fields]
            data[table_name] = frame.drop(columns=internal_columns, errors="ignore")
        except Exception as e:
            raise HTTPException(500, f"Failed to load table '{table_name}': {e}") from e

    if not data:
        raise HTTPException(422, "No matching tables found to export")

    engine = None
    try:
        engine = connection_engine(request, read_only=False)
        loader = DatabaseSchemaLoader(engine)
        loader.write_to_database(data, if_exists=request.if_exists)
        return {
            "ok": True,
            "jobId": request.job_id,
            "tablesExported": list(data.keys()),
            "totalRows": sum(len(df) for df in data.values()),
        }
    except (ImportError, ModuleNotFoundError):
        raise HTTPException(503, f"The {request.dialect} driver is not installed on the server. Ask the administrator to install the matching database driver, then retry.") from None
    except (SQLAlchemyError, pd.errors.DatabaseError):
        # Driver errors can echo credentials, SQL statements, and row values.
        raise HTTPException(400, "Could not export dataset. Check target columns, constraints, and database permissions.") from None
    except ValueError as error:
        # Preserve the connection export API's conflict response for target mismatches.
        raise HTTPException(409, str(error)) from None
    finally:
        if engine is not None:
            engine.dispose()
