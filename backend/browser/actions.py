"""
browser/actions.py
------------------
High-level reusable interaction primitives.
All functions operate on a Playwright `Page` object and can be called
directly by the AI Planner / Executor without needing a full BrowserController.
"""

import asyncio
import logging
from typing import List, Optional

from playwright.async_api import Page

logger = logging.getLogger("browser.actions")

# ---------------------------------------------------------------------------
# Fill / Input
# ---------------------------------------------------------------------------

async def fill_field(
    page: Page,
    selector: str,
    value: str,
    clear_first: bool = True,
    timeout: int = 5000,
) -> bool:
    """
    Clear (optional) and fill a text/input field.

    Args:
        page: Playwright Page object.
        selector: CSS selector or text selector.
        value: Text to fill.
        clear_first: If True, clears the field before typing.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        if clear_first:
            await locator.clear(timeout=timeout)
        await locator.fill(value, timeout=timeout)
        logger.debug(f"Filled '{selector}' with '{value[:40]}'")
        return True
    except Exception as exc:
        logger.error(f"fill_field failed [{selector}]: {exc}")
        return False


async def type_into(
    page: Page,
    selector: str,
    text: str,
    delay_ms: int = 75,
    timeout: int = 5000,
) -> bool:
    """
    Type text character-by-character (realistic typing) into an element.

    Args:
        page: Playwright Page.
        selector: Target element.
        text: Text to type.
        delay_ms: Delay between keystrokes in milliseconds.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout)
        await page.keyboard.type(text, delay=delay_ms)
        await asyncio.sleep(0.4)  # Brief pause after typing like a human
        logger.debug(f"Typed into '{selector}': '{text[:40]}'")
        return True
    except Exception as exc:
        logger.error(f"type_into failed [{selector}]: {exc}")
        return False


# ---------------------------------------------------------------------------
# Select / Dropdown
# ---------------------------------------------------------------------------

async def select_option(
    page: Page,
    selector: str,
    value: Optional[str] = None,
    label: Optional[str] = None,
    index: Optional[int] = None,
    timeout: int = 5000,
) -> bool:
    """
    Select an option in a <select> dropdown by value, visible label, or index.

    Args:
        page: Playwright Page.
        selector: CSS selector of the <select> element.
        value: Option value attribute.
        label: Visible text of the option.
        index: Zero-based index of the option.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)

        option: dict = {}
        if value is not None:
            option["value"] = value
        elif label is not None:
            option["label"] = label
        elif index is not None:
            option["index"] = index
        else:
            raise ValueError("One of value, label, or index must be provided.")

        await locator.select_option(**option, timeout=timeout)
        logger.debug(f"Selected option in '{selector}': {option}")
        return True
    except Exception as exc:
        logger.error(f"select_option failed [{selector}]: {exc}")
        return False


# ---------------------------------------------------------------------------
# Checkbox
# ---------------------------------------------------------------------------

async def check_checkbox(
    page: Page,
    selector: str,
    checked: bool = True,
    timeout: int = 5000,
) -> bool:
    """
    Set a checkbox to checked or unchecked state.

    Args:
        page: Playwright Page.
        selector: CSS selector of the checkbox input.
        checked: Desired state — True to check, False to uncheck.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        if checked:
            await locator.check(timeout=timeout)
        else:
            await locator.uncheck(timeout=timeout)
        logger.debug(f"Checkbox '{selector}' set to checked={checked}")
        return True
    except Exception as exc:
        logger.error(f"check_checkbox failed [{selector}]: {exc}")
        return False


async def get_checkbox_state(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> Optional[bool]:
    """
    Returns the current checked state of a checkbox.

    Args:
        page: Playwright Page.
        selector: CSS selector of the checkbox.
        timeout: Max wait in ms.

    Returns:
        True if checked, False if unchecked, None on error.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=timeout)
        return await locator.is_checked(timeout=timeout)
    except Exception as exc:
        logger.error(f"get_checkbox_state failed [{selector}]: {exc}")
        return None


# ---------------------------------------------------------------------------
# Radio Buttons
# ---------------------------------------------------------------------------

async def select_radio(
    page: Page,
    name: str,
    value: str,
    timeout: int = 5000,
) -> bool:
    """
    Select a radio button by its `name` attribute and `value`.

    Args:
        page: Playwright Page.
        name: The `name` attribute shared by the radio group.
        value: The `value` attribute of the radio to select.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    selector = f"input[type='radio'][name='{name}'][value='{value}']"

    try:
        locator = page.locator(selector).first

        await locator.wait_for(state="visible", timeout=timeout)

        await locator.scroll_into_view_if_needed()

        try:
            # Standard HTML radio buttons
            await locator.check(timeout=timeout)

        except Exception:
            # Fallback for custom-styled radio buttons
            label = locator.locator("xpath=..")
            await label.click(timeout=timeout)

        logger.debug(
            f"Radio button selected: name='{name}', value='{value}'"
        )

        return True

    except Exception as exc:
        logger.error(
            f"select_radio failed [name={name}, value={value}]: {exc}"
        )
        return False


async def select_radio_by_label(
    page: Page,
    label_text: str,
    timeout: int = 5000,
) -> bool:
    """
    Select a radio button that is associated with a label containing the given text.

    Args:
        page: Playwright Page.
        label_text: Visible text of the label associated with the radio.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.get_by_label(label_text).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.check(timeout=timeout)
        logger.debug(f"Radio button selected by label: '{label_text}'")
        return True
    except Exception as exc:
        logger.error(f"select_radio_by_label failed ['{label_text}']: {exc}")
        return False


# ---------------------------------------------------------------------------
# Click Actions
# ---------------------------------------------------------------------------

async def click(
    page: Page,
    selector: str,
    timeout: int = 5000,
    force: bool = False,
) -> bool:
    """
    Standard single click on an element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.
        force: Bypass actionability checks if True.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(timeout=timeout, force=force)
        logger.debug(f"Clicked '{selector}'")
        return True
    except Exception as exc:
        logger.error(f"click failed [{selector}]: {exc}")
        return False


async def double_click(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> bool:
    """
    Double-click on an element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.dblclick(timeout=timeout)
        logger.debug(f"Double-clicked '{selector}'")
        return True
    except Exception as exc:
        logger.error(f"double_click failed [{selector}]: {exc}")
        return False


async def right_click(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> bool:
    """
    Right-click (context menu trigger) on an element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.click(button="right", timeout=timeout)
        logger.debug(f"Right-clicked '{selector}'")
        return True
    except Exception as exc:
        logger.error(f"right_click failed [{selector}]: {exc}")
        return False


async def hover(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> bool:
    """
    Hover the mouse over an element (triggers CSS :hover states, tooltips, etc.).

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="visible", timeout=timeout)
        await locator.hover(timeout=timeout)
        logger.debug(f"Hovered over '{selector}'")
        return True
    except Exception as exc:
        logger.error(f"hover failed [{selector}]: {exc}")
        return False


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

async def press_key(
    page: Page,
    key: str,
    selector: Optional[str] = None,
    timeout: int = 5000,
) -> bool:
    """
    Press a keyboard key, optionally focused on a specific element.

    Args:
        page: Playwright Page.
        key: Key name (e.g. 'Enter', 'Tab', 'Escape', 'Control+A').
        selector: If provided, focus this element first.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        if selector:
            locator = page.locator(selector).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.press(key, timeout=timeout)
        else:
            await page.keyboard.press(key)
        logger.debug(f"Pressed key '{key}' on '{selector or 'page'}'")
        return True
    except Exception as exc:
        logger.error(f"press_key failed [key={key}]: {exc}")
        return False


# ---------------------------------------------------------------------------
# Scroll
# ---------------------------------------------------------------------------

async def scroll_page(
    page: Page,
    direction: str = "down",
    amount: int = 500,
) -> bool:
    """
    Scroll the page viewport up or down.

    Args:
        page: Playwright Page.
        direction: 'up' or 'down'.
        amount: Pixels to scroll.

    Returns:
        True on success, False on failure.
    """
    delta = amount if direction == "down" else -amount
    try:
        await page.evaluate(f"window.scrollBy(0, {delta});")
        await asyncio.sleep(0.4)
        logger.debug(f"Scrolled {direction} {amount}px")
        return True
    except Exception as exc:
        logger.error(f"scroll_page failed: {exc}")
        return False


async def scroll_to_element(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> bool:
    """
    Scroll the element with the given selector into view.

    Args:
        page: Playwright Page.
        selector: CSS selector of the element.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=timeout)
        await locator.scroll_into_view_if_needed(timeout=timeout)
        logger.debug(f"Scrolled element '{selector}' into view")
        return True
    except Exception as exc:
        logger.error(f"scroll_to_element failed [{selector}]: {exc}")
        return False


async def scroll_to_top(page: Page) -> bool:
    """Scroll to the very top of the page."""
    try:
        await page.evaluate("window.scrollTo(0, 0);")
        return True
    except Exception as exc:
        logger.error(f"scroll_to_top failed: {exc}")
        return False


async def scroll_to_bottom(page: Page) -> bool:
    """Scroll to the very bottom of the page."""
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        await asyncio.sleep(0.4)
        return True
    except Exception as exc:
        logger.error(f"scroll_to_bottom failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Drag & Drop
# ---------------------------------------------------------------------------

async def drag_and_drop(
    page: Page,
    source_selector: str,
    target_selector: str,
    timeout: int = 5000,
) -> bool:
    """
    Drag an element to a target element.

    Args:
        page: Playwright Page.
        source_selector: CSS selector of drag source.
        target_selector: CSS selector of drop target.
        timeout: Max wait in ms.

    Returns:
        True on success, False on failure.
    """
    try:
        source = page.locator(source_selector).first
        target = page.locator(target_selector).first
        await source.wait_for(state="visible", timeout=timeout)
        await target.wait_for(state="visible", timeout=timeout)
        await source.drag_to(target, timeout=timeout)
        logger.debug(f"Dragged '{source_selector}' -> '{target_selector}'")
        return True
    except Exception as exc:
        logger.error(f"drag_and_drop failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Data Extraction
# ---------------------------------------------------------------------------

async def get_text(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> Optional[str]:
    """
    Return the inner text of the first matching element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        Inner text string or None on failure.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=timeout)
        return (await locator.inner_text(timeout=timeout)).strip()
    except Exception as exc:
        logger.error(f"get_text failed [{selector}]: {exc}")
        return None


async def get_all_texts(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> List[str]:
    """
    Return inner text of all matching elements.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        List of text strings.
    """
    try:
        locator = page.locator(selector)
        await locator.first.wait_for(state="attached", timeout=timeout)
        return await locator.all_inner_texts()
    except Exception as exc:
        logger.error(f"get_all_texts failed [{selector}]: {exc}")
        return []


async def get_attribute(
    page: Page,
    selector: str,
    attribute: str,
    timeout: int = 5000,
) -> Optional[str]:
    """
    Return the value of a specific attribute from the first matching element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        attribute: HTML attribute name (e.g. 'href', 'src', 'value').
        timeout: Max wait in ms.

    Returns:
        Attribute value string or None.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=timeout)
        return await locator.get_attribute(attribute, timeout=timeout)
    except Exception as exc:
        logger.error(f"get_attribute failed [{selector}@{attribute}]: {exc}")
        return None


async def get_input_value(
    page: Page,
    selector: str,
    timeout: int = 5000,
) -> Optional[str]:
    """
    Return the current value of an input/textarea element.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        Input value string or None.
    """
    try:
        locator = page.locator(selector).first
        await locator.wait_for(state="attached", timeout=timeout)
        return await locator.input_value(timeout=timeout)
    except Exception as exc:
        logger.error(f"get_input_value failed [{selector}]: {exc}")
        return None


async def get_page_text(page: Page) -> str:
    """
    Return the full visible text content of the current page.

    Args:
        page: Playwright Page.

    Returns:
        Full page text as a string.
    """
    try:
        return await page.evaluate("() => document.body.innerText")
    except Exception as exc:
        logger.error(f"get_page_text failed: {exc}")
        return ""


async def is_visible(
    page: Page,
    selector: str,
    timeout: int = 3000,
) -> bool:
    """
    Return True if the element is visible on the page.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True if visible, False otherwise.
    """
    try:
        locator = page.locator(selector).first
        return await locator.is_visible(timeout=timeout)
    except Exception:
        return False


async def is_enabled(
    page: Page,
    selector: str,
    timeout: int = 3000,
) -> bool:
    """
    Return True if the element is enabled (not disabled).

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True if enabled, False otherwise.
    """
    try:
        locator = page.locator(selector).first
        return await locator.is_enabled(timeout=timeout)
    except Exception:
        return False
