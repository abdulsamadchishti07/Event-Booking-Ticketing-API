from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# Construct the SQLAlchemy database engine
engine = create_engine(settings.database_url)

# Factory for creating thread-safe database sessions
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy ORM models."""
    pass


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that provides a transactional database session per request
    and guarantees that the session is closed when the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()