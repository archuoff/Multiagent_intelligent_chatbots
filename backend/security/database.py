"""Runtime PostgreSQL engine and FastAPI session dependency.

The connection string is obtained only from ``DATABASE_URL`` at runtime. It is
not embedded in source code, YAML, or generated ingestion artifacts.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    """Creates a PostgreSQL session factory from an explicit URL or runtime environment."""
    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required to use PostgreSQL authentication.")
    engine = create_engine(url, pool_pre_ping=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def get_session() -> Generator[Session, None, None]:
    """Provides one database session per FastAPI request and always closes it."""
    try:
        session = create_session_factory()()
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    try:
        yield session
    finally:
        session.close()
