from __future__ import annotations

import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text, Uuid, create_engine, func
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "generation_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    input_path: Mapped[str] = mapped_column(Text)
    output_path: Mapped[str] = mapped_column(Text)
    input_sha256: Mapped[str] = mapped_column(String(64))
    settings: Mapped[dict[str, Any]] = mapped_column(JSON)
    generated_articles: Mapped[int | None]
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("Не задана обязательная переменная DATABASE_URL")
        connect_args = (
            {"check_same_thread": False} if database_url.startswith("sqlite") else {}
        )
        _engine = create_engine(
            database_url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return _engine


def get_session() -> Session:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
        )
    return _session_factory()


def create_tables() -> None:
    Base.metadata.create_all(get_engine())
