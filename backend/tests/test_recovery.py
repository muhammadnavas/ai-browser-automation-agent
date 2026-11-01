"""Unit tests for self-healing browser error classification and retry safety."""
import unittest

from browser.recovery import classify_browser_error, _requires_manual_recovery
from models.schemas import ActionType, BrowserAction


class RecoveryClassificationTests(unittest.TestCase):
    def test_classifies_missing_locator(self):
        self.assertEqual(
            classify_browser_error("Locator not found: #submit"),
            "locator_or_stale_state",
        )

    def test_classifies_timeout(self):
        self.assertEqual(classify_browser_error("Timed out waiting for selector"), "timeout")

    def test_classifies_extension_disconnect(self):
        self.assertEqual(
            classify_browser_error("Chrome Extension disconnected during command execution"),
            "extension_connection",
        )

    def test_click_is_not_blindly_retried(self):
        self.assertTrue(_requires_manual_recovery(BrowserAction(action=ActionType.CLICK, text="Send")))

    def test_fill_can_use_bounded_recovery(self):
        self.assertFalse(_requires_manual_recovery(
            BrowserAction(action=ActionType.FILL, selector="#email", value="person@example.com")
        ))

    def test_submission_language_blocks_ambiguous_navigation_retry(self):
        self.assertTrue(_requires_manual_recovery(
            BrowserAction(action=ActionType.NAVIGATE, url="https://example.com/submit", reasoning="submit application")
        ))


if __name__ == "__main__":
    unittest.main()
