"""
tests/test_issue2_auth.py
-------------------------
Regression test suite for Issue 2: Local Backend Authentication,
Session Management, CSRF Protection, Rate Limiting, Origin Check,
Bootstrap Token Authorization, CORS / Cross-Origin Bearer Auth (Vercel Compatibility),
and Reverse Proxy / Vite Header Handling.
"""

import os
import pytest
import time
from fastapi.testclient import TestClient
from backend.main import app
from backend.security.auth import (
    session_manager,
    session_rate_limiter,
    get_bootstrap_token,
    SessionManager,
    SESSION_COOKIE_NAME,
    CSRF_COOKIE_NAME,
    CSRF_HEADER_NAME,
)


@pytest.fixture
def client():
    session_rate_limiter.reset()
    return TestClient(app, base_url="http://127.0.0.1:8000")


class TestSessionManager:
    def test_create_and_retrieve_session(self):
        mgr = SessionManager()
        session = mgr.create_session(client_ip="127.0.0.1")
        assert session is not None
        assert session.session_id
        assert session.csrf_token
        assert session.is_loopback is True

        retrieved = mgr.get_session(session.session_id)
        assert retrieved is not None
        assert retrieved.session_id == session.session_id

    def test_session_expiration(self):
        mgr = SessionManager()
        session = mgr.create_session(client_ip="127.0.0.1")
        session.created_at = time.time() - 100000  # Expired
        assert mgr.get_session(session.session_id) is None

    def test_csrf_verification(self):
        mgr = SessionManager()
        session = mgr.create_session(client_ip="127.0.0.1")
        assert mgr.verify_csrf(session, session.csrf_token) is True
        assert mgr.verify_csrf(session, "wrong_token") is False
        assert mgr.verify_csrf(session, "") is False
        assert mgr.verify_csrf(session, None) is False

    def test_ws_ticket_lifecycle(self):
        mgr = SessionManager()
        session = mgr.create_session(client_ip="127.0.0.1")
        ticket = mgr.create_ws_ticket(session.session_id, task_id="task-123")
        assert ticket.startswith("wst_")

        # Wrong task id mismatch
        consumed_mismatch = mgr.consume_ws_ticket(ticket, expected_task_id="other-task")
        assert consumed_mismatch is None

        # Correct task id matches and consumes ticket
        consumed = mgr.consume_ws_ticket(ticket, expected_task_id="task-123")
        assert consumed is not None
        assert consumed.session_id == session.session_id

        # Ticket is single-use: second consumption must fail
        assert mgr.consume_ws_ticket(ticket, expected_task_id="task-123") is None


class TestAuthEndpointsAndMiddleware:
    def test_unauthenticated_api_request_returns_401(self, client):
        resp = client.get("/api/tasks/history")
        assert resp.status_code == 401
        assert "Authentication required" in resp.json().get("detail", "")

    def test_cors_headers_present_on_unauthenticated_responses(self, client):
        """401 responses must include CORS headers so browsers do not treat them as CORS errors."""
        resp = client.get("/api/tasks", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 401
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert resp.headers.get("access-control-allow-credentials") == "true"

    def test_transparent_session_creation(self, client):
        resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "authenticated"
        assert "session_id" in data
        assert "csrf_token" in data
        assert SESSION_COOKIE_NAME in resp.cookies
        assert CSRF_COOKIE_NAME in resp.cookies

    def test_untrusted_origin_cannot_mint_session(self, client):
        """Cross-origin requests from unauthorized web domains must be rejected."""
        resp = client.post(
            "/api/auth/session",
            headers={"Origin": "http://evil.com"},
        )
        assert resp.status_code == 403
        assert "forbidden" in resp.json().get("detail", "").lower()

    def test_untrusted_referer_cannot_mint_session(self, client):
        """Requests with referer from unauthorized domains must be rejected."""
        resp = client.post(
            "/api/auth/session",
            headers={"Referer": "http://evil.com/malicious.html"},
        )
        assert resp.status_code == 403
        assert "forbidden" in resp.json().get("detail", "").lower()

    def test_vite_proxied_browser_request_succeeds(self, client):
        """Requests forwarded by Vite dev server proxy (with referer but no origin) succeed."""
        resp = client.post(
            "/api/auth/session",
            headers={
                "Referer": "http://127.0.0.1:5173/",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "authenticated"

    def test_trusted_cors_origin_can_mint_session(self, client):
        """Requests from configured allowed origins (e.g. Vite frontend) succeed."""
        resp = client.post(
            "/api/auth/session",
            headers={"Origin": "http://localhost:5173"},
        )
        assert resp.status_code == 200
        assert resp.json().get("status") == "authenticated"

    def test_session_creation_rate_limiting(self, client):
        """Rapid repeated calls from the same IP are rate-limited with HTTP 429."""
        session_rate_limiter.reset()
        for i in range(30):
            r = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
            assert r.status_code == 200, f"Call {i} failed: {r.text}"

        # 31st call exceeds rate limit
        flood_resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        assert flood_resp.status_code == 429
        assert "rate limit exceeded" in flood_resp.json().get("detail", "").lower()
        assert flood_resp.headers.get("retry-after") == "5"
        session_rate_limiter.reset()

    def test_programmatic_bootstrap_token_validation(self, client):
        """Direct local non-browser processes must supply X-Bootstrap-Token."""
        session_rate_limiter.reset()
        prog_headers = {"User-Agent": "curl/7.88.1"}  # non-browser, no Origin

        # 1. No token -> 401
        resp_no_token = client.post("/api/auth/session", headers=prog_headers)
        assert resp_no_token.status_code == 401
        assert "bootstrap authorization" in resp_no_token.json().get("detail", "").lower()

        # 2. Forged / invalid token -> 401
        resp_bad_token = client.post(
            "/api/auth/session",
            headers={**prog_headers, "X-Bootstrap-Token": "invalid_forged_secret"},
        )
        assert resp_bad_token.status_code == 401

        # 3. Legitimate bootstrap token -> 200
        valid_tok = get_bootstrap_token()
        resp_good = client.post(
            "/api/auth/session",
            headers={**prog_headers, "X-Bootstrap-Token": valid_tok},
        )
        assert resp_good.status_code == 200
        assert resp_good.json()["status"] == "authenticated"

    def test_authenticated_request_with_cookie(self, client):
        auth_resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        cookies = auth_resp.cookies

        # GET request succeeds with valid cookie
        resp = client.get("/api/tasks/history", cookies=cookies)
        assert resp.status_code == 200

    def test_mutating_request_without_csrf_returns_403(self, client):
        auth_resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        cookies = auth_resp.cookies

        # Mutating request with session cookie but missing CSRF header
        resp = client.post("/api/tasks/cancel-all", cookies=cookies)
        assert resp.status_code == 403
        assert "CSRF validation failed" in resp.json().get("detail", "")

    def test_mutating_request_with_valid_csrf_succeeds(self, client):
        auth_resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        session_id = auth_resp.json()["session_id"]
        csrf_token = auth_resp.json()["csrf_token"]

        headers = {
            "Authorization": f"Bearer {session_id}",
            CSRF_HEADER_NAME: csrf_token,
        }
        resp = client.post("/api/tasks/cancel-all", headers=headers)
        assert resp.status_code == 200
        assert resp.json().get("status") in ("ok", "success")

    def test_cors_preflight_and_cross_origin_bearer_auth(self, client, monkeypatch):
        """Simulated Vercel cross-origin preflight and Bearer auth testing."""
        # 1. Preflight OPTIONS request for CORS check
        preflight = client.options(
            "/api/tasks/history",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization, X-CSRF-Token",
            },
        )
        assert preflight.status_code == 200
        assert preflight.headers.get("access-control-allow-origin") == "http://localhost:5173"

        # 2. Cross-origin Bearer token authentication
        auth_resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
        session_id = auth_resp.json()["session_id"]

        resp = client.get(
            "/api/tasks/history",
            headers={
                "Origin": "http://localhost:5173",
                "Authorization": f"Bearer {session_id}",
            },
        )
        assert resp.status_code == 200
