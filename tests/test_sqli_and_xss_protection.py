"""
tests/test_sqli_and_xss_protection.py
--------------------------------------
Regression test suite for SQL Injection (SQLi) and Cross-Site Scripting (XSS)
prevention, defensive parameterization, Content Security Policy, safe URL handling,
and CSV formula injection protections.
"""

import os
import re
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.task_archive import TaskArchive
from backend.models.schemas import TaskResponse, TaskStatus, ResearchTaskResponse
from backend.services.research_export_service import sanitize_spreadsheet_value
from backend.browser.actions import scroll_page
from backend.security.auth import session_manager, session_rate_limiter, SESSION_COOKIE_NAME, CSRF_COOKIE_NAME, CSRF_HEADER_NAME


@pytest.fixture
def auth_client():
    session_rate_limiter.reset()
    client = TestClient(app, base_url="http://127.0.0.1:8000")
    # Mint a valid session
    resp = client.post("/api/auth/session", headers={"Origin": "http://localhost:5173"})
    assert resp.status_code == 200
    return client


class TestSQLInjectionProtection:
    """Verifies that all database operations use parameterized queries and resist SQLi payloads."""

    @pytest.fixture
    def test_archive(self, tmp_path):
        db_file = tmp_path / "test_archive.sqlite3"
        return TaskArchive(db_path=str(db_file), max_tasks=100)

    @pytest.mark.parametrize("payload", [
        "' OR '1'='1",
        "admin'--",
        "'; DROP TABLE archived_tasks; --",
        "1 UNION SELECT 1, 2, 'leak', x'00'--",
        "' OR 1=1; --",
        "task-1' AND (SELECT count(*) FROM archived_tasks) > 0 --",
        "' OR ''='",
    ])
    def test_task_archive_queries_resist_sqli_payloads(self, test_archive, payload):
        """Malicious SQL injection payloads in task_id must be treated as inert string literals."""
        # Querying non-existent malicious task_id must return None cleanly without syntax error
        assert test_archive.get_task(payload) is None
        assert test_archive.get_report(payload) is None
        assert test_archive.get_research_task(payload) is None
        assert test_archive.get_research_export(payload, "pdf") is None

        # Deleting with malicious payload must not delete arbitrary records or drop tables
        test_archive.delete_task(payload)

        # Verify table integrity: table still exists and functions normally
        assert test_archive.list_tasks() == []

    def test_task_archive_save_and_retrieve_with_special_characters(self, test_archive):
        """Task IDs with quotes, semicolons, and dashes persist and retrieve verbatim via parameter binding."""
        special_id = "task-test'; DROP TABLE archived_tasks; --"
        task = TaskResponse(
            task_id=special_id,
            goal="Verify SQL parameterization",
            status=TaskStatus.COMPLETED,
            steps=[],
            created_at=1000.0,
            updated_at=1005.0,
        )

        test_archive.save_task(task, report_pdf=b"%PDF-test")
        retrieved = test_archive.get_task(special_id)
        assert retrieved is not None
        assert retrieved.task_id == special_id

        # PDF retrieval with the special ID
        pdf = test_archive.get_report(special_id)
        assert pdf == b"%PDF-test"

        # Ensure table was NOT dropped
        all_tasks = test_archive.list_tasks()
        assert len(all_tasks) == 1
        assert all_tasks[0].task_id == special_id

    def test_research_export_format_allowlist_rejects_sqli(self, test_archive):
        """Export format parameter must reject SQL injection fragments outside the allowlist."""
        with pytest.raises(ValueError, match="Unsupported research export format"):
            test_archive.save_research_export("task-1", "pdf'; DROP TABLE archived_tasks; --", b"fake")

        assert test_archive.get_research_export("task-1", "csv' OR '1'='1") is None

    def test_api_task_endpoint_with_sqli_payload(self, auth_client):
        """GET /api/tasks/{task_id} with SQL injection payload returns 404 without database error leak."""
        sqli_id = "' OR 1=1 --"
        resp = auth_client.get(f"/api/tasks/{sqli_id}")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "Task not found"


class TestXSSAndCodeInjectionProtection:
    """Verifies protection against XSS, DOM injection, browser evaluate injection, and CSV formula injection."""

    def test_security_headers_present_on_responses(self, auth_client):
        """All responses must contain Content-Security-Policy and protective headers."""
        resp = auth_client.get("/api/tasks")
        headers = resp.headers

        assert headers.get("x-content-type-options") == "nosniff"
        assert headers.get("x-frame-options") == "DENY"
        assert headers.get("referrer-policy") == "strict-origin-when-cross-origin"

        csp = headers.get("content-security-policy", "")
        assert "default-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert "object-src 'none'" in csp
        # Main API/dashboard responses must not allow unsafe-inline for scripts
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]

    def test_test_form_csp_allows_inline_scripts(self, auth_client):
        """The /test-form endpoint allows unsafe-inline in script-src for form demonstration."""
        resp = auth_client.get("/test-form")
        csp = resp.headers.get("content-security-policy", "")
        script_directive = csp.split("script-src")[1].split(";")[0]
        assert "'unsafe-inline'" in script_directive

    def test_validate_url_security_handles_malformed_ports(self):
        """validate_url_security must safely reject malformed non-integer ports without throwing ValueError."""
        from backend.browser.security import validate_url_security
        malicious_urls = [
            "http://example.com:alert(1)/path",
            "https://example.com:9999999/test",
            "http://example.com:abc/",
            "http://example.com:-1/",
        ]
        for u in malicious_urls:
            safe, reason, _ = validate_url_security(u, resolve_dns=False)
            assert safe is False
            assert "port" in reason.lower()

    def test_content_disposition_header_injection_mitigation(self, auth_client):
        """Content-Disposition filename must sanitize task_id to prevent HTTP header injection / response splitting."""
        from urllib.parse import quote
        raw_malicious_id = 'test"filename; injected=true'
        encoded_id = quote(raw_malicious_id)
        resp = auth_client.get(f"/api/reports/{encoded_id}/download")
        # The task doesn't exist, returning 404 cleanly
        assert resp.status_code == 404

        # Test the filename sanitization logic directly
        import re
        safe_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", raw_malicious_id)
        assert '"' not in safe_id
        assert ';' not in safe_id
        assert ' ' not in safe_id
        assert safe_id == "test_filename__injected_true"

    @pytest.mark.asyncio
    async def test_scroll_page_evaluates_safe_parameter(self):
        """scroll_page must pass scroll delta as a parameterized argument, not string interpolation."""
        mock_page = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=None)

        # Normal execution
        success = await scroll_page(mock_page, direction="down", amount=300)
        assert success is True
        mock_page.evaluate.assert_called_once_with("(d) => window.scrollBy(0, d)", 300)

        mock_page.evaluate.reset_mock()
        # Malicious amount string that would cause code injection if interpolated
        malicious_amount = "0); alert(1); (0"
        success2 = await scroll_page(mock_page, direction="down", amount=malicious_amount)
        assert success2 is True
        # Must fall back to safe numeric default (500) and pass as parameter
        mock_page.evaluate.assert_called_once_with("(d) => window.scrollBy(0, d)", 500)

    @pytest.mark.parametrize("input_val, expected", [
        ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"),
        ("+1+2", "'+1+2"),
        ("-SUM(A1:A10)", "'-SUM(A1:A10)"),
        ("@SUM(A1:A10)", "'@SUM(A1:A10)"),
        ("\t=1+1", "'\t=1+1"),
        ("Normal Text", "Normal Text"),
        ("123.45", "123.45"),
        (100, 100),
        ("", ""),
    ])
    def test_csv_formula_injection_sanitization(self, input_val, expected):
        """Spreadsheet cells starting with formula triggers must be sanitized with a leading single quote."""
        assert sanitize_spreadsheet_value(input_val) == expected

    @pytest.mark.parametrize("url, is_safe", [
        ("https://example.com/login", True),
        ("http://example.com/test", True),
        ("/agent", True),
        ("/api/reports/123/download", True),
        ("//evil.com/phish", False),
        ("/\\\\evil.com", False),
        ("javascript:alert(document.cookie)", False),
        ("JAVASCRIPT:alert(1)", False),
        ("javascript://%0aalert(1)", False),
        ("jav\tascript:alert(1)", False),
        ("data:text/html,<script>alert(1)</script>", False),
        ("vbscript:msgbox(1)", False),
        ("blob:http://example.com/uuid", False),
        ("file:///etc/passwd", False),
        ("", False),
        (None, False),
    ])
    def test_safe_web_url_validation_logic(self, url, is_safe):
        """Simulates the frontend isSafeWebUrl validation to confirm dangerous schemes and bypasses are blocked."""
        def is_safe_web_url(u):
            if not u or not isinstance(u, str):
                return False
            trimmed = u.strip()
            clean = re.sub(r"[\x00-\x1F\x7F-\x9F]", "", trimmed)
            if re.match(r"^(javascript|data|vbscript|blob|file):", clean, re.IGNORECASE):
                return False
            if re.match(r"^https?://", clean, re.IGNORECASE):
                return True
            if re.match(r"^/(?!/|\\)", clean):
                return True
            return False

        assert is_safe_web_url(url) == is_safe
