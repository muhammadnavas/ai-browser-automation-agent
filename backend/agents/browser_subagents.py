"""Concrete Chrome Extension subagents for navigation, research, forms and verification.

Each worker owns a dedicated extension task tab identified by its child ID.
No worker shares mutable page state with another worker. Form workers only fill
explicitly supplied values and never submit a form.
"""
import asyncio
from typing import Any, Dict, List, Tuple

from agents.subagents import SubagentContext, subagent_orchestrator
from browser.extension_adapter import ExtensionBrowserAdapter, extension_manager
from browser.security import validate_url_security
from models.schemas import ActionType, BrowserAction

# Filled forms remain open and unlocked for a human to review. The parent task
# owns these adapters until the user closes the review tab or cancels the parent.
_active_form_tabs: Dict[str, Tuple[str, ExtensionBrowserAdapter]] = {}

def _required_url(ctx: SubagentContext) -> str:
    url = str(ctx.input.get("url") or "").strip()
    if not url:
        raise ValueError("input.url is required for this browser worker")
    return url

async def _open_owned_tab(ctx: SubagentContext) -> ExtensionBrowserAdapter:
    if not extension_manager.is_connected:
        raise RuntimeError("Chrome Extension is disconnected")
    url = _required_url(ctx)
    safe, reason, normalized = await asyncio.to_thread(validate_url_security, url, True)
    if not safe:
        raise ValueError(f"Navigation blocked by URL security policy: {reason}")
    adapter = ExtensionBrowserAdapter(task_id=ctx.subagent_id, show_badges=False, screenshots_dir="screenshots")
    try:
        await adapter.start(normalized or url)
        return adapter
    except BaseException:
        # If tab creation succeeded but startup was interrupted, release the
        # extension-side mapping before propagating the original failure.
        try:
            await extension_manager.send_command(
                task_id=ctx.subagent_id,
                command_type="close_tab",
                payload={},
                timeout=5.0,
            )
        except Exception:
            pass
        try:
            await adapter.stop()
        except Exception:
            pass
        raise

async def _close_owned_tab(adapter: ExtensionBrowserAdapter) -> None:
    # Use the extension's dedicated close_tab command. A CLOSE_TAB browser
    # action is not equivalent: it is routed through the content-script action
    # executor and may not release the background task-tab mapping.
    try:
        await extension_manager.send_command(
            task_id=adapter.task_id,
            command_type="close_tab",
            payload={},
            timeout=5.0,
        )
    except Exception:
        pass
    try:
        await adapter.stop()
    except Exception:
        pass

async def navigation_worker(ctx: SubagentContext) -> Dict[str, Any]:
    """Open a URL in a child-owned tab and return a live DOM observation."""
    adapter = await _open_owned_tab(ctx)
    try:
        ctx.raise_if_cancelled()
        page = await adapter.parse_interactive_elements()
        if page.get("inspection_error") or not page.get("url"):
            raise RuntimeError("Navigation completed but live DOM inspection failed")
        return {"url": page.get("url"), "title": page.get("title"), "headings": (page.get("headings") or [])[:30], "element_count": len(page.get("elements") or [])}
    finally:
        await _close_owned_tab(adapter)

async def research_worker(ctx: SubagentContext) -> Dict[str, Any]:
    """Collect bounded visible-page evidence; does not click or mutate the page."""
    adapter = await _open_owned_tab(ctx)
    try:
        ctx.raise_if_cancelled()
        page = await adapter.parse_interactive_elements()
        if page.get("inspection_error") or not page.get("url"):
            raise RuntimeError("Research worker could not inspect the live page")
        elements = page.get("elements") or []
        links = []
        for item in elements:
            if isinstance(item, dict) and item.get("href") and item.get("text"):
                links.append({"text": str(item.get("text"))[:200], "url": str(item.get("href"))[:1000]})
            if len(links) >= 40: break
        return {"url": page.get("url"), "title": page.get("title"), "headings": (page.get("headings") or [])[:40], "page_text": str(page.get("page_text") or "")[:12000], "links": links}
    finally:
        await _close_owned_tab(adapter)

async def form_worker(ctx: SubagentContext) -> Dict[str, Any]:
    """Fill only explicitly mapped, visible, editable fields; never submit."""
    values = ctx.input.get("values")
    if not isinstance(values, dict) or not values:
        raise ValueError("input.values must be a non-empty selector/name-to-value mapping")
    adapter = await _open_owned_tab(ctx)
    filled: List[Dict[str, str]] = []
    skipped: List[Dict[str, str]] = []
    keep_open = False
    try:
        ctx.raise_if_cancelled()
        page = await adapter.parse_interactive_elements()
        if page.get("inspection_error") or not page.get("url"):
            raise RuntimeError("Form worker could not inspect the live page")
        fields = page.get("form_fields") or []
        for field in fields:
            ctx.raise_if_cancelled()
            if not getattr(field, "visible", True) or getattr(field, "disabled", False) or getattr(field, "readonly", False):
                continue
            kind = getattr(getattr(field, "field_type", None), "value", str(getattr(field, "field_type", ""))).lower()
            if kind in {"password", "file", "hidden", "submit"}:
                continue
            keys = [getattr(field, "selector", ""), getattr(field, "field_id", ""), getattr(field, "label", ""), getattr(field, "aria_label", "")]
            # A radio group's name is shared by every option; only accept an
            # option-specific mapping to avoid selecting every radio in a group.
            if kind != "radio":
                keys.insert(1, getattr(field, "name", ""))
            matched = next((key for key in keys if key and key in values), None)
            if not matched: continue
            raw_value = values[matched]
            if raw_value is None: continue
            value = str(raw_value)
            if kind in {"dropdown"}:
                action = BrowserAction(action=ActionType.SELECT_OPTION, selector=field.selector, value=value)
            elif kind in {"checkbox"}:
                should_check = str(raw_value).strip().lower() in {"1", "true", "yes", "on"}
                action = BrowserAction(
                    action=ActionType.CHECK_BOX if should_check else ActionType.UNCHECK,
                    selector=field.selector,
                )
            elif kind == "radio":
                action = BrowserAction(
                    action=ActionType.RADIO_BUTTON,
                    selector=field.selector,
                    radio_name=getattr(field, "name", None),
                    value=value,
                )
            else:
                action = BrowserAction(action=ActionType.FILL_FIELD, selector=field.selector, text=value)
            try:
                description, _ = await adapter.execute_action(action, task_form_fields=fields)
                filled.append({"field": str(getattr(field,"label",None) or matched)[:120], "action": description[:240]})
            except Exception as exc:
                skipped.append({"field": str(getattr(field,"label",None) or matched)[:120], "reason": str(exc)[:300]})
        if filled:
            # Unlock the tab so the user can inspect the populated form. It is
            # intentionally not submitted by this worker.
            await adapter.unlock_ui()
            _active_form_tabs[ctx.subagent_id] = (ctx.parent_task_id, adapter)
            keep_open = True
        return {
            "url": page.get("url"),
            "filled_count": len(filled),
            "filled": filled,
            "skipped": skipped,
            "submitted": False,
            "tab_open_for_review": keep_open,
            "subagent_id": ctx.subagent_id if keep_open else None,
            "note": "Form was not submitted; review and approve submission separately.",
        }
    finally:
        if not keep_open:
            await _close_owned_tab(adapter)

async def verification_worker(ctx: SubagentContext) -> Dict[str, Any]:
    """Verify URL and visible text postconditions on a fresh child-owned tab."""
    adapter = await _open_owned_tab(ctx)
    try:
        ctx.raise_if_cancelled()
        page = await adapter.parse_interactive_elements()
        if page.get("inspection_error") or not page.get("url"):
            raise RuntimeError("Verification worker could not inspect the live page")
        expected_text = str(ctx.input.get("expected_text") or "").strip()
        expected_url_contains = str(ctx.input.get("expected_url_contains") or "").strip().lower()
        text = str(page.get("page_text") or "")
        checks = {
            "url_observed": bool(page.get("url")),
            "expected_text_found": (expected_text.casefold() in text.casefold()) if expected_text else None,
            "expected_url_found": (expected_url_contains in str(page.get("url")).lower()) if expected_url_contains else None,
        }
        required = [v for v in checks.values() if v is not None]
        verified = bool(checks["url_observed"] and required and all(required))
        return {"url": page.get("url"), "title": page.get("title"), "checks": checks, "verified": verified, "evidence_excerpt": text[:1200]}
    finally:
        await _close_owned_tab(adapter)

def register_browser_subagents() -> None:
    subagent_orchestrator.register_worker("navigation", navigation_worker)
    subagent_orchestrator.register_worker("research", research_worker)
    subagent_orchestrator.register_worker("form", form_worker)
    subagent_orchestrator.register_worker("verification", verification_worker)


async def close_form_review_tab(parent_task_id: str, subagent_id: str) -> bool:
    """Close a retained form-review tab only when it belongs to this parent."""
    entry = _active_form_tabs.get(subagent_id)
    if not entry or entry[0] != parent_task_id:
        return False
    adapter = entry[1]
    await _close_owned_tab(adapter)
    _active_form_tabs.pop(subagent_id, None)
    return True


async def close_parent_form_review_tabs(parent_task_id: str) -> int:
    """Close every retained form-review tab owned by a cancelled parent."""
    child_ids = [child_id for child_id, entry in _active_form_tabs.items() if entry[0] == parent_task_id]
    closed = 0
    for child_id in child_ids:
        if await close_form_review_tab(parent_task_id, child_id):
            closed += 1
    return closed
