"""
backend/db/database.py
----------------------
Asynchronous connection pooling and database lifecycle management for PostgreSQL
using SQLAlchemy 2.x and asyncpg.
"""

import os
import ssl
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, Optional, Union
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine,
)
from sqlalchemy import text
from fastapi import HTTPException, status

logger = logging.getLogger("backend.db")

# Global engine and session factory
engine: Optional[AsyncEngine] = None
async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None
_db_available: bool = False


def extract_ssl_mode(url: str) -> Optional[str]:
    """Extract SSL mode from the database URL's query parameters."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # Check sslmode first, then ssl
        raw_mode = params.get("sslmode", params.get("ssl", [None]))[0]
        if raw_mode:
            clean = raw_mode.strip().lower().replace("_", "-")
            return clean
    except Exception:
        pass
    return None


def get_ssl_context_for_mode(mode: Optional[str]) -> Optional[Union[ssl.SSLContext, str, bool]]:
    """Create an appropriate ssl.SSLContext preserving the security guarantees of the requested SSL mode.
    
    Guarantees:
    - 'verify-full': Enforces server certificate validation against trusted CAs AND validates hostname.
    - 'verify-ca': Enforces server certificate validation against trusted CAs WITHOUT hostname checking.
    - 'require': Enforces TLS transport encryption without certificate validation.
    - 'prefer': Attempts TLS encryption, falling back if unsupported.
    - 'disable': Plain unencrypted TCP connection.
    """
    if not mode:
        return None

    clean = mode.strip().lower().replace("_", "-")
    if clean in ("disable", "none", "off", "false"):
        return False
    elif clean == "verify-full":
        ctx = ssl.create_default_context()
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    elif clean == "verify-ca":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        return ctx
    elif clean in ("require", "true"):
        return "require"
    elif clean == "prefer":
        return "prefer"
    return None


def get_asyncpg_connect_args(url: str, pool_timeout: float = 30.0) -> Dict[str, Any]:
    """Build connect_args dictionary for asyncpg with Neon/PgBouncer compatibility and SSL handling."""
    connect_args: Dict[str, Any] = {
        # Disable statement cache for PgBouncer / Neon connection pooler compatibility
        "prepared_statement_cache_size": 0,
        "timeout": pool_timeout,
    }
    mode = extract_ssl_mode(url)
    if mode:
        ssl_obj = get_ssl_context_for_mode(mode)
        if ssl_obj is not None:
            connect_args["ssl"] = ssl_obj
    return connect_args


def normalize_database_url(raw_url: str) -> str:
    """Ensure the URL uses the asyncpg driver and compatible SSL parameters for asynchronous SQLAlchemy.
    
    Preserves sslmode intent without downgrading verification:
    - sslmode=verify-full -> ssl=verify-full
    - sslmode=verify-ca   -> ssl=verify-ca
    - sslmode=require     -> ssl=require
    - sslmode=prefer      -> ssl=prefer
    - sslmode=disable     -> ssl=disable

    Filters out connection parameters unsupported by asyncpg keyword arguments,
    such as channel_binding and gssencmode, while preserving all supported parameters.
    """
    if not raw_url:
        return ""
    url = raw_url.strip()
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    elif url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]

    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url

        params = parse_qs(parsed.query, keep_blank_values=True)

        # Convert sslmode parameter to asyncpg's ssl query parameter while strictly preserving the mode
        if "sslmode" in params:
            mode = params.pop("sslmode")[0]
            if "ssl" not in params:
                params["ssl"] = [mode]

        # Remove unsupported asyncpg connection keyword arguments
        for unsupported in ("channel_binding", "gssencmode"):
            params.pop(unsupported, None)

        clean_query = urlencode([(k, v) for k, vals in params.items() for v in vals])
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            clean_query,
            parsed.fragment,
        ))
    except Exception:
        return url


def get_safe_database_url(url: str) -> str:
    """Return URL with password masked for safe logging."""
    try:
        parsed = urlparse(url)
        if parsed.password:
            netloc = f"{parsed.username or ''}:[REDACTED]@{parsed.hostname or 'localhost'}:{parsed.port or 5432}"
            return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
    except Exception:
        pass
    return url


def get_database_url() -> Optional[str]:
    """Retrieve DATABASE_URL from environment."""
    url = os.getenv("DATABASE_URL")
    if not url or not url.strip():
        return None
    return normalize_database_url(url)


def is_db_available() -> bool:
    """Return True if the database engine is initialized and connection is verified."""
    global _db_available, engine
    return _db_available and engine is not None


async def init_db(database_url: Optional[str] = None, create_tables: bool = False) -> bool:
    """Initialize the asynchronous PostgreSQL engine and session factory.
    
    If the database is unreachable or unconfigured, logs a warning and leaves
    _db_available = False (non-fatal failure mode, so task manager remains operational).
    """
    global engine, async_session_factory, _db_available

    target_url = normalize_database_url(database_url) if database_url is not None else get_database_url()
    if not target_url or not target_url.strip():
        logger.info("DATABASE_URL not set; PostgreSQL persistence is disabled (cache/archive only).")
        _db_available = False
        return False

    pool_min = int(os.getenv("DB_POOL_MIN_SIZE", "2"))
    pool_max = int(os.getenv("DB_POOL_MAX_SIZE", "10"))
    pool_timeout = float(os.getenv("DB_POOL_TIMEOUT", "30.0"))

    is_sqlite = target_url.startswith("sqlite")

    try:
        engine_kwargs = {
            "echo": False,
        }
        if not is_sqlite:
            engine_kwargs.update({
                "pool_size": pool_min,
                "max_overflow": max(0, pool_max - pool_min),
                "pool_timeout": pool_timeout,
                "pool_pre_ping": True,
                "connect_args": get_asyncpg_connect_args(target_url, pool_timeout=pool_timeout),
            })

        engine = create_async_engine(target_url, **engine_kwargs)
        async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autocommit=False,
            autoflush=False,
        )

        # Quick connectivity verification with bounded timeout (allow up to 15s for serverless cold-starts)
        async with asyncio.timeout(min(pool_timeout, 15.0)):
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        if create_tables:
            from backend.db.models import Base
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        _db_available = True
        logger.info("PostgreSQL persistence initialized successfully: endpoint=%s", get_safe_database_url(target_url))
        return True

    except Exception as exc:
        _db_available = False
        logger.warning(
            "PostgreSQL database connection failed: endpoint=%s, error_type=%s (%s). "
            "My Context functionality will be disabled or return 503 while PostgreSQL is unavailable.",
            get_safe_database_url(target_url),
            type(exc).__name__,
            exc,
        )
        return False


async def close_db() -> None:
    """Dispose of the database engine and connection pool cleanly on shutdown."""
    global engine, async_session_factory, _db_available
    if engine is not None:
        try:
            await engine.dispose()
            logger.info("PostgreSQL database connection pool closed cleanly.")
        except Exception as exc:
            logger.warning("Error closing database engine: %s", exc)
        finally:
            engine = None
            async_session_factory = None
            _db_available = False


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency for obtaining an asynchronous database session.
    
    Raises HTTPException 503 if PostgreSQL is currently unavailable.
    """
    if not is_db_available() or async_session_factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PostgreSQL database service is currently unavailable. Please verify DATABASE_URL and Neon connection status.",
        )

    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

