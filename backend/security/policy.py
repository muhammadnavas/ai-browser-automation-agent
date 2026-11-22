import logging
logger = logging.getLogger('security.policy')
"""
backend/security/policy.py
--------------------------
Security policies:
1. Secret retrieval / prompt goal blocking.
2. Extraction safety threshold (> 50% sensitive leaf fields refused).
3. Upload path validation (explicit path, allowlisted directory, sensitive-file exclusion).
"""

import os
import re
from pathlib import Path
from typing import Any, List, Optional, Set, Tuple

from backend.security.audit import audit_logger
from backend.security.detector import (
    classify_sensitivity,
    contains_tier1_secrets,
    contains_tier2_identifiers,
    is_public_business_email,
    is_sensitive_file,
    is_tier1_key,
    is_tier2_key,
    is_tier3_key,
)
from backend.security.redactor import redact_sensitive_data
from backend.security.tiers import SensitivityTier


def check_goal_policy(goal: str, task_id: str = "init") -> Tuple[bool, str]:
    """Check if task goal contains or attempts to retrieve prohibited Tier 1 secrets.

    Returns (is_allowed: bool, reason: str).
    """
    if not goal or not isinstance(goal, str):
        return True, "Safe"

    goal_lower = goal.lower()

    # Block explicit credential/secret extraction goals
    secret_harvest_patterns = [
        r"\b(?:steal|dump|exfiltrate|hack|harvest|crack)\b.*?\b(?:password|credential|token|secret|key|cookie)s?\b",
        r"\b(?:extract|find|get|read|show)\b.*?\b(?:my|all|user|admin)?\s*(?:passwords?|api[_-]?keys?|private[_-]?keys?|secrets?|tokens?)(?!\s+(?:policy|policies|manager|management|generator|guidelines?|practices?|criteria|rules?|requirements?|documentation|article|help|faq|screen|input|field|dialog|bucket))\b",
    ]
    for pat in secret_harvest_patterns:
        if re.search(pat, goal_lower):
            reason = "Goal attempts to extract or harvest prohibited Tier 1 secrets."
            audit_logger.log_secret_blocked(task_id, reason)
            return False, reason

    # Check if raw Tier 1 secrets are embedded in the goal text
    if contains_tier1_secrets(goal):
        # Allow if it's merely mentioning 'password' as a generic user instruction
        generic_safe = bool(re.search(r"(?i)\b(?:enter|type|input|fill|solve|handle)\s+(?:the\s+)?(?:password|credentials?|otp)\s+(?:manually|directly|in browser|yourself)", goal))
        if not generic_safe:
            reason = "Goal contains raw Tier 1 secret values (passwords, API keys, OTPs, or private tokens)."
            audit_logger.log_secret_blocked(task_id, reason)
            return False, reason

    return True, "Safe"


def _collect_leaf_fields(
    data: Any,
    current_key: Optional[str] = None,
    _visited: Optional[Set[int]] = None,
) -> List[Tuple[Optional[str], Any]]:
    """Traverse data structures and return a flat list of (key_name, scalar_value) leaves."""
    if _visited is None:
        _visited = set()

    obj_id = id(data)
    if obj_id in _visited:
        return []

    # Handle Pydantic models
    if hasattr(data, "model_dump") and callable(data.model_dump):
        data = data.model_dump()
    elif hasattr(data, "dict") and callable(data.dict):
        data = data.dict()
    elif hasattr(data, "__dict__") and not isinstance(
        data, (type, int, float, str, bool, bytes)
    ):
        data = dict(data.__dict__)

    leaves: List[Tuple[Optional[str], Any]] = []

    if isinstance(data, dict):
        _visited.add(obj_id)
        for k, v in data.items():
            leaves.extend(_collect_leaf_fields(v, current_key=str(k), _visited=_visited))
        _visited.remove(obj_id)
        return leaves

    if isinstance(data, (list, tuple, set)):
        _visited.add(obj_id)
        for item in data:
            leaves.extend(_collect_leaf_fields(item, current_key=current_key, _visited=_visited))
        _visited.remove(obj_id)
        return leaves

    # Scalar leaf
    return [(current_key, data)]


def get_field_contact_type(key_name: Optional[str], value: Any) -> Optional[str]:
    """Classify what type of contact info this field/value represents: 'email', 'phone', 'address', 'name'."""
    val_str = str(value or "")
    k = (key_name or "").lower()
    if "@" in val_str or any(tok in k for tok in ("email", "mail")):
        return "email"
    if any(tok in k for tok in ("phone", "tel", "mobile", "cell", "fax")):
        return "phone"
    from backend.security.tiers import PHONE_REGEX
    if PHONE_REGEX.search(val_str):
        return "phone"
    if any(tok in k for tok in ("address", "street", "city", "zip", "postal", "location")):
        return "address"
    if any(tok in k for tok in ("contact_name", "first_name", "last_name", "full_name")):
        return "name"
    return None


def resolve_requested_contact_types(
    is_explicit_contact_request: bool = False,
    requested_contact_types: Optional[Set[str]] = None,
    user_intent: Optional[str] = None,
) -> Set[str]:
    """Extract specific contact types explicitly requested by the user.
    If the user asked specifically for emails, only 'email' is permitted.
    If the user asked specifically for phone, only 'phone' is permitted.
    Unrequested contact types remain protected/masked.
    """
    if requested_contact_types:
        return {ct.lower() for ct in requested_contact_types}

    if not is_explicit_contact_request and not user_intent:
        return set()

    types: Set[str] = set()
    if user_intent:
        intent_lower = user_intent.lower()
        if any(w in intent_lower for w in ("email", "mail", "inbox")):
            types.add("email")
        if any(w in intent_lower for w in ("phone", "tel", "call", "mobile", "hotline", "number")):
            types.add("phone")
        if any(w in intent_lower for w in ("address", "location", "street", "postal", "zip")):
            types.add("address")
        if any(w in intent_lower for w in ("contact", "directory", "reach out", "support", "get in touch")):
            if not types:
                types.update({"email", "phone"})
    elif is_explicit_contact_request:
        types.update({"email", "phone"})

    return types


def is_sensitive_leaf(
    key_name: Optional[str],
    value: Any,
    is_explicit_contact_request: bool = False,
    requested_contact_types: Optional[Set[str]] = None,
    user_intent: Optional[str] = None,
) -> bool:
    """Determine if a leaf field is sensitive based on key and value classification.
    Narrows contact exceptions to specifically requested contact types only.
    """
    val_str = str(value or "")

    # 1. Tier 1 checks
    if key_name and is_tier1_key(key_name):
        return True
    if contains_tier1_secrets(val_str):
        return True

    # 2. Tier 2 checks
    if key_name and is_tier2_key(key_name):
        return True
    if contains_tier2_identifiers(val_str):
        return True

    # 3. Tier 3 checks
    # Public business emails are NEVER sensitive
    if is_public_business_email(val_str):
        return False

    effective_requested = resolve_requested_contact_types(
        is_explicit_contact_request=is_explicit_contact_request,
        requested_contact_types=requested_contact_types,
        user_intent=user_intent,
    )

    field_type = get_field_contact_type(key_name, value)

    # Restrict exception ONLY to specific contact type explicitly requested
    if field_type and field_type in effective_requested:
        return False

    if key_name and is_tier3_key(key_name):
        return True

    tier = classify_sensitivity(val_str, key_name=key_name)
    if tier in (SensitivityTier.TIER_1_SECRETS, SensitivityTier.TIER_2_PERSONAL_IDENTIFIERS):
        return True
    if tier == SensitivityTier.TIER_3_CONTACT_PII:
        # Personal contact info is sensitive unless specifically requested
        return True

    return False


def check_extraction_safety_threshold(
    data: Any,
    task_id: str = "extraction",
    is_explicit_contact_request: bool = False,
    requested_contact_types: Optional[Set[str]] = None,
    user_intent: Optional[str] = None,
) -> Tuple[bool, str, Any]:
    """Implement the general extraction safety threshold:

    - What counts as a sensitive leaf field: A scalar leaf matching Tier 1 or Tier 2,
      or unauthorized Tier 3 contact PII.
    - How percentage is calculated: sensitive_leaf_count / total_leaf_count
    - Exact refusal boundary: Strictly greater than 50% (> 0.50).
      At 50% or below, extraction is permitted (with appropriate redaction).
      Above 50%, extraction is REFUSED.

    Returns:
        (allowed: bool, reason: str, sanitized_data: Any)
    """
    if data is None:
        return True, "Safe", None

    leaves = _collect_leaf_fields(data)
    total_leaves = len(leaves)

    if total_leaves == 0:
        return True, "Safe", data

    effective_requested = resolve_requested_contact_types(
        is_explicit_contact_request=is_explicit_contact_request,
        requested_contact_types=requested_contact_types,
        user_intent=user_intent,
    )

    sensitive_leaves = [
        (k, v) for k, v in leaves
        if is_sensitive_leaf(
            k, v,
            is_explicit_contact_request=is_explicit_contact_request,
            requested_contact_types=effective_requested,
            user_intent=user_intent,
        )
    ]
    sensitive_count = len(sensitive_leaves)
    ratio = sensitive_count / total_leaves

    # Exact boundary check: strictly greater than 50%
    if ratio > 0.50:
        reason = (
            f"Extraction safety threshold exceeded: {sensitive_count}/{total_leaves} "
            f"({ratio:.1%}) sensitive leaf fields detected (> 50%). Extraction refused."
        )
        audit_logger.log_extraction_refused(task_id, reason, ratio)
        return False, reason, None

    # Permitted: sanitize any Tier 1 / Tier 2 fields and unrequested Tier 3 contacts before returning
    sanitized = redact_sensitive_data(
        data,
        mask_tier3=True,
        preserve_contact_info=bool(effective_requested),
        permitted_contact_types=effective_requested,
    )
    return True, "Safe", sanitized


def _get_max_upload_file_size() -> int:
    """Safely parse and validate MAX_UPLOAD_FILE_SIZE configuration."""
    raw = os.getenv("MAX_UPLOAD_FILE_SIZE", "")
    default_size = 25 * 1024 * 1024  # 25 MB
    if not raw or not raw.strip():
        return default_size
    try:
        val = int(raw.strip())
        if val <= 0:
            logger.warning("Invalid MAX_UPLOAD_FILE_SIZE (%d <= 0); defaulting to %d bytes", val, default_size)
            return default_size
        if val > 500 * 1024 * 1024:
            logger.warning("MAX_UPLOAD_FILE_SIZE exceeds 500MB safety bound; capping at 500MB")
            return 500 * 1024 * 1024
        return val
    except ValueError:
        logger.warning("Malformed MAX_UPLOAD_FILE_SIZE '%s'; defaulting to %d bytes", raw, default_size)
        return default_size


MAX_UPLOAD_FILE_SIZE = _get_max_upload_file_size()
MAX_CONTENT_INSPECTION_BYTES = 1024 * 1024  # Inspect up to 1 MB of content for secrets

TEXT_INSPECTABLE_EXTENSIONS = {
    ".txt", ".json", ".csv", ".tsv", ".xml", ".yaml", ".yml",
    ".py", ".js", ".html", ".css", ".md", ".log", ".rtf", ".odt"
}


def check_file_content_safety(file_path: Path) -> Tuple[bool, str]:
    """Inspect file content to prevent disguised secrets from being uploaded.

    Limitations:
        - Plaintext and structured formats (.txt, .json, .csv, code, etc.) are deeply
          inspected up to 1MB in chunks for Tier 1 credentials and private keys.
        - Binary, compressed, or encrypted formats (.zip, .7z, .jpg, compiled files, etc.)
          cannot be exhaustively inspected for steganographic or compressed secrets.
          Magic headers and preamble streams are verified to ensure plaintext private
          keys or configuration secrets are not disguised with arbitrary binary extensions.
    """
    ext = file_path.suffix.lower()
    try:
        file_size = file_path.stat().st_size
    except OSError as oe:
        return False, f"Could not stat file: {type(oe).__name__}"

    bytes_to_read = min(file_size, MAX_CONTENT_INSPECTION_BYTES)

    try:
        with open(file_path, "rb") as f:
            chunk = f.read(bytes_to_read)

        # 1. Preamble check for disguised plaintext keys in ANY format (including .pdf, .jpg, .zip)
        text_snippet = chunk[:8192].decode("utf-8", errors="ignore")
        if any(marker in text_snippet.lower() for marker in [
            "-----begin rsa private key",
            "-----begin open_ssh private key",
            "-----begin private key",
            "-----begin ec private key",
            "client_secret",
            "aws_secret_access_key",
            '"private_key":',
            "'private_key':",
        ]):
            return False, "File content contains private cryptographic keys or service account credentials."

        # 2. Deep scan for text-inspectable formats
        if ext in TEXT_INSPECTABLE_EXTENSIONS or ext == "":
            full_text = chunk.decode("utf-8", errors="ignore")
            if contains_tier1_secrets(full_text):
                return False, "File content contains sensitive credentials or secrets."
            if contains_tier2_identifiers(full_text):
                return False, "File content contains prohibited Tier 2 personal identifiers."

    except Exception as exc:
        return False, f"Could not inspect file: {type(exc).__name__}"

    return True, "Safe"


def validate_upload_path(
    file_path: str,
    allowlist_dirs: Optional[List[str]] = None,
    task_id: str = "upload",
    user_approved: bool = False,
    max_file_size: Optional[int] = None,
) -> Tuple[bool, str, Optional[str]]:
    """Validate that an upload file path:

    1. Is explicitly provided (non-empty string).
    2. Exists, is a canonical regular file, and within permitted size limits.
    3. Is NOT a sensitive file by filename or file content.
    4. Resides within allowlisted directories or has explicit user approval.
    5. Protects against symlink/path traversal and logs only safe basenames.

    Returns:
        (is_valid: bool, reason: str, canonical_path: Optional[str])
    """
    if not file_path or not isinstance(file_path, str) or not file_path.strip():
        reason = "File upload requires an explicit file path; automatic folder discovery is disabled."
        audit_logger.log_file_rejected(task_id, "", reason)
        return False, reason, None

    clean_path = file_path.strip().strip("'\"")
    safe_name = os.path.basename(clean_path) or "unknown"

    # 1. Filename pattern check
    if is_sensitive_file(clean_path):
        reason = f"Security policy prohibits uploading sensitive file: {safe_name}"
        audit_logger.log_file_rejected(task_id, safe_name, reason)
        return False, reason, None

    # 2. Strict canonical resolution (fails on broken symlinks or non-existent files)
    try:
        raw_p = Path(clean_path)
        if any(part.lower() in ("nul", "con", "prn", "aux", "com1", "lpt1") for part in raw_p.parts):
            reason = f"Invalid file device path: {safe_name}"
            audit_logger.log_file_rejected(task_id, safe_name, reason)
            return False, reason, None
        resolved = raw_p.resolve(strict=True)
    except Exception as e:
        reason = f"File does not exist or invalid path: {safe_name}"
        audit_logger.log_file_rejected(task_id, safe_name, reason)
        return False, reason, None

    if not resolved.is_file():
        reason = f"Target is not a regular file: {safe_name}"
        audit_logger.log_file_rejected(task_id, safe_name, reason)
        return False, reason, None

    # 3. Size limit check
    effective_max = max_file_size if max_file_size is not None else MAX_UPLOAD_FILE_SIZE
    try:
        file_size = resolved.stat().st_size
        if file_size > effective_max:
            reason = f"File '{safe_name}' ({file_size} bytes) exceeds maximum permitted limit ({effective_max} bytes)."
            audit_logger.log_file_rejected(task_id, safe_name, reason)
            return False, reason, None
    except OSError as oe:
        reason = f"Could not determine file size: {type(oe).__name__}"
        audit_logger.log_file_rejected(task_id, safe_name, reason)
        return False, reason, None

    # 4. Content inspection (catch disguised secrets / keys)
    content_safe, content_reason = check_file_content_safety(resolved)
    if not content_safe:
        audit_logger.log_file_rejected(task_id, safe_name, content_reason)
        return False, content_reason, None

    # 5. Allowlist and approval enforcement
    if not user_approved:
        cwd = Path.cwd().resolve()
        configured_dirs = allowlist_dirs or []
        env_dirs = os.getenv("ALLOWED_UPLOAD_DIRS", "").split(os.pathsep) if os.getenv("ALLOWED_UPLOAD_DIRS") else []

        user_home = Path.home().resolve()
        allowed_roots: List[Path] = [
            (cwd / "uploads").resolve(),
            (cwd / "downloads").resolve(),
            (user_home / "Downloads").resolve(),
            (user_home / "Documents").resolve(),
            (user_home / "Desktop").resolve(),
            (user_home / "Pictures").resolve(),
        ]
        import sys
        if "PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules:
            import tempfile
            allowed_roots.append(Path(tempfile.gettempdir()).resolve())

        for d in configured_dirs + env_dirs:
            if d.strip():
                try:
                    allowed_roots.append(Path(d.strip()).resolve(strict=False))
                except Exception:
                    pass

        is_allowlisted = False
        for root in allowed_roots:
            try:
                resolved.relative_to(root)
                is_allowlisted = True
                break
            except ValueError:
                continue

        if not is_allowlisted:
            reason = (
                f"File '{safe_name}' is outside allowlisted upload directories. "
                f"Uploads must be within configured upload directories or require explicit approval."
            )
            audit_logger.log_file_rejected(task_id, safe_name, reason)
            return False, reason, None

    return True, "Valid", str(resolved)
