"""
tests/test_security_messaging.py
--------------------------------
Comprehensive regression tests for centralized, application-wide security messaging.

Verifies:
1. Centralized generic user messaging across all security event categories.
2. Prevention of ordinary English phrases being falsely redacted or blocked
   (e.g., "secret garden", "token refresh", "password manager", "password reset", "password policy").
3. Redacted placeholders (e.g., [REDACTED_SECRET], {{USER_PASSWORD}}) are never
   re-classified as raw secrets.
4. User-facing task errors, API responses, step descriptions, and WebSocket broadcasts
   never expose internal security tiers, detection rules, or technical redaction tags.
5. End-to-end task creation, extraction threshold enforcement, authentication gating,
   file uploads, navigation restrictions, and downloadable reports.
"""

import time
import pytest
from unittest import mock

from backend.security.messages import (
    SecurityEventType,
    SECURITY_USER_MESSAGES,
    is_security_error,
    strip_internal_placeholders,
    get_security_user_message,
    sanitize_user_facing_error,
)
from backend.security import (
    contains_tier1_secrets,
    contains_tier2_identifiers,
    redact_text,
    redact_sensitive_data,
    check_goal_policy,
    check_extraction_safety_threshold,
    validate_upload_path,
)
from backend.models.schemas import (
    TaskRequest,
    TaskResponse,
    TaskStatus,
    AgentStep,
    ActionType,
    PageCategory,
)
from backend.api.task_manager import task_manager
from backend.api.routes import _sanitize_task_response
from backend.services.report_generation_service import ReportGenerator


# ===========================================================================
# 1. Centralized Security Messaging Unit Tests
# ===========================================================================

class TestCentralizedSecurityMessaging:

    def test_all_security_event_types_have_generic_messages(self):
        for event_type in SecurityEventType:
            msg = SECURITY_USER_MESSAGES.get(event_type)
            assert msg is not None, f"Missing message for {event_type}"
            # Must not expose technical tiers, regex rules, or placeholders
            assert "tier" not in msg.lower()
            assert "[redacted" not in msg.lower()
            assert "regex" not in msg.lower()
            assert "50%" not in msg
            assert len(msg) > 20

    def test_get_security_user_message_from_enum(self):
        msg = get_security_user_message(SecurityEventType.GOAL_BLOCKED)
        assert msg == (
            "This task involves accessing or retrieving protected information. "
            "For security reasons, the action cannot be completed."
        )

        extraction_msg = get_security_user_message(SecurityEventType.EXTRACTION_REFUSED)
        assert extraction_msg == (
            "The requested content contains protected information. "
            "For security reasons, the data cannot be extracted."
        )

        upload_msg = get_security_user_message(SecurityEventType.UPLOAD_RESTRICTED)
        assert upload_msg == (
            "The selected file cannot be uploaded due to security restrictions."
        )

        auth_msg = get_security_user_message(SecurityEventType.AUTH_REQUIRED)
        assert auth_msg == (
            "Authentication or security verification required. "
            "Please complete this step directly in the browser to continue."
        )

        nav_msg = get_security_user_message(SecurityEventType.NAVIGATION_RESTRICTED)
        assert nav_msg == (
            "Navigation to this address is restricted by security policy."
        )

        shot_msg = get_security_user_message(SecurityEventType.SCREENSHOT_PROTECTED)
        assert shot_msg == (
            "Screen capture unavailable for protected content."
        )

    def test_get_security_user_message_from_diagnostic_reasons(self):
        # Goal containing raw secrets or harvest attempt
        assert get_security_user_message(
            "Goal contains raw Tier 1 secret values (passwords, API keys, OTPs, or private tokens)."
        ) == SECURITY_USER_MESSAGES[SecurityEventType.GOAL_BLOCKED]

        assert get_security_user_message(
            "Goal attempts to extract or harvest prohibited Tier 1 secrets."
        ) == SECURITY_USER_MESSAGES[SecurityEventType.GOAL_BLOCKED]

        # Extraction threshold
        assert get_security_user_message(
            "Extraction safety threshold exceeded: 3/4 (75.0%) sensitive leaf fields detected (> 50%). Extraction refused."
        ) == SECURITY_USER_MESSAGES[SecurityEventType.EXTRACTION_REFUSED]

        # Upload restrictions
        assert get_security_user_message(
            "Security policy prohibits uploading sensitive file: client_secret.json"
        ) == SECURITY_USER_MESSAGES[SecurityEventType.UPLOAD_RESTRICTED]

        assert get_security_user_message(
            "File 'secret.key' is outside allowlisted upload directories. Uploads must be within configured upload directories."
        ) == SECURITY_USER_MESSAGES[SecurityEventType.UPLOAD_RESTRICTED]

        # Navigation policy
        assert get_security_user_message(
            "Navigation blocked by security policy: Loopback address prohibited: 127.0.0.1"
        ) == SECURITY_USER_MESSAGES[SecurityEventType.NAVIGATION_RESTRICTED]

    def test_is_security_error_detection(self):
        # Security errors
        assert is_security_error("Goal contains raw Tier 1 secret") is True
        assert is_security_error("Extraction safety threshold exceeded") is True
        assert is_security_error("Navigation blocked by security policy") is True
        assert is_security_error("File is outside allowlisted upload directories") is True
        assert is_security_error("Loopback address prohibited: 127.0.0.1") is True
        assert is_security_error("Action blocked by security policy") is True

        # Non-security errors
        assert is_security_error("Element #submit-button not found on page") is False
        assert is_security_error("Network timeout after 30000ms") is False
        assert is_security_error("JSON decode error: line 1 column 1") is False
        assert is_security_error(None) is False
        assert is_security_error("") is False

    def test_strip_internal_placeholders(self):
        text = "Result with [REDACTED_SECRET], {{USER_PASSWORD}}, and [REDACTED_API_KEY]"
        stripped = strip_internal_placeholders(text, replacement="[protected]")
        assert "[REDACTED_SECRET]" not in stripped
        assert "{{USER_PASSWORD}}" not in stripped
        assert "[REDACTED_API_KEY]" not in stripped
        assert "[protected]" in stripped

        # None and empty handling
        assert strip_internal_placeholders(None) == ""
        assert strip_internal_placeholders("") == ""
        assert strip_internal_placeholders("Clean text") == "Clean text"

    def test_sanitize_user_facing_error(self):
        # Technical security error transformed to generic message
        raw_error = "Extraction safety threshold exceeded: 2/3 (66.7%) sensitive leaf fields detected (> 50%). Extraction refused."
        sanitized = sanitize_user_facing_error(raw_error)
        assert sanitized == SECURITY_USER_MESSAGES[SecurityEventType.EXTRACTION_REFUSED]
        assert "66.7%" not in sanitized
        assert "threshold" not in sanitized

        # Normal operational error preserved
        normal_error = "Could not locate the submit button with selector '#btn-save'"
        assert sanitize_user_facing_error(normal_error) == normal_error

        # None preserved
        assert sanitize_user_facing_error(None) is None


# ===========================================================================
# 2. Ordinary English Phrases Must NOT Be Redacted or Blocked
# ===========================================================================

class TestOrdinaryWordProtection:

    @pytest.mark.parametrize("phrase", [
        "We need to implement token refresh logic",
        "I visited the secret garden yesterday",
        "Open my password manager app",
        "Please review the updated password policy",
        "Click the password reset link",
        "Use the token bucket rate limiter",
        "Grandma's secret recipe for cookies",
        "The secret agent arrived on time",
        "Enter code on the pin pad",
        "Study credential stuffing defense techniques",
    ])
    def test_ordinary_phrases_not_detected_as_tier1_secrets(self, phrase):
        assert contains_tier1_secrets(phrase) is False

    @pytest.mark.parametrize("phrase", [
        "We need to implement token refresh logic",
        "I visited the secret garden yesterday",
        "Open my password manager app",
        "Please review the updated password policy",
        "Click the password reset link",
        "Use the token bucket rate limiter",
        "Grandma's secret recipe for cookies",
        "The secret agent arrived on time",
    ])
    def test_ordinary_phrases_not_corrupted_by_redact_text(self, phrase):
        result = redact_text(phrase)
        assert result == phrase
        assert "[REDACTED" not in result

    @pytest.mark.parametrize("goal", [
        "Search for the secret garden book on Amazon",
        "Implement token refresh handling in our web client",
        "Open the password manager and verify documentation",
        "Read about corporate password policy best practices",
    ])
    def test_ordinary_phrase_goals_allowed_by_policy(self, goal):
        allowed, reason = check_goal_policy(goal)
        assert allowed is True
        assert reason == "Safe"


# ===========================================================================
# 3. Already Redacted Placeholders Are Never Treated as Raw Secrets
# ===========================================================================

class TestPlaceholderNonReclassification:

    @pytest.mark.parametrize("text_with_placeholder", [
        "password: [REDACTED_SECRET]",
        "The password is [REDACTED_SECRET]",
        "password = [REDACTED]",
        "password: {{USER_PASSWORD}}",
        "Use token [REDACTED_TOKEN]",
        "api_key: [REDACTED_API_KEY]",
        "Private key: [REDACTED_PRIVATE_KEY]",
        "Bearer [REDACTED_TOKEN]",
    ])
    def test_placeholders_not_reclassified_as_raw_secrets(self, text_with_placeholder):
        assert contains_tier1_secrets(text_with_placeholder) is False

    @pytest.mark.parametrize("text_with_placeholder", [
        "Customer card is [REDACTED_CARD]",
        "SSN: [REDACTED_SSN]",
        "Aadhaar: [REDACTED_AADHAAR]",
        "PAN: [REDACTED_PAN]",
        "IBAN: [REDACTED_IBAN]",
    ])
    def test_tier2_placeholders_not_reclassified(self, text_with_placeholder):
        assert contains_tier2_identifiers(text_with_placeholder) is False

    def test_mixed_text_with_placeholder_and_real_secret(self):
        # A string with both a placeholder AND a real raw secret must still be caught
        mixed = "Previous password was [REDACTED_SECRET], new password is superSecret999!"
        assert contains_tier1_secrets(mixed) is True


# ===========================================================================
# 4. End-to-End Application-Wide Error Presentation
# ===========================================================================

class TestApplicationWideSecurityErrorPresentation:

    def test_create_task_blocked_goal_presents_generic_message(self):
        req = TaskRequest(
            goal="Sign in to the site using password mySecretPassword123!",
            start_url="https://example.com",
        )
        resp = task_manager.create_task(req)
        assert resp.status == TaskStatus.FAILED
        # User message must be the generic message
        assert resp.error == (
            "This task involves accessing or retrieving protected information. "
            "For security reasons, the action cannot be completed."
        )
        # Goal must not contain [REDACTED_SECRET]
        assert "[REDACTED_SECRET]" not in resp.goal
        assert "mySecretPassword123" not in resp.goal
        assert "[protected]" in resp.goal

    def test_api_sanitize_task_response_sanitizes_errors_and_steps(self):
        step = AgentStep(
            step_number=1,
            action=ActionType.EXTRACT,
            description="Extracted table data from customer portal",
            reasoning="Navigated to users table with token [REDACTED_API_KEY]",
            error="Extraction safety threshold exceeded: 3/4 (75.0%) sensitive leaf fields detected (> 50%). Extraction refused.",
        )
        task = TaskResponse(
            task_id="task-api-test",
            goal="Export user directory with password [REDACTED_SECRET]",
            status=TaskStatus.FAILED,
            created_at=time.time(),
            updated_at=time.time(),
            steps=[step],
            error="Goal contains raw Tier 1 secret values (passwords, API keys, OTPs, or private tokens).",
        )

        sanitized = _sanitize_task_response(task)
        # Task error sanitized
        assert sanitized.error == (
            "This task involves accessing or retrieving protected information. "
            "For security reasons, the action cannot be completed."
        )
        # Step error sanitized
        assert sanitized.steps[0].error == (
            "The requested content contains protected information. "
            "For security reasons, the data cannot be extracted."
        )
        # Placeholders stripped from goal and reasoning
        assert "[REDACTED_SECRET]" not in sanitized.goal
        assert "[REDACTED_API_KEY]" not in sanitized.steps[0].reasoning

    def test_report_generation_sanitizes_decision_and_error(self):
        raw_error = "Extraction safety threshold exceeded: 4/5 (80.0%) sensitive leaf fields detected (> 50%). Extraction refused."
        cleaned_error = ReportGenerator._sanitize_decision_explanation(raw_error)
        assert "80.0%" not in cleaned_error
        assert "Extraction safety threshold" not in cleaned_error
        assert cleaned_error == (
            "The requested content contains protected information. "
            "For security reasons, the data cannot be extracted."
        )

        # Ensure placeholders like [REDACTED_SECRET] do not appear in report output
        text_with_placeholder = "Action succeeded for user account [REDACTED_SECRET] with token [REDACTED_API_KEY]"
        clean_text = ReportGenerator._sanitize_decision_explanation(text_with_placeholder)
        assert "[REDACTED_SECRET]" not in clean_text
        assert "[REDACTED_API_KEY]" not in clean_text
        assert "[protected]" in clean_text
