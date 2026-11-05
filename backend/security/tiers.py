"""
backend/security/tiers.py
-------------------------
Definitions of sensitivity tiers and pattern constants for the
AI Browser Automation Agent.

Tiers:
- Tier 1 — Secrets: Passwords, OTPs, API keys, access tokens, session credentials,
  private keys, CVVs, PINs. Never sent to LLM, stored in vault, or exposed.
- Tier 2 — Personal identifiers: Government IDs, personal financial identifiers,
  private account numbers. Redacted before LLM and persistence.
- Tier 3 — Contact PII: Personal email addresses, phone numbers, postal addresses.
  Protected during processing, but permit explicitly requested contact extraction
  and authorized form filling. Public business contacts remain usable.
"""

from enum import Enum
import re
from typing import List, Set, Tuple


class SensitivityTier(str, Enum):
    TIER_1_SECRETS = "tier_1_secrets"
    TIER_2_PERSONAL_IDENTIFIERS = "tier_2_personal_identifiers"
    TIER_3_CONTACT_PII = "tier_3_contact_pii"
    PUBLIC_BUSINESS_CONTACT = "public_business_contact"
    NOT_SENSITIVE = "not_sensitive"

    # Aliases
    TIER1_SECRET = "tier_1_secrets"
    TIER2_IDENTIFIER = "tier_2_personal_identifiers"
    TIER3_CONTACT = "tier_3_contact_pii"


# ---------------------------------------------------------------------------
# Tier 1 — Secrets: Tokens & Patterns
# ---------------------------------------------------------------------------

TIER_1_KEY_TOKENS: Set[str] = {
    "password",
    "passwd",
    "pwd",
    "passphrase",
    "secret",
    "token",
    "jwt",
    "bearer",
    "auth",
    "authorization",
    "credential",
    "credentials",
    "cookie",
    "cookies",
    "session",
    "sessionid",
    "session_id",
    "apikey",
    "api_key",
    "cvv",
    "cvv2",
    "cvc",
    "cvc2",
    "pin",
    "pincode",
    "pin_code",
    "otp",
    "one_time_code",
    "verification_code",
    "2fa",
    "mfa",
    "private_key",
    "client_secret",
}

TIER_1_KEY_COMPOUND_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"api[_-]?key", re.IGNORECASE),
    re.compile(r"private[_-]?key", re.IGNORECASE),
    re.compile(r"client[_-]?secret", re.IGNORECASE),
    re.compile(r"secret[_-]?key", re.IGNORECASE),
    re.compile(r"app[_-]?secret", re.IGNORECASE),
    re.compile(r"access[_-]?token", re.IGNORECASE),
    re.compile(r"refresh[_-]?token", re.IGNORECASE),
    re.compile(r"id[_-]?token", re.IGNORECASE),
    re.compile(r"auth[_-]?token", re.IGNORECASE),
    re.compile(r"session[_-]?id", re.IGNORECASE),
    re.compile(r"session[_-]?token", re.IGNORECASE),
    re.compile(r"security[_-]?code", re.IGNORECASE),
    re.compile(r"card[_-]?code", re.IGNORECASE),
    re.compile(r"cvv[2]?", re.IGNORECASE),
    re.compile(r"cvc[2]?", re.IGNORECASE),
    re.compile(r"\botp\b", re.IGNORECASE),
    re.compile(r"one[_-]?time[_-]?code", re.IGNORECASE),
    re.compile(r"verification[_-]?code", re.IGNORECASE),
)

TIER_1_VALUE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # JWT tokens (header.payload.signature starting with eyJ)
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-+/=]{10,}\b"
        ),
        "[REDACTED_JWT]",
    ),
    # Bearer tokens in text
    (
        re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
        "Bearer [REDACTED_TOKEN]",
    ),
    # Common API key formats (OpenAI, Google AIza, GitHub, Slack tokens, AWS)
    (
        re.compile(
            r"\b(sk-[a-zA-Z0-9_-]{20,}|AIza[0-9A-Za-z-_]{35}|ghp_[a-zA-Z0-9]{36}|gho_[a-zA-Z0-9]{36}|xox[baprs]-[0-9a-zA-Z]{10,48}|AKIA[0-9A-Z]{16})\b"
        ),
        "[REDACTED_API_KEY]",
    ),
    # Private Key PEM blocks
    (
        re.compile(
            r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    # Explicit assignment for passwords, secrets, tokens, otps
    (
        re.compile(
            r"((?:password|passwd|pwd|token|access_token|api[_-]?key|secret|cvv|cvc|pin|otp|credential)s?\s*(?:[:=]|:=|\bis\b|\bas\b|\bwith\b|\bset\s+to\b|\bto\b|\s)\s*)['\"]?"
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
            re.IGNORECASE,
        ),
        r"\1[REDACTED_SECRET]",
    ),
]

# Sensitive file patterns reused from os_dialog
SENSITIVE_FILE_PATTERNS: List[re.Pattern] = [
    re.compile(r"client_secret", re.I),
    re.compile(r"credentials?", re.I),
    re.compile(r"\btoken\b", re.I),
    re.compile(r"\bsecret\b", re.I),
    re.compile(r"\bpasswd\b", re.I),
    re.compile(r"\bpassword\b", re.I),
    re.compile(r"\.env", re.I),
    re.compile(r"id_rsa", re.I),
    re.compile(r"id_ed25519", re.I),
    re.compile(r"\.pem$", re.I),
    re.compile(r"\.key$", re.I),
    re.compile(r"\.pfx$", re.I),
    re.compile(r"\.p12$", re.I),
    re.compile(r"\.kdbx$", re.I),
]


# ---------------------------------------------------------------------------
# Tier 2 — Personal Identifiers: Tokens & Patterns
# ---------------------------------------------------------------------------

TIER_2_KEY_TOKENS: Set[str] = {
    "ssn",
    "social_security",
    "social_security_number",
    "national_id",
    "aadhaar",
    "pan",
    "passport",
    "driver_license",
    "driving_licence",
    "credit_card",
    "debit_card",
    "card_num",
    "card_number",
    "bank_account",
    "iban",
    "routing_number",
}

TIER_2_KEY_COMPOUND_PATTERNS: Tuple[re.Pattern, ...] = (
    re.compile(r"\bcard[_-]?(?:num|number)\b", re.IGNORECASE),
    re.compile(r"\bcredit[_-]?card\b", re.IGNORECASE),
    re.compile(r"\bdebit[_-]?card\b", re.IGNORECASE),
    re.compile(r"\bcc[_-]?(?:num|number)\b", re.IGNORECASE),
    re.compile(r"\baccount[_-]?number\b", re.IGNORECASE),
    re.compile(r"\bbank[_-]?account\b", re.IGNORECASE),
    re.compile(r"\brouting[_-]?num(?:ber)?\b", re.IGNORECASE),
    re.compile(r"\bssn\b", re.IGNORECASE),
    re.compile(r"\bsocial[_-]?security\b", re.IGNORECASE),
    re.compile(r"\bpan(?:[_-]?(?:num|card|number))?\b", re.IGNORECASE),
    re.compile(r"\baadhaar\b", re.IGNORECASE),
    re.compile(r"\bpassport(?:[_-]?(?:num|number))?\b", re.IGNORECASE),
)

TIER_2_VALUE_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # Credit / Debit card numbers: 13-19 digits with optional spaces or dashes
    (re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,7}\b"), "[REDACTED_CARD]"),
    # US Social Security Number (SSN: 3-2-4 digits)
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    # Indian Aadhaar number (12 digits with spaces or hyphens)
    (re.compile(r"\b\d{4}[ -]\d{4}[ -]\d{4}\b"), "[REDACTED_AADHAAR]"),
    # Indian PAN (Permanent Account Number: 5 uppercase letters, 4 digits, 1 uppercase letter)
    (re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), "[REDACTED_PAN]"),
    # International Bank Account Number (IBAN: e.g. GB29 XAAA 0101 2345 6789 01)
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{4}\d{7}([A-Z0-9]?){0,16}\b"), "[REDACTED_IBAN]"),
]


# ---------------------------------------------------------------------------
# Tier 3 — Contact PII: Tokens & Patterns
# ---------------------------------------------------------------------------

TIER_3_KEY_TOKENS: Set[str] = {
    "email",
    "email_address",
    "mail",
    "phone",
    "telephone",
    "mobile",
    "cellphone",
    "tel",
    "address",
    "postal",
    "postal_code",
    "zip",
    "zipcode",
    "street",
    "contact",
    "contacts",
    "contact_info",
}

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

# Phone numbers (US/International standard formats)
PHONE_REGEX = re.compile(
    r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"
)

# Common public business/role email prefixes
PUBLIC_BUSINESS_EMAIL_PREFIXES: Set[str] = {
    "info",
    "support",
    "sales",
    "contact",
    "press",
    "help",
    "service",
    "services",
    "hello",
    "team",
    "office",
    "media",
    "inquiries",
    "inquiry",
    "billing",
    "careers",
    "jobs",
    "admin",
    "security",
    "privacy",
    "legal",
}
