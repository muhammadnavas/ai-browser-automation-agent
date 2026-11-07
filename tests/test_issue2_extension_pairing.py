"""
tests/test_issue2_extension_pairing.py
--------------------------------------
Regression test suite for Issue 2: Chrome Extension Authentication,
TOFU Pairing, Extension ID Validation, Key Revocation, Single-Pairing
Enforcement, and Simultaneous/Repeated Pairing Protection.
"""

import concurrent.futures
import json
import pytest
from backend.security.extension_auth import ExtensionAuthManager


class TestExtensionAuthManager:
    def test_tofu_pairing_over_loopback(self):
        mgr = ExtensionAuthManager()
        # Initial connection without token on loopback succeeds via TOFU
        ok, token, reason = mgr.register_or_authenticate(
            token=None,
            extension_id="test_ext_id",
            is_loopback=True,
        )
        assert ok is True
        assert token is not None
        assert token.startswith("ext_sec_")
        assert "Trust-On-First-Use" in reason

        # Subsequent connection with the issued token succeeds
        ok2, token2, reason2 = mgr.register_or_authenticate(
            token=token,
            extension_id="test_ext_id",
            is_loopback=True,
        )
        assert ok2 is True
        assert token2 == token
        assert "Authenticated successfully" in reason2

    def test_single_trusted_initial_pairing_rejects_subsequent_unauthenticated_attempts(self):
        """Verifies that once paired, any subsequent unauthenticated attempt is rejected."""
        mgr = ExtensionAuthManager()
        # Initial pairing succeeds
        ok1, token1, _ = mgr.register_or_authenticate(
            token=None,
            extension_id="legitimate_ext",
            is_loopback=True,
        )
        assert ok1 is True
        assert token1 is not None

        # Rogue local client attempting to obtain pairing credentials without token
        ok2, token2, reason2 = mgr.register_or_authenticate(
            token=None,
            extension_id="rogue_local_client",
            is_loopback=True,
        )
        assert ok2 is False
        assert token2 is None
        assert "already paired" in reason2.lower()

        # Client presenting invalid token is rejected
        ok3, token3, reason3 = mgr.register_or_authenticate(
            token="ext_sec_forged_invalid_token",
            extension_id="legitimate_ext",
            is_loopback=True,
        )
        assert ok3 is False
        assert token3 is None
        assert "invalid" in reason3.lower()

        # Legitimate client with valid token continues to authenticate
        ok4, token4, _ = mgr.register_or_authenticate(
            token=token1,
            extension_id="legitimate_ext",
            is_loopback=True,
        )
        assert ok4 is True
        assert token4 == token1

        # After explicit revocation, a new client can pair
        assert mgr.revoke_token(token1) is True
        ok5, token5, reason5 = mgr.register_or_authenticate(
            token=None,
            extension_id="replacement_ext",
            is_loopback=True,
        )
        assert ok5 is True
        assert token5 is not None
        assert "Trust-On-First-Use" in reason5

    def test_simultaneous_pairing_race_protection(self):
        """Verifies that race conditions cannot result in multiple extensions pairing simultaneously."""
        mgr = ExtensionAuthManager()
        successes = []
        failures = []

        def attempt_pairing(client_id):
            return mgr.register_or_authenticate(
                token=None,
                extension_id=f"ext_{client_id}",
                is_loopback=True,
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(attempt_pairing, i) for i in range(20)]
            for f in concurrent.futures.as_completed(futures):
                ok, tok, reason = f.result()
                if ok:
                    successes.append((tok, reason))
                else:
                    failures.append(reason)

        # Exactly 1 pairing must succeed; 19 must fail
        assert len(successes) == 1
        assert len(failures) == 19
        assert all("already paired" in r.lower() for r in failures)
        assert mgr.get_registered_count() == 1

    def test_rejection_of_non_loopback(self):
        mgr = ExtensionAuthManager()
        ok, token, reason = mgr.register_or_authenticate(
            token=None,
            extension_id="test_ext_id",
            is_loopback=False,
        )
        assert ok is False
        assert token is None
        assert "Non-loopback" in reason

    def test_allowed_extension_id_restriction(self, monkeypatch):
        monkeypatch.setenv("ALLOWED_EXTENSION_IDS", "allowed_id_1,allowed_id_2")
        mgr = ExtensionAuthManager()
        mgr.reload_allowed_ids()

        # Unauthorized extension ID rejected
        ok, token, reason = mgr.register_or_authenticate(
            token=None,
            extension_id="unauthorized_id",
            is_loopback=True,
        )
        assert ok is False
        assert "Unauthorized extension ID" in reason

        # Authorized extension ID accepted
        ok_auth, token_auth, reason_auth = mgr.register_or_authenticate(
            token=None,
            extension_id="allowed_id_1",
            is_loopback=True,
        )
        assert ok_auth is True
        assert token_auth is not None

    def test_token_revocation(self):
        mgr = ExtensionAuthManager()
        ok, token, _ = mgr.register_or_authenticate(
            token=None,
            extension_id="ext_to_revoke",
            is_loopback=True,
        )
        assert mgr.is_token_valid(token) is True

        # Revoke token
        revoked = mgr.revoke_token(token)
        assert revoked is True
        assert mgr.is_token_valid(token) is False

        # Attempting to reconnect with revoked token fails
        ok_after, _, reason_after = mgr.register_or_authenticate(
            token=token,
            extension_id="ext_to_revoke",
            is_loopback=True,
        )
        assert ok_after is False
        assert "revoked" in reason_after.lower()

    def test_manifest_has_no_cookies_permission(self):
        with open("chrome_extension/manifest.json", "r", encoding="utf-8") as f:
            manifest = json.load(f)
        permissions = manifest.get("permissions", [])
        assert "cookies" not in permissions, "Chrome extension manifest must not contain 'cookies' permission."
