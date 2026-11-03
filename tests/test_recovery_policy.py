"""Regression tests for bounded browser recovery classification and retry policy."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from models.schemas import ActionType, BrowserAction
from browser.recovery import (
    classify_browser_error,
    should_retry_browser_error,
    execute_with_retry_and_fallback,
    RetryPolicy,
)


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("strict mode violation: locator resolved to 2 elements", "locator_or_stale_state"),
        ("waiting for locator('#save') timed out", "locator_or_stale_state"),
        ("Timeout 5000ms exceeded", "timeout"),
        ("net::ERR_NAME_NOT_RESOLVED", "navigation_or_page_state"),
        ("Chrome Extension disconnected: websocket closed", "extension_connection"),
        ("403 Forbidden: approval required", "approval_or_permission"),
        ("unexpected browser response", "unknown"),
    ],
)
def test_browser_error_classification(message, category):
    assert classify_browser_error(message) == category


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("locator not found", True),
        ("operation timed out", True),
        ("net::ERR_CONNECTION_RESET", True),
        ("extension disconnected", False),
        ("CAPTCHA requires approval", False),
        ("unexpected browser response", False),
    ],
)
def test_retry_policy_only_allows_recoverable_failure_classes(message, expected):
    assert should_retry_browser_error(message) is expected


@pytest.mark.asyncio
async def test_permission_failure_stops_without_repeating_idempotent_action():
    execute = AsyncMock(return_value=("403 Forbidden: approval required", None, False))
    controller = MagicMock()
    controller.current_page = None
    controller.is_running = True
    action = BrowserAction(action=ActionType.FILL, selector="#email", text="user@example.com")

    description, extracted, success, recovery_log = await execute_with_retry_and_fallback(
        execute,
        action,
        controller,
        retry_policy=RetryPolicy(max_retries=3, base_delay=0, jitter=False),
    )

    assert execute.await_count == 1
    assert success is False
    assert extracted is None
    assert "approval_or_permission" in description
    assert any("not automatically retryable" in item for item in recovery_log)


@pytest.mark.asyncio
async def test_unknown_failure_stops_without_repeated_primary_execution():
    execute = AsyncMock(return_value=("Unexpected browser response", None, False))
    controller = MagicMock()
    controller.current_page = None
    controller.is_running = True
    action = BrowserAction(action=ActionType.FILL, selector="#email", text="user@example.com")

    _, _, success, recovery_log = await execute_with_retry_and_fallback(
        execute,
        action,
        controller,
        retry_policy=RetryPolicy(max_retries=3, base_delay=0, jitter=False),
    )

    assert execute.await_count == 1
    assert success is False
    assert any("category 'unknown'" in item for item in recovery_log)
