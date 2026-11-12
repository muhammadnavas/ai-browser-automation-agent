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
    r"transfer|withdraw|share|grant access|make public|upload|fill|edit|update)\b",
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


import json
import logging
import asyncio

logger = logging.getLogger(__name__)

_ALLOWED_AUTOMATIC_ROLES = {"navigation", "research", "verification"}
_MUTATING_INTENT = re.compile(
    r"\\b(delete|remove|trash|send|submit|publish|post|buy|purchase|pay|checkout|"
    r"transfer|withdraw|share|grant access|make public|upload|fill|edit|update)\\b",
    re.IGNORECASE,
)


async def plan_subagent_specs(planner, goal: str, start_url: Optional[str]) -> List[SubagentSpec]:
    """Ask the configured master LLM for a bounded delegation plan.

    Only registered read-only roles are eligible for automatic delegation.
    Any malformed or unsafe response falls back to the deterministic policy.
    """
    url = (start_url or "").strip()
    if not url or not goal or _MUTATING_INTENT.search(goal):
        return []

    prompt = f"""You are the master orchestrator for a browser automation system.
Decide whether specialist read-only workers can safely help with this task.
User goal: {goal}
Known starting URL: {url}

Available workers:
- navigation: open the known URL and report title/headings/interactive element count.
- research: read visible page content and links without clicking or changing anything.
- verification: inspect a known URL for expected visible text or URL fragments.

Return a JSON object only:
{{"subtasks":[{{"role":"research","goal":"specific read-only objective","input":{{"url":"{url}"}},"timeout_seconds":90}}]}}

Rules:
- Use 0 to 3 subtasks; use [] when delegation adds no value.
- Only use the exact roles navigation, research, verification.
- Every subtask must use the known URL exactly; never invent URLs.
- Delegate independent read-only investigation only.
- Never delegate delete, send, submit, upload, edit, purchase, payment, transfer, sharing, or any state-changing action.
- Do not split a task just to create agents. Prefer parallel independent questions.
- Do not include credentials, personal data, or secrets in inputs.
"""
    try:
        raw = await asyncio.to_thread(planner._call_llm, prompt, "application/json")
        parsed = json.loads(raw)
        rows = parsed.get("subtasks", []) if isinstance(parsed, dict) else []
        if not isinstance(rows, list) or len(rows) > 3:
            raise ValueError("Invalid subtask list")
        specs: List[SubagentSpec] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            role = str(row.get("role") or "").strip().lower()
            subgoal = str(row.get("goal") or "").strip()
            inputs = row.get("input") if isinstance(row.get("input"), dict) else {}
            candidate_url = str(inputs.get("url") or "").strip()
            if role not in _ALLOWED_AUTOMATIC_ROLES or not subgoal:
                continue
            if candidate_url != url or _MUTATING_INTENT.search(subgoal):
                continue
            timeout = row.get("timeout_seconds", 90)
            try:
                timeout = max(10, min(120, int(timeout)))
            except (TypeError, ValueError):
                timeout = 90
            specs.append(SubagentSpec(
                role=role,
                goal=subgoal[:1000],
                input={"url": url},
                timeout_seconds=timeout,
            ))
        return specs
    except Exception as exc:
        logger.info("LLM delegation planning unavailable; using safe intent fallback: %s", exc)
        return build_automatic_subagent_specs(goal, url)
