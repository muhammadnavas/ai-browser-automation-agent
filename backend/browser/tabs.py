"""
browser/tabs.py
---------------
Multi-tab and popup window management.
All methods operate on a Playwright `BrowserContext` or `Page` object.
"""

import logging
from typing import Callable, Coroutine, List, Optional, Any

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("browser.tabs")


class TabManager:
    """
    Manages multiple browser tabs/windows within a single BrowserContext.

    Usage::

        tm = TabManager(context)
        new_page = await tm.open_new_tab()
        await tm.switch_to(new_page)
        await tm.close_tab(new_page)
    """

    def __init__(self, context: BrowserContext):
        self.context = context
        # Playwright does not expose the OS/browser's true focused tab. Track
        # the most recently opened or explicitly focused page managed here.
        pages = list(context.pages)
        self._active_page: Optional[Page] = pages[-1] if pages else None
        self.context.on("page", self._on_page_created)
        for page in pages:
            page.on("close", lambda _=None, closed=page: self._on_page_closed(closed))

    def _on_page_created(self, page: Page) -> None:
        self._active_page = page
        page.on("close", lambda _=None, closed=page: self._on_page_closed(closed))

    def _on_page_closed(self, page: Page) -> None:
        if self._active_page is page:
            remaining = [candidate for candidate in self.context.pages if not candidate.is_closed()]
            self._active_page = remaining[-1] if remaining else None

    def _is_managed_page(self, page: Page) -> bool:
        return page in self.context.pages and not page.is_closed()

    # ------------------------------------------------------------------
    # Tab lifecycle
    # ------------------------------------------------------------------

    async def open_new_tab(self, url: Optional[str] = None) -> Page:
        """
        Open a new blank browser tab, optionally navigate to *url*.

        Args:
            url: URL to navigate to after opening. Defaults to about:blank.

        Returns:
            The newly opened Playwright Page.
        """
        page = await self.context.new_page()
        self._active_page = page
        if url:
            if not url.startswith("http://") and not url.startswith("https://"):
                url = "https://" + url
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            logger.debug(f"Opened new tab and navigated to '{url}'")
        else:
            logger.debug("Opened new blank tab")
        return page

    async def close_tab(self, page: Page) -> bool:
        """
        Close the given tab/page.

        Args:
            page: Playwright Page to close.

        Returns:
            True on success, False on error.
        """
        try:
            page_url = page.url
            await page.close()
            self._on_page_closed(page)
            logger.debug(f"Closed tab: {page_url}")
            return True
        except Exception as exc:
            logger.error(f"close_tab failed: {exc}")
            return False

    async def close_all_except(self, keep_page: Page) -> None:
        """
        Close every tab except *keep_page*.

        Args:
            keep_page: The Page that should remain open.
        """
        for pg in self.context.pages:
            if pg != keep_page:
                await self.close_tab(pg)

    # ------------------------------------------------------------------
    # Tab navigation
    # ------------------------------------------------------------------

    def list_tabs(self) -> List[Page]:
        """
        Return a list of all currently open pages/tabs in this context.

        Returns:
            List of Playwright Page objects.
        """
        return list(self.context.pages)

    def get_tab_count(self) -> int:
        """Return the number of open tabs."""
        return len(self.context.pages)

    def get_tab(self, index: int) -> Optional[Page]:
        """
        Return the tab at a zero-based *index*.

        Args:
            index: Zero-based tab index.

        Returns:
            Playwright Page or None if index is out of range.
        """
        pages = self.context.pages
        if 0 <= index < len(pages):
            return pages[index]
        logger.warning(f"get_tab: index {index} out of range ({len(pages)} tabs open)")
        return None

    async def switch_to(self, page: Page) -> None:
        """
        Bring the given tab to the front (focus it).

        Args:
            page: Playwright Page to focus.
        """
        if not self._is_managed_page(page):
            raise ValueError("Cannot switch to a page that is closed or belongs to another browser context.")
        await page.bring_to_front()
        self._active_page = page
        logger.debug(f"Switched to tab: {page.url}")

    async def switch_to_index(self, index: int) -> Optional[Page]:
        """
        Switch to a tab by zero-based *index* and bring it to the front.

        Args:
            index: Zero-based tab index.

        Returns:
            The switched-to Page, or None if index is invalid.
        """
        page = self.get_tab(index)
        if page:
            await self.switch_to(page)
        return page

    # ------------------------------------------------------------------
    # Popup / new window handling
    # ------------------------------------------------------------------

    async def wait_for_popup(
        self,
        trigger: Callable[[], Coroutine[Any, Any, None]],
        timeout: int = 10000,
    ) -> Optional[Page]:
        """
        Wait for a new popup/window to open after *trigger* is called.

        This is the correct approach for links that open target="_blank"
        or window.open() calls.

        Args:
            trigger: Async callable that causes a popup to open.
            timeout: Max wait in ms.

        Returns:
            The new popup Page, or None on timeout.

        Usage::

            popup = await tm.wait_for_popup(
                lambda: page.click("a[target='_blank']")
            )
        """
        try:
            async with self.context.expect_page(timeout=timeout) as page_info:
                await trigger()
            popup_page = await page_info.value
            self._active_page = popup_page
            popup_page.on("close", lambda _=None, closed=popup_page: self._on_page_closed(closed))
            await popup_page.wait_for_load_state("domcontentloaded", timeout=timeout)
            logger.debug(f"Popup opened: {popup_page.url}")
            return popup_page
        except Exception as exc:
            logger.error(f"wait_for_popup failed: {exc}")
            return None

    async def get_active_tab(self) -> Optional[Page]:
        """
        Return the most recently opened or explicitly focused managed tab.

        Playwright does not expose actual OS-level tab focus, so this tracks
        focus changes made through this manager and newly created popups/tabs.
        """
        if self._active_page and self._is_managed_page(self._active_page):
            return self._active_page
        pages = [page for page in self.context.pages if not page.is_closed()]
        self._active_page = pages[-1] if pages else None
        return self._active_page

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_tab_urls(self) -> List[str]:
        """
        Return the current URLs of all open tabs.

        Returns:
            List of URL strings.
        """
        return [pg.url for pg in self.context.pages]

    async def navigate_tab(
        self,
        page: Page,
        url: str,
        wait_until: str = "domcontentloaded",
        timeout: int = 30000,
    ) -> str:
        """
        Navigate a specific tab to *url*.

        Args:
            page: Playwright Page.
            url: Destination URL.
            wait_until: Load condition to wait for.
            timeout: Max wait in ms.

        Returns:
            The final URL after navigation.
        """
        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url
        try:
            await page.goto(url, wait_until=wait_until, timeout=timeout)
            logger.debug(f"Navigated tab to '{url}'")
        except Exception as exc:
            logger.warning(f"navigate_tab partial failure for '{url}': {exc}")
        return page.url
