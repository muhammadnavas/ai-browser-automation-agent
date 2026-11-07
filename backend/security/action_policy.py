"""
backend/security/action_policy.py
---------------------------------
Deterministic Security Policy Enforcement Engine (Issue 2, Section 4 & 5).

Key Features:
- Backend-enforced policy with ALLOW, REQUIRE_APPROVAL, and BLOCK decisions.
- STRICT mode by default (configurable via SECURITY_POLICY_MODE).
- LLM and webpage instructions cannot override a BLOCK decision.
- Browser execution security covering navigation, uploads, automation actions, and retries.
- Repeated violation threshold enforcement (halts tasks after repeated policy violations).
- Integration with SensitiveDataTracker for cross-origin data exfiltration prevention.
- Comprehensive audit logging via audit_logger.
"""

from enum import Enum
import logging
import os
from typing import Any, Dict, Optional, Tuple

from backend.browser.security import validate_url_security
from backend.models.schemas import ActionType, BrowserAction
from backend.security.audit import audit_logger
from backend.security.data_tracker import data_tracker
from backend.security.detector import contains_tier1_secrets
from backend.security.policy import validate_upload_path
from backend.security.messages import get_security_user_message, SecurityEventType

logger = logging.getLogger("security.action_policy")

MAX_SECURITY_VIOLATIONS = int(os.getenv("MAX_SECURITY_VIOLATIONS", "3"))


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    BLOCK = "block"


class PolicyMode(str, Enum):
    STRICT = "strict"
    PERMISSIVE = "permissive"


# Keywords indicating destructive or high-risk administrative operations
DESTRUCTIVE_ACTION_KEYWORDS = (
    "delete account",
    "close account",
    "terminate account",
    "cancel subscription",
    "wipe data",
    "factory reset",
    "purge database",
    "delete organization",
    "remove admin",
    "wire transfer",
    "send payment",
    "confirm purchase",
    "order now",
    "change password",
    "reset master password",
)


class DeterministicActionPolicy:
    """Evaluates proposed actions against deterministic security rules."""

    def __init__(self):
        mode_str = os.getenv("SECURITY_POLICY_MODE", "strict").lower().strip()
        self.mode = PolicyMode.PERMISSIVE if mode_str == "permissive" else PolicyMode.STRICT
        # task_id -> violation count
        self._violation_counts: Dict[str, int] = {}

    def get_violation_count(self, task_id: str) -> int:
        return self._violation_counts.get(task_id, 0)

    def record_violation(self, task_id: str) -> int:
        count = self._violation_counts.get(task_id, 0) + 1
        self._violation_counts[task_id] = count
        return count

    def reset_task(self, task_id: str) -> None:
        self._violation_counts.pop(task_id, None)

    def evaluate_action(
        self,
        action: BrowserAction,
        current_url: str,
        task_id: str,
        task_goal: str = "",
        is_retry: bool = False,
    ) -> Tuple[PolicyDecision, str]:
        """
        Evaluate an action deterministically.

        Returns:
            (decision: PolicyDecision, reason: str)
        """
        # 1. Check repeated violations ceiling
        current_violations = self.get_violation_count(task_id)
        if current_violations >= MAX_SECURITY_VIOLATIONS:
            audit_logger.record_event(
                event_type="task_halted_max_violations",
                task_id=task_id,
                details={"violation_count": current_violations},
            )
            return PolicyDecision.BLOCK, "Task halted safely after exceeding the maximum allowable security violations."

        action_type = getattr(action, "action", None)
        action_name = action_type.value if hasattr(action_type, "value") else str(action_type or "")

        # 2. Navigation / Target URL Protection
        target_url = action.url or (action.text if action_type == ActionType.NAVIGATE else None) or (action.value if action_type == ActionType.NAVIGATE else None)
        if target_url and isinstance(target_url, str) and ("://" in target_url or action_type == ActionType.NAVIGATE):
            safe, reason, normalized = validate_url_security(target_url, resolve_dns=True)
            if not safe:
                violations = self.record_violation(task_id)
                audit_logger.record_event(
                    event_type="navigation_blocked_by_policy",
                    task_id=task_id,
                    details={"url": target_url, "reason": reason, "violation_count": violations},
                )
                user_msg = get_security_user_message(SecurityEventType.NAVIGATION_RESTRICTED)
                return PolicyDecision.BLOCK, f"Action blocked by policy: {user_msg}"

        # 3. File Upload Safety
        file_path = getattr(action, "file_path", None)
        if file_path or action_type == ActionType.UPLOAD_FILE:
            target_path = file_path or action.text or ""
            safe, reason = validate_upload_path(target_path, task_id=task_id)
            if not safe:
                violations = self.record_violation(task_id)
                audit_logger.record_event(
                    event_type="upload_blocked_by_policy",
                    task_id=task_id,
                    details={"file": target_path, "reason": reason, "violation_count": violations},
                )
                user_msg = get_security_user_message(SecurityEventType.UPLOAD_RESTRICTED)
                return PolicyDecision.BLOCK, f"Action blocked by policy: {user_msg}"

        # 4. Form inputs, typing, and cross-origin sensitive data transfer
        input_value = getattr(action, "text", None) or getattr(action, "value", None)
        if input_value and isinstance(input_value, str):
            # 4a. Check cross-origin exfiltration
            allowed, x_reason = data_tracker.validate_cross_origin_transfer(
                task_id=task_id,
                destination_url=current_url,
                input_value=input_value,
            )
            if not allowed:
                violations = self.record_violation(task_id)
                audit_logger.record_event(
                    event_type="cross_origin_transfer_blocked_by_policy",
                    task_id=task_id,
                    details={"reason": x_reason, "violation_count": violations},
                )
                return PolicyDecision.BLOCK, f"Action blocked by policy: Cross-origin data transfer of sensitive information prohibited."

            # 4b. Check raw Tier 1 secrets
            # Disallow typing raw secrets unless authorized TaskVault placeholder
            if not (input_value.startswith("{{") and input_value.endswith("}}")):
                if contains_tier1_secrets(input_value):
                    violations = self.record_violation(task_id)
                    audit_logger.record_event(
                        event_type="raw_secret_input_blocked",
                        task_id=task_id,
                        details={"field": getattr(action, "selector", ""), "violation_count": violations},
                    )
                    user_msg = get_security_user_message(SecurityEventType.GOAL_BLOCKED)
                    return PolicyDecision.BLOCK, f"Action blocked by policy: {user_msg}"

        # 5. Destructive actions check
        action_text_combined = f"{action.text or ''} {action.selector or ''} {action_name}".lower()
        if any(keyword in action_text_combined for keyword in DESTRUCTIVE_ACTION_KEYWORDS):
            audit_logger.record_event(
                event_type="destructive_action_requires_approval",
                task_id=task_id,
                details={"action": action_name, "snippet": action_text_combined[:100]},
            )
            return PolicyDecision.REQUIRE_APPROVAL, "High-impact destructive action requires explicit human approval."

        # Action is permitted
        return PolicyDecision.ALLOW, "Action permitted by deterministic security policy."


# Global singleton action policy
action_policy = DeterministicActionPolicy()
