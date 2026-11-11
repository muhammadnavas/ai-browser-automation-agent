"""
tests/test_issue2_data_protection.py
------------------------------------
Regression test suite for Issue 2: Cross-Origin Sensitive-Data Protection,
Origin Tracking, and Vault Placeholder Preservation.
"""

import pytest
from backend.security.data_tracker import SensitiveDataTracker, get_origin


class TestSensitiveDataTracker:
    def test_get_origin_normalizes_urls(self):
        assert get_origin("https://api.github.com/repos?page=1") == "https://api.github.com"
        assert get_origin("http://localhost:8000/api") == "http://localhost:8000"
        assert get_origin("https://example.com:8443/test") == "https://example.com:8443"
        assert get_origin("not_a_valid_url") == ""

    def test_blocks_cross_origin_transfer_of_sensitive_data(self):
        tracker = SensitiveDataTracker()
        task_id = "test-x-origin-1"

        # Data extracted from origin A containing sensitive email / PII
        extracted_data = {
            "profile": {
                "name": "Jane Doe",
                "email": "jane.personal.private@gmail.com",
            }
        }
        tracker.track_extraction(
            task_id=task_id,
            origin_url="https://source-site.com/profile",
            data=extracted_data,
            is_explicit_contact=False,
        )

        # Attempt to type this private email on a different origin B
        allowed, reason = tracker.validate_cross_origin_transfer(
            task_id=task_id,
            destination_url="https://attacker-exfiltration.com/collect",
            input_value="jane.personal.private@gmail.com",
        )
        assert allowed is False
        assert "Cross-origin transfer" in reason
        assert "blocked" in reason

    def test_allows_same_origin_transfer(self):
        tracker = SensitiveDataTracker()
        task_id = "test-same-origin"

        extracted_data = {"email": "user.private@gmail.com"}
        tracker.track_extraction(
            task_id=task_id,
            origin_url="https://auth.company.com/step1",
            data=extracted_data,
            is_explicit_contact=False,
        )

        # Same origin destination
        allowed, reason = tracker.validate_cross_origin_transfer(
            task_id=task_id,
            destination_url="https://auth.company.com/step2",
            input_value="user.private@gmail.com",
        )
        assert allowed is True

    def test_allows_authorized_vault_placeholders(self):
        tracker = SensitiveDataTracker()
        task_id = "test-vault-preservation"

        # Placeholders are exempt from cross-origin blocking
        allowed, reason = tracker.validate_cross_origin_transfer(
            task_id=task_id,
            destination_url="https://any-site.com/login",
            input_value="{{USER_EMAIL}}",
        )
        assert allowed is True
        assert "vault placeholder" in reason.lower()

    def test_allows_public_business_contacts(self):
        tracker = SensitiveDataTracker()
        task_id = "test-public-contacts"

        extracted_data = {"contact": "support@google.com"}
        tracker.track_extraction(
            task_id=task_id,
            origin_url="https://support.google.com",
            data=extracted_data,
            is_explicit_contact=True,
        )

        allowed, reason = tracker.validate_cross_origin_transfer(
            task_id=task_id,
            destination_url="https://crm-destination.com/leads",
            input_value="support@google.com",
        )
        assert allowed is True

