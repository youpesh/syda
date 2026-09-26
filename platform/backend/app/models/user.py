from fastapi_users_db_sqlalchemy import SQLAlchemyBaseUserTableUUID
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(SQLAlchemyBaseUserTableUUID, Base):
    """FastAPI Users identity shared by Syda workspace routes."""

    display_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
