"""Lightweight page-state classification and interaction strategy selection.

This module is deliberately deterministic: it does not call an LLM and it
never interprets page text as instructions. Page content is treated only as
evidence about the current browser state.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item is not None).lower()
    return str(value or "").lower()


def classify_page_state(page_info: Dict[str, Any]) -> Dict[str, Any]:
    """Classify common browser states from the current DOM snapshot."""
    page = page_info if isinstance(page_info, dict) else {}
    url = _text(page.get("url"))
    title = _text(page.get("title"))
    body = _text(page.get("page_text"))
    headings = _text(page.get("headings"))
    content = " ".join((url, title, body[:5000], headings))
    elements: List[Dict[str, Any]] = [
        item for item in (page.get("elements") or []) if isinstance(item, dict)
    ]
    visual = page.get("visual_context") or {}
    dialogs = visual.get("active_dialogs") or []
    overlays = visual.get("fixed_or_sticky_overlays") or []

    state = "normal"
    reason = "Page has no strong signals of a blocking or transitional state."
    if not url or url == "about:blank" or "chrome-error://" in url or "chromewebdata" in url:
        state, reason = "browser_error", "The active tab is blank or showing a browser navigation error."
    elif any(token in url for token in ("captcha", "challenge-platform", "/sorry/")) or re.search(
        r"\b(captcha|verify you are human|checking your browser)\b", content
    ):
        state, reason = "security_challenge", "A CAPTCHA or bot-verification challenge appears to be present."
    elif any(token in url for token in ("signin", "login", "oauth")) or re.search(
        r"\b(sign in to continue|log in to continue|enter your password|welcome back)\b", content
    ):
        state, reason = "authentication", "The page appears to require authentication."
    elif re.search(r"\b(404|page not found|this site can.t be reached|site can.t be reached|server not found)\b", content):
        state, reason = "navigation_error", "The page appears to show a navigation or not-found error."
    elif re.search(r"\b(no results|nothing found|no matches|0 results)\b", content):
        state, reason = "empty_state", "The current page indicates that the requested content or search results are empty."
    elif dialogs:
        state, reason = "modal_dialog", "A dialog or modal is currently active."
    elif re.search(r"\b(loading|please wait|just a moment|skeleton)\b", title + " " + headings) and not elements:
        state, reason = "loading", "The page appears to still be loading and has no usable interactive controls."

    visible = [e for e in elements if e.get("visible") is not False and e.get("disabled") is not True]
    unobstructed = [e for e in visible if e.get("center_hit") is not False]
    canvas_present = bool(page.get("canvas_present")) or bool(page.get("visual_only"))
    if canvas_present or (not unobstructed and overlays):
        strategy = "visual"
        strategy_reason = "The page has canvas/visual-only content or DOM targets are obstructed."
    elif unobstructed:
        strategy = "dom"
        strategy_reason = "Usable, visible DOM controls are available; prefer semantic DOM interaction."
    else:
        strategy = "hybrid"
        strategy_reason = "DOM targets are sparse or ambiguous; inspect the current screenshot before acting."

    return {
        "state": state,
        "state_reason": reason,
        "interaction_strategy": strategy,
        "strategy_reason": strategy_reason,
        "visible_interactive_count": len(visible),
        "unobstructed_interactive_count": len(unobstructed),
        "active_dialog_count": len(dialogs),
        "overlay_count": len(overlays),
    }
