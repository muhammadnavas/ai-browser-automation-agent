import logging
import asyncio
from typing import List, Optional
from playwright.async_api import Page, Locator
from models.schemas import BrowserAction, ActionType
from browser.form_discovery import FormDiscovery, FormField
from browser import actions as browser_actions

logger = logging.getLogger("ActionExecutor")

class ActionExecutor:
    """
    Executes BrowserActions on a Playwright Page with dynamic element resolution,
    verification, and automatic retries if validation checks fail.
    """

    def __init__(self, page: Page):
        self.page = page
        self.discovery = FormDiscovery()
        # Keep a small, consistent cadence between actions without adding
        # long fixed sleeps to every interaction.
        self.action_interval = 0.15
        self._last_action_at = 0.0

    async def _pace_action(self) -> None:
        """Apply a small minimum interval between actions for consistent pacing."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        remaining = self.action_interval - (now - self._last_action_at)
        if remaining > 0:
            await asyncio.sleep(remaining)
        self._last_action_at = loop.time()

    async def execute(self, action: BrowserAction, form_fields: Optional[List[FormField]] = None) -> bool:
        """
        Execute a BrowserAction with dynamic resolution, verification, and retry logic.
        """
        # Keep action pacing consistent; only a short delay is added when
        # actions are being issued faster than the configured cadence.
        await self._pace_action()

        # Resolve target element locator if applicable
        locator = await self.resolve_locator(action, form_fields)

        # Perform action
        success = await self._perform_action(action, locator)
        if not success:
            logger.warning(f"Initial action execution failed for: {action.action}. Attempting step fallbacks...")
            from browser.recovery import _fallback_click, _fallback_fill, _fallback_select
            act_type = action.action

            for fallback_step in range(1, 3):
                await asyncio.sleep(0.2 * fallback_step)
                if act_type in (ActionType.CLICK, ActionType.DOUBLE_CLICK):
                    success, desc = await _fallback_click(self.page, action, fallback_step, form_fields)
                elif act_type in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD):
                    success, desc = await _fallback_fill(self.page, action, fallback_step, form_fields)
                elif act_type in (ActionType.SELECT, ActionType.SELECT_OPTION):
                    success, desc = await _fallback_select(self.page, action, fallback_step)

                if success:
                    logger.info(f"Action recovered via fallback step {fallback_step}: {desc}")
                    break

        if not success:
            logger.warning(f"All action execution attempts and fallbacks failed for: {action.action}")
            return False

        # Verify action
        verified = await self._verify_action(action, locator)
        if verified:
            return True

        # If verification fails, retry once: re-inspect page, re-resolve element, and retry
        logger.warning(f"Verification failed for action {action.action}. Retrying...")
        
        # 1. Re-inspect page
        fresh_result = await self.discovery.discover(self.page)
        fresh_fields = fresh_result.all_fields

        # 2. Re-resolve element
        locator = await self.resolve_locator(action, fresh_fields)
        
        # 3. Retry action
        success = await self._perform_action(action, locator)
        if not success:
            return False

        # 4. Re-verify
        return await self._verify_action(action, locator)

    async def resolve_locator(self, action: BrowserAction, form_fields: Optional[List[FormField]] = None) -> Optional[Locator]:
        """
        Dynamically resolve a locator using selector, ID, name, label, placeholder, text, or form_fields attributes.
        """
        target_selector = action.selector
        
        # Try to find matching field info from form_fields for extra signals (label, placeholder, ID, name)
        field_info: Optional[FormField] = None
        if form_fields and target_selector:
            for f in form_fields:
                if f.selector == target_selector or f.field_id == target_selector.replace("#", ""):
                    field_info = f
                    break
        
        # Let's collect all possible locator candidates
        candidates = []

        # For radio buttons, if radio_name and value are provided, compile the selector
        if action.action == ActionType.RADIO_BUTTON and action.radio_name and action.value:
            candidates.append(self.page.locator(f"input[type='radio'][name='{action.radio_name}'][value='{action.value}']").first)

        # 1. Selector-based locators
        if target_selector:
            candidates.append(self.page.locator(target_selector).first)

        # 2. Field Info attributes
        if field_info:
            if field_info.field_id:
                candidates.append(self.page.locator(f"#{field_info.field_id}").first)
            if field_info.name:
                candidates.append(self.page.locator(f"[name='{field_info.name}']").first)
            if field_info.label:
                candidates.append(self.page.get_by_label(field_info.label).first)
                # Fallback label contains text
                candidates.append(self.page.locator(f"label:has-text('{field_info.label}')").first)
            if field_info.placeholder:
                candidates.append(self.page.get_by_placeholder(field_info.placeholder).first)
            if field_info.aria_label:
                candidates.append(self.page.get_by_label(field_info.aria_label).first)

        # 3. Extra text/aria checks from action payload
        if action.text and action.action in (ActionType.CLICK, ActionType.SELECT_OPTION, ActionType.SELECT):
            candidates.append(self.page.get_by_text(action.text).first)
            candidates.append(self.page.get_by_role("button", name=action.text).first)

        # Try to find the first candidate that exists and is attached/visible
        for loc in candidates:
            try:
                # Check if it exists in DOM
                if await loc.count() > 0:
                    return loc
            except Exception:
                pass

        # Default fallback
        if target_selector:
            return self.page.locator(target_selector).first
        return None

    async def _perform_action(self, action: BrowserAction, locator: Optional[Locator]) -> bool:
        """Maps Action to Playwright operations."""
        act_type = action.action
        val = action.value or action.text or ""
        
        # Normalize actions
        if act_type in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD):
            if not locator:
                return False
            try:
                await locator.wait_for(state="visible", timeout=1800)
                await locator.scroll_into_view_if_needed()
                await locator.fill(val)
                if action.press_enter or (act_type == ActionType.TYPE_TEXT and action.press_enter is not None and action.press_enter):
                    await self.page.keyboard.press("Enter")
                    await asyncio.sleep(0.05)
                return True
            except Exception as exc:
                logger.error(f"fill failed: {exc}")
                return False

        elif act_type == ActionType.CLICK:
            if not locator:
                if action.text:
                    locator = self.page.get_by_text(action.text).first
                else:
                    return False
            try:
                await locator.wait_for(state="visible", timeout=3000)
                await locator.scroll_into_view_if_needed()
                await locator.click()
                return True
            except Exception as exc:
                logger.error(f"click failed: {exc}")
                return False

        elif act_type in (ActionType.SELECT, ActionType.SELECT_OPTION):
            if not locator:
                return False
            try:
                await locator.wait_for(state="visible", timeout=3000)
                await locator.scroll_into_view_if_needed()
                
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "select":
                    label = action.select_label or val
                    if label:
                        await locator.select_option(label=label)
                    else:
                        await locator.select_option(value=val)
                    return True
                else:
                    # Custom select
                    await locator.click()
                    await asyncio.sleep(0.15)
                    option_text = action.select_label or val
                    option_locator = self.page.locator(f"[role='option']:has-text('{option_text}')").first
                    if await option_locator.count() == 0:
                        option_locator = self.page.locator(f"li:has-text('{option_text}')").first
                    if await option_locator.count() == 0:
                        option_locator = self.page.get_by_text(option_text).first
                        
                    await option_locator.click()
                    return True
            except Exception as exc:
                logger.error(f"select failed: {exc}")
                return False

        elif act_type in (ActionType.CHECK, ActionType.CHECK_BOX) or (act_type == ActionType.UNCHECK):
            if not locator:
                return False
            try:
                await locator.wait_for(state="visible", timeout=3000)
                await locator.scroll_into_view_if_needed()
                checked = action.checked if action.checked is not None else (act_type != ActionType.UNCHECK)
                if checked:
                    await locator.check()
                else:
                    await locator.uncheck()
                return True
            except Exception as exc:
                logger.error(f"checkbox toggle failed: {exc}")
                return False

        elif act_type in (ActionType.UPLOAD, ActionType.UPLOAD_FILE):
            if not locator:
                return False
            try:
                files = action.upload_paths or ([action.text] if action.text else [])
                if not files:
                    logger.warning("No files provided for upload")
                    return False
                logger.info(f"Uploading {len(files)} file(s) into file input")
                await locator.set_input_files(files)
                return True
            except Exception as exc:
                logger.error(f"upload failed: {exc}")
                return False

        elif act_type == ActionType.SCROLL:
            direction = action.scroll_direction or "down"
            amount = action.scroll_amount or 500
            delta = amount if direction == "down" else -amount
            try:
                await self.page.evaluate(f"window.scrollBy(0, {delta});")
                await asyncio.sleep(0.5)
                return True
            except Exception as exc:
                logger.error(f"scroll failed: {exc}")
                return False

        elif act_type == ActionType.FOCUS:
            if not locator:
                return False
            try:
                await locator.focus()
                return True
            except Exception as exc:
                logger.error(f"focus failed: {exc}")
                return False

        elif act_type in (ActionType.PRESS, ActionType.PRESS_KEY):
            key = action.key or action.text or "Enter"
            try:
                if locator:
                    await locator.press(key)
                else:
                    await self.page.keyboard.press(key)
                return True
            except Exception as exc:
                logger.error(f"press key failed: {exc}")
                return False

        elif act_type == ActionType.RADIO_BUTTON:
            name = action.radio_name or action.selector or ""
            val = action.value or action.text or ""
            try:
                if locator:
                    await locator.wait_for(state="visible", timeout=3000)
                    await locator.scroll_into_view_if_needed()
                    await locator.click()
                    return True
                else:
                    return await browser_actions.select_radio(self.page, name, val)
            except Exception as exc:
                logger.error(f"radio_button failed: {exc}")
                return False

        elif act_type == ActionType.WAIT:
            sel = action.wait_selector or action.selector
            state = action.wait_state or "visible"
            timeout = action.wait_timeout or 10000
            try:
                if sel:
                    await self.page.wait_for_selector(sel, state=state, timeout=timeout)
                else:
                    await asyncio.sleep(timeout / 1000)
                return True
            except Exception as exc:
                logger.error(f"wait failed: {exc}")
                return False

        logger.warning(f"Action type '{act_type}' not handled by executor.")
        return False

    async def _verify_action(self, action: BrowserAction, locator: Optional[Locator]) -> bool:
        """Verifies if the action successfully updated the DOM state."""
        act_type = action.action
        val = action.value or action.text or ""
        
        if act_type not in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD,
                            ActionType.SELECT, ActionType.SELECT_OPTION,
                            ActionType.CHECK, ActionType.CHECK_BOX, ActionType.UNCHECK,
                            ActionType.RADIO_BUTTON,
                            ActionType.UPLOAD, ActionType.UPLOAD_FILE):
            return True

        if not locator:
            if act_type == ActionType.RADIO_BUTTON:
                name = action.radio_name or action.selector or ""
                val = action.value or action.text or ""
                try:
                    return await self.page.locator(f"input[type='radio'][name='{name}'][value='{val}']").is_checked()
                except Exception:
                    return False
            return False

        try:
            await asyncio.sleep(0.2)

            if act_type in (ActionType.FILL, ActionType.TYPE_TEXT, ActionType.FILL_FIELD):
                is_editable = await locator.evaluate("el => el.getAttribute('contenteditable') === 'true' || el.isContentEditable")
                if is_editable:
                    input_val = await locator.inner_text()
                else:
                    try:
                        input_val = await locator.input_value()
                    except Exception:
                        input_val = await locator.inner_text()
                
                match = str(val).strip().lower() in str(input_val).strip().lower()
                if not match:
                    logger.warning(f"Verification failed: expected '{val}', got '{input_val}'")
                return match

            elif act_type in (ActionType.CHECK, ActionType.CHECK_BOX, ActionType.UNCHECK):
                expected = action.checked if action.checked is not None else (act_type != ActionType.UNCHECK)
                actual = await locator.is_checked()
                return expected == actual

            elif act_type == ActionType.RADIO_BUTTON:
                if locator:
                    return await locator.is_checked()
                else:
                    name = action.radio_name or action.selector or ""
                    val = action.value or action.text or ""
                    return await self.page.locator(f"input[type='radio'][name='{name}'][value='{val}']").is_checked()

            elif act_type in (ActionType.SELECT, ActionType.SELECT_OPTION):
                tag_name = await locator.evaluate("el => el.tagName.toLowerCase()")
                if tag_name == "select":
                    selected_val = await locator.evaluate("el => el.value")
                    selected_label = await locator.evaluate("el => el.options[el.selectedIndex]?.text")
                    target = action.select_label or val
                    match = (str(target).strip().lower() in str(selected_label).strip().lower() or
                             str(val).strip().lower() in str(selected_val).strip().lower())
                    return match
                else:
                    return True

            elif act_type in (ActionType.UPLOAD, ActionType.UPLOAD_FILE):
                files_count = await locator.evaluate("el => el.files.length")
                return files_count > 0

        except Exception as exc:
            logger.warning(f"Verification check failed with exception: {exc}")
            return False

        return True
