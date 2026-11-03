"""Deterministic browser page-state classification.

Page text is treated as untrusted evidence only. Keep detection conservative:
false authentication/empty-state positives can unnecessarily stop a task.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import urlparse


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(str(item) for item in value if item is not None).lower()
    return str(value or "").lower()


def _matches(pattern: str, value: str) -> bool:
    return bool(re.search(pattern, value, flags=re.IGNORECASE))


def classify_page_state(page_info: Dict[str, Any]) -> Dict[str, Any]:
    """Classify common browser states from a fresh DOM/visual snapshot."""
    page = page_info if isinstance(page_info, dict) else {}
    url = _text(page.get("url")).strip()
    title = _text(page.get("title")).strip()
    body = _text(page.get("page_text"))
    headings = _text(page.get("headings"))
    elements: List[Dict[str, Any]] = [
        item for item in (page.get("elements") or []) if isinstance(item, dict)
    ]

    visual = page.get("visual_context")
    visual = visual if isinstance(visual, dict) else {}
    dialogs = visual.get("active_dialogs") or []
    overlays = visual.get("fixed_or_sticky_overlays") or []

    try:
        parsed_url = urlparse(url)
        path = (parsed_url.path or "").lower()
    except ValueError:
        path = ""

    state = "normal"
    reason = "Page has no strong signals of a blocking or transitional state."

    browser_error = (
        not url
        or url == "about:blank"
        or url.startswith(("chrome-error://", "edge-error://", "chromewebdata"))
    )
    challenge = (
        _matches(r"(captcha|challenge-platform|/sorry/)", url)
        or _matches(r"\\b(captcha|verify you are human|checking your browser|complete the security check)\\b",
                    title + " " + headings + " " + body[:2500])
    )
    auth_path = _matches(r"/(login|log-in|signin|sign-in|authenticate|oauth)(/|$)", path)
    auth_copy = _matches(
        r"\\b(sign in to continue|log in to continue|enter your password|"
        r"your session has expired|authentication required)\\b",
        title + " " + headings + " " + body[:2500],
    )
    auth_title = _matches(r"^(sign in|log in|login|sign-in)(\\s*[-|:].*)?$", title)

    error_copy = _matches(
        r"\\b(page not found|this site can.t be reached|site can.t be reached|"
        r"server not found|dns_probe_finished|err_connection_refused)\\b",
        title + " " + headings + " " + body[:1500],
    ) or _matches(r"^404(\\s|$|[-:])", title)

    empty_copy = _matches(
        r"\\b(no results found|no results to show|nothing found|no matches found|"
        r"your search returned no results|0 results)\\b",
        title + " " + headings + " " + body[:2500],
    )
    loading_copy = _matches(
        r"^(loading|please wait|just a moment|loading\\.{0,3})$",
        title,
    ) or _matches(r"\\b(loading|please wait|just a moment)\\b", headings)

    if browser_error:
        state, reason = "browser_error", "The active tab is blank or showing a browser navigation error."
    elif challenge:
        state, reason = "security_challenge", "A CAPTCHA or bot-verification challenge appears to be present."
    elif auth_path or auth_copy or auth_title:
        state, reason = "authentication", "The page appears to require authentication."
    elif error_copy:
        state, reason = "navigation_error", "The page appears to show a navigation or not-found error."
    elif dialogs:
        state, reason = "modal_dialog", "A dialog or modal is currently active."
    elif empty_copy:
        state, reason = "empty_state", "The page explicitly indicates that the requested results are empty."
    elif loading_copy and not elements:
        state, reason = "loading", "The page appears to still be loading and has no usable interactive controls."

    visible = [
        e for e in elements
        if e.get("visible") is not False and e.get("disabled") is not True
    ]
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
