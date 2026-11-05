"""tests/test_security_module.py

Comprehensive security test suite covering:
1. Secret detection and blocking (Tier 1 Secrets).
2. Sensitive personal identifier detection and redaction (Tier 2 Identifiers).
3. Contact PII and public business contact handling (Tier 3 Contact PII).
4. False-positive handling for non-sensitive words and normal navigation.
5. Per-task in-memory sensitive data vault (placeholders, substitution, cleanup, secret rejection).
6. No sensitive values or vault plaintext in TaskContext filled_fields, task archives, or PDF exports.
7. Extraction safety threshold (> 50% boundary cases, public contact preservation).
8. User-assisted authentication pause/resume (is_auth_field, screenshot_url=None, audit events).
9. Upload safety (explicit paths, allowlisted directories, sensitive-file rejection, mock-file forbidden).
10. Shared security audit module (recording events without sensitive values or secrets).
"""

import os
import json
import time
import pytest
from pathlib import Path

from backend.security import (
    SensitivityTier,
    classify_sensitivity,
    is_tier1_key,
    is_tier2_key,
    is_tier3_key,
    is_sensitive_key,
    is_sensitive_file,
    is_auth_field,
    is_public_business_email,
    contains_tier1_secrets,
    contains_tier2_identifiers,
    redact_text,
    redact_sensitive_data,
    TaskVault,
    get_task_vault,
    clear_task_vault,
    check_goal_policy,
    check_extraction_safety_threshold,
    validate_upload_path,
    audit_logger,
)

# ---------------------------------------------------------------------------
# Test Canary Constants
# ---------------------------------------------------------------------------
CANARY_PASSWORD = "canary_pass_SECRET_987654"
CANARY_API_KEY = "sk-canary_live_abcdef1234567890"
CANARY_OTP = "987654"
CANARY_PRIVATE_KEY = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0canary..."
CANARY_SSN = "987-65-4321"
CANARY_CREDIT_CARD = "4111 2222 3333 4444"
CANARY_AADHAAR = "9876 5432 1098"
CANARY_PAN = "ABCDE1234F"
CANARY_IBAN = "GB82WEST12345698765432"
CANARY_PERSONAL_EMAIL = "canary.alice.private@gmail.com"
CANARY_PERSONAL_PHONE = "+1-555-019-2834"
CANARY_PUBLIC_BUSINESS_EMAIL = "info@acme-corp.com"
CANARY_PUBLIC_BUSINESS_PHONE = "+1-800-555-0199"


# ===========================================================================
# 1. Tier 1 Secret Detection and Blocking
# ===========================================================================
class TestTier1SecretDetection:

    def test_classify_tier1_secrets(self):
        assert classify_sensitivity(CANARY_PASSWORD, key_name="password") == SensitivityTier.TIER_1_SECRETS
        assert classify_sensitivity(CANARY_API_KEY, key_name="api_key") == SensitivityTier.TIER_1_SECRETS
        assert classify_sensitivity("bearer eyJhbGciOi...", key_name="token") == SensitivityTier.TIER_1_SECRETS
        assert classify_sensitivity(CANARY_OTP, key_name="otp_code") == SensitivityTier.TIER_1_SECRETS
        assert classify_sensitivity("123", key_name="cvv") == SensitivityTier.TIER_1_SECRETS
        assert classify_sensitivity(CANARY_PRIVATE_KEY, key_name="private_key") == SensitivityTier.TIER_1_SECRETS

    def test_is_tier1_key_helper(self):
        assert is_tier1_key("user_password") is True
        assert is_tier1_key("client_secret") is True
        assert is_tier1_key("auth_token") is True
        assert is_tier1_key("session_id") is True
        assert is_tier1_key("first_name") is False

    def test_contains_tier1_secrets(self):
        assert contains_tier1_secrets(f"Please use password {CANARY_PASSWORD}") is True
        assert contains_tier1_secrets(f"Here is api_key={CANARY_API_KEY}") is True
        assert contains_tier1_secrets("Find cheap flights from New York to London") is False

    def test_check_goal_policy_blocks_tier1_secrets(self):
        # Goal containing password must be blocked
        allowed, reason = check_goal_policy(f"Login with password {CANARY_PASSWORD}", task_id="task-test-t1")
        assert allowed is False
        assert "Tier 1 secret" in reason

        # Goal containing API key must be blocked
        allowed, reason = check_goal_policy(f"Deploy using {CANARY_API_KEY}", task_id="task-test-t1")
        assert allowed is False
        assert "Tier 1 secret" in reason

        # Normal goal must be permitted
        allowed, reason = check_goal_policy("Search for hotels in Paris and extract prices", task_id="task-test-t1")
        assert allowed is True

    def test_audit_logs_event_without_canary_leak(self):
        audit_logger.clear()
        task_id = "task-canary-secret-blocked"
        check_goal_policy(f"Login with password {CANARY_PASSWORD}", task_id=task_id)

        events = audit_logger.get_events_for_task(task_id)
        assert len(events) >= 1
        assert any(e.event_type == "secret_containing_goal_blocked" for e in events)

        # Verify CANARY_PASSWORD is NOT in the audit event log
        event_str = json.dumps([e.to_dict() for e in events])
        assert CANARY_PASSWORD not in event_str


# ===========================================================================
# 2. Tier 2 Personal Identifier Detection and Redaction
# ===========================================================================
class TestTier2PersonalIdentifiers:

    def test_classify_tier2_identifiers(self):
        assert classify_sensitivity(CANARY_SSN, key_name="ssn") == SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS
        assert classify_sensitivity(CANARY_CREDIT_CARD, key_name="credit_card") == SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS
        assert classify_sensitivity(CANARY_AADHAAR, key_name="aadhaar_number") == SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS
        assert classify_sensitivity(CANARY_PAN, key_name="pan_number") == SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS
        assert classify_sensitivity(CANARY_IBAN, key_name="iban") == SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS

    def test_contains_tier2_identifiers(self):
        assert contains_tier2_identifiers(f"Customer SSN is {CANARY_SSN}") is True
        assert contains_tier2_identifiers(f"Card number: {CANARY_CREDIT_CARD}") is True
        assert contains_tier2_identifiers("Just a regular message with no identifiers") is False

    def test_redact_tier2_in_text(self):
        text = f"User SSN: {CANARY_SSN}, Card: {CANARY_CREDIT_CARD}, IBAN: {CANARY_IBAN}"
        redacted = redact_text(text)
        assert CANARY_SSN not in redacted
        assert CANARY_CREDIT_CARD not in redacted
        assert CANARY_IBAN not in redacted
        assert "[REDACTED_SSN]" in redacted
        assert "[REDACTED_CARD]" in redacted
        assert "[REDACTED_IBAN]" in redacted

    def test_redact_sensitive_data_recursive(self):
        data = {
            "user": {
                "name": "Alice Doe",
                "ssn": CANARY_SSN,
                "financial": {
                    "card": CANARY_CREDIT_CARD,
                    "pin": "1234",
                },
            },
            "records": [
                {"id": 1, "pan": CANARY_PAN},
                {"id": 2, "secret": CANARY_PASSWORD},
            ],
        }
        sanitized = redact_sensitive_data(data)
        out_str = json.dumps(sanitized)
        assert CANARY_SSN not in out_str
        assert CANARY_CREDIT_CARD not in out_str
        assert CANARY_PAN not in out_str
        assert CANARY_PASSWORD not in out_str
        assert sanitized["user"]["name"] == "Alice Doe"


# ===========================================================================
# 3. Tier 3 Contact PII vs Public Business Contacts
# ===========================================================================
class TestContactPIIAndPublicBusiness:

    def test_tier3_personal_contacts(self):
        assert classify_sensitivity(CANARY_PERSONAL_EMAIL, key_name="email") == SensitivityTier.TIER_3_CONTACT_PII
        assert classify_sensitivity(CANARY_PERSONAL_PHONE, key_name="phone") == SensitivityTier.TIER_3_CONTACT_PII

    def test_public_business_contacts(self):
        assert is_public_business_email(CANARY_PUBLIC_BUSINESS_EMAIL) is True
        assert is_public_business_email("sales@enterprise.org") is True
        assert is_public_business_email("support@company.co.uk") is True
        assert is_public_business_email("help@service.io") is True
        assert is_public_business_email(CANARY_PERSONAL_EMAIL) is False

        assert classify_sensitivity(CANARY_PUBLIC_BUSINESS_EMAIL, key_name="email") == SensitivityTier.PUBLIC_BUSINESS_CONTACT
        assert classify_sensitivity("contact@startup.ai", key_name="email") == SensitivityTier.PUBLIC_BUSINESS_CONTACT

    def test_false_positive_handling(self):
        non_sensitive_keys = [
            "status_code", "update_order", "pass_rate", "user_name",
            "total_items", "description", "category", "product_title"
        ]
        for key in non_sensitive_keys:
            assert is_tier1_key(key) is False
            assert is_tier2_key(key) is False
            assert classify_sensitivity("standard_value", key_name=key) == SensitivityTier.NOT_SENSITIVE

    def test_authorized_contact_extraction_preserves_public_contact(self):
        extracted = {
            "company": "Acme Inc.",
            "email": CANARY_PUBLIC_BUSINESS_EMAIL,
            "support_phone": CANARY_PUBLIC_BUSINESS_PHONE,
            "internal_secret": CANARY_PASSWORD,
        }
        # When contact extraction is explicitly authorized:
        sanitized = redact_sensitive_data(
            extracted,
            mask_tier3=False,
            preserve_contact_info=True,
        )
        assert sanitized["company"] == "Acme Inc."
        assert sanitized["email"] == CANARY_PUBLIC_BUSINESS_EMAIL
        assert sanitized["support_phone"] == CANARY_PUBLIC_BUSINESS_PHONE
        # Tier 1 secret is STILL redacted!
        assert CANARY_PASSWORD not in str(sanitized)
        assert sanitized["internal_secret"] == "[REDACTED]"


# ===========================================================================
# 4. Per-Task Sensitive Data Vault
# ===========================================================================
class TestTaskVault:

    def test_vault_placeholder_generation_and_substitution(self):
        task_id = "task-vault-test-1"
        vault = get_task_vault(task_id, calling_task_id=task_id)

        # Vault contact values
        ph_email = vault.store_contact("email", CANARY_PERSONAL_EMAIL)
        ph_phone = vault.store_contact("phone", CANARY_PERSONAL_PHONE)

        assert ph_email == "{{USER_EMAIL}}"
        assert ph_phone == "{{USER_PHONE}}"

        # Sanitizing text with vault replaces values with placeholders
        text = f"Please contact me at {CANARY_PERSONAL_EMAIL} or call {CANARY_PERSONAL_PHONE}"
        sanitized = vault.sanitize_and_vault_text(text)
        assert CANARY_PERSONAL_EMAIL not in sanitized
        assert CANARY_PERSONAL_PHONE not in sanitized
        assert "{{USER_EMAIL}}" in sanitized
        assert "{{USER_PHONE}}" in sanitized

        # Form-filling stage: placeholder substitution restores authorized values
        restored = vault.substitute_placeholders(sanitized, calling_task_id=task_id)
        assert CANARY_PERSONAL_EMAIL in restored
        assert CANARY_PERSONAL_PHONE in restored

    def test_vault_rejects_tier1_secrets(self):
        vault = TaskVault("task-vault-sec")
        with pytest.raises(ValueError) as exc_info:
            vault.store_contact("password", CANARY_PASSWORD)
        assert "Tier 1 secrets cannot be stored" in str(exc_info.value)

        with pytest.raises(ValueError) as exc_info:
            vault.store_contact("api_key", CANARY_API_KEY)
        assert "Tier 1 secrets cannot be stored" in str(exc_info.value)

    def test_vault_cleanup_on_terminal_task(self):
        task_id = "task-vault-cleanup"
        vault = get_task_vault(task_id, calling_task_id=task_id)
        vault.store_contact("email", CANARY_PERSONAL_EMAIL)
        assert len(vault.get_all_placeholders()) == 1

        clear_task_vault(task_id)
        new_vault = get_task_vault(task_id, calling_task_id=task_id)
        assert len(new_vault.get_all_placeholders()) == 0


# ===========================================================================
# 5. Task Context & Filled Fields Protection
# ===========================================================================
class TestTaskContextProtection:

    def test_filled_fields_masks_sensitive_values(self):
        from models.schemas import TaskContext
        ctx = TaskContext()

        # Sensitive password / token fields masked to '********'
        ctx.record_fill(selector="#pwd", label="Account Password", value=CANARY_PASSWORD)
        ctx.record_fill(selector="#card", label="Credit Card Number", value=CANARY_CREDIT_CARD)
        ctx.record_fill(selector="#cvv", label="CVV Code", value="123")

        # Vault placeholder remains placeholder
        ctx.record_fill(selector="#email", label="Email Address", value="{{USER_EMAIL}}")

        filled = ctx.filled_fields
        assert filled["#pwd"]["value"] == "********"
        assert filled["#card"]["value"] == "********"
        assert filled["#cvv"]["value"] == "********"
        assert filled["#email"]["value"] == "{{USER_EMAIL}}"

        # Canary secrets must NEVER appear in filled_fields
        filled_str = json.dumps(filled)
        assert CANARY_PASSWORD not in filled_str
        assert CANARY_CREDIT_CARD not in filled_str


# ===========================================================================
# 6. Task Archive & Storage Sanitization
# ===========================================================================
class TestTaskArchiveSanitization:

    def test_task_archive_sanitizes_canaries(self, tmp_path):
        from services.task_archive import TaskArchive
        from models.schemas import TaskResponse, TaskStatus, AgentStep, ActionType

        archive_db = tmp_path / "test_archive.db"
        archive = TaskArchive(db_path=str(archive_db))

        now = time.time()
        task = TaskResponse(
            task_id="task-archive-canary",
            goal=f"Fill application form with password {CANARY_PASSWORD}",
            status=TaskStatus.COMPLETED,
            created_at=now,
            updated_at=now,
            steps=[
                AgentStep(
                    step_number=1,
                    action=ActionType.TYPE_TEXT,
                    description=f"Typed credential {CANARY_PASSWORD}",
                    reasoning=f"Using token {CANARY_API_KEY}",
                )
            ],
            extracted_data=[
                {"user_ssn": CANARY_SSN, "card": CANARY_CREDIT_CARD}
            ],
        )

        archive.save_task(task)

        # Inspect raw SQLite database content
        import sqlite3
        conn = sqlite3.connect(str(archive_db))
        row = conn.execute("SELECT task_json FROM archived_tasks WHERE task_id = ?", ("task-archive-canary",)).fetchone()
        conn.close()

        assert row is not None
        saved_json = row[0]

        # Verify NONE of the canaries leaked into persisted task JSON
        assert CANARY_PASSWORD not in saved_json
        assert CANARY_API_KEY not in saved_json
        assert CANARY_SSN not in saved_json
        assert CANARY_CREDIT_CARD not in saved_json
        assert "[REDACTED]" in saved_json or "[REDACTED_SSN]" in saved_json


# ===========================================================================
# 7. Report Generator PDF Sanitization
# ===========================================================================
class TestReportGeneratorSanitization:

    def test_report_generator_sanitizes_canaries(self):
        from services.report_generation_service import ReportGenerator

        step_data = {
            "password": CANARY_PASSWORD,
            "api_key": CANARY_API_KEY,
            "credit_card": CANARY_CREDIT_CARD,
            "ssn": CANARY_SSN,
            "safe_metric": "42 items",
        }
        sanitized_json = ReportGenerator._sanitize_step_data(step_data)
        assert CANARY_PASSWORD not in sanitized_json
        assert CANARY_API_KEY not in sanitized_json
        assert CANARY_CREDIT_CARD not in sanitized_json
        assert CANARY_SSN not in sanitized_json
        assert "42 items" in sanitized_json

        explanation = f"Action taken with password={CANARY_PASSWORD} and key={CANARY_API_KEY}"
        clean_exp = ReportGenerator._sanitize_decision_explanation(explanation)
        assert CANARY_PASSWORD not in clean_exp
        assert CANARY_API_KEY not in clean_exp


# ===========================================================================
# 8. Extraction Safety Threshold
# ===========================================================================
class TestExtractionSafetyThreshold:

    def test_under_50_percent_allowed(self):
        # 1 sensitive leaf out of 4 (25%) -> Permitted
        data = {
            "company": "Tech Corp",
            "location": "San Francisco",
            "employees": 150,
            "financial_card": CANARY_CREDIT_CARD,
        }
        allowed, reason, sanitized = check_extraction_safety_threshold(data, task_id="task-ext-1")
        assert allowed is True
        assert reason == "Safe"
        assert CANARY_CREDIT_CARD not in str(sanitized)

    def test_exact_50_percent_boundary_allowed(self):
        # Exactly 2 sensitive out of 4 leaves (50.0%) -> Permitted (refusal is strictly > 50%)
        data = {
            "item_name": "Laptop",
            "stock": 10,
            "card_num": CANARY_CREDIT_CARD,
            "user_ssn": CANARY_SSN,
        }
        allowed, reason, sanitized = check_extraction_safety_threshold(data, task_id="task-ext-boundary")
        assert allowed is True
        assert reason == "Safe"
        assert CANARY_CREDIT_CARD not in str(sanitized)
        assert CANARY_SSN not in str(sanitized)

    def test_over_50_percent_refused(self):
        # 3 sensitive out of 4 leaves (75.0%) -> Refused
        audit_logger.clear()
        data = {
            "title": "Confidential Extract",
            "card_num": CANARY_CREDIT_CARD,
            "user_ssn": CANARY_SSN,
            "password": CANARY_PASSWORD,
        }
        allowed, reason, sanitized = check_extraction_safety_threshold(data, task_id="task-ext-refused")
        assert allowed is False
        assert "Extraction safety threshold exceeded" in reason
        assert "75.0%" in reason
        assert sanitized is None

        # Verify audit event was logged
        events = audit_logger.get_events_for_task("task-ext-refused")
        assert any(e.event_type == "extraction_refused" for e in events)

    def test_preserves_explicit_public_business_contacts(self):
        data = {
            "company": "Global Solutions",
            "website": "https://globalsolutions.com",
            "email": CANARY_PUBLIC_BUSINESS_EMAIL,
            "phone": CANARY_PUBLIC_BUSINESS_PHONE,
        }
        allowed, reason, sanitized = check_extraction_safety_threshold(
            data,
            task_id="task-ext-biz",
            is_explicit_contact_request=True,
        )
        assert allowed is True
        assert sanitized["email"] == CANARY_PUBLIC_BUSINESS_EMAIL
        assert sanitized["phone"] == CANARY_PUBLIC_BUSINESS_PHONE


# ===========================================================================
# 9. User-Assisted Authentication Checks
# ===========================================================================
class TestUserAssistedAuthentication:

    def test_is_auth_field_detection(self):
        # Password and OTP inputs detected
        assert is_auth_field({"type": "password", "name": "pwd"}) is True
        assert is_auth_field({"tag": "input", "autocomplete": "current-password"}) is True
        assert is_auth_field({"tag": "input", "autocomplete": "one-time-code"}) is True
        assert is_auth_field({"name": "otp_verification"}) is True
        assert is_auth_field({"placeholder": "Enter 2FA Code"}) is True
        assert is_auth_field({"aria_label": "Security Passcode"}) is True

        # Normal form fields are not auth fields
        assert is_auth_field({"type": "text", "name": "username"}) is False
        assert is_auth_field({"type": "email", "name": "user_email"}) is False
        assert is_auth_field({"type": "text", "name": "shipping_address"}) is False

    def test_auth_audit_events_logged(self):
        audit_logger.clear()
        task_id = "task-auth-test"
        audit_logger.log_auth_event(task_id, action="paused", reason="Password input detected")
        audit_logger.log_auth_event(task_id, action="resumed", reason="User signed in")

        events = audit_logger.get_events_for_task(task_id)
        assert len(events) == 2
        assert events[0].event_type == "user_assisted_auth_paused"
        assert events[1].event_type == "user_assisted_auth_resumed"


# ===========================================================================
# 10. Upload Safety & Allowlist Validation
# ===========================================================================
class TestUploadSafety:

    def test_explicit_path_and_allowlist(self, tmp_path):
        # Empty or missing path rejected
        valid, reason, _ = validate_upload_path("")
        assert valid is False
        assert "explicit file path" in reason

        # Sensitive file rejected
        env_file = tmp_path / ".env"
        env_file.write_text("API_SECRET=123")
        valid, reason, _ = validate_upload_path(str(env_file))
        assert valid is False
        assert "sensitive file" in reason.lower()

        # Regular file in allowlisted directory accepted
        doc = tmp_path / "resume.pdf"
        doc.write_bytes(b"%PDF-1.4 dummy")
        valid, reason, resolved = validate_upload_path(str(doc))
        assert valid is True
        assert resolved == str(doc.resolve())

        # Disallowed system path rejected
        valid, reason, _ = validate_upload_path("C:/Windows/System32/cmd.exe")
        assert valid is False
        assert "outside allowlisted" in reason


# ===========================================================================
# 11. Cache Service Sanitization Integration
# ===========================================================================
class TestCacheServiceSanitization:

    def test_cache_service_sanitizes_canaries(self):
        from services.cache_service import default_cache_service

        sensitive_dict = {
            "password": CANARY_PASSWORD,
            "api_key": CANARY_API_KEY,
            "normal_field": "safe_value",
        }
        sanitized = default_cache_service.sanitize_sensitive_data(sensitive_dict)
        assert sanitized["password"] == "[REDACTED]"
        assert sanitized["api_key"] == "[REDACTED]"
        assert sanitized["normal_field"] == "safe_value"
        assert CANARY_PASSWORD not in str(sanitized)
        assert CANARY_API_KEY not in str(sanitized)


# ===========================================================================
# 12. Audit Logger Non-Leakage Verification
# ===========================================================================
class TestAuditLoggerNonLeakage:

    def test_audit_logger_never_records_canaries(self):
        # Verify no canary secret exists anywhere in the entire audit logger history
        all_events_json = json.dumps([e.to_dict() for e in audit_logger.events])
        assert CANARY_PASSWORD not in all_events_json
        assert CANARY_API_KEY not in all_events_json
        assert CANARY_PRIVATE_KEY not in all_events_json
        assert CANARY_CREDIT_CARD not in all_events_json
        assert CANARY_SSN not in all_events_json


# ===========================================================================
# 13. Security Hardening Regression Tests (Issue 1 Review Fixes)
# ===========================================================================
import unittest.mock as mock
from models.schemas import PageCategory, PageMetadata, ExtractionResult, TaskRequest, TaskResponse, TaskStatus, AgentStep, ActionType
from browser.extractor import InfoExtractor
from api.routes import _sanitize_task_response
from api.task_manager import task_manager


class TestExtractionSafetyHardening:

    @pytest.mark.asyncio
    async def test_extraction_policy_exception_fails_closed(self):
        extractor = InfoExtractor(wait_for_idle=False)
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/data"
        mock_page.title.return_value = "Example Page"
        mock_page.evaluate.return_value = {}

        with mock.patch("backend.security.check_extraction_safety_threshold", side_effect=RuntimeError("Security engine failure")):
            result = await extractor.extract(page=mock_page, category=PageCategory.GENERIC)
            assert result.data.get("status") == "Refused"
            assert "failed" in result.data.get("error", "").lower()
            assert any("failed" in e.lower() for e in result.errors)

    @pytest.mark.asyncio
    async def test_extraction_metadata_redaction(self):
        extractor = InfoExtractor(wait_for_idle=False)
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/contact"
        mock_page.title.return_value = "Normal Title"
        mock_metadata = PageMetadata(
            page_title=f"Secret Page with token {CANARY_API_KEY}",
            meta_description=f"Admin password is {CANARY_PASSWORD}",
        )
        with mock.patch("browser.extractor.extract_metadata", return_value=mock_metadata):
            result = await extractor.extract(page=mock_page, category=PageCategory.GENERIC)
            assert CANARY_PASSWORD not in str(result.metadata)
            assert CANARY_API_KEY not in str(result.metadata)

    @pytest.mark.asyncio
    async def test_contact_category_without_explicit_intent(self):
        extractor = InfoExtractor(wait_for_idle=False)
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/team"
        mock_metadata = PageMetadata(page_title="Team")
        contact_payload = {
            "email": CANARY_PERSONAL_EMAIL,
            "phone": CANARY_PERSONAL_PHONE,
            "description": "Personal developer profile",
        }
        with mock.patch("browser.extractor.extract_metadata", return_value=mock_metadata):
            with mock.patch.object(extractor, "_dispatch", return_value=contact_payload):
                result = await extractor.extract(
                    page=mock_page,
                    category=PageCategory.CONTACT,
                    user_intent=None,
                    is_explicit_contact_request=False,
                )
                assert CANARY_PERSONAL_EMAIL not in str(result.data)
                assert CANARY_PERSONAL_PHONE not in str(result.data)

    @pytest.mark.asyncio
    async def test_contact_category_with_explicit_intent(self):
        extractor = InfoExtractor(wait_for_idle=False)
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/contact"
        mock_metadata = PageMetadata(page_title="Contact Us")
        contact_payload = {
            "name": "Acme Support",
            "email": CANARY_PUBLIC_BUSINESS_EMAIL,
            "phone": CANARY_PUBLIC_BUSINESS_PHONE,
        }
        with mock.patch("browser.extractor.extract_metadata", return_value=mock_metadata):
            with mock.patch.object(extractor, "_dispatch", return_value=contact_payload):
                result = await extractor.extract(
                    page=mock_page,
                    category=PageCategory.CONTACT,
                    user_intent="Extract the contact details and email address of support",
                    is_explicit_contact_request=True,
                )
                assert result.data["email"] == CANARY_PUBLIC_BUSINESS_EMAIL


class TestUploadSafetyHardening:

    def test_oversized_upload_rejected(self, tmp_path):
        big_file = tmp_path / "large_file.txt"
        big_file.write_bytes(b"A" * 2000)
        valid, reason, resolved = validate_upload_path(
            str(big_file),
            max_file_size=1000,
        )
        assert valid is False
        assert "exceeds maximum permitted limit" in reason.lower()

    def test_file_content_inspection_rejects_disguised_keys(self, tmp_path):
        fake_doc = tmp_path / "document.pdf"
        fake_doc.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0canary...")
        valid, reason, resolved = validate_upload_path(str(fake_doc))
        assert valid is False
        assert "cryptographic keys" in reason.lower() or "credentials" in reason.lower() or "secrets" in reason.lower()

    def test_upload_logging_does_not_log_full_local_paths(self, tmp_path):
        audit_logger.clear()
        nested_dir = tmp_path / "private_user_home" / "subfolder"
        nested_dir.mkdir(parents=True)
        secret_file = nested_dir / ".env"
        secret_file.write_text("FOO=BAR")
        valid, reason, resolved = validate_upload_path(str(secret_file))
        assert valid is False
        events = audit_logger.get_events_for_task("upload")
        assert len(events) >= 1
        logged_filename = events[-1].details.get("filename", "")
        assert "private_user_home" not in logged_filename
        assert "private_user_home" not in reason

    def test_explicit_user_approved_upload(self, tmp_path):
        outside_file = tmp_path / "external_approved_file.pdf"
        outside_file.write_bytes(b"%PDF-1.4 sample approved content")
        valid, reason, resolved = validate_upload_path(str(outside_file), user_approved=True)
        assert valid is True
        assert resolved == str(outside_file.resolve())


class TestAuditLogSanitizationHardening:

    def test_audit_event_values_sanitized(self):
        audit_logger.clear()
        audit_logger.record_event(
            event_type="test_event",
            task_id="task-123",
            details={
                "reason": f"Authentication failed with password {CANARY_PASSWORD}",
                "description": f"Customer email is {CANARY_PERSONAL_EMAIL} and phone is {CANARY_PERSONAL_PHONE}",
                "error_info": f"Token verification failed for {CANARY_API_KEY}",
            },
        )
        events = audit_logger.get_events_for_task("task-123")
        assert len(events) == 1
        d = events[0].details
        assert CANARY_PASSWORD not in d["reason"]
        assert CANARY_PERSONAL_EMAIL not in d["description"]
        assert CANARY_PERSONAL_PHONE not in d["description"]
        assert CANARY_API_KEY not in d["error_info"]


class TestTaskVaultIsolationAndCleanupHardening:

    def test_vault_get_all_placeholders_does_not_leak_raw_values(self):
        vault = TaskVault("task-vault-test-1")
        ph_email = vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)
        ph_phone = vault.store_contact("PHONE", CANARY_PERSONAL_PHONE)

        all_placeholders = vault.get_all_placeholders()
        assert CANARY_PERSONAL_EMAIL not in str(all_placeholders)
        assert CANARY_PERSONAL_PHONE not in str(all_placeholders)
        assert all_placeholders[ph_email] == "[VAULTED_CONTACT]"
        assert all_placeholders[ph_phone] == "[VAULTED_CONTACT]"

    def test_vault_cross_task_access_blocked(self):
        vault_a = TaskVault("task-A")
        ph = vault_a.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)

        res = vault_a.substitute_placeholders(f"Send to {ph}", calling_task_id="task-B")
        assert CANARY_PERSONAL_EMAIL not in res
        assert ph in res

        raw = vault_a.get_raw_value(ph, calling_task_id="task-B")
        assert raw is None

        res_ok = vault_a.substitute_placeholders(f"Send to {ph}", calling_task_id="task-A")
        assert res_ok == f"Send to {CANARY_PERSONAL_EMAIL}"

    def test_vault_cleanup_on_terminal_task_completion(self):
        task_id = "task-cleanup-test"
        vault = get_task_vault(task_id, calling_task_id=task_id)
        vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)
        assert len(vault.get_placeholders()) > 0

        clear_task_vault(task_id)
        assert len(vault.get_placeholders()) == 0

    @pytest.mark.asyncio
    async def test_task_manager_delete_task_cleans_vault(self):
        task_id = "task-del-vault-test"
        task_resp = TaskResponse(
            task_id=task_id,
            goal="Do something",
            status=TaskStatus.COMPLETED,
            created_at=time.time(),
            updated_at=time.time(),
        )
        task_manager.tasks[task_id] = task_resp
        vault = get_task_vault(task_id, calling_task_id=task_id)
        vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)

        await task_manager.delete_task(task_id)
        assert len(vault.get_placeholders()) == 0


class TestApiRoutesResponseSanitizationHardening:

    def test_sanitize_task_response_hides_secrets(self):
        step = AgentStep(
            step_number=1,
            action=ActionType.CLICK,
            description=f"Logged in using password {CANARY_PASSWORD}",
            reasoning=f"Found session token {CANARY_API_KEY}",
            error=f"Leaked personal email {CANARY_PERSONAL_EMAIL}",
        )
        task = TaskResponse(
            task_id="task-resp-test",
            goal=f"Update account with secret {CANARY_PASSWORD}",
            status=TaskStatus.RUNNING,
            created_at=time.time(),
            updated_at=time.time(),
            steps=[step],
            error=f"Fatal error with password {CANARY_PASSWORD}",
        )
        sanitized = _sanitize_task_response(task)
        assert CANARY_PASSWORD not in sanitized.goal
        assert CANARY_PASSWORD not in sanitized.error
        assert CANARY_PASSWORD not in sanitized.steps[0].description
        assert CANARY_API_KEY not in sanitized.steps[0].reasoning
        assert CANARY_PERSONAL_EMAIL not in sanitized.steps[0].error

    def test_create_task_blocked_goal_redacts_goal(self):
        req = TaskRequest(
            goal=f"Authenticate using password {CANARY_PASSWORD}",
            start_url="https://example.com",
        )
        resp = task_manager.create_task(req)
        assert resp.status == TaskStatus.FAILED
        assert CANARY_PASSWORD not in resp.goal
        assert CANARY_PASSWORD not in resp.error


# ===========================================================================
# 14. Deepened Security Remediation Regression Tests (Issue 1 Follow-up)
# ===========================================================================

class TestVaultTaskIdentityEnforcement:

    def test_vault_get_raw_value_rejects_none_caller_id(self):
        vault = TaskVault("task-ident-1")
        ph = vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)
        # Missing caller identity MUST NOT bypass isolation
        assert vault.get_raw_value(ph, calling_task_id=None) is None

    def test_vault_substitute_placeholders_rejects_none_caller_id(self):
        vault = TaskVault("task-ident-2")
        ph = vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)
        # Missing caller identity must return text un-substituted
        result = vault.substitute_placeholders(f"Contact {ph}", calling_task_id=None)
        assert result == f"Contact {ph}"
        assert CANARY_PERSONAL_EMAIL not in result

    def test_vault_manager_get_vault_rejects_none_caller_id(self):
        from backend.security.vault import default_vault_manager
        with pytest.raises(PermissionError) as exc_info:
            default_vault_manager.get_vault("task-ident-3", calling_task_id=None)
        assert "requires valid calling_task_id" in str(exc_info.value)

    def test_get_task_vault_rejects_none_caller_id(self):
        with pytest.raises(PermissionError) as exc_info:
            get_task_vault("task-ident-4", calling_task_id=None)
        assert "requires valid calling_task_id" in str(exc_info.value)

    def test_legitimate_task_execution_with_matching_identity_succeeds(self):
        task_id = "task-ident-5"
        vault = get_task_vault(task_id, calling_task_id=task_id)
        ph = vault.store_contact("EMAIL", CANARY_PERSONAL_EMAIL)
        raw = vault.get_raw_value(ph, calling_task_id=task_id)
        assert raw == CANARY_PERSONAL_EMAIL
        sub = vault.substitute_placeholders(f"Send to {ph}", calling_task_id=task_id)
        assert sub == f"Send to {CANARY_PERSONAL_EMAIL}"


class TestNarrowExplicitContactExceptions:

    def test_is_sensitive_leaf_email_only_intent_masks_phone_and_address(self):
        from backend.security.policy import is_sensitive_leaf
        # Explicit intent specifically for email: email is allowed, phone and address are sensitive
        assert is_sensitive_leaf("email", CANARY_PERSONAL_EMAIL, user_intent="Extract support email") is False
        assert is_sensitive_leaf("phone", CANARY_PERSONAL_PHONE, user_intent="Extract support email") is True
        assert is_sensitive_leaf("address", "123 Private St", user_intent="Extract support email") is True

    def test_is_sensitive_leaf_phone_only_intent_masks_personal_email(self):
        from backend.security.policy import is_sensitive_leaf
        # Explicit intent specifically for phone: phone is allowed, personal email is sensitive
        assert is_sensitive_leaf("phone", CANARY_PERSONAL_PHONE, user_intent="Call the office phone") is False
        assert is_sensitive_leaf("email", CANARY_PERSONAL_EMAIL, user_intent="Call the office phone") is True

    def test_check_extraction_mixed_contact_preserves_only_requested_type(self):
        mixed_payload = {
            "name": "Jane Recruiter",
            "email": CANARY_PERSONAL_EMAIL,
            "phone": CANARY_PERSONAL_PHONE,
            "address": "456 Private Ave",
        }
        # User only requested email
        allowed, reason, sanitized = check_extraction_safety_threshold(
            mixed_payload,
            task_id="task-mixed-contact",
            is_explicit_contact_request=True,
            user_intent="Please extract the recruiter's email address",
        )
        assert allowed is True
        assert sanitized["email"] == CANARY_PERSONAL_EMAIL
        assert sanitized["phone"] == "[REDACTED]"
        assert CANARY_PERSONAL_PHONE not in str(sanitized)

    def test_mixed_contact_public_and_personal(self):
        mixed_payload = {
            "support_email": CANARY_PUBLIC_BUSINESS_EMAIL,
            "personal_direct_cell": CANARY_PERSONAL_PHONE,
        }
        allowed, reason, sanitized = check_extraction_safety_threshold(
            mixed_payload,
            task_id="task-mixed-pub-priv",
            is_explicit_contact_request=True,
            user_intent="Get the contact email for customer service",
        )
        assert allowed is True
        assert sanitized["support_email"] == CANARY_PUBLIC_BUSINESS_EMAIL
        assert sanitized["personal_direct_cell"] in ("[REDACTED]", "[REDACTED_PHONE]")
        assert CANARY_PERSONAL_PHONE not in str(sanitized)


class TestUploadContentInspectionAndSafety:

    def test_upload_content_inspection_deep_text_secret_beyond_4kb(self, tmp_path):
        deep_secret_file = tmp_path / "data_export.csv"
        # 6KB of harmless data followed by a secret
        filler = "col1,col2,col3\n" + ("safe_item,123,regular_entry\n" * 200)
        deep_secret_file.write_text(filler + f"admin_key,{CANARY_API_KEY},restricted\n")
        valid, reason, _ = validate_upload_path(str(deep_secret_file))
        assert valid is False
        assert "sensitive credentials" in reason.lower() or "secrets" in reason.lower()

    def test_upload_disguised_secret_with_binary_extension(self, tmp_path):
        fake_image = tmp_path / "avatar.png"
        fake_image.write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0canary...")
        valid, reason, _ = validate_upload_path(str(fake_image))
        assert valid is False
        assert "cryptographic keys" in reason.lower() or "credentials" in reason.lower()

    def test_max_upload_file_size_safe_fallback_on_invalid_env(self, monkeypatch):
        from backend.security.policy import _get_max_upload_file_size
        monkeypatch.setenv("MAX_UPLOAD_FILE_SIZE", "not_an_int")
        assert _get_max_upload_file_size() == 25 * 1024 * 1024

        monkeypatch.setenv("MAX_UPLOAD_FILE_SIZE", "-999")
        assert _get_max_upload_file_size() == 25 * 1024 * 1024

        monkeypatch.setenv("MAX_UPLOAD_FILE_SIZE", "5000000000")  # > 500MB
        assert _get_max_upload_file_size() == 500 * 1024 * 1024

    def test_handle_os_file_dialog_defaults_user_approved_to_false(self):
        import inspect
        from backend.browser.os_dialog import handle_os_file_dialog
        sig = inspect.signature(handle_os_file_dialog)
        assert "user_approved" in sig.parameters
        assert sig.parameters["user_approved"].default is False


class TestAuditLoggerVerificationDeep:

    def test_audit_logger_nested_dictionaries_and_lists(self):
        audit_logger.clear()
        nested_details = {
            "outer": {
                "inner_list": [
                    {"secret_note": f"password is {CANARY_PASSWORD}"},
                    f"token: {CANARY_API_KEY}",
                ],
                "contact_info": {
                    "personal_email": CANARY_PERSONAL_EMAIL,
                    "phone": CANARY_PERSONAL_PHONE,
                },
            }
        }
        audit_logger.record_event(
            event_type="nested_test",
            task_id="task-nest-1",
            details=nested_details,
        )
        events = audit_logger.get_events_for_task("task-nest-1")
        assert len(events) == 1
        event_str = json.dumps(events[0].to_dict())
        assert CANARY_PASSWORD not in event_str
        assert CANARY_API_KEY not in event_str
        assert CANARY_PERSONAL_EMAIL not in event_str
        assert CANARY_PERSONAL_PHONE not in event_str

    def test_audit_logger_exception_messages(self):
        audit_logger.clear()
        exc = RuntimeError(f"Connection failure: token={CANARY_API_KEY} pwd={CANARY_PASSWORD}")
        audit_logger.record_event(
            event_type="error_event",
            task_id="task-err-1",
            details={"exception": exc},
        )
        events = audit_logger.get_events_for_task("task-err-1")
        assert len(events) == 1
        event_str = json.dumps(events[0].to_dict())
        assert CANARY_PASSWORD not in event_str
        assert CANARY_API_KEY not in event_str

    def test_audit_logger_arbitrary_string_values_and_task_id(self):
        audit_logger.clear()
        audit_logger.record_event(
            event_type=f"event_{CANARY_PASSWORD}",
            task_id=f"task_{CANARY_API_KEY}",
            details={"arbitrary_str": f"Sensitive data {CANARY_SSN} and {CANARY_PERSONAL_PHONE}"},
        )
        assert len(audit_logger.events) >= 1
        last_event = audit_logger.events[-1]
        assert CANARY_PASSWORD not in last_event.event_type
        assert CANARY_API_KEY not in last_event.task_id
        assert CANARY_SSN not in str(last_event.details)
        assert CANARY_PERSONAL_PHONE not in str(last_event.details)


# ===========================================================================
# 16. Final Remediation Regression Tests (Priorities 1 - 5)
# ===========================================================================
from browser.controller import BrowserController
from browser.extension_adapter import ExtensionBrowserAdapter
from browser.form_discovery import _parse_raw_field


class TestDetectionAccuracyAndClassification:

    def test_custom_domain_personal_emails_not_classified_as_public(self):
        # Custom-domain personal emails must NOT be treated as public business mailboxes
        custom_personal_1 = "john.doe@customdomain.com"
        custom_personal_2 = "alice@smithfamily.org"
        custom_personal_3 = "developer@personalportfolio.me"

        assert is_public_business_email(custom_personal_1) is False
        assert is_public_business_email(custom_personal_2) is False
        assert is_public_business_email(custom_personal_3) is False

        assert classify_sensitivity(custom_personal_1, key_name="email") == SensitivityTier.TIER_3_CONTACT_PII
        assert classify_sensitivity(custom_personal_2, key_name="contact") == SensitivityTier.TIER_3_CONTACT_PII

    def test_public_business_role_emails_classified_correctly(self):
        # Public business role prefixes on corporate domains
        assert is_public_business_email("info@acme-corp.com") is True
        assert is_public_business_email("support@techcorp.io") is True
        assert is_public_business_email("sales@enterprisesolutions.net") is True
        assert is_public_business_email("help-desk@company.org") is True
        assert is_public_business_email("customer.service@store.com") is True

        assert classify_sensitivity("info@acme-corp.com", key_name="email") == SensitivityTier.PUBLIC_BUSINESS_CONTACT

    def test_personal_domain_emails_always_not_public(self):
        # Even role-like names on personal domains are not public business mailboxes
        assert is_public_business_email("info@gmail.com") is False
        assert is_public_business_email("support@yahoo.com") is False
        assert is_public_business_email("user@hotmail.com") is False

    def test_is_sensitive_key_includes_tier3_keys(self):
        # Documented behavior: represents sensitive data in ANY tier
        assert is_sensitive_key("email") is True
        assert is_sensitive_key("phone_number") is True
        assert is_sensitive_key("shipping_address") is True
        assert is_sensitive_key("home_address") is True
        assert is_sensitive_key("postal_code") is True
        assert is_sensitive_key("contact_info") is True

        # Tier 1 and 2 keys still return True
        assert is_sensitive_key("password") is True
        assert is_sensitive_key("access_token") is True
        assert is_sensitive_key("credit_card") is True
        assert is_sensitive_key("ssn") is True

        # Non-sensitive keys return False
        assert is_sensitive_key("query") is False
        assert is_sensitive_key("title") is False
        assert is_sensitive_key("category") is False


class TestDOMSanitizationAndProtection:

    @pytest.mark.asyncio
    async def test_controller_dom_sanitization_masks_contacts_headings_and_questions(self):
        controller = BrowserController(screenshots_dir="./screenshots")
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/checkout"
        controller.current_page = mock_page

        raw_dom = {
            "title": "Checkout",
            "url": "https://example.com/checkout",
            "elements": [
                {
                    "index": 1,
                    "tag": "input",
                    "type": "text",
                    "value": CANARY_PERSONAL_EMAIL,
                    "label": f"Customer email: {CANARY_PERSONAL_EMAIL}",
                    "placeholder": f"e.g. {CANARY_PERSONAL_EMAIL}",
                    "text": f"Direct contact: {CANARY_PERSONAL_PHONE}",
                    "aria_label": f"Enter phone {CANARY_PERSONAL_PHONE}",
                    "title": f"Contact via {CANARY_PERSONAL_EMAIL}",
                },
                {
                    "index": 2,
                    "tag": "div",
                    "type": "text",
                    "value": "",
                    "label": "Public support line",
                    "text": f"For help contact {CANARY_PUBLIC_BUSINESS_EMAIL}",
                }
            ],
            "page_text": f"Contact the client at {CANARY_PERSONAL_EMAIL} or call {CANARY_PERSONAL_PHONE}.",
            "headings": [f"Account details for {CANARY_PERSONAL_EMAIL}"],
            "form_questions": [f"Is your phone number {CANARY_PERSONAL_PHONE}?"],
        }

        mock_page.evaluate = mock.AsyncMock(return_value=raw_dom)

        result = await controller.parse_interactive_elements(use_cache=False)
        elem = result["elements"][0]

        # Tier 3 personal contacts must be masked
        assert CANARY_PERSONAL_EMAIL not in elem["value"]
        assert CANARY_PERSONAL_EMAIL not in elem["label"]
        assert CANARY_PERSONAL_EMAIL not in elem["placeholder"]
        assert CANARY_PERSONAL_PHONE not in elem["text"]
        assert CANARY_PERSONAL_PHONE not in elem["aria_label"]
        assert CANARY_PERSONAL_EMAIL not in elem["title"]
        assert CANARY_PERSONAL_EMAIL not in result["page_text"]
        assert CANARY_PERSONAL_PHONE not in result["page_text"]
        assert CANARY_PERSONAL_EMAIL not in result["headings"][0]
        assert CANARY_PERSONAL_PHONE not in result["form_questions"][0]

        # Legitimate public business email is preserved
        assert CANARY_PUBLIC_BUSINESS_EMAIL in result["elements"][1]["text"]

    @pytest.mark.asyncio
    async def test_controller_dom_sanitization_fails_closed(self):
        controller = BrowserController(screenshots_dir="./screenshots")
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/checkout"
        controller.current_page = mock_page

        mock_page.evaluate = mock.AsyncMock(return_value={"elements": [{"value": "something"}]})

        with mock.patch("backend.security.redact_text", side_effect=RuntimeError("Sanitization fault")):
            result = await controller.parse_interactive_elements(use_cache=False)
            assert result["title"] == "Error"
            assert result["elements"] == []
            assert result["page_text"] == ""


class TestScreenshotProtection:

    @pytest.mark.asyncio
    async def test_element_screenshot_skips_protected_origins(self):
        controller = BrowserController(screenshots_dir="./screenshots")
        mock_page = mock.AsyncMock()
        mock_page.url = "http://localhost:8000/admin"
        controller.current_page = mock_page

        fp, url = await controller.capture_element_screenshot("#btn", "task-1", 1)
        assert fp == ""
        assert url == ""
        mock_page.locator.assert_not_called()

    @pytest.mark.asyncio
    async def test_element_screenshot_skips_sensitive_selectors(self):
        controller = BrowserController(screenshots_dir="./screenshots")
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/login"
        controller.current_page = mock_page

        # Password / secret selector
        fp, url = await controller.capture_element_screenshot("input[type='password']", "task-1", 1)
        assert fp == ""
        assert url == ""
        mock_page.locator.assert_not_called()

        # OTP selector
        fp, url = await controller.capture_element_screenshot("#otp-input", "task-1", 2)
        assert fp == ""
        assert url == ""
        mock_page.locator.assert_not_called()

    @pytest.mark.asyncio
    async def test_screenshot_skips_when_safe_masking_fails(self):
        controller = BrowserController(screenshots_dir="./screenshots")
        mock_page = mock.AsyncMock()
        mock_page.url = "https://example.com/login"
        controller.current_page = mock_page

        # In Playwright, locator is synchronous returning a Locator object whose count() is async
        mock_loc = mock.MagicMock()
        mock_loc.count = mock.AsyncMock(side_effect=RuntimeError("DOM disconnected"))
        mock_page.locator = mock.MagicMock(return_value=mock_loc)

        fp, url = await controller.capture_screenshot("task-1", 1)
        assert fp == ""
        assert url == ""

        fp_el, url_el = await controller.capture_element_screenshot("#normal-button", "task-1", 1)
        assert fp_el == ""
        assert url_el == ""


class TestApiResponseProtectionHardening:

    def test_sanitize_task_response_fails_closed_on_exception(self):
        from api.routes import _sanitize_task_response
        task = TaskResponse(
            task_id="task-leak-check",
            goal=f"Do secret work with {CANARY_PASSWORD}",
            status=TaskStatus.RUNNING,
            created_at=time.time(),
            updated_at=time.time(),
        )

        with mock.patch("backend.security.redact_sensitive_data", side_effect=RuntimeError("Engine crash")):
            sanitized = _sanitize_task_response(task)
            assert CANARY_PASSWORD not in str(sanitized.goal)
            assert sanitized.goal == "[REDACTED]"
            assert sanitized.status == TaskStatus.FAILED
            assert "safely sanitized" in sanitized.error

    @pytest.mark.asyncio
    async def test_approve_human_step_fails_closed_on_redaction_error(self):
        from api.routes import approve_human_step
        task_id = "task-app-1"
        task_manager.tasks[task_id] = TaskResponse(
            task_id=task_id,
            goal="Wait for approval",
            status=TaskStatus.WAITING_APPROVAL,
            created_at=time.time(),
            updated_at=time.time(),
        )

        with mock.patch("backend.security.redact_text", side_effect=RuntimeError("Redactor error")):
            with mock.patch.object(task_manager, "approve_human_step") as mock_approve:
                await approve_human_step(task_id, approved=True, user_input=CANARY_PASSWORD)
                # Raw password input must NOT be passed to task_manager.approve_human_step
                mock_approve.assert_called_once()
                args = mock_approve.call_args[0]
                assert CANARY_PASSWORD not in str(args)
                assert args[2] == "[REDACTED]"


class TestExtensionAdapterProtection:

    @pytest.mark.asyncio
    async def test_extension_parse_interactive_elements_sanitizes_at_boundary(self):
        mock_mgr = mock.AsyncMock()
        adapter = ExtensionBrowserAdapter(manager=mock_mgr, task_id="task-ext-boundary")

        raw_data = {
            "url": "https://example.com/form",
            "elements": [
                {
                    "index": 1,
                    "value": CANARY_PERSONAL_EMAIL,
                    "label": f"Your Email: {CANARY_PERSONAL_EMAIL}",
                    "placeholder": f"e.g. {CANARY_PERSONAL_EMAIL}",
                    "text": f"Direct: {CANARY_PERSONAL_PHONE}",
                }
            ],
            "page_text": f"Owner contact: {CANARY_PERSONAL_EMAIL}",
            "headings": [f"Profile of {CANARY_PERSONAL_EMAIL}"],
            "form_questions": [f"Confirm phone {CANARY_PERSONAL_PHONE}?"],
            "form_fields": [
                {
                    "field_type": "text",
                    "selector": "#cust-email",
                    "current_value": CANARY_PERSONAL_EMAIL,
                    "label": f"Email: {CANARY_PERSONAL_EMAIL}",
                }
            ]
        }
        mock_mgr.send_command.return_value = {"success": True, "data": raw_data}

        result = await adapter.parse_interactive_elements()
        elem = result["elements"][0]
        ff = result["form_fields"][0]

        assert CANARY_PERSONAL_EMAIL not in elem["value"]
        assert CANARY_PERSONAL_EMAIL not in elem["label"]
        assert CANARY_PERSONAL_PHONE not in elem["text"]
        assert CANARY_PERSONAL_EMAIL not in result["page_text"]
        assert CANARY_PERSONAL_EMAIL not in result["headings"][0]
        assert CANARY_PERSONAL_PHONE not in result["form_questions"][0]
        assert CANARY_PERSONAL_EMAIL not in str(ff.current_value)
        assert CANARY_PERSONAL_EMAIL not in str(ff.label)

    @pytest.mark.asyncio
    async def test_extension_screenshot_skips_when_auth_fields_present(self):
        mock_mgr = mock.AsyncMock()
        adapter = ExtensionBrowserAdapter(manager=mock_mgr, task_id="task-ext-shot")
        adapter._current_url = "https://example.com/signin"
        adapter.element_map = {
            1: {"index": 1, "type": "password", "name": "pwd"}
        }

        fp, url = await adapter.capture_screenshot("task-ext-shot", 1)
        assert fp is None
        assert url is None
        mock_mgr.send_command.assert_not_called()
