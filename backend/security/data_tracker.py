"""
backend/security/data_tracker.py
--------------------------------
Cross-Origin Sensitive-Data Protection Tracker (Issue 2, Section 7).

Key Features:
- Tracks the origin (scheme + domain + port) where sensitive data or PII was observed.
- Validates cross-origin form filling, inputs, and uploads.
- Blocks unauthorized transfer of raw credentials and sensitive personal data across origins.
- Preserves legitimate TaskVault placeholders (e.g. {{USER_PASSWORD}}) and public business contacts.
- Comprehensive audit logging without leaking sensitive values.
"""

import hashlib
import logging
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse

from backend.security.audit import audit_logger
from backend.security.detector import (
    contains_tier1_secrets,
    contains_tier2_identifiers,
    is_public_business_email,
)
from backend.security.tiers import SensitivityTier

logger = logging.getLogger("security.data_tracker")


def get_origin(url: Optional[str]) -> str:
    """Extract normalized origin (scheme://hostname[:port]) from a URL."""
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url.strip())
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    except Exception:
        return ""


def hash_value(val: str) -> str:
    """Generate SHA-256 hash of a value to avoid storing raw secrets in memory."""
    return hashlib.sha256(val.encode("utf-8", errors="ignore")).hexdigest()


class SensitiveDataTracker:
    """
    Per-task registry that maps sensitive values (by hash) to their originating web origin.
    """

    def __init__(self):
        # task_id -> {value_hash -> (origin, preview_tier)}
        self._tracked_data: Dict[str, Dict[str, Tuple[str, SensitivityTier]]] = {}
        # task_id -> set of raw sensitive strings for cross-origin search
        self._tracked_strings: Dict[str, Dict[str, str]] = {}  # task_id -> {val: origin}

    def track_extraction(
        self,
        task_id: str,
        origin_url: str,
        data: any,
        is_explicit_contact: bool = False,
    ) -> None:
        """Record extracted data and bind any sensitive leaves to the source origin."""
        origin = get_origin(origin_url)
        if not origin or not data:
            return

        if task_id not in self._tracked_strings:
            self._tracked_strings[task_id] = {}

        def _traverse(item):
            if isinstance(item, dict):
                for k, v in item.items():
                    _traverse(v)
            elif isinstance(item, (list, tuple, set)):
                for v in item:
                    _traverse(v)
            elif isinstance(item, str):
                s = item.strip()
                if len(s) < 4:
                    return
                # Check if leaf contains Tier 1 or Tier 2 secrets or personal emails
                has_t1 = contains_tier1_secrets(s)
                has_t2 = contains_tier2_identifiers(s)
                is_pub_email = "@" in s and is_public_business_email(s)

                if (has_t1 or has_t2) or ("@" in s and not is_pub_email and not is_explicit_contact):
                    # Track this sensitive value under the origin
                    self._tracked_strings[task_id][s] = origin
                    h = hash_value(s)
                    if task_id not in self._tracked_data:
                        self._tracked_data[task_id] = {}
                    tier = SensitivityTier.TIER_1_SECRETS if has_t1 else SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS
                    self._tracked_data[task_id][h] = (origin, tier)

        try:
            _traverse(data)
        except Exception as exc:
            logger.warning("Error traversing extracted data for tracking: %s", exc)

    def validate_cross_origin_transfer(
        self,
        task_id: str,
        destination_url: str,
        input_value: Optional[str],
    ) -> Tuple[bool, str]:
        """
        Validate whether input_value contains sensitive data sourced from a different origin.

        Returns:
            (is_allowed: bool, reason: str)
        """
        if not input_value or not isinstance(input_value, str):
            return True, "No input value to validate."

        # Authorized TaskVault placeholders are explicitly permitted
        if input_value.startswith("{{") and input_value.endswith("}}"):
            return True, "Authorized vault placeholder."

        dest_origin = get_origin(destination_url)
        task_origins = self._tracked_strings.get(task_id, {})
        if not task_origins:
            return True, "No cross-origin sensitive data tracked for this task."

        for tracked_val, source_origin in task_origins.items():
            if tracked_val in input_value:
                # If destination origin differs from source origin
                if dest_origin and source_origin and dest_origin != source_origin:
                    audit_logger.record_event(
                        event_type="cross_origin_data_transfer_blocked",
                        task_id=task_id,
                        details={
                            "source_origin": source_origin,
                            "destination_origin": dest_origin,
                            "reason": "Unauthorized cross-origin sensitive data transfer blocked.",
                        },
                    )
                    return False, f"Cross-origin transfer of sensitive data from '{source_origin}' to '{dest_origin}' blocked."

        return True, "Transfer safe."

    def clear_task(self, task_id: str) -> None:
        """Clear tracking data for a completed or terminated task."""
        self._tracked_data.pop(task_id, None)
        self._tracked_strings.pop(task_id, None)


# Global tracker instance
data_tracker = SensitiveDataTracker()

