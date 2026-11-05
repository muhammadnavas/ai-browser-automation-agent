"""
backend/security/messages.py
----------------------------
Centralized security messaging system for the AI Browser Automation Agent.

Generates clear, generic, user-facing security messages for blocked actions,
preventing exposure of internal security tiers, detection rules, regex internals,
raw secrets, or technical redaction placeholders.
"""

from enum import Enum
import re
from typing import Optional, Union


class SecurityEventType(str, Enum):
    """Enumeration of security event categories for generic user-facing messaging."""
    GOAL_BLOCKED = "goal_blocked"
    EXTRACTION_REFUSED = "extraction_refused"
    UPLOAD_RESTRICTED = "upload_restricted"
    AUTH_REQUIRED = "auth_required"
    NAVIGATION_RESTRICTED = "navigation_restricted"
    SCREENSHOT_PROTECTED = "screenshot_protected"
    POLICY_VIOLATION = "policy_violation"


# Canonical generic user-facing messages per event type
SECURITY_USER_MESSAGES = {
    SecurityEventType.GOAL_BLOCKED: (
        "This task involves accessing or retrieving protected information. "
        "For security reasons, the action cannot be completed."
    ),
    SecurityEventType.EXTRACTION_REFUSED: (
        "The requested content contains protected information. "
        "For security reasons, the data cannot be extracted."
    ),
    SecurityEventType.UPLOAD_RESTRICTED: (
        "The selected file cannot be uploaded due to security restrictions."
    ),
    SecurityEventType.AUTH_REQUIRED: (
        "Authentication or security verification required. "
        "Please complete this step directly in the browser to continue."
    ),
    SecurityEventType.NAVIGATION_RESTRICTED: (
        "Navigation to this address is restricted by security policy."
    ),
    SecurityEventType.SCREENSHOT_PROTECTED: (
        "Screen capture unavailable for protected content."
    ),
    SecurityEventType.POLICY_VIOLATION: (
        "This action cannot be completed due to security restrictions."
    ),
}

# Regex matching internal placeholders and vault tokens
INTERNAL_PLACEHOLDER_REGEX = re.compile(
    r"\[REDACTED(?:_[A-Z0-9_]+)?\]|\{\{[A-Z0-9_]+\}\}|\[CIRCULAR_REFERENCE\]|\[UNSERIALIZABLE\]"
)

# Regex patterns indicating internal technical security errors
SECURITY_INDICATOR_PATTERNS = [
    re.compile(r"tier\s*[123]|tier[123]", re.I),
    re.compile(r"extraction\s+(?:safety\s+)?threshold|sensitive\s+leaf", re.I),
    re.compile(r"prohibit(?:s|ed)?\s+(?:uploading\s+)?sensitive", re.I),
    re.compile(r"outside\s+allowlisted\s+upload", re.I),
    re.compile(r"cryptographic\s+keys?|service\s+account\s+credentials", re.I),
    re.compile(r"blocked\s+by\s+(?:url\s+)?security\s+policy|navigation\s+blocked", re.I),
    re.compile(r"cloud\s+metadata\s+ip|loopback\s+address\s+prohibited|private/internal\s+network", re.I),
    re.compile(r"prohibited\s+(?:administrative/internal\s+)?port", re.I),
    re.compile(r"harvest\b.*?\b(?:password|credential|token|secret|key)", re.I),
    re.compile(r"contains\s+raw\s+tier|attempts\s+to\s+extract\s+or\s+harvest", re.I),
    re.compile(r"protected\s+information|security\s+restrictions", re.I),
    re.compile(r"password\s+or\s+otp\s+(?:input\s+)?field\s+detected", re.I),
]


def is_security_error(error_text: Optional[str]) -> bool:
    """Determine if a given error message relates to security policy or internal protection."""
    if not error_text or not isinstance(error_text, str):
        return False
    return any(pat.search(error_text) for pat in SECURITY_INDICATOR_PATTERNS)


def strip_internal_placeholders(text: Optional[str], replacement: str = "[protected]") -> str:
    """Strip internal technical placeholders (e.g. [REDACTED_SECRET], {{USER_PASSWORD}})

    from text destined for user-facing display or downloadable reports.
    """
    if not text or not isinstance(text, str):
        return "" if text is None else str(text)
    cleaned = INTERNAL_PLACEHOLDER_REGEX.sub(replacement, text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def get_security_user_message(
    event_or_reason: Union[SecurityEventType, str, None],
    default: Optional[str] = None,
) -> str:
    """Generate a clean, generic, user-facing security message.

    Maps either an explicit SecurityEventType or an internal diagnostic reason string
    to its user-safe generic counterpart without exposing internal tiers or secrets.
    """
    if event_or_reason is None:
        return default or SECURITY_USER_MESSAGES[SecurityEventType.POLICY_VIOLATION]

    if isinstance(event_or_reason, SecurityEventType):
        return SECURITY_USER_MESSAGES[event_or_reason]

    text = str(event_or_reason).strip()
    if not text:
        return default or SECURITY_USER_MESSAGES[SecurityEventType.POLICY_VIOLATION]

    text_lower = text.lower()

    # 1. Extraction safety threshold or sensitive leaf fields
    if any(k in text_lower for k in [
        "extraction safety threshold", "sensitive leaf", "extraction refused",
        "ratio", "leaf fields detected", "metadata refused"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.EXTRACTION_REFUSED]

    # 2. Task goal prohibited secret harvest or raw secrets
    if any(k in text_lower for k in [
        "harvest prohibited", "raw tier 1", "goal contains raw", "goal attempts to extract",
        "embedded secret", "raw secret values"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.GOAL_BLOCKED]

    # 3. File upload restrictions
    if any(k in text_lower for k in [
        "upload", "allowlisted upload", "sensitive file", "cryptographic key",
        "exceeds maximum permitted limit", "device path"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.UPLOAD_RESTRICTED]

    # 4. Authentication or credential gating
    if any(k in text_lower for k in [
        "password or otp input", "credential input", "2fa", "authentication required"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.AUTH_REQUIRED]

    # 5. URL navigation restrictions
    if any(k in text_lower for k in [
        "navigation blocked", "loopback address prohibited", "cloud metadata ip",
        "prohibited port", "private/internal network", "url security policy"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.NAVIGATION_RESTRICTED]

    # 6. Screenshot protections
    if any(k in text_lower for k in [
        "screenshot", "screen capture", "protected backend/dashboard origin",
        "safe masking cannot be guaranteed"
    ]):
        return SECURITY_USER_MESSAGES[SecurityEventType.SCREENSHOT_PROTECTED]

    # 7. Other security policy indicators
    if is_security_error(text):
        return SECURITY_USER_MESSAGES[SecurityEventType.POLICY_VIOLATION]

    return default if default is not None else SECURITY_USER_MESSAGES[SecurityEventType.POLICY_VIOLATION]


def sanitize_user_facing_error(error_message: Optional[str]) -> Optional[str]:
    """Sanitize any error message for user-facing presentation (API responses, WebSockets, UI).

    If the error is security-related, replaces it with the appropriate generic security message.
    Otherwise, ensures no internal placeholders or accidental secret markers are leaked.
    """
    if error_message is None:
        return None
    if not isinstance(error_message, str):
        error_message = str(error_message)

    if is_security_error(error_message):
        return get_security_user_message(error_message)

    return strip_internal_placeholders(error_message, replacement="[protected]")
