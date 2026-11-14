"""
Canonical high-level plan format shared by initial planning and adaptive replanning.

The internal/API representation remains List[str] for compatibility. LLMs may
return the canonical JSON array or a documented object envelope; all variants
are normalized and validated at this boundary.
"""
from __future__ import annotations

import json
import re
from typing import Any, List


MIN_MILESTONES = 3
MAX_MILESTONES = 8
MAX_WORDS_PER_MILESTONE = 18


def _strip_fences(raw: str) -> str:
    value = (raw or "").strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].lstrip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def _extract_items(parsed: Any) -> List[Any]:
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in ("plan", "milestones", "steps", "remaining_steps", "execution_plan", "tasks"):
            value = parsed.get(key)
            if isinstance(value, list):
                return value
    raise ValueError("Plan response must be a JSON array or an object containing a plan array.")


def normalize_plan_response(\n    raw: str,\n    *,\n    allow_completion_marker: bool = False,\n    min_milestones: int = MIN_MILESTONES,\n) -> List[str]:
    """Parse, normalize, de-duplicate and validate a model-generated plan."""
    try:
        parsed = json.loads(_strip_fences(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Planner returned invalid JSON; expected a JSON array of milestone strings.") from exc

    normalized: List[str] = []
    seen = set()
    for item in _extract_items(parsed):
        if isinstance(item, str):
            title = item
        elif isinstance(item, dict):
            title = next(
                (item.get(key) for key in ("title", "description", "milestone", "step", "action", "name")
                 if isinstance(item.get(key), str) and item.get(key).strip()),
                "",
            )
        else:
            title = ""
        title = re.sub(r"^\s*(?:step\s*)?\d+[.)\-:]\s*", "", str(title))
        title = re.sub(r"\s+", " ", title).strip(" \t\r\n.;")
        if not title:
            continue
        key = title.casefold()
        if key not in seen:
            seen.add(key)
            normalized.append(title)

    completion = (
        allow_completion_marker
        and len(normalized) == 1
        and normalized[0].casefold() in {
            "verify the requested outcome and finish",
            "verify completion and finish",
        }
    )
    if not completion and not min_milestones <= len(normalized) <= MAX_MILESTONES:
        raise ValueError(
            f"Planner must return {min_milestones}-{MAX_MILESTONES} unique, non-empty milestones; got {len(normalized)}."
        )

    for index, title in enumerate(normalized, start=1):
        words = title.split()
        if len(words) > MAX_WORDS_PER_MILESTONE:
            normalized[index - 1] = " ".join(words[:MAX_WORDS_PER_MILESTONE])
    return normalized
