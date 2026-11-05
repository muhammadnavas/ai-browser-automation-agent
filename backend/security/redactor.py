"""
backend/security/redactor.py
----------------------------
Recursive redaction and sanitization of sensitive values across all tiers.
Safe against circular references.
"""

from typing import Any, Dict, List, Optional, Set, Tuple

from backend.security.detector import (
    is_public_business_email,
    is_tier1_key,
    is_tier2_key,
    is_tier3_key,
)
from backend.security.tiers import (
    EMAIL_REGEX,
    PHONE_REGEX,
    TIER_1_VALUE_PATTERNS,
    TIER_2_VALUE_PATTERNS,
)


def redact_text(
    text: str,
    mask_tier3: bool = False,
    preserve_public_contacts: bool = True,
    permitted_contact_types: Optional[Set[str]] = None,
) -> str:
    """Sanitize sensitive patterns across tiers in a string."""
    if not isinstance(text, str):
        return text

    result = text

    # Redact Tier 1 secrets
    for pattern, replacement in TIER_1_VALUE_PATTERNS:
        result = pattern.sub(replacement, result)

    # Redact Tier 2 identifiers
    for pattern, replacement in TIER_2_VALUE_PATTERNS:
        result = pattern.sub(replacement, result)

    # Redact Tier 3 contact PII if requested
    if mask_tier3:
        perm_types = {ct.lower() for ct in permitted_contact_types} if permitted_contact_types else set()

        def _replace_email(match):
            email = match.group(0)
            if preserve_public_contacts and is_public_business_email(email):
                return email
            if "email" in perm_types:
                return email
            return "[REDACTED_EMAIL]"

        def _replace_phone(match):
            phone = match.group(0)
            if "phone" in perm_types:
                return phone
            return "[REDACTED_PHONE]"

        result = EMAIL_REGEX.sub(_replace_email, result)
        result = PHONE_REGEX.sub(_replace_phone, result)

    return result


def redact_sensitive_data(
    data: Any,
    mask_tier3: bool = False,
    preserve_contact_info: bool = False,
    permitted_contact_types: Optional[Set[str]] = None,
    _visited: Optional[Set[int]] = None,
) -> Any:
    """Recursively sanitize sensitive fields (Tier 1 secrets, Tier 2 personal identifiers,

    and conditional Tier 3 contacts) across complex data structures.

    Handles dicts, lists, tuples, sets, Pydantic models, dataclasses, and strings.
    Uses cycle detection to prevent infinite recursion.
    """
    if data is None:
        return None

    if _visited is None:
        _visited = set()

    obj_id = id(data)
    if obj_id in _visited:
        return "[CIRCULAR_REFERENCE]"

    # Support Pydantic models (v1 and v2)
    if hasattr(data, "model_dump") and callable(data.model_dump):
        data = data.model_dump()
    elif hasattr(data, "dict") and callable(data.dict):
        data = data.dict()
    elif hasattr(data, "__dict__") and not isinstance(
        data, (type, int, float, str, bool, bytes)
    ):
        data = dict(data.__dict__)

    if isinstance(data, dict):
        _visited.add(obj_id)
        sanitized_dict: Dict[str, Any] = {}
        for k, v in data.items():
            k_str = str(k)
            if is_tier1_key(k_str):
                sanitized_dict[k_str] = "[REDACTED]"
            elif is_tier2_key(k_str):
                sanitized_dict[k_str] = "[REDACTED]"
            elif is_tier3_key(k_str) and mask_tier3:
                perm_types = {ct.lower() for ct in permitted_contact_types} if permitted_contact_types else set()
                field_type = None
                k_lower = k_str.lower()
                if any(tok in k_lower for tok in ("email", "mail")):
                    field_type = "email"
                elif any(tok in k_lower for tok in ("phone", "tel", "mobile", "cell", "fax")):
                    field_type = "phone"
                elif any(tok in k_lower for tok in ("address", "street", "city", "zip", "postal")):
                    field_type = "address"

                if preserve_contact_info and (not perm_types or (field_type and field_type in perm_types)):
                    sanitized_dict[k_str] = redact_sensitive_data(
                        v,
                        mask_tier3=mask_tier3,
                        preserve_contact_info=preserve_contact_info,
                        permitted_contact_types=permitted_contact_types,
                        _visited=_visited,
                    )
                else:
                    sanitized_dict[k_str] = "[REDACTED]"
            else:
                sanitized_dict[k_str] = redact_sensitive_data(
                    v,
                    mask_tier3=mask_tier3,
                    preserve_contact_info=preserve_contact_info,
                    permitted_contact_types=permitted_contact_types,
                    _visited=_visited,
                )
        _visited.remove(obj_id)
        return sanitized_dict

    if isinstance(data, list):
        _visited.add(obj_id)
        sanitized_list = [
            redact_sensitive_data(
                item,
                mask_tier3=mask_tier3,
                preserve_contact_info=preserve_contact_info,
                permitted_contact_types=permitted_contact_types,
                _visited=_visited,
            )
            for item in data
        ]
        _visited.remove(obj_id)
        return sanitized_list

    if isinstance(data, tuple):
        _visited.add(obj_id)
        sanitized_tuple = tuple(
            redact_sensitive_data(
                item,
                mask_tier3=mask_tier3,
                preserve_contact_info=preserve_contact_info,
                permitted_contact_types=permitted_contact_types,
                _visited=_visited,
            )
            for item in data
        )
        _visited.remove(obj_id)
        return sanitized_tuple

    if isinstance(data, set):
        _visited.add(obj_id)
        sanitized_set = [
            redact_sensitive_data(
                item,
                mask_tier3=mask_tier3,
                preserve_contact_info=preserve_contact_info,
                permitted_contact_types=permitted_contact_types,
                _visited=_visited,
            )
            for item in data
        ]
        _visited.remove(obj_id)
        return sanitized_set

    if isinstance(data, str):
        return redact_text(
            data,
            mask_tier3=mask_tier3,
            preserve_public_contacts=preserve_contact_info,
            permitted_contact_types=permitted_contact_types,
        )

    if isinstance(data, (int, float, bool)):
        return data

    try:
        return redact_text(
            str(data),
            mask_tier3=mask_tier3,
            preserve_public_contacts=preserve_contact_info,
            permitted_contact_types=permitted_contact_types,
        )
    except Exception:
        return "[UNSERIALIZABLE]"
