"""
backend/db
----------
PostgreSQL persistence layer and database models for My Context.
"""

from backend.db.database import (
    get_db_session,
    init_db,
    close_db,
    is_db_available,
    engine,
    async_session_factory,
)
from backend.db.models import Base, UserProfile, Document, Memory

__all__ = [
    "get_db_session",
    "init_db",
    "close_db",
    "is_db_available",
    "engine",
    "async_session_factory",
    "Base",
    "UserProfile",
    "Document",
    "Memory",
]

