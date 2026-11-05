"""
backend/security/detector.py
----------------------------
Reusable detection, classification, and inspection helpers across
the AI Browser Automation Agent.
"""

import os
import re
from typing import Any, Dict, Optional, Set

from backend.security.tiers import (
    SensitivityTier,
    TIER_1_KEY_TOKENS,
    TIER_1_KEY_COMPOUND_PATTERNS,
    TIER_1_VALUE_PATTERNS,
    TIER_2_KEY_TOKENS,
    TIER_2_KEY_COMPOUND_PATTERNS,
    TIER_2_VALUE_PATTERNS,
    TIER_3_KEY_TOKENS,
    EMAIL_REGEX,
    PHONE_REGEX,
    PUBLIC_BUSINESS_EMAIL_PREFIXES,
    SENSITIVE_FILE_PATTERNS,
)

PERSONAL_EMAIL_DOMAINS: Set[str] = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "ymail.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "zoho.com",
    "mail.com",
    "gmx.com",
}


def normalize_key(key_name: Any) -> str:
    """Normalize a key name to lowercase snake_case."""
    if not key_name or not isinstance(key_name, (str, int, float)):
        return ""
    key_str = str(key_name)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_str).lower()
    normalized = re.sub(r"[^a-z0-9]", "_", normalized)
    return normalized


def is_tier1_key(key_name: str) -> bool:
    """Return True if dictionary key represents Tier 1 secret."""
    norm = normalize_key(key_name)
    parts = set(filter(None, norm.split("_")))
    if parts & TIER_1_KEY_TOKENS:
        return True
    for pat in TIER_1_KEY_COMPOUND_PATTERNS:
        if pat.search(norm):
            return True
    return False


def is_tier2_key(key_name: str) -> bool:
    """Return True if dictionary key represents Tier 2 personal identifier."""
    norm = normalize_key(key_name)
    parts = set(filter(None, norm.split("_")))
    if parts & TIER_2_KEY_TOKENS:
        return True
    for pat in TIER_2_KEY_COMPOUND_PATTERNS:
        if pat.search(norm):
            return True
    return False


def is_tier3_key(key_name: str) -> bool:
    """Return True if dictionary key represents Tier 3 contact PII."""
    norm = normalize_key(key_name)
    if norm in TIER_3_KEY_TOKENS:
        return True
    parts = set(filter(None, norm.split("_")))
    if parts & TIER_3_KEY_TOKENS:
        return True
    for token in TIER_3_KEY_TOKENS:
        if "_" in token and token in norm:
            return True
    return False


def is_sensitive_key(key_name: str) -> bool:
    """Return True if a key name represents sensitive data in any tier.

    Handles snake_case, camelCase, kebab-case, and PascalCase.
    """
    return is_tier1_key(key_name) or is_tier2_key(key_name) or is_tier3_key(key_name)


def is_sensitive_file(filename: str) -> bool:
    """Return True if filename or path suggests private credentials/secrets."""
    base = os.path.basename(filename) if filename else ""
    for pat in SENSITIVE_FILE_PATTERNS:
        if pat.search(filename or "") or pat.search(base):
            return True
    return False


def is_public_business_email(email: str) -> bool:
    """Return True if email represents a public or business mailbox."""
    if not email or not isinstance(email, str) or "@" not in email:
        return False
    clean = email.strip().lower()
    local_part, domain = clean.split("@", 1)
    if domain in PERSONAL_EMAIL_DOMAINS:
        return False
    if local_part in PUBLIC_BUSINESS_EMAIL_PREFIXES:
        return True
    local_tokens = set(re.split(r"[-._+]", local_part))
    if local_tokens & PUBLIC_BUSINESS_EMAIL_PREFIXES:
        return True
    return False


REDACTION_PLACEHOLDER_PATTERN = re.compile(
    r"\[REDACTED(?:_[A-Z0-9_]+)?\]|\{\{[A-Z0-9_]+\}\}|\[CIRCULAR_REFERENCE\]|\[UNSERIALIZABLE\]"
)


def contains_tier1_secrets(text: str) -> bool:
    """Check if string contains raw Tier 1 secrets (passwords, tokens, API keys, CVVs, private keys)."""
    if not text or not isinstance(text, str):
        return False

    # Redacted placeholders and vault tokens are never interpreted as raw secrets
    clean_text = REDACTION_PLACEHOLDER_PATTERN.sub("", text)
    if not clean_text.strip():
        return False

    for pat, _ in TIER_1_VALUE_PATTERNS:
        if pat.search(clean_text):
            return True

    # Check for common password in assignment syntax, e.g. password=secret or password is mypass
    if re.search(
        r"(?i)\b(?:password|passwd|pwd)\s*(?:[:=]|:=|\bis\b|\bas\b|\bwith\b|\bset\s+to\b|\bto\b|\s)\s*['\"]?"
        r"(?!(?:"
        r"manager|management|vault|reset|policy|policies|generator|protection|"
        r"protected|field|fields|input|inputs|prompt|prompts|dialog|dialogs|box|boxes|"
        r"page|pages|screen|screens|button|buttons|entry|entries|label|labels|strength|"
        r"hash|hashes|digest|checker|recovery|hint|hints|change|changes|rule|rules|"
        r"expiry|expiration|criteria|requirement|requirements|complexity|authentication|"
        r"auth|form|forms|toggle|visibility|masking|unmasking|leak|leaks|breach|breaches|"
        r"helper|helpers|option|options|setting|settings|refresh|bucket|buckets|ring|rings|"
        r"validation|lifetime|exchange|rotation|revocation|passing|based|store|stores|"
        r"storage|cache|service|services|endpoint|endpoints|count|counter|counters|limit|"
        r"limits|usage|cost|costs|garden|gardens|agent|agents|recipe|recipes|santa|keeper|"
        r"keepers|society|societies|identity|identities|shopper|shoppers|handshake|passage|"
        r"passages|door|doors|doorway|doorways|message|messages|note|notes|place|places|"
        r"location|locations|word|words|phrase|phrases|question|questions|answer|answers|"
        r"code|codes|sharing|stuffing|theft|provider|providers"
        r")\b|\[REDACTED|\[CIRCULAR|\[UNSERIALIZABLE|\{\{)"
        r"([^\s,;&\"'<>]{4,})",
        clean_text,
    ):
        return True

    # Check for OTP or verification code patterns
    if re.search(r"(?i)\b(?:otp|one[- ]?time[- ]?code|verification[- ]?code)\s*(?:[:=]|:=|\bis\b|\bas\b|\bset\s+to\b|\bto\b|\s)\s*(\d{4,8})\b", clean_text):
        return True

    return False


def contains_tier2_identifiers(text: str) -> bool:
    """Check if string contains Tier 2 personal identifiers (cards, SSN, Aadhaar, PAN, IBAN)."""
    if not text or not isinstance(text, str):
        return False

    clean_text = REDACTION_PLACEHOLDER_PATTERN.sub("", text)
    if not clean_text.strip():
        return False

    for pat, _ in TIER_2_VALUE_PATTERNS:
        if pat.search(clean_text):
            return True
    return False


def classify_sensitivity(text: str, key_name: Optional[str] = None) -> SensitivityTier:
    """Classify the sensitivity tier of a given text and/or field key."""
    # Support callers passing (key, value) positionally
    if key_name and is_sensitive_key(text) and not is_sensitive_key(str(key_name)):
        text, key_name = str(key_name), text

    if key_name:
        if is_tier1_key(key_name):
            return SensitivityTier.TIER_1_SECRETS
        if is_tier2_key(key_name):
            return SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS

    if not text or not isinstance(text, str):
        return SensitivityTier.NOT_SENSITIVE

    if contains_tier1_secrets(text):
        return SensitivityTier.TIER_1_SECRETS

    if contains_tier2_identifiers(text):
        return SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS

    # Check for emails
    emails = EMAIL_REGEX.findall(text)
    if emails:
        # Check if all are public business emails
        if all(is_public_business_email(e) for e in emails):
            return SensitivityTier.PUBLIC_BUSINESS_CONTACT
        return SensitivityTier.TIER_3_CONTACT_PII

    # Check for phone numbers
    if PHONE_REGEX.search(text):
        if key_name and is_tier3_key(key_name):
            return SensitivityTier.TIER_3_CONTACT_PII
        # If in contact context
        return SensitivityTier.TIER_3_CONTACT_PII

    if key_name and is_tier3_key(key_name):
        return SensitivityTier.TIER_3_CONTACT_PII

    return SensitivityTier.NOT_SENSITIVE


def is_auth_field(field_info: Dict[str, Any]) -> bool:
    """Check if a DOM element or form field dictionary represents a password or OTP credential field."""
    if not isinstance(field_info, dict):
        return False

    ftype = str(field_info.get("type") or field_info.get("field_type") or field_info.get("input_type") or "").lower()
    if ftype == "password":
        return True

    autocomplete = str(field_info.get("autocomplete") or "").lower()
    if autocomplete in ("current-password", "new-password", "one-time-code"):
        return True

    indicators = [
        str(field_info.get("name") or ""),
        str(field_info.get("id") or ""),
        str(field_info.get("label") or ""),
        str(field_info.get("aria_label") or ""),
        str(field_info.get("placeholder") or ""),
        str(field_info.get("selector") or ""),
    ]
    combined = " ".join(indicators).lower()

    if any(k in combined for k in ["password", "passwd", "passphrase"]):
        return True

    if re.search(r"(?i)(?:\b|_)(?:otp|2fa|mfa|passcode|one-time-code|verification[-_ ]?code)(?:\b|_)", combined) or any(k in combined for k in ["one-time-code", "verification code", "2fa code", "security code"]):
        return True

    return False
