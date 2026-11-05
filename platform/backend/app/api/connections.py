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
from syda.db_schema_loader import DatabaseSchemaLoader
from .auth import require_authenticated
from ..database import get_db
from ..models.entities import ConnectionProfile, JobRecord
from ..models.user import User
from ..job_queue import DATA_DIR, output_directory

router = APIRouter(prefix="/connections", tags=["Connections"], dependencies=[Depends(require_authenticated)])

class ConnectionFields(BaseModel):
    dialect: Literal["PostgreSQL", "MySQL", "Oracle"]
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
    driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql", "Oracle": "oracle+oracledb"}[request.dialect]
    options = {"connect_timeout": 10} if request.dialect != "Oracle" else {"tcp_connect_timeout": 10}
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
        if len(names) > 200:
            raise HTTPException(422, "This database has more than 200 tables. Use a database account scoped to the tables needed for this scenario.")
        schemas = DatabaseSchemaLoader(engine).load_schemas(names)
        return {"schemas": schemas, "tableCount": len(schemas)}
    except (ImportError, ModuleNotFoundError):
        raise HTTPException(503, f"The {request.dialect} driver is not installed on the server. Ask the administrator to install the matching database driver, then retry.") from None
    except SQLAlchemyError:
        # Driver exceptions can contain connection strings and credentials.
        raise HTTPException(400, "Could not read the database schema. Check the host, port, database, credentials, network access, and schema permissions, then retry.") from None
    finally:
        if engine is not None:
            engine.dispose()

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
    for csv_file in csv_files:
        table_name = csv_file.stem
        if request.table_filter and table_name.lower() not in [t.lower() for t in request.table_filter]:
            continue
        try:
            data[table_name] = pd.read_csv(csv_file)
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
    except ValueError as error:
        # pandas raises this for if_exists="fail" when the table exists; the message names only the table.
        raise HTTPException(409, str(error)) from None
    except SQLAlchemyError:
        # Driver errors can echo credentials and row values.
        raise HTTPException(400, "Could not export dataset to database. Check the connection, write permissions, and that existing tables match the generated columns.") from None
    finally:
        if engine is not None:
            engine.dispose()
