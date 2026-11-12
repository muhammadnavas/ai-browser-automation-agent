"""Automatic task-to-subagent delegation policy.

The master task manager uses this lightweight policy to choose specialist
workers. It deliberately delegates only independent, read-only preflight work;
the main browser executor remains responsible for user-requested mutations.
"""
from __future__ import annotations

import re
from typing import List, Optional

from agents.subagents import SubagentSpec


_RESEARCH = re.compile(
    r"\b(research|find|search|read|summari[sz]e|extract|compare|inspect|"
    r"look up|collect|gather|identify)\b",
    re.IGNORECASE,
)
_MUTATING = re.compile(
    r"\b(delete|remove|trash|send|submit|publish|post|buy|purchase|pay|checkout|"
    r"transfer|withdraw|share|grant access|make public|upload|fill|edit|update)\\b",
    re.IGNORECASE,
)
_NAVIGATION_ONLY = re.compile(
    r"^\s*(please\s+)?(open|visit|navigate to|go to)\b",
    re.IGNORECASE,
)


def build_automatic_subagent_specs(
    goal: str,
    start_url: Optional[str],
    extracted_url: Optional[str] = None,
) -> List[SubagentSpec]:
    """Choose zero or one safe specialist based on task intent and known URL.

    Research runs only for information-gathering goals with an explicit URL.
    Simple navigation goals use the navigation worker. Mutating tasks are left
    to the main executor so child tabs cannot perform unintended side effects.
    """
    url = (start_url or extracted_url or "").strip()
    text = (goal or "").strip()
    if not url or not text or _MUTATING.search(text):
        return []

    if _RESEARCH.search(text):
        return [SubagentSpec(
            role="research",
            goal=f"Read-only preflight research for the user's task: {text}",
            input={"url": url},
            timeout_seconds=90,
        )]

    if _NAVIGATION_ONLY.search(text):
        return [SubagentSpec(
            role="navigation",
            goal=f"Inspect the destination before the main task: {text}",
            input={"url": url},
            timeout_seconds=60,
        )]

    # Do not auto-delegate form filling, sending, deleting, purchasing,
    # sharing, or other mutations. The primary executor retains control.
    return []
