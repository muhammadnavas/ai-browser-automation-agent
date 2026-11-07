"""
tests/test_issue2_policy.py
---------------------------
Regression test suite for Issue 2: Deterministic Action Security Policy,
Execution Boundaries, Repeated Violation Limits, Unoverrideable Blocks,
IPv4-Mapped IPv6 Bypass Prevention, and Extension Adapter URL Protection.
"""

from unittest.mock import AsyncMock, MagicMock
import pytest
from backend.models.schemas import BrowserAction, ActionType
from backend.security.action_policy import (
    DeterministicActionPolicy,
    PolicyDecision,
    PolicyMode,
    MAX_SECURITY_VIOLATIONS,
)
from backend.browser.security import validate_url_security
from backend.browser.extension_adapter import ExtensionBrowserAdapter


class TestDeterministicActionPolicy:
    def test_default_policy_mode_is_strict(self):
        policy = DeterministicActionPolicy()
        assert policy.mode == PolicyMode.STRICT

    def test_navigation_to_loopback_is_blocked(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.NAVIGATE, text="http://127.0.0.1:8000/api/tasks")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com",
            task_id="task-1",
        )
        assert decision == PolicyDecision.BLOCK
        assert "Navigation to this address is restricted" in reason or "blocked" in reason.lower()

    def test_navigation_to_prohibited_ports_is_blocked(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.NAVIGATE, text="https://example.com:22")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com",
            task_id="task-1",
        )
        assert decision == PolicyDecision.BLOCK

    def test_navigation_to_public_url_is_allowed(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.NAVIGATE, text="https://example.com/about")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com",
            task_id="task-1",
        )
        assert decision == PolicyDecision.ALLOW

    def test_destructive_account_actions_require_approval(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.CLICK, text="Delete Account Permanently", selector="#btn-delete")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com/settings",
            task_id="task-1",
        )
        assert decision == PolicyDecision.REQUIRE_APPROVAL
        assert "destructive" in reason.lower() or "approval" in reason.lower()

    def test_raw_tier1_secret_input_is_blocked(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.FILL, text="sk-proj-abc1234567890abcdef1234567890", selector="#api-key")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com/login",
            task_id="task-1",
        )
        assert decision == PolicyDecision.BLOCK
        assert "protected information" in reason.lower() or "blocked" in reason.lower()

    def test_vault_placeholder_input_is_allowed(self):
        policy = DeterministicActionPolicy()
        action = BrowserAction(action=ActionType.FILL, text="{{USER_PASSWORD}}", selector="#password")
        decision, reason = policy.evaluate_action(
            action=action,
            current_url="https://example.com/login",
            task_id="task-1",
        )
        assert decision == PolicyDecision.ALLOW

    def test_repeated_violations_halt_task(self):
        policy = DeterministicActionPolicy()
        task_id = "violating-task"

        # Trigger violations up to the ceiling
        for _ in range(MAX_SECURITY_VIOLATIONS):
            bad_action = BrowserAction(action=ActionType.NAVIGATE, text="http://127.0.0.1:8000/api")
            decision, _ = policy.evaluate_action(bad_action, "https://example.com", task_id)
            assert decision == PolicyDecision.BLOCK

        # Next action must be halted due to exceeding maximum violations
        any_action = BrowserAction(action=ActionType.CLICK, text="Safe click")
        decision, reason = policy.evaluate_action(any_action, "https://example.com", task_id)
        assert decision == PolicyDecision.BLOCK
        assert "maximum allowable security violations" in reason

    def test_ipv4_mapped_ipv6_addresses_prohibited(self):
        """IPv4-mapped IPv6 formats (::ffff:x.x.x.x) must be unwrapped and prohibited."""
        # 1. Cloud metadata IP mapped to IPv6
        safe1, reason1, _ = validate_url_security("http://[::ffff:100.100.100.200]/latest/meta-data", resolve_dns=False)
        assert safe1 is False
        assert "metadata" in reason1.lower()

        # 2. AWS cloud metadata IP mapped to IPv6
        safe2, reason2, _ = validate_url_security("http://[::ffff:169.254.169.254]/latest/meta-data", resolve_dns=False)
        assert safe2 is False
        assert "metadata" in reason2.lower()

        # 3. Loopback IP mapped to IPv6
        safe3, reason3, _ = validate_url_security("http://[::ffff:127.0.0.1]/api", resolve_dns=False)
        assert safe3 is False
        assert "loopback" in reason3.lower()

        # 4. Private network IP mapped to IPv6
        safe4, reason4, _ = validate_url_security("http://[::ffff:192.168.1.1]/admin", resolve_dns=False)
        assert safe4 is False
        assert "private" in reason4.lower()

    @pytest.mark.asyncio
    async def test_extension_adapter_url_security_enforcement(self):
        """Extension adapter methods must validate URLs and block loopback/metadata before dispatch."""
        mock_mgr = MagicMock()
        mock_mgr.is_connected = True
        mock_mgr.send_command = AsyncMock()

        adapter = ExtensionBrowserAdapter(task_id="test-task", manager=mock_mgr)

        # 1. open_auth_tab blocked on unsafe loopback URL
        with pytest.raises(ValueError) as exc1:
            await adapter.open_auth_tab("http://127.0.0.1:8000/secret")
        assert "blocked by security policy" in str(exc1.value).lower()
        mock_mgr.send_command.assert_not_called()

        # 2. open_recovery_tab blocked on unsafe cloud metadata URL
        with pytest.raises(ValueError) as exc2:
            await adapter.open_recovery_tab("http://169.254.169.254/credentials")
        assert "blocked by security policy" in str(exc2.value).lower()
        mock_mgr.send_command.assert_not_called()

        # 3. start blocked on unsafe initial URL
        with pytest.raises(ValueError) as exc3:
            await adapter.start(initial_url="http://[::ffff:127.0.0.1]:8000/admin")
        assert "blocked by security policy" in str(exc3.value).lower()
        mock_mgr.send_command.assert_not_called()
