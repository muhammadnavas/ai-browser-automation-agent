"""Deterministic confirmation policy for consequential browser actions.

Page text is evidence, not authority. A confirmation is required only when the
user's goal requests a consequential operation and the selected live control
matches that operation.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional, Tuple

_RISK_PATTERNS = {
    "delete": re.compile(r"\b(delete|remove|trash|discard|empty|erase)\b", re.I),
    "send": re.compile(r"\b(send|submit|publish|post)\b", re.I),
    "purchase": re.compile(r"\b(buy|purchase|pay|checkout|place order)\b", re.I),
    "transfer": re.compile(r"\b(transfer|withdraw|refund)\b", re.I),
    "share": re.compile(r"\b(share|grant access|make public)\b", re.I),
}


def confirmation_category(goal: str, target: str) -> Optional[str]:
    """Return the matching high-impact category, if both goal and target agree."""
    goal_text = str(goal or "")
    target_text = str(target or "")
    if not goal_text.strip() or not target_text.strip():
        return None
    for category, pattern in _RISK_PATTERNS.items():
        if pattern.search(goal_text) and pattern.search(target_text):
            return category
    return None


def approval_was_granted(user_approval_input: Optional[str], history: Iterable[Dict[str, Any]]) -> bool:
    """Recognize affirmative approval; explicit rejection always takes precedence."""
    response = str(user_approval_input or "").strip().lower()
    if response:
        if re.search(r"\b(no|deny|denied|reject|rejected|cancel|stop|don't|do not|not approved|not confirmed)\b", response):
            return False
        return bool(re.search(r"\b(yes|approve|approved|confirm|confirmed|proceed|continue|go ahead|granted)\b", response))

    for item in history or []:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").lower()
        evidence = " ".join(
            str(item.get(key) or "")
            for key in ("description", "reasoning", "result", "status", "message")
        ).lower()
        if action in {"request_approval", "approval"}:
            if re.search(r"\b(denied|rejected|declined|cancelled|not approved)\b", evidence):
                continue
            if re.search(r"\b(granted|approved|user confirmed|approval received)\b", evidence):
                return True
    return False
