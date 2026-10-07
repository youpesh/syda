"""Authenticated, schema-only database inspection. Credentials are never persisted."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from syda.db_schema_loader import DatabaseSchemaLoader
from .auth import require_authenticated

router = APIRouter(prefix="/connections", tags=["Connections"], dependencies=[Depends(require_authenticated)])

class ConnectionRequest(BaseModel):
    dialect: Literal["PostgreSQL", "MySQL", "Oracle"]
    host: str = Field(min_length=1, max_length=255, pattern=r"^[a-zA-Z0-9._:\-]+$")
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr

def connection_engine(request: ConnectionRequest):
    driver = {"PostgreSQL": "postgresql+psycopg2", "MySQL": "mysql+pymysql", "Oracle": "oracle+oracledb"}[request.dialect]
    options = {"connect_timeout": 10} if request.dialect != "Oracle" else {"tcp_connect_timeout": 10}
    if request.dialect == "PostgreSQL":
        options["options"] = "-c statement_timeout=15000 -c default_transaction_read_only=on"
    if request.dialect == "MySQL":
        options.update(read_timeout=15, write_timeout=15)
    url = URL.create(driver, username=request.username, password=request.password.get_secret_value(), host=request.host, port=request.port, database=request.database if request.dialect != "Oracle" else None, query={"service_name": request.database} if request.dialect == "Oracle" else {})
    return create_engine(url, connect_args=options, poolclass=NullPool)

@router.post("/inspect")
def inspect_connection(request: ConnectionRequest):
    engine = None
    try:
        engine = connection_engine(request)
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
