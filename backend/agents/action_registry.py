"""
agents/action_registry.py
-------------------------
Single source of truth for browser-action capabilities exposed to the planner.

The registry describes the public action contract. Execution remains in the
Chrome extension and is still guarded by TaskManager's security policy.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Tuple

from models.schemas import ActionType


# Descriptions are deliberately task-oriented: the model chooses an operation,
# while the extension remains responsible for interacting with the live page.
_ACTION_SPECS: Dict[str, Dict[str, Any]] = {
    "navigate": {"description": "Navigate the active tab to a URL.", "args": "url (or text containing URL)"},
    "go_back": {"description": "Navigate one page backward in browser history.", "args": "none"},
    "go_forward": {"description": "Navigate one page forward in browser history.", "args": "none"},
    "reload": {"description": "Reload the current page.", "args": "none"},
    "click": {"description": "Click a visible, enabled page element.", "args": "element_id or selector; text may help identify target"},
    "double_click": {"description": "Double-click a page element.", "args": "element_id or selector"},
    "right_click": {"description": "Open an element's context menu.", "args": "element_id or selector"},
    "hover": {"description": "Move the pointer over an element to reveal hover content.", "args": "element_id or selector"},
    "drag_drop": {"description": "Drag one page element onto another.", "args": "source_selector and target_selector"},
    "type_text": {"description": "Type text into a focused or targeted input.", "args": "text and optionally element_id or selector"},
    "fill_field": {"description": "Fill a form field with a value.", "args": "value or text and element_id or selector"},
    "select_option": {"description": "Choose an option from a dropdown.", "args": "element_id or selector and select_label or value"},
    "check_box": {"description": "Set a checkbox state.", "args": "element_id or selector and checked"},
    "radio_button": {"description": "Select a radio option.", "args": "radio_name and optionally value"},
    "press_key": {"description": "Send a keyboard key to the active page or focused element.", "args": "key"},
    "upload_file": {"description": "Upload a user-authorized local file to a file input.", "args": "upload_paths or file metadata"},
    "fill": {"description": "Fill a targeted input or editable control.", "args": "element_id or selector and text/value"},
    "select": {"description": "Select a dropdown option.", "args": "element_id or selector and value/select_label"},
    "check": {"description": "Check a checkbox or toggle.", "args": "element_id or selector"},
    "uncheck": {"description": "Uncheck a checkbox or toggle.", "args": "element_id or selector"},
    "upload": {"description": "Upload a user-authorized file.", "args": "upload_paths or file metadata"},
    "press": {"description": "Press a key or keyboard shortcut.", "args": "key"},
    "focus": {"description": "Focus a page element.", "args": "element_id or selector"},
    "scroll": {"description": "Scroll the current page.", "args": "scroll_direction and optional scroll_amount"},
    "scroll_to": {"description": "Scroll a target element into view.", "args": "element_id or selector"},
    "new_tab": {"description": "Open a new browser tab.", "args": "optional URL"},
    "close_tab": {"description": "Close a browser tab.", "args": "tab_index when targeting a specific tab"},
    "switch_tab": {"description": "Switch to an existing browser tab.", "args": "tab_index"},
    "handle_dialog": {"description": "Accept or dismiss a browser dialog.", "args": "dialog_action"},
    "download_file": {"description": "Download a file from the current page.", "args": "target and optional download filename"},
    "extract": {"description": "Extract relevant visible page information.", "args": "none; only when the user requested information gathering"},
    "screenshot": {"description": "Capture the current browser tab screenshot.", "args": "none"},
    "wait": {"description": "Wait for a page state or element before continuing.", "args": "wait_selector or wait_timeout"},
    "request_approval": {"description": "Pause and ask the user to handle a sensitive or blocked step.", "args": "text explaining what the user must do"},
    "finish": {"description": "Stop only when the requested outcome has been achieved and verified.", "args": "extracted_data/summary when useful"},
    "error": {"description": "Report that the task cannot safely continue.", "args": "reasoning"},
    "plan_cache": {"description": "Internal plan-cache marker; do not select as a browser operation.", "args": "none"},
}


# These checks catch malformed tool calls before they reach the extension.
# Target fields are intentionally permissive because the content script can
# resolve natural-language text against the fresh DOM snapshot.
_TARGET_ACTIONS = {
    "click", "double_click", "right_click", "hover", "scroll_to",
    "fill_field", "select_option", "check_box", "focus", "fill",
    "select", "check", "uncheck",
}
_VALUE_ACTIONS = {"type_text", "fill_field", "fill", "select", "select_option"}
_CONTROL_ONLY = {"plan_cache"}


def iter_action_specs() -> Iterable[Tuple[str, Dict[str, Any]]]:
    """Yield registered actions in stable enum order."""
    for action in ActionType:
        name = action.value
        yield name, _ACTION_SPECS.get(
            name,
            {"description": "Execute the corresponding supported browser operation.", "args": "action-specific fields"},
        )


def get_action_catalog_prompt() -> str:
    """Render the action registry as compact, model-readable tool documentation."""
    lines = []
    for name, spec in iter_action_specs():
        if name in _CONTROL_ONLY:
            continue
        lines.append(f"- {name}: {spec['description']} Arguments: {spec['args']}.")
    return "\n".join(lines)


def validate_action_payload(action_name: Any, payload: Any) -> Tuple[bool, str]:
    """Validate the action name and basic argument shape without guessing targets."""
    if not isinstance(action_name, str) or not action_name:
        return False, "missing action name"
    try:
        ActionType(action_name)
    except ValueError:
        return False, f"unsupported action '{action_name}'"
    if not isinstance(payload, dict):
        return False, "action arguments must be a JSON object"
    if action_name in _CONTROL_ONLY:
        return False, f"'{action_name}' is an internal action, not a browser tool"
    if action_name == "navigate" and not (payload.get("url") or payload.get("text")):
        return False, "navigate requires url or text"
    if action_name in _TARGET_ACTIONS:
        has_target = any(payload.get(key) not in (None, "") for key in ("element_id", "selector", "text"))
        if not has_target:
            return False, f"{action_name} requires an element_id, selector, or resolvable text target"
    if action_name in _VALUE_ACTIONS and not any(
        payload.get(key) not in (None, "") for key in ("text", "value", "select_label")
    ):
        return False, f"{action_name} requires text, value, or select_label"
    if action_name in {"press", "press_key"} and not (payload.get("key") or payload.get("text")):
        return False, f"{action_name} requires key or text"
    if action_name == "drag_drop" and not (payload.get("source_selector") and payload.get("target_selector")):
        return False, "drag_drop requires source_selector and target_selector"
    return True, ""
