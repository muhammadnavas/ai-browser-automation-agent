"""
tests/test_my_context_phase1.py
-------------------------------
Phase 1 tests for My Context:
- PostgreSQL database engine, connection pooling, and lifecycle
- Non-fatal database failure handling (503 response without crashing the application)
- UserProfile CRUD operations with SQLAlchemy 2.x async models
- Ownership and user isolation enforcement
- Session authentication and CSRF protection on My Context API endpoints
"""

import os
import ssl
import pytest
import pytest_asyncio
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from backend.main import app
from backend.db.database import (
    init_db,
    close_db,
    is_db_available,
    normalize_database_url,
    get_safe_database_url,
    get_db_session,
    extract_ssl_mode,
    get_ssl_context_for_mode,
    get_asyncpg_connect_args,
)
from backend.db.models import Base, UserProfile, GUID
from sqlalchemy.dialects.postgresql import dialect as postgresql_dialect
import uuid
from backend.services.profile_service import profile_service
from backend.security.auth import (
    session_manager,
    session_rate_limiter,
    SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
)


@pytest_asyncio.fixture
async def async_test_db(tmp_path):
    """Provides an isolated in-memory or temporary SQLite async database for testing."""
    db_file = tmp_path / "test_context.sqlite3"
    test_url = f"sqlite+aiosqlite:///{db_file}"
    test_engine = create_async_engine(test_url, echo=False)

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session

    await test_engine.dispose()


class TestDatabaseLifecycleAndPool:
    """Verifies connection URL normalization, masking, pool parameters, and lifecycle."""

    def test_normalize_database_url(self):
        assert normalize_database_url("postgresql://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"
        assert normalize_database_url("postgres://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"
        assert normalize_database_url("postgresql+asyncpg://user:pass@localhost:5432/db") == "postgresql+asyncpg://user:pass@localhost:5432/db"
        assert normalize_database_url("sqlite+aiosqlite:///file.db") == "sqlite+aiosqlite:///file.db"
        # Neon PostgreSQL URL normalization (sslmode=require -> ssl=require for asyncpg)
        neon_url = "postgresql://user:secret@ep-cool-fog-123.us-east-2.aws.neon.tech/neondb?sslmode=require"
        normalized = normalize_database_url(neon_url)
        assert "postgresql+asyncpg://" in normalized
        assert "ssl=require" in normalized
        assert "sslmode=" not in normalized

    def test_normalize_database_url_strips_unsupported_params(self):
        """Verify that channel_binding and gssencmode are stripped while preserving supported parameters and SSL."""
        # Neon connection strings with channel_binding=require
        neon_url = "postgresql://user:secret@ep-cool-fog-123.us-east-2.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
        normalized = normalize_database_url(neon_url)
        assert "postgresql+asyncpg://" in normalized
        assert "ssl=require" in normalized
        assert "channel_binding" not in normalized
        assert "sslmode" not in normalized

        # URL with both channel_binding and gssencmode, plus supported parameter
        full_url = "postgresql://user:pass@host:5432/db?sslmode=require&channel_binding=require&gssencmode=disable&application_name=my_agent"
        norm_full = normalize_database_url(full_url)
        assert "channel_binding" not in norm_full
        assert "gssencmode" not in norm_full
        assert "ssl=require" in norm_full
        assert "application_name=my_agent" in norm_full

        # Credentials with special characters and URL fragment preservation
        frag_url = "postgresql://user%40acc:p%23ss@host:5432/db?sslmode=require&channel_binding=require#myfrag"
        norm_frag = normalize_database_url(frag_url)
        assert "user%40acc:p%23ss@host:5432/db" in norm_frag
        assert "channel_binding" not in norm_frag
        assert norm_frag.endswith("#myfrag")

        # Avoid duplicate SSL parameters if both ssl and sslmode are present
        dup_url = "postgresql://user:pass@host:5432/db?ssl=require&sslmode=require"
        norm_dup = normalize_database_url(dup_url)
        assert norm_dup.count("ssl=require") == 1
        assert "sslmode" not in norm_dup

    def test_neon_ssl_mode_preservation_and_guarantees(self):
        """Verifies that verify-full and verify-ca are never downgraded to plain require."""
        # 1. extract_ssl_mode correctly identifies parameters
        assert extract_ssl_mode("postgresql://u:p@h/d?sslmode=verify-full") == "verify-full"
        assert extract_ssl_mode("postgresql://u:p@h/d?sslmode=verify-ca") == "verify-ca"
        assert extract_ssl_mode("postgresql://u:p@h/d?sslmode=require") == "require"
        assert extract_ssl_mode("postgresql://u:p@h/d?ssl=verify-full") == "verify-full"
        assert extract_ssl_mode("postgresql://u:p@h/d?ssl=require") == "require"
        assert extract_ssl_mode("postgresql://u:p@h/d") is None

        # 2. normalize_database_url preserves the full mode name in query
        vf_norm = normalize_database_url("postgresql://u:p@h/d?sslmode=verify-full")
        assert "ssl=verify-full" in vf_norm
        assert "sslmode=" not in vf_norm

        vca_norm = normalize_database_url("postgresql://u:p@h/d?sslmode=verify-ca")
        assert "ssl=verify-ca" in vca_norm
        assert "sslmode=" not in vca_norm

        req_norm = normalize_database_url("postgresql://u:p@h/d?sslmode=require")
        assert "ssl=require" in req_norm
        assert "sslmode=" not in req_norm

        # 3. get_ssl_context_for_mode creates appropriate SSLContext
        # verify-full: check_hostname=True, verify_mode=CERT_REQUIRED
        ctx_vf = get_ssl_context_for_mode("verify-full")
        assert isinstance(ctx_vf, ssl.SSLContext)
        assert ctx_vf.check_hostname is True
        assert ctx_vf.verify_mode == ssl.CERT_REQUIRED

        # verify-ca: check_hostname=False, verify_mode=CERT_REQUIRED
        ctx_vca = get_ssl_context_for_mode("verify-ca")
        assert isinstance(ctx_vca, ssl.SSLContext)
        assert ctx_vca.check_hostname is False
        assert ctx_vca.verify_mode == ssl.CERT_REQUIRED

        # require and disable return appropriate constants for asyncpg
        assert get_ssl_context_for_mode("require") == "require"
        assert get_ssl_context_for_mode("disable") is False

        # 4. get_asyncpg_connect_args constructs valid connect_args
        args_vf = get_asyncpg_connect_args("postgresql+asyncpg://u:p@h/d?sslmode=verify-full", pool_timeout=25)
        assert "ssl" in args_vf
        assert isinstance(args_vf["ssl"], ssl.SSLContext)
        assert args_vf["ssl"].check_hostname is True
        assert args_vf["timeout"] == 25

        args_req = get_asyncpg_connect_args("postgresql+asyncpg://u:p@h/d?sslmode=require", pool_timeout=15)
        assert args_req["ssl"] == "require"
        assert args_req["timeout"] == 15

    def test_safe_database_url_masks_credentials(self):
        url = "postgresql+asyncpg://admin_user:SuperSecretPass123!@db.host.internal:5432/production_db"
        safe = get_safe_database_url(url)
        assert "SuperSecretPass123!" not in safe
        assert "[REDACTED]" in safe
        assert "admin_user" in safe
        assert "db.host.internal" in safe

    @pytest.mark.asyncio
    async def test_init_db_disabled_when_empty_url(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=False):
            success = await init_db(database_url="")
            assert success is False
            assert is_db_available() is False

    @pytest.mark.asyncio
    async def test_init_db_failure_handles_gracefully_non_fatal(self):
        # Unreachable host or connection error should fail non-fatally without raising unhandled exceptions
        with patch("sqlalchemy.ext.asyncio.AsyncEngine.connect", side_effect=OSError("Connection refused")):
            success = await init_db(database_url="postgresql+asyncpg://invalid_user:pass@127.0.0.1:54329/nonexistent_db")
            assert success is False
            assert is_db_available() is False

    @pytest.mark.asyncio
    async def test_async_engine_and_pool_lifecycle(self, tmp_path):
        db_path = tmp_path / "lifecycle.sqlite3"
        test_url = f"sqlite+aiosqlite:///{db_path}"
        success = await init_db(database_url=test_url, create_tables=True)
        assert success is True
        assert is_db_available() is True

        await close_db()
        assert is_db_available() is False

    @pytest.mark.asyncio
    async def test_neon_database_init_and_session_query(self):
        """Verify that a Neon PostgreSQL connection string with channel_binding initializes and queries successfully."""
        from sqlalchemy import text
        import backend.db.database as db_module

        raw_url = os.getenv("DATABASE_URL")
        if not raw_url or not raw_url.strip():
            pytest.skip("DATABASE_URL not set in environment")

        try:
            success = await init_db(database_url=raw_url)
            assert success is True
            assert is_db_available() is True
            assert db_module.async_session_factory is not None

            async with db_module.async_session_factory() as session:
                res = await session.execute(text("SELECT 1"))
                assert res.scalar() == 1
        finally:
            await close_db()
            assert is_db_available() is False


class TestProfileService:
    """Verifies UserProfile CRUD operations and multi-tenant ownership isolation."""

    @pytest.mark.asyncio
    async def test_create_and_get_profile(self, async_test_db):
        user_id = "user-alice-123"
        profile_data = {
            "personal_details": {
                "full_name": "Alice Developer",
                "email": "alice@example.com",
                "phone": "+1-555-0199",
                "location": "San Francisco, CA",
            },
            "education": {
                "college": "Stanford University",
                "degree": "B.S. Computer Science",
                "graduation_year": 2024,
                "cgpa": "3.9",
            },
            "skills_and_experience": {
                "skills": ["Python", "FastAPI", "React", "PostgreSQL"],
                "experience": "2 years software engineering internship",
            },
            "career_interests": "AI Engineering and Automation",
            "job_preferences": "Remote, Full-time",
            "custom_instructions": "Focus on high-impact AI startup positions.",
        }

        created = await profile_service.upsert_profile(async_test_db, user_id, profile_data)
        assert created.user_id == user_id
        assert created.profile_data["personal_details"]["full_name"] == "Alice Developer"

        # Fetch profile dictionary
        profile_dict = await profile_service.get_profile_dict(async_test_db, user_id)
        assert profile_dict["exists"] is True
        assert profile_dict["user_id"] == user_id
        assert profile_dict["profile_data"]["personal_details"]["email"] == "alice@example.com"
        assert profile_dict["profile_data"]["education"]["college"] == "Stanford University"

    @pytest.mark.asyncio
    async def test_update_profile_merges_data(self, async_test_db):
        user_id = "user-bob-456"
        initial_data = {
            "personal_details": {"full_name": "Bob Smith", "location": "London"},
            "career_interests": "Frontend Development",
        }
        await profile_service.upsert_profile(async_test_db, user_id, initial_data)

        # Update career interests and add education
        update_data = {
            "career_interests": "Full Stack Architecture",
            "education": {"degree": "M.S. Software Engineering"},
        }
        updated = await profile_service.upsert_profile(async_test_db, user_id, update_data)
        assert updated.profile_data["career_interests"] == "Full Stack Architecture"
        assert updated.profile_data["personal_details"]["full_name"] == "Bob Smith"
        assert updated.profile_data["education"]["degree"] == "M.S. Software Engineering"

    @pytest.mark.asyncio
    async def test_delete_profile(self, async_test_db):
        user_id = "user-charlie-789"
        await profile_service.upsert_profile(async_test_db, user_id, {"full_name": "Charlie"})
        
        # Verify exists
        assert (await profile_service.get_profile(async_test_db, user_id)) is not None

        # Delete
        deleted = await profile_service.delete_profile(async_test_db, user_id)
        assert deleted is True

        # Verify gone
        assert (await profile_service.get_profile(async_test_db, user_id)) is None

        # Deleting again returns False
        deleted_again = await profile_service.delete_profile(async_test_db, user_id)
        assert deleted_again is False

    @pytest.mark.asyncio
    async def test_user_isolation_prevent_cross_tenant_leakage(self, async_test_db):
        """User A must never be able to access, modify, or overwrite User B's profile."""
        user_a = "user_alpha"
        user_b = "user_beta"

        await profile_service.upsert_profile(async_test_db, user_a, {"secret": "Alice Secret"})
        await profile_service.upsert_profile(async_test_db, user_b, {"secret": "Bob Secret"})

        profile_a = await profile_service.get_profile_dict(async_test_db, user_a)
        profile_b = await profile_service.get_profile_dict(async_test_db, user_b)

        assert profile_a["profile_data"]["secret"] == "Alice Secret"
        assert profile_b["profile_data"]["secret"] == "Bob Secret"
        assert profile_a["user_id"] != profile_b["user_id"]

    @pytest.mark.asyncio
    async def test_profile_validation_rejects_non_dict(self, async_test_db):
        with pytest.raises(ValueError, match="profile_data must be a dictionary"):
            await profile_service.upsert_profile(async_test_db, "user-1", "not a dict")


class TestContextAPIEndpoints:
    """Verifies API endpoints, session ownership, CSRF, and 503 fallback."""

    @pytest.fixture
    def auth_client_with_db(self, tmp_path):
        session_rate_limiter.reset()
        db_path = tmp_path / "api_test.sqlite3"
        test_url = f"sqlite+aiosqlite:///{db_path}"

        import asyncio
        asyncio.run(init_db(database_url=test_url, create_tables=True))

        client = TestClient(app, base_url="http://127.0.0.1:8000")
        resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 200
        data = resp.json()

        yield {
            "client": client,
            "session_id": data["session_id"],
            "csrf_token": data["csrf_token"],
        }

        asyncio.run(close_db())

    def test_unauthenticated_profile_request_rejected_401(self):
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        resp = client.get("/api/context/profile")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json().get("detail", "")

    def test_profile_create_and_read_api(self, auth_client_with_db):
        client = auth_client_with_db["client"]
        csrf = auth_client_with_db["csrf_token"]

        # Initial get should return empty profile
        get_res = client.get("/api/context/profile")
        assert get_res.status_code == 200
        assert get_res.json()["exists"] is False

        # Post profile
        payload = {
            "profile_data": {
                "personal_details": {
                    "full_name": "Sarah Connor",
                    "email": "sarah@resistance.org",
                    "phone": "555-0100",
                    "location": "Los Angeles, CA",
                },
                "skills_and_experience": {
                    "skills": ["Leadership", "Tactical Automation", "Python"],
                },
            }
        }
        post_res = client.post(
            "/api/context/profile",
            json=payload,
            headers={CSRF_HEADER_NAME: csrf},
        )
        assert post_res.status_code == 200
        saved = post_res.json()
        assert saved["exists"] is True
        assert saved["profile_data"]["personal_details"]["full_name"] == "Sarah Connor"

        # Verify get returns saved data
        verify_res = client.get("/api/context/profile")
        assert verify_res.status_code == 200
        assert verify_res.json()["profile_data"]["personal_details"]["full_name"] == "Sarah Connor"

    def test_profile_csrf_protection_on_mutations(self, auth_client_with_db):
        """Mutating POST/PUT/DELETE requests without CSRF token must be rejected with 403."""
        client = auth_client_with_db["client"]

        resp = client.post(
            "/api/context/profile",
            json={"profile_data": {"test": "val"}},
            # Missing CSRF Header
        )
        assert resp.status_code == 403
        assert "CSRF validation failed" in resp.json().get("detail", "")

    def test_client_supplied_user_id_is_ignored(self, auth_client_with_db):
        """Even if malicious payload includes a user_id, backend binds strictly to authenticated session."""
        client = auth_client_with_db["client"]
        csrf = auth_client_with_db["csrf_token"]

        payload = {
            "profile_data": {
                "user_id": "malicious_spoofed_user",
                "personal_details": {"full_name": "Target"},
            }
        }
        res = client.post(
            "/api/context/profile",
            json=payload,
            headers={CSRF_HEADER_NAME: csrf},
        )
        assert res.status_code == 200
        # The stored user_id must remain the authenticated user, not the spoofed string
        session = session_manager.get_session(auth_client_with_db["session_id"])
        assert res.json()["user_id"] == session.user_id
        assert res.json()["user_id"] != "malicious_spoofed_user"

    def test_profile_endpoint_returns_503_when_db_offline(self):
        """When database is offline, endpoint cleanly returns 503 without crashing server."""
        import asyncio
        asyncio.run(close_db())

        session_rate_limiter.reset()
        client = TestClient(app, base_url="http://127.0.0.1:8000")
        resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 200
        csrf = resp.json()["csrf_token"]

        res = client.get("/api/context/profile")
        assert res.status_code == 503
        assert "PostgreSQL database service is currently unavailable" in res.json().get("detail", "")

    def test_app_user_id_developer_partitioning(self):
        """When APP_USER_ID is set in environment, new sessions bind to that developer ID."""
        with patch.dict(os.environ, {"APP_USER_ID": "deeksha_developer"}, clear=False):
            session = session_manager.create_session(client_ip="127.0.0.1")
            assert session.user_id == "deeksha_developer"

    def test_explicit_user_id_precedence_over_app_user_id(self):
        """APP_USER_ID can NEVER override an explicit authenticated user identity."""
        with patch.dict(os.environ, {"APP_USER_ID": "deeksha_developer"}, clear=False):
            session = session_manager.create_session(client_ip="127.0.0.1", user_id="real_authenticated_alice")
            assert session.user_id == "real_authenticated_alice"

    def test_app_user_id_ignored_in_production(self):
        """In production environment, APP_USER_ID and dev fallbacks must be ignored."""
        with patch.dict(os.environ, {"ENVIRONMENT": "production", "APP_USER_ID": "sneaky_dev"}, clear=False):
            session = session_manager.create_session(client_ip="127.0.0.1")
            assert session.user_id == "default_user"

    def test_app_user_id_unset_in_dev_derives_machine_user_with_warning(self, caplog):
        """When APP_USER_ID is unset in local dev, backend derives a dev machine username fallback and logs a warning."""
        import logging
        with patch.dict(os.environ, {"APP_USER_ID": "", "ENVIRONMENT": "development"}, clear=False):
            with caplog.at_level(logging.WARNING):
                session = session_manager.create_session(client_ip="127.0.0.1")
                assert session.user_id.startswith("dev_")
                assert any("APP_USER_ID is not configured in .env" in record.message for record in caplog.records)



class TestPostgreSQLModelTypeBindings:
    def test_guid_keeps_uuid_objects_for_native_postgresql_uuid(self):
        value = uuid.uuid4()
        bound = GUID().process_bind_param(value, postgresql_dialect())
        assert bound == value
        assert isinstance(bound, uuid.UUID)

    def test_guid_normalizes_string_values_for_native_postgresql_uuid(self):
        value = uuid.uuid4()
        bound = GUID().process_bind_param(str(value), postgresql_dialect())
        assert bound == value
        assert isinstance(bound, uuid.UUID)
