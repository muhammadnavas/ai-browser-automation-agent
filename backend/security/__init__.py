"""
backend/security/__init__.py
----------------------------
Centralized Security Module for the AI Browser Automation Agent.
Provides detection, classification, redaction, per-task vault, audit logging,
and policy checks across the application.
"""

from backend.security.tiers import (
    SensitivityTier,
    TIER_1_KEY_TOKENS,
    TIER_2_KEY_TOKENS,
    TIER_3_KEY_TOKENS,
    SENSITIVE_FILE_PATTERNS,
)
from backend.security.detector import (
    classify_sensitivity,
    contains_tier1_secrets,
    contains_tier2_identifiers,
    is_sensitive_key,
    is_sensitive_file,
    is_public_business_email,
    is_auth_field,
    is_tier1_key,
    is_tier2_key,
    is_tier3_key,
)
from backend.security.redactor import (
    redact_text,
    redact_sensitive_data,
)
from backend.security.vault import (
    TaskVault,
    TaskVaultManager,
    get_task_vault,
    clear_task_vault,
    default_vault_manager,
)
from backend.security.audit import (
    SecurityAuditEvent,
    SecurityAuditLogger,
    audit_logger,
)
from backend.security.policy import (
    check_goal_policy,
    check_extraction_safety_threshold,
    validate_upload_path,
    is_sensitive_leaf,
)
from backend.security.messages import (
    SecurityEventType,
    SECURITY_USER_MESSAGES,
    is_security_error,
    strip_internal_placeholders,
    get_security_user_message,
    sanitize_user_facing_error,
)

__all__ = [
    "SensitivityTier",
    "TIER_1_KEY_TOKENS",
    "TIER_2_KEY_TOKENS",
    "TIER_3_KEY_TOKENS",
    "SENSITIVE_FILE_PATTERNS",
    "classify_sensitivity",
    "contains_tier1_secrets",
    "contains_tier2_identifiers",
    "is_sensitive_key",
    "is_sensitive_file",
    "is_public_business_email",
    "is_auth_field",
    "is_tier1_key",
    "is_tier2_key",
    "is_tier3_key",
    "redact_text",
    "redact_sensitive_data",
    "TaskVault",
    "TaskVaultManager",
    "get_task_vault",
    "clear_task_vault",
    "default_vault_manager",
    "SecurityAuditEvent",
    "SecurityAuditLogger",
    "audit_logger",
    "check_goal_policy",
    "check_extraction_safety_threshold",
    "validate_upload_path",
    "is_sensitive_leaf",
    "SecurityEventType",
    "SECURITY_USER_MESSAGES",
    "is_security_error",
    "strip_internal_placeholders",
    "get_security_user_message",
    "sanitize_user_facing_error",
]
