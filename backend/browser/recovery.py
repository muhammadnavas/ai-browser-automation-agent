"""
browser/recovery.py
-------------------
Automatic retry and error recovery system with exponential backoff and
action-level fallback strategies for the AI Browser Automation Agent.

Provides:
- RetryPolicy: Configurable exponential backoff with jitter.
- Action-level fallback strategies for CLICK, FILL, NAVIGATE, SELECT, SCROLL, etc.
- execute_with_retry_and_fallback: Unified runner for resilient action execution
  across both Playwright and Chrome Extension layers.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Any, List, Optional, Tuple

from models.schemas import BrowserAction, ActionType, TaskStatus
from browser.form_discovery import FormField

logger = logging.getLogger("browser.recovery")


@dataclass
class RetryPolicy:
    """Configurable exponential backoff policy with jitter."""
    max_retries: int = 3
    base_delay: float = 1.0     # Base delay in seconds (1.0s)
    max_delay: float = 8.0      # Maximum delay in seconds
    backoff_factor: float = 2.0 # Multiplier (1s -> 2s -> 4s)
    jitter: bool = True         # Add randomized jitter to avoid thundering herd

    def get_delay(self, attempt: int) -> float:
        """
        Calculate backoff delay for the given retry attempt (1-indexed).
        attempt 1 -> ~1.0s, attempt 2 -> ~2.0s, attempt 3 -> ~4.0s
        """
        raw_delay = min(self.max_delay, self.base_delay * (self.backoff_factor ** (attempt - 1)))
        if self.jitter:
            jitter_offset = random.uniform(0.05, 0.3)
            raw_delay += jitter_offset
        return round(raw_delay, 2)


# ---------------------------------------------------------------------------
# Fallback Strategies for Playwright Page Actions
# ---------------------------------------------------------------------------

async def _fallback_click(
    page: Any,
    action: BrowserAction,
    strategy_step: int,
    form_fields: Optional[List[FormField]] = None,
) -> Tuple[bool, str]:
    """
    Step-level fallback strategies for click actions:
      Step 1: Force click with scroll into view.
      Step 2: Text / ARIA / Role matching locator fallback.
      Step 3: JavaScript direct evaluation (el.click() / synthetic dispatch).
      Step 4: Viewport coordinate center click via page.mouse.click.
    """
    sel = action.selector
    text = action.text or ""

    # Strategy 1: Scroll into view + force click
    if strategy_step == 1:
        if sel:
            try:
                loc = page.locator(sel).first
                await loc.scroll_into_view_if_needed(timeout=2500)
                await loc.click(force=True, timeout=2500)
                return True, f"Click succeeded via force click on '{sel}'"
            except Exception as e:
                logger.debug(f"Fallback click strategy 1 failed: {e}")

    # Strategy 2: Text / Role / ARIA match
    if strategy_step == 2:
        candidates = []
        if text:
            candidates.append(page.get_by_role("button", name=text).first)
            candidates.append(page.get_by_text(text).first)
            candidates.append(page.locator(f"button:has-text('{text}')").first)
            candidates.append(page.locator(f"a:has-text('{text}')").first)
        if form_fields:
            for f in form_fields:
                if f.label and text and text.lower() in f.label.lower():
                    candidates.append(page.get_by_label(f.label).first)
                if f.selector:
                    candidates.append(page.locator(f.selector).first)

        for candidate in candidates:
            try:
                if await candidate.count() > 0 and await candidate.is_visible():
                    await candidate.scroll_into_view_if_needed(timeout=2000)
                    await candidate.click(timeout=2500)
                    return True, f"Click succeeded via semantic text/role locator matching '{text}'"
            except Exception:
                continue

    # Strategy 3: JavaScript direct click (bypasses pointer-events / overlay issues)
    if strategy_step == 3:
        if sel:
            try:
                js_script = """(selector) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.scrollIntoView({behavior: 'instant', block: 'center'});
                    // HTMLElement.click() already dispatches the click event.
                    // Dispatching another synthetic event here would activate the
                    // control twice (dangerous for submit/delete/purchase buttons).
                    el.click();
                    return true;
                }"""
                clicked = await page.evaluate(js_script, sel)
                if clicked:
                    return True, f"Click succeeded via JavaScript el.click() on '{sel}'"
            except Exception as e:
                logger.debug(f"Fallback click strategy 3 failed: {e}")

        if text:
            try:
                js_text_script = """(targetText) => {
                    const elements = Array.from(document.querySelectorAll('button, a, input[type="submit"], input[type="button"], [role="button"]'));
                    const match = elements.find(el => (el.innerText || el.textContent || '').trim().toLowerCase().includes(targetText.toLowerCase()));
                    if (match) {
                        match.scrollIntoView({behavior: 'instant', block: 'center'});
                        match.click();
                        return true;
                    }
                    return false;
                }"""
                clicked = await page.evaluate(js_text_script, text)
                if clicked:
                    return True, f"Click succeeded via JavaScript text match on '{text}'"
            except Exception as e:
                logger.debug(f"Fallback click strategy 3 text failed: {e}")

    # Strategy 4: Viewport coordinate center click
    if strategy_step == 4:
        if sel:
            try:
                loc = page.locator(sel).first
                box = await loc.bounding_box()
                if box:
                    cx = box["x"] + box["width"] / 2
                    cy = box["y"] + box["height"] / 2
                    await page.mouse.click(cx, cy)
                    return True, f"Click succeeded via coordinate click at ({cx:.1f}, {cy:.1f})"
            except Exception as e:
                logger.debug(f"Fallback click strategy 4 failed: {e}")

    return False, "All click fallback strategies exhausted"


async def _fallback_fill(
    page: Any,
    action: BrowserAction,
    strategy_step: int,
    form_fields: Optional[List[FormField]] = None,
) -> Tuple[bool, str]:
    """
    Step-level fallback strategies for fill / type actions:
      Step 1: Focus + Select-All + Backspace + keyboard character typing.
      Step 2: JavaScript native value setter with Input and Change event dispatch.
      Step 3: Attribute fallback matching (by placeholder, name, label).
    """
    sel = action.selector
    val = action.value or action.text or ""

    # Strategy 1: Focus + Keyboard typing
    if strategy_step == 1:
        if sel:
            try:
                loc = page.locator(sel).first
                await loc.scroll_into_view_if_needed(timeout=2000)
                await loc.click(timeout=2000)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Backspace")
                await page.keyboard.type(val, delay=25)
                if action.press_enter:
                    await page.keyboard.press("Enter")
                return True, f"Fill succeeded via focus + keyboard typing on '{sel}'"
            except Exception as e:
                logger.debug(f"Fallback fill strategy 1 failed: {e}")

    # Strategy 2: JavaScript DOM value assignment with event dispatch (React/Vue compatible)
    if strategy_step == 2:
        if sel:
            try:
                js_fill = """({selector, value}) => {
                    const el = document.querySelector(selector);
                    if (!el) return false;
                    el.focus();
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
                        || Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')?.set;
                    if (nativeInputValueSetter) {
                        nativeInputValueSetter.call(el, value);
                    } else {
                        el.value = value;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    return true;
                }"""
                filled = await page.evaluate(js_fill, {"selector": sel, "value": val})
                if filled:
                    if action.press_enter:
                        await page.keyboard.press("Enter")
                    return True, f"Fill succeeded via JavaScript DOM assignment on '{sel}'"
            except Exception as e:
                logger.debug(f"Fallback fill strategy 2 failed: {e}")

    # Strategy 3: Alternative attribute match
    if strategy_step == 3 and form_fields:
        for f in form_fields:
            if f.selector == sel or (f.label and action.text and action.text in f.label):
                candidates = []
                if f.field_id:
                    candidates.append(page.locator(f"#{f.field_id}").first)
                if f.name:
                    candidates.append(page.locator(f"[name='{f.name}']").first)
                if f.placeholder:
                    candidates.append(page.get_by_placeholder(f.placeholder).first)
                for cand in candidates:
                    try:
                        if await cand.count() > 0:
                            await cand.fill(val, timeout=2000)
                            return True, f"Fill succeeded via attribute fallback locator for '{f.name or f.field_id}'"
                    except Exception:
                        continue

    return False, "All fill fallback strategies exhausted"


async def _fallback_navigate(
    controller: Any,
    action: BrowserAction,
    strategy_step: int,
) -> Tuple[bool, str]:
    """
    Step-level fallback strategies for navigation actions:
      Step 1: Relaxed wait_until ("commit" instead of "domcontentloaded" or "networkidle").
      Step 2: URL repair (ensure https:// protocol, strip trailing punctuation/spaces).
    """
    url = action.url or action.text or ""
    url = url.strip()

    # Strategy 1: URL scheme repair
    if strategy_step == 1:
        repaired_url = url
        if not repaired_url.startswith(("http://", "https://", "about:")):
            repaired_url = f"https://{repaired_url}"
        try:
            nav_res = await controller.navigate(repaired_url)
            return True, f"Navigation succeeded via URL protocol repair -> {nav_res}"
        except Exception as e:
            logger.debug(f"Fallback navigate strategy 1 failed: {e}")

    # Strategy 2: Relaxed wait using direct Playwright page if available
    if strategy_step == 2:
        page = getattr(controller, "current_page", None)
        if page:
            target = url if url.startswith(("http://", "https://")) else f"https://{url}"
            try:
                await page.goto(target, wait_until="commit", timeout=25000)
                await asyncio.sleep(1.0)
                return True, f"Navigation succeeded via relaxed wait_until='commit' -> {target}"
            except Exception as e:
                logger.debug(f"Fallback navigate strategy 2 failed: {e}")

    return False, "All navigation fallback strategies exhausted"


async def _fallback_select(
    page: Any,
    action: BrowserAction,
    strategy_step: int,
) -> Tuple[bool, str]:
    """
    Step-level fallback strategies for dropdown select actions:
      Step 1: Click dropdown container to open, then click option item by text.
      Step 2: JavaScript option value setter.
    """
    sel = action.selector
    val = action.value or action.text or action.select_label or ""

    # Strategy 1: Custom select container click + option text click
    if strategy_step == 1 and sel:
        try:
            loc = page.locator(sel).first
            await loc.click(timeout=2000)
            await asyncio.sleep(0.4)
            opt_loc = page.locator(f"[role='option']:has-text('{val}')").first
            if await opt_loc.count() == 0:
                opt_loc = page.locator(f"li:has-text('{val}')").first
            if await opt_loc.count() == 0:
                opt_loc = page.get_by_text(val).first
            await opt_loc.click(timeout=2000)
            return True, f"Select succeeded via custom dropdown menu click on option '{val}'"
        except Exception as e:
            logger.debug(f"Fallback select strategy 1 failed: {e}")

    # Strategy 2: JavaScript select option value assignment
    if strategy_step == 2 and sel:
        try:
            js_select = """({selector, value}) => {
                const el = document.querySelector(selector);
                if (!el || el.tagName.toLowerCase() !== 'select') return false;
                for (let i = 0; i < el.options.length; i++) {
                    if (el.options[i].value === value || el.options[i].text.toLowerCase().includes(value.toLowerCase())) {
                        el.selectedIndex = i;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }
                }
                return false;
            }"""
            selected = await page.evaluate(js_select, {"selector": sel, "value": val})
            if selected:
                return True, f"Select succeeded via JavaScript option selection on '{sel}'"
        except Exception as e:
            logger.debug(f"Fallback select strategy 2 failed: {e}")

    return False, "All select fallback strategies exhausted"


# ---------------------------------------------------------------------------
# Error classification and bounded recovery policy
# ---------------------------------------------------------------------------

def classify_browser_error(error: Any) -> str:
    """Map browser failures to stable diagnostic categories.

    Match specific locator/connection conditions before generic timeout or
    navigation tokens so recovery policy receives the most useful category.
    """
    message = str(error or "").lower()
    if any(token in message for token in (
        "extension disconnected", "websocket", "connection closed",
        "connection reset", "failed to fetch", "failed to connect",
    )):
        return "extension_connection"
    if any(token in message for token in (
        "permission", "forbidden", "unauthorized", "captcha",
        "payment approval", "approval required", "access denied",
        "http 401", "http 403",
    )):
        return "approval_or_permission"
    if "timed out waiting for selector" in message or (
        "waiting for selector" in message and any(token in message for token in ("timeout", "timed out", "deadline exceeded"))
    ):
        return "timeout"
    if any(token in message for token in (
        "strict mode violation", "no element", "no elements", "detached",
        "stale element", "stale locator", "waiting for locator",
        "waiting for selector", "locator not found", "selector not found",
    )):
        return "locator_or_stale_state"
    if any(token in message for token in (
        "timeout", "timed out", "deadline exceeded", "waiting for",
        "did not become ready", "not become ready",
    )):
        return "timeout"
    if any(token in message for token in (
        "navigation", "net::err", "chrome-error", "target closed",
        "page closed", "dns_probe", "name_not_resolved",
    )):
        return "navigation_or_page_state"
    return "unknown"


def should_retry_browser_error(error: Any) -> bool:
    """Return whether another bounded recovery attempt is useful and safe."""
    return classify_browser_error(error) in {
        "locator_or_stale_state",
        "timeout",
        "navigation_or_page_state",
    }


# Actions whose repeated execution can create duplicate or irreversible side effects.
# These are not retried automatically when the result is ambiguous.
_NON_IDEMPOTENT_ACTIONS = {
    ActionType.CLICK, ActionType.DOUBLE_CLICK, ActionType.RIGHT_CLICK,
    ActionType.UPLOAD, ActionType.UPLOAD_FILE, ActionType.DOWNLOAD_FILE,
    ActionType.CLOSE_TAB, ActionType.NEW_TAB, ActionType.PRESS,
    ActionType.PRESS_KEY, ActionType.DRAG_DROP,
}


def _requires_manual_recovery(action: BrowserAction) -> bool:
    if action.action in _NON_IDEMPOTENT_ACTIONS:
        return True
    text = " ".join(str(part or "") for part in (action.text, action.selector, action.reasoning)).lower()
    return action.action == ActionType.NAVIGATE and any(
        word in text for word in ("submit", "send", "delete", "pay", "purchase", "apply", "confirm")
    )


async def _verify_recovered_action(page: Any, action: BrowserAction, controller: Any) -> Tuple[bool, str]:
    """Verify a fallback's postcondition where the active Playwright page allows it."""
    if page is None:
        # Extension primary execution reports success from the content script; it
        # cannot be independently verified from this Playwright-only helper.
        return True, "Primary/extension executor reported success"
    try:
        if action.action in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD):
            selector = action.selector
            expected = action.value if action.value is not None else (action.text or "")
            if not selector:
                return False, "Fill action has no selector to verify"
            locator = page.locator(selector).first
            if await locator.count() == 0 or not await locator.is_visible():
                return False, "Fill target is missing or not visible after the action"
            actual = await locator.input_value(timeout=2000)
            if actual != expected:
                return False, "Fill postcondition failed: persisted input value did not match"
            return True, "Verified persisted input value"

        if action.action in (ActionType.SELECT, ActionType.SELECT_OPTION):
            selector = action.selector
            expected = action.value or action.text or action.select_label or ""
            if not selector:
                return False, "Select action has no selector to verify"
            locator = page.locator(selector).first
            if await locator.count() == 0 or not await locator.is_visible():
                return False, "Select target is missing or not visible after the action"
            tag = (await locator.evaluate("(el) => el.tagName.toLowerCase()")).lower()
            if tag == "select":
                actual = await locator.input_value(timeout=2000)
                option_text = await locator.locator("option:checked").inner_text()
                if actual != expected and expected.lower() not in (option_text or "").lower():
                    return False, "Select postcondition failed: selected option did not match"
            else:
                actual = await locator.get_attribute("aria-valuetext") or await locator.inner_text()
                if expected.lower() not in (actual or "").lower():
                    return False, "Custom select postcondition failed: selected value was not reflected"
            return True, "Verified selected option"

        if action.action == ActionType.NAVIGATE:
            current = str(getattr(page, "url", "") or "")
            try:
                state = await page.evaluate("""() => ({
                    ready: document.readyState,
                    title: document.title || "",
                    body: !!document.body,
                    text: (document.body?.innerText || "").slice(0, 500)
                })""")
            except Exception as exc:
                return False, f"Navigation page could not be inspected: {exc}"
            if (not current or current == "about:blank" or current.startswith("chrome-error://")
                    or not state.get("body") or state.get("ready") == "uninitialized"):
                return False, f"Navigation did not reach a usable page ({current or 'unknown URL'})"
            return True, f"Verified usable navigation destination at {current}"

        if action.action in (ActionType.CLICK, ActionType.DOUBLE_CLICK):
            # A click can be successful without changing URL (e.g. opening a menu).
            # Require a live page and a responsive DOM rather than blindly repeating it.
            await page.evaluate("() => document.readyState")
            return True, "Verified page remained responsive after click fallback"
        return True, "Fallback executor reported success"
    except Exception as exc:
        return False, f"Postcondition verification failed: {classify_browser_error(exc)}: {exc}"


# ---------------------------------------------------------------------------
# High-Level Resilient Action Execution Runner
# ---------------------------------------------------------------------------

async def execute_with_retry_and_fallback(
    execute_primary_fn,
    action: BrowserAction,
    controller: Any,
    task: Any = None,
    task_id: str = "",
    retry_policy: Optional[RetryPolicy] = None,
) -> Tuple[str, Any, bool, List[str]]:
    """
    Executes an action with automatic retries, exponential backoff, and
    step-level fallback strategies.

    Returns:
        (exec_description: str, extracted_item: Any, success: bool, recovery_log: List[str])
    """
    policy = retry_policy or RetryPolicy()
    recovery_log: List[str] = []
    page = getattr(controller, "current_page", None)
    form_fields = getattr(task, "_current_form_fields", None)

    last_error = "No recovery attempt was made"
    error_category = "unknown"
    for attempt in range(1, policy.max_retries + 1):
        # Never blindly repeat an action that might already have caused a side effect.
        # Only the first primary attempt is allowed for such actions; ambiguous outcomes
        # are surfaced to the planner/user instead of risking duplicate submissions.
        if attempt > 1 and _requires_manual_recovery(action):
            # A click can safely try a different locator only when the primary
            # failure explicitly indicates the target was not found/stale before
            # interaction. Any other click outcome is ambiguous and must stop.
            safe_locator_recovery = (
                action.action in (ActionType.CLICK, ActionType.DOUBLE_CLICK)
                and attempt == 2
                and classify_browser_error(last_error) == "locator_or_stale_state"
                and page is not None
            )
            if not safe_locator_recovery:
                recovery_log.append(
                    f"Recovery paused: {action.action.value} may have side effects; "
                    "automatic retry skipped to avoid duplicate execution."
                )
                break

        # Fast exit if task was cancelled or controller was stopped
        is_cancelled = (
            getattr(task, "status", None) == TaskStatus.CANCELLED
            or getattr(controller, "is_running", True) is False
        )
        if is_cancelled:
            logger.info(f"Task {task_id} cancellation detected inside recovery loop (attempt {attempt}). Aborting.")
            return "Task execution was cancelled by user.", None, False, ["Task cancelled by user."]

        try:
            if attempt == 1:
                # ── Primary execution attempt ──────────────────────────
                desc, extracted_item, success = await execute_primary_fn()
                if success and action.action in (
                    ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD,
                    ActionType.SELECT, ActionType.SELECT_OPTION, ActionType.NAVIGATE,
                ):
                    # Re-read the controller's active page after the action; navigation
                    # may have replaced the page object captured before execution.
                    page = getattr(controller, "current_page", None) or page
                    verified, verification_note = await _verify_recovered_action(page, action, controller)
                    if verified:
                        recovery_log.append(f"Postcondition: {verification_note}")
                        return desc, extracted_item, True, recovery_log
                    success = False
                    desc = f"{desc}; independent verification failed: {verification_note}"
                elif success:
                    return desc, extracted_item, True, recovery_log
                last_error = desc
                error_category = classify_browser_error(last_error)
            else:
                # ── Fallback execution attempt (Strategy step = attempt - 1) ──
                strategy_step = attempt - 1
                fallback_success = False
                fallback_desc = ""

                if action.action in (ActionType.CLICK, ActionType.DOUBLE_CLICK) and page:
                    fallback_success, fallback_desc = await _fallback_click(
                        page, action, strategy_step, form_fields
                    )
                elif action.action in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD) and page:
                    fallback_success, fallback_desc = await _fallback_fill(
                        page, action, strategy_step, form_fields
                    )
                elif action.action == ActionType.NAVIGATE:
                    fallback_success, fallback_desc = await _fallback_navigate(
                        controller, action, strategy_step
                    )
                elif action.action in (ActionType.SELECT, ActionType.SELECT_OPTION) and page:
                    fallback_success, fallback_desc = await _fallback_select(
                        page, action, strategy_step
                    )
                elif action.action == ActionType.SCROLL and page:
                    direction = action.scroll_direction or "down"
                    amount = action.scroll_amount or 500
                    delta = amount if direction == "down" else -amount
                    try:
                        await page.evaluate(f"window.scrollBy(0, {delta});")
                        fallback_success = True
                        fallback_desc = f"Scroll recovered via window.scrollBy({delta})"
                    except Exception as e:
                        fallback_desc = str(e)
                else:
                    # Chrome Extension or unhandled fallback action: re-invoke primary fn
                    desc, extracted_item, success = await execute_primary_fn()
                    if success:
                        recovery_log.append(f"Attempt {attempt}: Primary retry succeeded ({desc})")
                        return desc, extracted_item, True, recovery_log
                    last_error = desc

                if fallback_success:
                    verified, verification_note = await _verify_recovered_action(page, action, controller)
                    if verified:
                        note = f"Attempt {attempt} RECOVERED via fallback: {fallback_desc}; {verification_note}"
                        logger.info("Task %s: %s", task_id, note)
                        recovery_log.append(note)
                        return f"{action.action.value.upper()}: {fallback_desc}", None, True, recovery_log
                    fallback_desc = f"{fallback_desc}; verification failed: {verification_note}"
                    fallback_success = False

                if not fallback_success:
                    last_error = fallback_desc
                    error_category = classify_browser_error(last_error)

        except Exception as exc:
            last_error = str(exc)
            error_category = classify_browser_error(last_error)
            logger.warning(
                "Task %s recovery attempt %s failed [category=%s]: %s",
                task_id, attempt, error_category, last_error,
            )

        # Check cancellation before sleeping
        if (
            getattr(task, "status", None) == TaskStatus.CANCELLED
            or getattr(controller, "is_running", True) is False
        ):
            logger.info(f"Task {task_id} cancelled after attempt {attempt}. Aborting.")
            return "Task execution was cancelled by user.", None, False, ["Task cancelled by user."]

        # Do not spend retries on failures that require user intervention or
        # a restored extension connection. Unknown failures are surfaced rather
        # than repeatedly issuing commands without a diagnosed recovery path.
        if not should_retry_browser_error(last_error):
            recovery_log.append(
                f"Recovery stopped: failure category '{error_category}' is not automatically retryable."
            )
            break

        # If not successful and retries remain, back off exponentially
        if attempt < policy.max_retries:
            delay = policy.get_delay(attempt)
            note = f"Attempt {attempt} failed ({last_error}). Retrying in {delay}s with exponential backoff..."
            logger.info(f"Task {task_id}: {note}")
            recovery_log.append(note)

            # Interruptible backoff sleep (0.1s increments)
            elapsed = 0.0
            while elapsed < delay:
                if (
                    getattr(task, "status", None) == TaskStatus.CANCELLED
                    or getattr(controller, "is_running", True) is False
                ):
                    logger.info(f"Task {task_id} cancelled during backoff sleep. Aborting immediately.")
                    return "Task execution was cancelled by user.", None, False, ["Task cancelled during backoff."]
                step = min(0.1, delay - elapsed)
                await asyncio.sleep(step)
                elapsed += step

            # Settle page if Playwright is available
            if page and getattr(controller, "is_running", True):
                try:
                    from browser.waiter import settle_page
                    await settle_page(page, timeout=int(delay * 1000))
                except Exception:
                    pass

    failure_msg = (
        f"{action.action.value.upper()} failed [{error_category}]: {last_error}. "
        "Recovery exhausted; inspect the page or request user intervention before retrying."
    )
    return failure_msg, None, False, recovery_log
