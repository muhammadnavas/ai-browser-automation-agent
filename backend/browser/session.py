"""
browser/session.py
------------------
Browser context / session management utilities.
Handles cookies, localStorage, viewport, geolocation, and HTTP headers.
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger("browser.session")


class SessionManager:
    """
    Manages browser session state — cookies, storage, viewport, and context options.

    Usage::

        sm = SessionManager()
        await sm.save_storage(context, "session.json")
        await sm.load_storage(context, "session.json")
    """

    # ------------------------------------------------------------------
    # Storage state (cookies + localStorage)
    # ------------------------------------------------------------------

    async def save_storage(
        self,
        context: BrowserContext,
        path: str,
    ) -> bool:
        """
        Save the full browser storage state (cookies + localStorage) to a JSON file.
        The saved file can be passed to `browser.new_context(storage_state=path)`
        on next launch to restore the session.

        Args:
            context: Playwright BrowserContext.
            path: File path to write (e.g. "session.json").

        Returns:
            True on success, False on failure.
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            await context.storage_state(path=path)
            logger.info(f"Storage state saved to '{path}'")
            return True
        except Exception as exc:
            logger.error(f"save_storage failed: {exc}")
            return False

    async def load_storage(
        self,
        context: BrowserContext,
        path: str,
    ) -> bool:
        """
        Load cookies from a JSON storage-state file into the current context.
        Note: localStorage can only be restored via a new context; this method
        restores the cookie portion into the running context.

        Args:
            context: Playwright BrowserContext.
            path: Path to the JSON storage-state file.

        Returns:
            True on success, False on failure.
        """
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            cookies = state.get("cookies", [])
            if cookies:
                await context.add_cookies(cookies)
            logger.info(f"Loaded {len(cookies)} cookies from '{path}'")
            return True
        except FileNotFoundError:
            logger.warning(f"load_storage: file not found '{path}'")
            return False
        except Exception as exc:
            logger.error(f"load_storage failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Cookies
    # ------------------------------------------------------------------

    async def get_cookies(
        self,
        context: BrowserContext,
        urls: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Return current cookies for the given context.

        Args:
            context: Playwright BrowserContext.
            urls: Optional list of URLs to filter cookies by origin.

        Returns:
            List of cookie dicts.
        """
        try:
            return await context.cookies(urls=urls)
        except Exception as exc:
            logger.error(f"get_cookies failed: {exc}")
            return []

    async def add_cookies(
        self,
        context: BrowserContext,
        cookies: List[Dict[str, Any]],
    ) -> bool:
        """
        Add (or replace) cookies in the current context.

        Args:
            context: Playwright BrowserContext.
            cookies: List of cookie dicts with at minimum 'name', 'value', 'url'.

        Returns:
            True on success, False on failure.
        """
        try:
            await context.add_cookies(cookies)
            logger.debug(f"Added {len(cookies)} cookies to context")
            return True
        except Exception as exc:
            logger.error(f"add_cookies failed: {exc}")
            return False

    async def clear_cookies(self, context: BrowserContext) -> bool:
        """
        Clear all cookies from the current context.

        Args:
            context: Playwright BrowserContext.

        Returns:
            True on success, False on failure.
        """
        try:
            await context.clear_cookies()
            logger.debug("Cleared all cookies from context")
            return True
        except Exception as exc:
            logger.error(f"clear_cookies failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Viewport
    # ------------------------------------------------------------------

    async def set_viewport(
        self,
        page: Page,
        width: int,
        height: int,
    ) -> bool:
        """
        Resize the browser viewport for a specific page.

        Args:
            page: Playwright Page.
            width: Viewport width in pixels.
            height: Viewport height in pixels.

        Returns:
            True on success, False on failure.
        """
        try:
            await page.set_viewport_size({"width": width, "height": height})
            logger.debug(f"Viewport set to {width}x{height}")
            return True
        except Exception as exc:
            logger.error(f"set_viewport failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Geolocation
    # ------------------------------------------------------------------

    async def set_geolocation(
        self,
        context: BrowserContext,
        latitude: float,
        longitude: float,
        accuracy: float = 10.0,
    ) -> bool:
        """
        Override the browser's reported geolocation.

        Args:
            context: Playwright BrowserContext.
            latitude: Decimal degrees latitude.
            longitude: Decimal degrees longitude.
            accuracy: Position accuracy in metres.

        Returns:
            True on success, False on failure.

        Note:
            The context must have been created with `geolocation` permissions
            granted, or you must call `context.grant_permissions(["geolocation"])`.
        """
        try:
            await context.set_geolocation(
                {"latitude": latitude, "longitude": longitude, "accuracy": accuracy}
            )
            logger.debug(f"Geolocation set to ({latitude}, {longitude})")
            return True
        except Exception as exc:
            logger.error(f"set_geolocation failed: {exc}")
            return False

    async def grant_geolocation(self, context: BrowserContext) -> bool:
        """
        Grant geolocation permissions to the context so set_geolocation works.

        Args:
            context: Playwright BrowserContext.

        Returns:
            True on success, False on failure.
        """
        try:
            await context.grant_permissions(["geolocation"])
            logger.debug("Geolocation permission granted")
            return True
        except Exception as exc:
            logger.error(f"grant_geolocation failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Extra HTTP Headers
    # ------------------------------------------------------------------

    async def set_extra_headers(
        self,
        context: BrowserContext,
        headers: Dict[str, str],
    ) -> bool:
        """
        Add extra HTTP request headers to every request made by this context.

        Args:
            context: Playwright BrowserContext.
            headers: Dict of header name → value.

        Returns:
            True on success, False on failure.

        Usage::

            await sm.set_extra_headers(context, {
                "Authorization": "Bearer my-token",
                "X-Custom-Header": "value"
            })
        """
        try:
            await context.set_extra_http_headers(headers)
            logger.debug(f"Set extra headers: {list(headers.keys())}")
            return True
        except Exception as exc:
            logger.error(f"set_extra_headers failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # LocalStorage helpers (run in-page)
    # ------------------------------------------------------------------

    async def set_local_storage(
        self,
        page: Page,
        key: str,
        value: str,
    ) -> bool:
        """
        Set a localStorage key/value on the current page.

        Args:
            page: Playwright Page.
            key: localStorage key.
            value: localStorage value (string).

        Returns:
            True on success, False on failure.
        """
        try:
            await page.evaluate(
                "([k, v]) => localStorage.setItem(k, v)", [key, value]
            )
            logger.debug(f"localStorage['{key}'] set")
            return True
        except Exception as exc:
            logger.error(f"set_local_storage failed: {exc}")
            return False

    async def get_local_storage(
        self,
        page: Page,
        key: str,
    ) -> Optional[str]:
        """
        Get a localStorage value from the current page.

        Args:
            page: Playwright Page.
            key: localStorage key.

        Returns:
            Value string or None if key does not exist.
        """
        try:
            return await page.evaluate(
                "(k) => localStorage.getItem(k)", key
            )
        except Exception as exc:
            logger.error(f"get_local_storage failed: {exc}")
            return None

    async def clear_local_storage(self, page: Page) -> bool:
        """
        Clear all localStorage for the current page's origin.

        Args:
            page: Playwright Page.

        Returns:
            True on success, False on failure.
        """
        try:
            await page.evaluate("() => localStorage.clear()")
            logger.debug("localStorage cleared")
            return True
        except Exception as exc:
            logger.error(f"clear_local_storage failed: {exc}")
            return False
