"""
tests/test_extension_pairing_auth.py
------------------------------------
Regression test suite for Chrome Extension pairing authentication,
persistence across backend restarts, token revocation, clean WebSocket
disconnection, and unhandled RuntimeError prevention.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.main import app
from backend.security.extension_auth import ExtensionAuthManager, ExtensionRegistration
from browser.extension_adapter import extension_manager


class TestExtensionAuthPersistenceAndValidation:
    """Verifies token generation, disk persistence across restarts, and security validation."""

    def test_tofu_pairing_generates_and_persists_token(self, tmp_path):
        store_file = tmp_path / "pairing_test.json"
        mgr = ExtensionAuthManager(persistence_file=str(store_file))

        ok, token, reason = mgr.register_or_authenticate(
            token=None,
            extension_id="chrome_ext_alpha",
            is_loopback=True,
        )
        assert ok is True
        assert token is not None
        assert token.startswith("ext_sec_")
        assert "Trust-On-First-Use" in reason
        assert store_file.exists()

        # Verify persisted JSON structure
        with open(store_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert token in data["registrations"]
        assert data["registrations"][token]["extension_id"] == "chrome_ext_alpha"
        assert data["registrations"][token]["revoked"] is False

    def test_reconnect_after_backend_restart_uses_persisted_token(self, tmp_path):
        """Simulates server restart by initializing a new manager instance from the same store."""
        store_file = tmp_path / "pairing_test.json"
        mgr1 = ExtensionAuthManager(persistence_file=str(store_file))
        ok1, token1, _ = mgr1.register_or_authenticate(
            token=None,
            extension_id="chrome_ext_beta",
            is_loopback=True,
        )
        assert ok1 is True

        # Simulate backend restart: new manager instance reads from the store
        mgr2 = ExtensionAuthManager(persistence_file=str(store_file))
        assert mgr2.is_token_valid(token1) is True

        # Extension reconnects with the existing token
        ok2, token2, reason2 = mgr2.register_or_authenticate(
            token=token1,
            extension_id="chrome_ext_beta",
            is_loopback=True,
        )
        assert ok2 is True
        assert token2 == token1
        assert "Authenticated successfully" in reason2

    def test_invalid_token_rejected_with_exact_reason(self, tmp_path):
        store_file = tmp_path / "pairing_test.json"
        mgr = ExtensionAuthManager(persistence_file=str(store_file))

        ok, token, reason = mgr.register_or_authenticate(
            token="ext_sec_stale_or_fabricated_token",
            extension_id="chrome_ext_gamma",
            is_loopback=True,
        )
        assert ok is False
        assert token is None
        assert reason == "Invalid extension pairing token."

    def test_mismatching_extension_id_rejected(self, tmp_path):
        """A client presenting a valid token with a different extension ID is rejected."""
        store_file = tmp_path / "pairing_test.json"
        mgr = ExtensionAuthManager(persistence_file=str(store_file))

        ok1, token1, _ = mgr.register_or_authenticate(
            token=None,
            extension_id="legitimate_ext_id_111",
            is_loopback=True,
        )
        assert ok1 is True

        # Attacker with rogue extension ID attempts to use the token
        ok2, token2, reason2 = mgr.register_or_authenticate(
            token=token1,
            extension_id="rogue_ext_id_999",
            is_loopback=True,
        )
        assert ok2 is False
        assert token2 is None
        assert "different extension ID" in reason2

    def test_revoked_token_persists_and_cannot_reconnect(self, tmp_path):
        """Revocation is written to disk and persists across restarts."""
        store_file = tmp_path / "pairing_test.json"
        mgr1 = ExtensionAuthManager(persistence_file=str(store_file))

        ok1, token1, _ = mgr1.register_or_authenticate(
            token=None,
            extension_id="ext_to_revoke",
            is_loopback=True,
        )
        assert ok1 is True

        # Revoke token
        assert mgr1.revoke_token(token1) is True
        assert mgr1.is_token_valid(token1) is False

        # Simulate restart
        mgr2 = ExtensionAuthManager(persistence_file=str(store_file))
        assert mgr2.is_token_valid(token1) is False

        # Attempting reconnection with the revoked token must fail
        ok2, token2, reason2 = mgr2.register_or_authenticate(
            token=token1,
            extension_id="ext_to_revoke",
            is_loopback=True,
        )
        assert ok2 is False
        assert token2 is None
        assert "revoked" in reason2.lower()


class TestExtensionWebSocketLifecycleAndDisconnect:
    """Verifies WebSocket handshake, clean disconnection, and error handling in routes.py."""

    @pytest.fixture(autouse=True)
    def clean_extension_state(self, tmp_path):
        """Isolate extension auth and manager state for WebSocket tests."""
        test_store = tmp_path / ".test_ext_pairing.json"
        from backend.security import extension_auth
        old_mgr = extension_auth.extension_auth_manager
        fresh_mgr = ExtensionAuthManager(persistence_file=str(test_store))
        extension_auth.extension_auth_manager = fresh_mgr

        yield fresh_mgr

        # Teardown
        extension_auth.extension_auth_manager = old_mgr
        import asyncio
        try:
            asyncio.run(extension_manager.disconnect_all())
        except Exception:
            pass

    def test_valid_pairing_and_clean_websocket_flow(self):
        """Tests complete successful TOFU pairing and registration via WebSocket."""
        client = TestClient(app, base_url="http://127.0.0.1:8000")

        with client.websocket_connect("/api/extension/ws") as ws:
            # Send initial registration handshake
            ws.send_json({
                "type": "register",
                "client": "chrome-extension",
                "version": "1.0.0",
                "token": None,
                "extension_id": "test_ext_client",
                "active_tasks": [],
            })

            resp = ws.receive_json()
            assert resp["type"] == "registered"
            assert resp["token"] is not None
            assert resp["token"].startswith("ext_sec_")
            assert extension_manager.is_connected is True

        # After client context exits (disconnected), extension_manager cleans up
        assert extension_manager.is_connected is False

    def test_invalid_token_websocket_closes_cleanly_without_runtime_error(self):
        """Verifies that an invalid token receives auth_failed and disconnects without raising unhandled RuntimeError."""
        client = TestClient(app, base_url="http://127.0.0.1:8000")

        with client.websocket_connect("/api/extension/ws") as ws:
            ws.send_json({
                "type": "register",
                "client": "chrome-extension",
                "version": "1.0.0",
                "token": "ext_sec_completely_invalid_token_12345",
                "extension_id": "test_ext_client",
                "active_tasks": [],
            })

            resp = ws.receive_json()
            assert resp["type"] == "auth_failed"
            assert resp["reason"] == "Invalid extension pairing token."

        # Extension manager must have cleaned up the connection
        assert extension_manager.is_connected is False

    def test_reconnect_with_valid_token_succeeds(self, clean_extension_state):
        """Extension pairs initially, disconnects, and reconnects with its issued token."""
        client = TestClient(app, base_url="http://127.0.0.1:8000")

        # 1. Initial TOFU pairing
        with client.websocket_connect("/api/extension/ws") as ws:
            ws.send_json({
                "type": "register",
                "token": None,
                "extension_id": "stable_ext_id",
            })
            resp = ws.receive_json()
            issued_token = resp["token"]
            assert resp["type"] == "registered"

        # 2. Reconnection with the issued token
        with client.websocket_connect("/api/extension/ws") as ws2:
            ws2.send_json({
                "type": "register",
                "token": issued_token,
                "extension_id": "stable_ext_id",
            })
            resp2 = ws2.receive_json()
            assert resp2["type"] == "registered"
            assert resp2["token"] == issued_token
            assert extension_manager.is_connected is True

        assert extension_manager.is_connected is False
