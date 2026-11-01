"""
browser/waiter.py
-----------------
Smart async wait utilities for Playwright pages.
Centralises all explicit wait strategies so the rest of the code doesn't
need to sprinkle raw asyncio.sleep() or page.wait_for_* calls.
"""

import asyncio
import logging
import re
from typing import Callable, Coroutine, Optional, Any

from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError

logger = logging.getLogger("browser.waiter")


async def wait_for_selector(
    page: Page,
    selector: str,
    state: str = "visible",
    timeout: int = 6000,
) -> bool:
    """
    Wait for an element matching *selector* to reach a specific *state*.

    Args:
        page: Playwright Page.
        selector: CSS or text selector.
        state: 'attached' | 'detached' | 'visible' | 'hidden'.
        timeout: Max wait in ms.

    Returns:
        True if element reached desired state, False on timeout.
    """
    try:
        await page.wait_for_selector(selector, state=state, timeout=timeout)
        logger.debug(f"Selector '{selector}' reached state='{state}'")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Timeout waiting for selector '{selector}' state='{state}'")
        return False
    except Exception as exc:
        logger.error(f"wait_for_selector error [{selector}]: {exc}")
        return False


async def wait_for_text(
    page: Page,
    text: str,
    timeout: int = 10000,
    case_sensitive: bool = False,
) -> bool:
    """
    Wait until the given *text* appears anywhere on the page.

    Args:
        page: Playwright Page.
        text: Text string to look for.
        timeout: Max wait in ms.
        case_sensitive: Whether the match is case-sensitive.

    Returns:
        True when text found, False on timeout.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(re.escape(text), flags)
    deadline = asyncio.get_event_loop().time() + timeout / 1000

    while asyncio.get_event_loop().time() < deadline:
        try:
            content = await page.evaluate("() => document.body.innerText")
            if pattern.search(content):
                logger.debug(f"Text found on page: '{text[:60]}'")
                return True
        except Exception:
            pass
        await asyncio.sleep(0.2)

    logger.warning(f"Timeout waiting for text '{text[:60]}' on page")
    return False


async def wait_for_navigation(
    page: Page,
    url_pattern: Optional[str] = None,
    timeout: int = 10000,
) -> bool:
    """
    Wait for the page to navigate (URL change).

    Args:
        page: Playwright Page.
        url_pattern: Optional glob pattern the new URL must match.
        timeout: Max wait in ms.

    Returns:
        True when navigation completes, False on timeout.
    """
    try:
        if url_pattern:
            await page.wait_for_url(url_pattern, timeout=timeout)
        else:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        logger.debug(f"Navigation complete. URL: {page.url}")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Timeout waiting for navigation (pattern={url_pattern})")
        return False
    except Exception as exc:
        logger.error(f"wait_for_navigation error: {exc}")
        return False


async def wait_for_load_state(
    page: Page,
    state: str = "domcontentloaded",
    timeout: int = 15000,
) -> bool:
    """
    Wait for the page to reach a specific load state.

    Args:
        page: Playwright Page.
        state: 'load' | 'domcontentloaded' | 'networkidle'.
        timeout: Max wait in ms.

    Returns:
        True on success, False on timeout.
    """
    try:
        await page.wait_for_load_state(state, timeout=timeout)
        logger.debug(f"Page reached load state: '{state}'")
        return True
    except PlaywrightTimeoutError:
        logger.warning(f"Timeout waiting for load state '{state}'")
        return False
    except Exception as exc:
        logger.error(f"wait_for_load_state error: {exc}")
        return False


async def wait_for_network_idle(
    page: Page,
    timeout: int = 15000,
) -> bool:
    """
    Wait until there are no in-flight network requests (networkidle).
    Equivalent to `wait_for_load_state('networkidle')` but exposed separately
    for clarity.

    Args:
        page: Playwright Page.
        timeout: Max wait in ms.

    Returns:
        True on success, False on timeout.
    """
    return await wait_for_load_state(page, state="networkidle", timeout=timeout)


async def wait_for_element_hidden(
    page: Page,
    selector: str,
    timeout: int = 10000,
) -> bool:
    """
    Wait until an element becomes hidden or detached.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True when hidden, False on timeout.
    """
    return await wait_for_selector(page, selector, state="hidden", timeout=timeout)


async def wait_for_element_visible(
    page: Page,
    selector: str,
    timeout: int = 10000,
) -> bool:
    """
    Wait until an element becomes visible.

    Args:
        page: Playwright Page.
        selector: CSS selector.
        timeout: Max wait in ms.

    Returns:
        True when visible, False on timeout.
    """
    return await wait_for_selector(page, selector, state="visible", timeout=timeout)


async def wait_for_url_change(
    page: Page,
    original_url: str,
    timeout: int = 10000,
) -> bool:
    """
    Wait until the page URL changes away from *original_url*.

    Args:
        page: Playwright Page.
        original_url: URL before the action was triggered.
        timeout: Max wait in ms.

    Returns:
        True when URL changes, False on timeout.
    """
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        if page.url != original_url:
            logger.debug(f"URL changed: {original_url} -> {page.url}")
            return True
        await asyncio.sleep(0.15)
    logger.warning(f"Timeout waiting for URL to change from '{original_url}'")
    return False


async def wait_for_condition(
    condition: Callable[[], Coroutine[Any, Any, bool]],
    timeout: int = 10000,
    poll_interval: float = 0.5,
) -> bool:
    """
    Poll an arbitrary async condition until it returns True or times out.

    Args:
        condition: An async callable that returns True when done.
        timeout: Max wait in ms.
        poll_interval: Polling frequency in seconds.

    Returns:
        True when condition passes, False on timeout.

    Example::

        result = await wait_for_condition(
            lambda: page.locator('#spinner').is_hidden()
        )
    """
    deadline = asyncio.get_event_loop().time() + timeout / 1000
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await condition():
                return True
        except Exception:
            pass
        await asyncio.sleep(poll_interval)
    logger.warning("wait_for_condition timed out")
    return False


async def sleep(seconds: float) -> None:
    """
    Simple async sleep helper — avoids raw asyncio.sleep imports elsewhere.

    Args:
        seconds: Number of seconds to wait.
    """
    await asyncio.sleep(seconds)


async def settle_page(page: Page, timeout: int = 3000) -> bool:
    """
    Dynamically wait for the page to settle (DOM content loaded + brief network idle).
    Falls back gracefully on fast/slow networks without blocking execution.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        # Attempt networkidle with shorter timeout for quick response
        await page.wait_for_load_state("networkidle", timeout=min(timeout, 3000))
        return True
    except PlaywrightTimeoutError:
        logger.debug("Page settle reached timeout limit, proceeding")
        return False
    except Exception as exc:
        logger.debug(f"settle_page notice: {exc}")
        return False

