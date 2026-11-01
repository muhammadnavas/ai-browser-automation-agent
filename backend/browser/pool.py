"""
browser/pool.py
----------------
Thread-safe Async Browser Context Pool for high-throughput production deployment.

Maintains a single warm Playwright Browser instance in memory and provisions
isolated BrowserContext and Page instances per API request or worker task.
Eliminates repeated OS process spawning overhead.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright, Playwright, Browser, BrowserContext, Page
from browser.stealth import apply_stealth_to_context, build_proxy_config

logger = logging.getLogger("browser.pool")


class BrowserPool:
    """
    Singleton warm Playwright Browser Pool.
    """

    _instance: Optional["BrowserPool"] = None
    _lock = asyncio.Lock()

    def __init__(
        self,
        browser_type: str = "chromium",
        headless: bool = True,
        proxy_server: Optional[str] = None,
        proxy_user: Optional[str] = None,
        proxy_pass: Optional[str] = None,
    ):
        self.browser_type = browser_type.lower()
        self.headless = headless
        self.proxy_server = proxy_server
        self.proxy_user = proxy_user
        self.proxy_pass = proxy_pass

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._is_started = False
        self._active_contexts: int = 0
        self._total_contexts_created: int = 0
        self.max_concurrent_contexts: int = max(1, int(os.getenv("BROWSER_POOL_MAX_CONTEXTS", "8")))
        self._context_semaphore = asyncio.Semaphore(self.max_concurrent_contexts)

    @classmethod
    async def get_instance(cls, **kwargs) -> "BrowserPool":
        """Get or initialize the global singleton BrowserPool instance."""
        async with cls._lock:
            if cls._instance is None:
                cls._instance = cls(**kwargs)
                await cls._instance.start()
            return cls._instance

    async def start(self) -> None:
        """Start warm browser process if not already running."""
        if self._is_started and self._browser and self._browser.is_connected():
            return

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        self._playwright = await async_playwright().start()

        engine = getattr(self._playwright, self.browser_type, self._playwright.chromium)
        args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
        ]
        if not self.headless:
            args.append("--start-maximized")
        else:
            args.append("--window-size=1280,800")

        launch_args: Dict[str, Any] = {
            "headless": self.headless,
            "args": args,
        }

        proxy_cfg = build_proxy_config(self.proxy_server, self.proxy_user, self.proxy_pass)
        if proxy_cfg:
            launch_args["proxy"] = proxy_cfg

        self._browser = await engine.launch(**launch_args)
        self._is_started = True
        logger.info(f"BrowserPool initialized warm browser [{self.browser_type}, headless={self.headless}]")

    async def stop(self) -> None:
        """Close browser process and cleanup Playwright instance."""
        async with self._lock:
            if self._browser:
                try:
                    await self._browser.close()
                except Exception:
                    pass
                self._browser = None
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
            self._is_started = False
            BrowserPool._instance = None
            logger.info("BrowserPool stopped")

    @asynccontextmanager
    async def acquire_context(
        self,
        viewport: Optional[Dict[str, int]] = None,
        user_agent: Optional[str] = None,
        storage_state: Optional[str] = None,
    ) -> AsyncGenerator[Tuple[BrowserContext, Page], None]:
        """Acquire a context while enforcing the configured concurrency ceiling."""
        async with self._context_semaphore:
            async with self._acquire_context_unlimited(viewport, user_agent, storage_state) as result:
                yield result

    @asynccontextmanager
    async def _acquire_context_unlimited(
        self,
        viewport: Optional[Dict[str, int]] = None,
        user_agent: Optional[str] = None,
        storage_state: Optional[str] = None,
    ) -> AsyncGenerator[Tuple[BrowserContext, Page], None]:
        """
        Acquire an isolated BrowserContext and Page from the warm Browser instance.
        Applies stealth evasion scripts automatically.
        """
        if not self._is_started or not self._browser or not self._browser.is_connected():
            self._is_started = False
            await self.start()

        opts: Dict[str, Any] = {
            "user_agent": user_agent or (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "accept_downloads": True,
        }
        if not self.headless and viewport is None:
            opts["no_viewport"] = True
        else:
            opts["viewport"] = viewport or {"width": 1280, "height": 800}
        if storage_state:
            opts["storage_state"] = storage_state

        try:
            context = await self._browser.new_context(**opts)
        except Exception:
            logger.warning("Warm browser channel disconnected, re-launching BrowserPool...")
            self._is_started = False
            self._browser = None
            await self.start()
            context = await self._browser.new_context(**opts)

        await apply_stealth_to_context(context)
        page = await context.new_page()
        self._active_contexts += 1
        self._total_contexts_created += 1

        try:
            yield context, page
        finally:
            self._active_contexts = max(0, self._active_contexts - 1)
            await context.close()

    def get_metrics(self) -> Dict[str, Any]:
        """Returns browser pool status, active contexts, and concurrency metrics."""
        return {
            "is_started": self._is_started,
            "browser_type": self.browser_type,
            "headless": self.headless,
            "active_contexts": self._active_contexts,
            "max_concurrent": getattr(self, "max_concurrent_contexts", 8),
            "total_contexts_created": self._total_contexts_created,
            "connected": bool(self._browser and self._browser.is_connected()),
        }


# Global pool accessor
async def get_browser_pool() -> BrowserPool:
    return await BrowserPool.get_instance()
