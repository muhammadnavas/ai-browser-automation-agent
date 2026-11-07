"""
browser/controller.py
---------------------
Core Playwright browser lifecycle manager.

Handles:
  - Launching / stopping the browser (Chromium, Firefox, WebKit)
  - Headless vs. headed mode
  - Context creation (viewport, user-agent, downloads, geolocation)
  - Page navigation
  - DOM inspection & interactive element tagging
  - Screenshots
  - CAPTCHA / payment-wall detection
  - Delegates advanced behaviour to sub-modules:
      browser.actions   — form/input/scroll primitives
      browser.tabs      — multi-tab management (TabManager)
      browser.dialogs   — alert/confirm/prompt/upload handlers
      browser.waiter    — smart wait utilities
      browser.downloader — file download management (DownloadManager)
      browser.session   — cookies / localStorage / headers (SessionManager)
"""

import asyncio
import os
import json
import re
import hashlib
import logging
from typing import Dict, Any, Optional, Tuple

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from browser.tabs import TabManager
from browser.downloader import DownloadManager
from browser.session import SessionManager
from services.cache_service import CacheService, default_cache_service

logger = logging.getLogger("BrowserController")


def generate_dom_fingerprint(
    page_info: Dict[str, Any],
    cache_service: Optional[CacheService] = None,
) -> str:
    """
    Generate a deterministic SHA-256 fingerprint for page_info state.
    Matches the exact hashing algorithm of AIAgentPlanner._generate_state_fingerprint.
    """
    if not isinstance(page_info, dict):
        return ""

    raw_url = page_info.get("url", "")
    norm_url = (
        cache_service.normalize_url(raw_url)
        if cache_service
        else raw_url.strip().lower()
    )

    title = str(page_info.get("title", "")).strip().lower()

    headings = [
        str(h).strip().lower()
        for h in page_info.get("headings", [])
        if str(h).strip()
    ]

    sanitized_elements = []
    for el in page_info.get("elements", []):
        if not isinstance(el, dict):
            continue

        el_type = str(el.get("type", "")).strip().lower()
        el_name = str(el.get("name", "")).strip()
        el_id = str(el.get("id", "")).strip()

        is_sensitive = el_type == "password" or (
            cache_service is not None
            and (
                cache_service.is_sensitive_key(el_name)
                or cache_service.is_sensitive_key(el_id)
            )
        )

        if is_sensitive:
            sanitized_elements.append({
                "index": el.get("index"),
                "tag": str(el.get("tag", "")).strip().lower(),
                "type": el_type,
                "is_sensitive": True,
            })
            continue

        label_text = str(
            el.get("label") or el.get("text") or el.get("aria_label") or ""
        ).strip().lower()
        if cache_service and hasattr(cache_service, "sanitize_string"):
            sanitized_lbl = cache_service.sanitize_string(label_text)
            if isinstance(sanitized_lbl, str):
                label_text = sanitized_lbl

        placeholder_text = str(el.get("placeholder", "")).strip().lower()
        if cache_service and hasattr(cache_service, "sanitize_string"):
            sanitized_ph = cache_service.sanitize_string(placeholder_text)
            if isinstance(sanitized_ph, str):
                placeholder_text = sanitized_ph

        el_item = {
            "index": el.get("index"),
            "tag": str(el.get("tag", "")).strip().lower(),
            "type": el_type,
            "id": el_id,
            "name": el_name,
            "selector": str(el.get("selector", "")).strip(),
            "label": label_text,
            "placeholder": placeholder_text,
            "href": str(el.get("href", "")).strip(),
            "checked": bool(el.get("checked", False)),
            "disabled": bool(el.get("disabled", False)),
        }
        sanitized_elements.append(el_item)

    sanitized_elements.sort(key=lambda x: (x.get("index") is None, x.get("index") or 0))

    compact_forms = page_info.get("compact_forms") or []
    if isinstance(compact_forms, dict):
        compact_forms = [compact_forms]

    form_qs = [
        str(q).strip().lower()
        for q in page_info.get("form_questions", [])
        if str(q).strip()
    ]

    raw_text = str(page_info.get("page_text", "")).strip()[:1000]
    if cache_service and hasattr(cache_service, "sanitize_string"):
        sanitized_txt = cache_service.sanitize_string(raw_text)
        if isinstance(sanitized_txt, str):
            raw_text = sanitized_txt
    page_text = re.sub(r"\s+", " ", raw_text).lower()

    canonical_state = {
        "url": norm_url,
        "title": title,
        "headings": headings,
        "elements": sanitized_elements,
        "forms": compact_forms,
        "form_questions": form_qs,
        "text_sample": page_text,
    }

    payload = json.dumps(canonical_state, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class BrowserController:
    """
    Async Playwright browser lifecycle manager for the AI Browser Automation Agent.

    Provides:
      - Multi-browser launch (chromium / firefox / webkit)
      - Headless and headed modes
      - Context-level download acceptance and realistic user-agent
      - Navigation with auto-protocol prefix
      - DOM inspection with optional visual element-index badges
      - Screenshot capture
      - CAPTCHA / payment-wall detection
      - Sub-module accessors: .tabs, .downloads, .session

    Example::

        controller = BrowserController(headless=False, browser_type="chromium")
        await controller.start()
        await controller.navigate("https://example.com")
        filepath, url = await controller.capture_screenshot("task1", 1)
        await controller.stop()
    """

    def __init__(
        self,
        headless: bool = True,
        browser_type: str = "chromium",
        screenshots_dir: str = "screenshots",
        downloads_dir: str = "downloads",
        show_badges: bool = False,
        storage_state: Optional[str] = None,
        record_video_dir: Optional[str] = None,
        maximized: bool = False,
        cache_service: Optional[CacheService] = None,
        agent_version: str = "1.0.0",
    ):
        """
        Args:
            headless: Run browser without a visible window when True.
            browser_type: 'chromium' | 'firefox' | 'webkit'.
            screenshots_dir: Directory to save captured screenshots.
            downloads_dir: Directory to save downloaded files.
            show_badges: Overlay numbered badges on interactive elements.
            storage_state: Path to a saved storage-state JSON file for session restore.
            record_video_dir: If set, record video to this directory.
            maximized: Start browser window maximized in headed mode when True.
            cache_service: Optional CacheService instance for Redis DOM caching.
            agent_version: Version identifier for DOM cache keying.
        """
        self.headless = headless
        self.browser_type = browser_type.lower()
        self.screenshots_dir = screenshots_dir
        self.downloads_dir = downloads_dir
        self.show_badges = show_badges
        self.storage_state = storage_state
        self.record_video_dir = record_video_dir
        self.maximized = maximized
        self.cache_service = cache_service if cache_service is not None else default_cache_service
        self.agent_version = agent_version

        os.makedirs(self.screenshots_dir, exist_ok=True)
        os.makedirs(self.downloads_dir, exist_ok=True)

        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.current_page: Optional[Page] = None
        self._current_title_cache: str = ""
        self.element_map: Dict[int, Dict[str, Any]] = {}
        self._observed_dom_version: Optional[int] = None

        # Sub-module instances (initialised after context is created in start())
        self._tab_manager: Optional[TabManager] = None
        self._download_manager: Optional[DownloadManager] = None
        self._session_manager: Optional[SessionManager] = None

    # ------------------------------------------------------------------
    # Sub-module accessors
    # ------------------------------------------------------------------

    @property
    def tabs(self) -> TabManager:
        """Access the TabManager for multi-tab control."""
        if self._tab_manager is None:
            raise RuntimeError("Browser not started. Call await controller.start() first.")
        return self._tab_manager

    @property
    def downloads(self) -> DownloadManager:
        """Access the DownloadManager for file download handling."""
        if self._download_manager is None:
            raise RuntimeError("Browser not started. Call await controller.start() first.")
        return self._download_manager

    @property
    def session(self) -> SessionManager:
        """Access the SessionManager for cookies, storage, and headers."""
        if self._session_manager is None:
            raise RuntimeError("Browser not started. Call await controller.start() first.")
        return self._session_manager

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """
        Start Playwright, launch the browser, and create its context/page.
        Safe to call repeatedly after a successful start. A failed partial
        startup is rolled back so a later call can retry cleanly.
        """
        if self.playwright and self.browser and self.context and self.current_page:
            if not self.current_page.is_closed():
                return

        # Clear any partially initialized state left by an earlier attempt.
        await self.stop()

        try:
            self.playwright = await async_playwright().start()

            engine_map = {
                "chromium": self.playwright.chromium,
                "firefox": self.playwright.firefox,
                "webkit": self.playwright.webkit,
            }
            engine = engine_map.get(self.browser_type, self.playwright.chromium)

            launch_args: Dict[str, Any] = {"headless": self.headless}
            is_maximized = self.maximized or not self.headless
            if self.browser_type == "chromium":
                launch_args["args"] = [
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ]
                if is_maximized:
                    launch_args["args"].append("--start-maximized")
                else:
                    launch_args["args"].append("--window-size=1280,800")

            self.browser = await engine.launch(**launch_args)

            context_opts: Dict[str, Any] = {
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "accept_downloads": True,
            }
            if is_maximized:
                context_opts["no_viewport"] = True
            else:
                context_opts["viewport"] = {"width": 1280, "height": 800}

            if self.storage_state and os.path.isfile(self.storage_state):
                context_opts["storage_state"] = self.storage_state
            if self.record_video_dir:
                os.makedirs(self.record_video_dir, exist_ok=True)
                context_opts["record_video_dir"] = self.record_video_dir

            self.context = await self.browser.new_context(**context_opts)
            self.current_page = await self.context.new_page()
            try:
                from backend.browser.security import install_navigation_guard
                await install_navigation_guard(self.current_page)
            except Exception as _guard_err:
                logger.warning(f"Could not install navigation guard on initial page: {_guard_err}")

            self._tab_manager = TabManager(self.context)
            self._download_manager = DownloadManager(save_dir=self.downloads_dir)
            self._session_manager = SessionManager()
            self._current_title_cache = ""
            self.element_map = {}

            logger.info(
                f"BrowserController started [{self.browser_type}, "
                f"headless={self.headless}]"
            )
        except BaseException:
            # Includes cancellation: never leave a half-started browser behind.
            await self.stop()
            raise

    async def stop(self) -> None:
        """
        Close every initialized Playwright resource independently.
        Safe to call repeatedly, including after partial startup failures.
        """
        resources = (
            ("context", self.context, "close"),
            ("browser", self.browser, "close"),
            ("playwright", self.playwright, "stop"),
        )
        try:
            for label, resource, method_name in resources:
                if resource is None:
                    continue
                try:
                    await getattr(resource, method_name)()
                except Exception as exc:
                    logger.warning(f"Error closing browser {label}: {exc}")
        finally:
            self.context = None
            self.browser = None
            self.playwright = None
            self.current_page = None
            self._current_title_cache = ""
            self.element_map = {}
            self._tab_manager = None
            self._download_manager = None
            self._session_manager = None
            logger.info("BrowserController stopped")

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserController":
        await self.start()
        return self

    async def __aexit__(self, *_) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _invalidate_page_observation(self) -> None:
        """Discard page-derived mappings and metadata after a navigation."""
        self.element_map.clear()
        self._current_title_cache = ""
        self._observed_dom_version = None


    async def navigate(self, url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> str:
        """
        Navigate the current page to *url*.

        Automatically prepends 'https://' if no scheme is present.

        Args:
            url: Target URL.
            wait_until: Load condition — 'load' | 'domcontentloaded' | 'networkidle'.
            timeout: Max wait in ms.

        Returns:
            The final URL after navigation.
        """
        if not self.current_page:
            await self.start()

        if not url.startswith("http://") and not url.startswith("https://"):
            url = "https://" + url

        from backend.browser.security import validate_url_security
        safe, reason, normalized = validate_url_security(url, resolve_dns=True)
        if not safe:
            raise ValueError(f"Navigation blocked by security policy: {reason}")
        url = normalized or url

        self._invalidate_page_observation()
        try:
            await self.current_page.goto(url, wait_until=wait_until, timeout=timeout)
            await asyncio.sleep(1.2)   # Allow JS-driven animations to settle
            logger.debug(f"Navigated to: {self.current_page.url}")
        except Exception as exc:
            logger.warning(f"Navigation partial failure for '{url}': {exc}")

        return self.current_page.url if self.current_page else url

    async def reload(self) -> str:
        """Reload the current page and return the final URL."""
        if not self.current_page:
            return ""
        self._invalidate_page_observation()
        try:
            await self.current_page.reload(wait_until="domcontentloaded", timeout=30000)
            return self.current_page.url
        except Exception as exc:
            logger.error(f"reload failed: {exc}")
            return self.current_page.url

    async def go_back(self) -> str:
        """Navigate back one step in browser history."""
        if not self.current_page:
            return ""
        self._invalidate_page_observation()
        try:
            await self.current_page.go_back(wait_until="domcontentloaded", timeout=10000)
            return self.current_page.url
        except Exception as exc:
            logger.error(f"go_back failed: {exc}")
            return self.current_page.url

    async def go_forward(self) -> str:
        """Navigate forward one step in browser history."""
        if not self.current_page:
            return ""
        self._invalidate_page_observation()
        try:
            await self.current_page.go_forward(wait_until="domcontentloaded", timeout=10000)
            return self.current_page.url
        except Exception as exc:
            logger.error(f"go_forward failed: {exc}")
            return self.current_page.url

    @property
    def current_url(self) -> str:
        """Return the URL of the currently active page."""
        if self.current_page:
            return self.current_page.url
        return ""

    @property
    def current_title(self) -> str:
        """Return the last asynchronously observed title; it may be stale after navigation."""
        return self._current_title_cache

    async def get_title(self) -> str:
        """Return and cache the current page title."""
        if not self.current_page or self.current_page.is_closed():
            self._current_title_cache = ""
            return ""
        try:
            self._current_title_cache = await self.current_page.title()
        except Exception:
            self._current_title_cache = ""
        return self._current_title_cache

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def capture_screenshot(
        self,
        task_id: str,
        step_num: int,
        full_page: bool = False,
    ) -> Tuple[str, str]:
        """
        Capture a screenshot of the current viewport (or full page).

        Args:
            task_id: Task identifier used in the filename.
            step_num: Step number used in the filename.
            full_page: Capture the full scrollable page when True.

        Returns:
            Tuple of (absolute_filepath, relative_url_path).
        """
        if not self.current_page:
            return "", ""

        # Protect against capturing screenshots of local dashboard or backend administrative origins
        try:
            from urllib.parse import urlparse
            p_url = urlparse(self.current_page.url or "")
            h = (p_url.hostname or "").lower()
            p = p_url.port
            if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and (p in (8000, 5173, 3000, 8080)):
                logger.warning("Skipping screenshot: protected backend/dashboard origin.")
                return "", ""
        except Exception:
            pass

        filename = f"{task_id}_step_{step_num}.png"
        filepath = os.path.join(self.screenshots_dir, filename)

        try:
            mask_locators = []
            try:
                passwords = self.current_page.locator(
                    'input[type="password"], input[autocomplete*="password"], input[autocomplete*="one-time-code"], [data-sensitive="true"]'
                )
                if await passwords.count() > 0:
                    mask_locators.append(passwords)
            except Exception as mask_err:
                logger.warning(f"Safe masking cannot be guaranteed for screenshot: {mask_err}")
                return "", ""

            if mask_locators:
                await self.current_page.screenshot(path=filepath, full_page=full_page, mask=mask_locators)
            else:
                await self.current_page.screenshot(path=filepath, full_page=full_page)
            relative_url = f"/screenshots/{filename}"
            logger.debug(f"Screenshot saved: '{filepath}'")
            return filepath, relative_url
        except Exception as exc:
            logger.error(f"capture_screenshot failed: {exc}")
            return "", ""

    async def capture_element_screenshot(
        self,
        selector: str,
        task_id: str,
        step_num: int,
        suffix: str = "element",
    ) -> Tuple[str, str]:
        """
        Capture a screenshot of a specific element.

        Args:
            selector: CSS selector of the element.
            task_id: Task identifier used in the filename.
            step_num: Step number.
            suffix: Extra label appended to filename.

        Returns:
            Tuple of (absolute_filepath, relative_url_path).
        """
        if not self.current_page:
            return "", ""

        # Protect against capturing screenshots of local dashboard or backend administrative origins
        try:
            from urllib.parse import urlparse
            p_url = urlparse(self.current_page.url or "")
            h = (p_url.hostname or "").lower()
            p = p_url.port
            if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and (p in (8000, 5173, 3000, 8080)):
                logger.warning("Skipping element screenshot: protected backend/dashboard origin.")
                return "", ""
        except Exception:
            pass

        # Protect against capturing element screenshots of sensitive/secret inputs directly
        try:
            from backend.security import is_sensitive_key
            sel_lower = selector.lower()
            if any(term in sel_lower for term in ("password", "passwd", "pwd", "otp", "token", "cvv", "secret", "card", "ssn")) or is_sensitive_key(selector):
                logger.warning(f"Skipping element screenshot: target selector '{selector}' represents sensitive input.")
                return "", ""
        except Exception:
            return "", ""

        filename = f"{task_id}_step_{step_num}_{suffix}.png"
        filepath = os.path.join(self.screenshots_dir, filename)

        try:
            mask_locators = []
            try:
                passwords = self.current_page.locator(
                    'input[type="password"], input[autocomplete*="password"], input[autocomplete*="one-time-code"], [data-sensitive="true"]'
                )
                if await passwords.count() > 0:
                    mask_locators.append(passwords)
            except Exception as mask_err:
                logger.warning(f"Safe masking cannot be guaranteed for element screenshot: {mask_err}")
                return "", ""

            locator = self.current_page.locator(selector).first
            if mask_locators:
                await locator.screenshot(path=filepath, mask=mask_locators)
            else:
                await locator.screenshot(path=filepath)
            relative_url = f"/screenshots/{filename}"
            logger.debug(f"Element screenshot saved: '{filepath}'")
            return filepath, relative_url
        except Exception as exc:
            logger.error(f"capture_element_screenshot failed [{selector}]: {exc}")
            return "", ""

    # ------------------------------------------------------------------
    # DOM Inspection
    # ------------------------------------------------------------------

    async def parse_interactive_elements(self, use_cache: bool = True) -> Dict[str, Any]:
        """
        Scan the DOM for interactive elements (buttons, inputs, links, textareas,
        selects), build a lightweight representation for the LLM, and optionally
        draw numbered badge overlays on the page.

        Includes state-aware Redis caching for DOM analysis and element mappings.

        Returns:
            Dict with keys: 'title', 'url', 'elements' (list of element dicts).
        """
        if not self.current_page:
            return {"elements": [], "summary": "No page open"}

        # Prevent dashboard/backend-origin content from being exposed as webpage content
        curr_url = self.current_page.url if self.current_page else ""
        try:
            from urllib.parse import urlparse
            p_url = urlparse(curr_url)
            h = (p_url.hostname or "").lower()
            p = p_url.port
            if h in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and (p in (8000, 5173, 3000, 8080)):
                logger.warning(f"Protected internal dashboard/backend origin: '{curr_url}'. Refusing to inspect.")
                return {
                    "title": "Protected Origin",
                    "url": curr_url,
                    "elements": [],
                    "page_text": "",
                    "form_questions": [],
                    "headings": [],
                }
        except Exception:
            pass

        js_script = """
        (showBadges) => {
            // Track in-place SPA updates and DOM rerenders between observations.
            // Ignore our own badge overlays so instrumentation doesn't create noise.
            if (!window.__aiAgentDomObserver) {
                window.__aiAgentDomVersion = 0;
                window.__aiAgentLastDomMutation = Date.now();
                window.__aiAgentDomObserver = new MutationObserver(records => {
                    const relevant = records.some(record => {
                        const target = record.target instanceof Element
                            ? record.target : record.target.parentElement;
                        return !target?.closest('.ai-agent-badge');
                    });
                    if (relevant) {
                        window.__aiAgentDomVersion += 1;
                        window.__aiAgentLastDomMutation = Date.now();
                    }
                });
                window.__aiAgentDomObserver.observe(document.documentElement, {
                    subtree: true,
                    childList: true,
                    attributes: true,
                    characterData: true
                });
            }

            // Remove old overlays if any
            document.querySelectorAll('.ai-agent-badge').forEach(e => e.remove());

            const interactiveSelectors =
                'button, a, input, textarea, select, ' +
                '[role="button"], [role="link"], [tabindex]:not([tabindex="-1"])';
            const elements = Array.from(document.querySelectorAll(interactiveSelectors));

            const results = [];
            let index = 1;

            elements.forEach(el => {
                const rect = el.getBoundingClientRect();
                // Filter invisible elements
                if (rect.width === 0 || rect.height === 0 ||
                    rect.top < 0 || rect.top > window.innerHeight) {
                    return;
                }
                const cs = window.getComputedStyle(el);
                if (cs.visibility === 'hidden' ||
                    cs.display === 'none' ||
                    cs.opacity === '0') {
                    return;
                }

                const tag  = el.tagName.toLowerCase();
                const text = (el.innerText || el.textContent || el.value ||
                              el.placeholder || el.alt || el.ariaLabel || '')
                             .trim().replace(/\\s+/g, ' ');
                const type  = el.getAttribute('type') || tag;
                const elId  = el.id ? '#' + el.id : '';
                const rawName = el.getAttribute('name') || '';
                const name = rawName ? `[name="${CSS.escape(rawName)}"]` : '';

                // Prefer stable unique selectors; use an nth-of-type path when
                // several controls share the same tag, name, or label.
                const buildUniqueSelector = (node) => {
                    if (node.id) {
                        const byId = '#' + CSS.escape(node.id);
                        if (document.querySelectorAll(byId).length === 1) return byId;
                    }
                    const testIdValue = node.getAttribute('data-testid');
                    if (testIdValue) {
                        const byTestId = `[data-testid="${CSS.escape(testIdValue)}"]`;
                        if (document.querySelectorAll(byTestId).length === 1) return byTestId;
                    }
                    const parts = [];
                    let current = node;
                    while (current && current.nodeType === Node.ELEMENT_NODE &&
                           current !== document.documentElement) {
                        let part = current.tagName.toLowerCase();
                        if (current.id) {
                            parts.unshift('#' + CSS.escape(current.id));
                            break;
                        }
                        const parent = current.parentElement;
                        if (parent) {
                            const peers = Array.from(parent.children)
                                .filter(child => child.tagName === current.tagName);
                            if (peers.length > 1) {
                                part += `:nth-of-type(${peers.indexOf(current) + 1})`;
                            }
                        }
                        parts.unshift(part);
                        current = parent;
                    }
                    return parts.join(' > ');
                };
                const stableSelector = buildUniqueSelector(el);
                const ariaLabel = el.getAttribute('aria-label') || '';
                const role = el.getAttribute('role') || '';

                // Try to find a nearby label for better LLM context
                let labelText = '';
                if (el.id) {
                    const lbl = document.querySelector(`label[for="${el.id}"]`);
                    if (lbl) labelText = lbl.innerText?.trim() || '';
                }
                if (!labelText && el.getAttribute('aria-label')) {
                    labelText = el.getAttribute('aria-label');
                }
                if (!labelText && el.getAttribute('aria-labelledby')) {
                    const lblEl = document.getElementById(el.getAttribute('aria-labelledby'));
                    if (lblEl) labelText = lblEl.innerText?.trim() || '';
                }
                // Walk up to find a parent container with a heading/label
                if (!labelText) {
                    let parent = el.parentElement;
                    for (let i = 0; i < 5 && parent; i++) {
                        const heading = parent.querySelector('[role="heading"],[class*="Title"],[class*="label"]');
                        if (heading && heading !== el) {
                            labelText = heading.innerText?.trim() || '';
                            break;
                        }
                        parent = parent.parentElement;
                    }
                }

                if (showBadges) {
                    const badge = document.createElement('div');
                    badge.className = 'ai-agent-badge';
                    badge.innerText = index.toString();
                    Object.assign(badge.style, {
                        position: 'absolute',
                        left: `${window.scrollX + rect.left}px`,
                        top: `${window.scrollY + rect.top}px`,
                        backgroundColor: '#ff3366',
                        color: '#ffffff',
                        fontSize: '12px',
                        fontWeight: 'bold',
                        padding: '2px 6px',
                        borderRadius: '10px',
                        zIndex: '999999',
                        pointerEvents: 'none',
                        boxShadow: '0 2px 5px rgba(0,0,0,0.3)',
                    });
                    document.body.appendChild(badge);
                }

                results.push({
                    index: index,
                    tag: tag,
                    type: type,
                    text: text.substring(0, 80),
                    label: labelText.substring(0, 80),
                    id: elId,
                    name: rawName,
                    role: role,
                    aria_label: ariaLabel.substring(0, 120),
                    title: (el.getAttribute('title') || '').substring(0, 120),
                    selector: stableSelector,
                    selector_unique: stableSelector
                        ? document.querySelectorAll(stableSelector).length === 1 : false,
                    input_type: el.getAttribute('type') || '',
                    autocomplete: el.getAttribute('autocomplete') || '',
                    required: !!el.required,
                    multiple: !!el.multiple,
                    checked: el.checked !== undefined ? el.checked : null,
                    disabled: el.disabled || false,
                    placeholder: el.placeholder || '',
                    href: el.href || '',
                    value: (tag === 'input' && (el.type === 'password' || (el.getAttribute('autocomplete') || '').includes('password') || (el.name || '').toLowerCase().includes('password') || (el.name || '').toLowerCase().includes('otp')))
                           ? ''
                           : ((el.tagName === 'SELECT') ? el.options[el.selectedIndex]?.value : el.value || ''),
                });

                index++;
            });

            // ── Extra: collect visible page text for LLM context ──────────────
            const pageTextParts = [];
            const seen = new Set();
            document.querySelectorAll(
                'h1,h2,h3,h4,h5,h6,label,legend,p,' +
                '[role="heading"],[aria-label],[data-params]'
            ).forEach(el => {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return;
                const t = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                if (t && t.length > 2 && t.length < 300 && !seen.has(t)) {
                    seen.add(t);
                    pageTextParts.push(t);
                }
            });

            // ── Extra: Google Forms question containers ────────────────────────
            const formQuestions = [];
            document.querySelectorAll('[jsmodel],[data-params],[role="listitem"]').forEach(container => {
                const qEl = container.querySelector(
                    '[role="heading"],[class*="freebirdFormviewerViewItemsItemItemTitle"]'
                );
                if (qEl) {
                    const qText = qEl.innerText?.trim();
                    if (qText && !formQuestions.includes(qText)) formQuestions.push(qText);
                }
            });

            return {
                title: document.title,
                url: window.location.href,
                dom_version: window.__aiAgentDomVersion || 0,
                last_dom_mutation: window.__aiAgentLastDomMutation || Date.now(),
                document_ready_state: document.readyState,
                elements: results.slice(0, 60),
                page_text: pageTextParts.slice(0, 60).join(' | '),
                form_questions: formQuestions,
                headings: Array.from(document.querySelectorAll('h1,h2,h3,[role="heading"]'))
                    .map(h => h.innerText?.trim()).filter(Boolean).slice(0, 8),
            };
        }
        """
        try:
            data = await self.current_page.evaluate(js_script, self.show_badges)

            # Sanitize sensitive inputs, labels, placeholders, headings, questions, and page text
            try:
                from backend.security import redact_text, is_auth_field, redact_sensitive_data
                for item in data.get("elements", []):
                    if is_auth_field(item):
                        item["value"] = ""
                    elif item.get("value"):
                        item["value"] = redact_text(str(item["value"]), mask_tier3=True, preserve_public_contacts=True)
                    if item.get("placeholder"):
                        item["placeholder"] = redact_text(str(item["placeholder"]), mask_tier3=True, preserve_public_contacts=True)
                    if item.get("label"):
                        item["label"] = redact_text(str(item["label"]), mask_tier3=True, preserve_public_contacts=True)
                    if item.get("text"):
                        item["text"] = redact_text(str(item["text"]), mask_tier3=True, preserve_public_contacts=True)
                    if item.get("aria_label"):
                        item["aria_label"] = redact_text(str(item["aria_label"]), mask_tier3=True, preserve_public_contacts=True)
                    if item.get("title"):
                        item["title"] = redact_text(str(item["title"]), mask_tier3=True, preserve_public_contacts=True)

                if data.get("page_text"):
                    data["page_text"] = redact_text(data["page_text"], mask_tier3=True, preserve_public_contacts=True)
                if data.get("headings"):
                    data["headings"] = [redact_text(h, mask_tier3=True, preserve_public_contacts=True) for h in data["headings"]]
                if data.get("form_questions"):
                    data["form_questions"] = [redact_text(q, mask_tier3=True, preserve_public_contacts=True) for q in data["form_questions"]]
                if data.get("form_fields"):
                    data["form_fields"] = redact_sensitive_data(data["form_fields"], mask_tier3=True, preserve_contact_info=False)
            except Exception as sec_exc:
                logger.error(f"DOM sanitization failed, failing closed: {sec_exc}")
                return {
                    "title": "Error",
                    "url": self.current_page.url if self.current_page else "",
                    "elements": [],
                    "page_text": "",
                    "headings": [],
                    "form_questions": [],
                    "form_fields": [],
                }

            self.element_map = {item["index"]: item for item in data.get("elements", [])}
            version = data.get("dom_version")
            self._observed_dom_version = int(version) if version is not None else None
        except Exception as exc:
            logger.error(f"parse_interactive_elements error: {exc}")
            return {"title": "Error", "url": self.current_page.url if self.current_page else "", "elements": []}

        if not use_cache or self.cache_service is None:
            return data

        url = data.get("url", "")
        current_fingerprint = generate_dom_fingerprint(data, self.cache_service)
        redis_key: Optional[str] = None

        try:
            redis_key = self.cache_service.generate_key(
                cache_type="dom_analysis",
                agent_version=self.agent_version,
                task_or_goal="",
                url=url,
                dom_fingerprint=current_fingerprint,
            )

            def validate_dom_payload(cached_obj: Any) -> bool:
                if not isinstance(cached_obj, dict):
                    return False
                if "elements" not in cached_obj or not isinstance(cached_obj.get("elements"), list):
                    return False
                if cached_obj.get("state_fingerprint") != current_fingerprint:
                    logger.info("DOM analysis cache validation failed: state fingerprint mismatch")
                    return False
                cached_url = cached_obj.get("url", "")
                if self.cache_service and self.cache_service.normalize_url(cached_url) != self.cache_service.normalize_url(url):
                    return False
                if str(cached_obj.get("agent_version", "")).strip().lower() != self.agent_version.strip().lower():
                    return False
                return True

            cached_payload = await self.cache_service.get_validated(
                key=redis_key,
                validator=validate_dom_payload,
            )

            if cached_payload:
                logger.info(f"DOM analysis HIT for URL '{url}' (key='{redis_key}')")
                # Keep the element map generated from the live DOM above.
                # Cached selectors can be stale even when page text/fingerprint
                # validation succeeds (for example, after client-side rerenders).

                restored_data = {
                    "title": cached_payload.get("title", data.get("title", "")),
                    "url": cached_payload.get("url", url),
                    "elements": cached_payload.get("elements", []),
                    "page_text": cached_payload.get("page_text", ""),
                    "form_questions": cached_payload.get("form_questions", []),
                    "headings": cached_payload.get("headings", []),
                }
                if "form_fields" in cached_payload:
                    restored_data["form_fields"] = cached_payload["form_fields"]
                if "compact_forms" in cached_payload:
                    restored_data["compact_forms"] = cached_payload["compact_forms"]

                return restored_data
            else:
                logger.info(f"DOM analysis MISS for URL '{url}' (key='{redis_key}')")

        except Exception as exc:
            logger.warning(f"DOM analysis cache lookup failed (non-fatal, continuing): {exc}")

        # Store safe payload in Redis
        try:
            if redis_key:
                raw_elements = data.get("elements", [])
                safe_elements = []
                for el in raw_elements:
                    if isinstance(el, dict):
                        el_copy = dict(el)
                        if (
                            el_copy.get("type") == "password"
                            or self.cache_service.is_sensitive_key(str(el_copy.get("name", "")))
                            or self.cache_service.is_sensitive_key(str(el_copy.get("id", "")))
                            or self.cache_service.is_sensitive_key(str(el_copy.get("type", "")))
                        ):
                            el_copy["is_sensitive"] = True
                            if "value" in el_copy:
                                el_copy["value"] = "[REDACTED]"
                        safe_elements.append(el_copy)
                    else:
                        safe_elements.append(el)

                safe_element_map = {}
                for k, v in self.element_map.items():
                    if isinstance(v, dict):
                        v_copy = dict(v)
                        if (
                            v_copy.get("type") == "password"
                            or self.cache_service.is_sensitive_key(str(v_copy.get("name", "")))
                            or self.cache_service.is_sensitive_key(str(v_copy.get("id", "")))
                            or self.cache_service.is_sensitive_key(str(v_copy.get("type", "")))
                        ):
                            v_copy["is_sensitive"] = True
                            if "value" in v_copy:
                                v_copy["value"] = "[REDACTED]"
                        safe_element_map[k] = v_copy
                    else:
                        safe_element_map[k] = v

                cache_payload = {
                    "url": data.get("url", ""),
                    "title": data.get("title", ""),
                    "elements": safe_elements,
                    "page_text": data.get("page_text", ""),
                    "headings": data.get("headings", []),
                    "form_questions": data.get("form_questions", []),
                    "element_map": safe_element_map,
                    "state_fingerprint": current_fingerprint,
                    "agent_version": self.agent_version,
                }
                if "form_fields" in data:
                    cache_payload["form_fields"] = data["form_fields"]
                if "compact_forms" in data:
                    cache_payload["compact_forms"] = data["compact_forms"]

                sanitized_payload = self.cache_service.sanitize_sensitive_data(cache_payload)
                logger.info(f"DOM analysis SET for URL '{url}' (key='{redis_key}')")
                await self.cache_service.set(redis_key, sanitized_payload, ttl=300)
        except Exception as exc:
            logger.warning(f"DOM analysis cache store failed (non-fatal): {exc}")

        return data

    async def clear_badges(self) -> None:
        """Remove all visual element-index badge overlays from the page."""
        if not self.current_page:
            return
        try:
            await self.current_page.evaluate(
                "() => document.querySelectorAll('.ai-agent-badge').forEach(e => e.remove())"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Element Interactions
    # ------------------------------------------------------------------

    async def _refresh_if_dom_changed(self) -> bool:
        """Refresh the DOM snapshot when the page mutates after the last observation.

        Returns True when a refresh occurred. The caller must re-plan/retry because
        numeric element IDs may now refer to different controls.
        """
        if not self.current_page or self._observed_dom_version is None:
            return False
        try:
            current_version = await self.current_page.evaluate(
                "() => window.__aiAgentDomVersion ?? null"
            )
            if current_version is None:
                return False
            if int(current_version) == self._observed_dom_version:
                return False

            logger.info(
                "DOM changed since last observation (version %s -> %s); refreshing snapshot",
                self._observed_dom_version,
                current_version,
            )
            await self.parse_interactive_elements(use_cache=False)
            return True
        except Exception as exc:
            logger.warning("Could not verify DOM version before action: %s", exc)
            # Fail closed: do not act on a snapshot whose freshness is unknown.
            return True

    async def _resolve_live_element(
        self,
        element_id: Optional[int],
        selector: Optional[str],
        timeout: int,
    ):
        """Resolve an element and reject mappings that no longer match the live DOM."""
        expected = self.element_map.get(element_id) if element_id is not None else None
        target_selector = selector or (expected or {}).get("selector")
        if not self.current_page or not target_selector:
            return None

        try:
            dom_changed = await self._refresh_if_dom_changed()
            if dom_changed and expected is None:
                # A caller-supplied selector has no observed identity to compare.
                # Require the caller/planner to observe and decide again.
                return None

            locator = self.current_page.locator(target_selector)
            if dom_changed:
                # A DOM refresh may make a previously unique selector ambiguous.
                # Never silently choose the first matching duplicate.
                if await locator.count() != 1:
                    logger.warning("DOM changed and target selector is no longer unique: %s", target_selector)
                    return None
            locator = locator.first
            await locator.wait_for(state="attached", timeout=timeout)
            if not await locator.is_visible():
                raise ValueError("element is no longer visible")
            if not await locator.is_enabled():
                raise ValueError("element is disabled")

            if expected:
                identity = await locator.evaluate(
                    """el => ({
                        tag: el.tagName.toLowerCase(),
                        id: el.id ? '#' + el.id : '',
                        name: el.getAttribute('name') || ''
                    })"""
                )
                if identity["tag"] != expected.get("tag"):
                    raise ValueError("element tag changed")
                if expected.get("id") and identity["id"] != expected["id"]:
                    raise ValueError("element id changed")
                if expected.get("name") and identity["name"] != expected["name"]:
                    raise ValueError("element name changed")

            return locator
        except Exception as exc:
            if element_id is not None:
                self.element_map.pop(element_id, None)
            logger.warning(
                "Rejected stale/unavailable DOM element %s (%s): %s",
                element_id if element_id is not None else target_selector,
                target_selector,
                exc,
            )
            return None


    async def click_element(
        self,
        element_id: Optional[int] = None,
        selector: Optional[str] = None,
        timeout: int = 5000,
    ) -> bool:
        """
        Click an element by its numerical badge ID or CSS selector.

        Args:
            element_id: Index from the element_map (set by parse_interactive_elements).
            selector: CSS selector fallback.
            timeout: Max wait in ms.

        Returns:
            True on success, False on failure.
        """
        if not self.current_page:
            return False
        try:
            locator = await self._resolve_live_element(element_id, selector, timeout)
            if locator is None:
                return False

            await locator.click(timeout=timeout)
            logger.debug(f"Clicked element [{element_id or selector}]")
            return True
        except Exception as exc:
            logger.error(f"click_element failed: {exc}")
            return False

    async def type_text(
        self,
        text: str,
        element_id: Optional[int] = None,
        selector: Optional[str] = None,
        press_enter: bool = True,
        timeout: int = 5000,
    ) -> bool:
        """
        Clear and fill a text field with *text*, optionally pressing Enter after.

        Args:
            text: Text to type.
            element_id: Index from element_map.
            selector: CSS selector fallback.
            press_enter: Press Enter key after typing when True.
            timeout: Max wait in ms.

        Returns:
            True on success, False on failure.
        """
        if not self.current_page:
            return False
        try:
            locator = await self._resolve_live_element(element_id, selector, timeout)
            if locator is None:
                return False

            await locator.fill(text, timeout=timeout)

            if press_enter:
                await self.current_page.keyboard.press("Enter")
                await asyncio.sleep(1.2)

            logger.debug(f"Typed '{text[:40]}' into [{element_id or selector or 'resolved element'}]")
            return True
        except Exception as exc:
            logger.error(f"type_text failed: {exc}")
            return False

    async def scroll(
        self,
        direction: str = "down",
        amount: int = 500,
    ) -> bool:
        """
        Scroll the current page viewport.

        Args:
            direction: 'up' or 'down'.
            amount: Pixels to scroll.

        Returns:
            True on success, False on failure.
        """
        if not self.current_page:
            return False
        delta = amount if direction == "down" else -amount
        try:
            await self.current_page.evaluate(f"window.scrollBy(0, {delta});")
            await asyncio.sleep(0.4)
            logger.debug(f"Scrolled {direction} {amount}px")
            return True
        except Exception as exc:
            logger.error(f"scroll failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Data Extraction
    # ------------------------------------------------------------------

    async def extract_page_data(self) -> Dict[str, Any]:
        """
        Extract structured content from the current page — headings, paragraphs,
        tables, and links.

        Returns:
            Dict with keys: 'title', 'url', 'headings', 'paragraphs', 'tables', 'links'.
        """
        if not self.current_page:
            return {}

        js_extract = """
        () => {
            const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
                .map(h => h.innerText.trim()).filter(Boolean);

            const paragraphs = Array.from(document.querySelectorAll('p'))
                .map(p => p.innerText.trim())
                .filter(p => p.length > 20)
                .slice(0, 15);

            const tables = Array.from(document.querySelectorAll('table')).map(table => {
                const headers = Array.from(table.querySelectorAll('th'))
                    .map(th => th.innerText.trim());
                const rows = Array.from(table.querySelectorAll('tr')).map(tr => {
                    return Array.from(tr.querySelectorAll('td'))
                        .map(td => td.innerText.trim());
                }).filter(r => r.length > 0);
                return { headers, rows };
            });

            const links = Array.from(document.querySelectorAll('a[href]'))
                .map(a => ({ text: a.innerText.trim(), href: a.href }))
                .filter(l => l.text.length > 2)
                .slice(0, 20);

            const metaDesc = document.querySelector('meta[name="description"]');

            return {
                title: document.title,
                url: window.location.href,
                meta_description: metaDesc ? metaDesc.content : '',
                headings: headings.slice(0, 10),
                paragraphs,
                tables,
                links,
            };
        }
        """
        try:
            return await self.current_page.evaluate(js_extract)
        except Exception as exc:
            logger.error(f"extract_page_data failed: {exc}")
            return {"title": "Error", "content": ""}

    async def get_page_html(self) -> str:
        """Return the full outer HTML of the current page."""
        if not self.current_page:
            return ""
        try:
            return await self.current_page.content()
        except Exception as exc:
            logger.error(f"get_page_html failed: {exc}")
            return ""

    # ------------------------------------------------------------------
    # Security / Gate Detection
    # ------------------------------------------------------------------

    async def check_captcha_or_payment(self) -> Optional[str]:
        """
        Detect the presence of a CAPTCHA or payment wall that requires
        human intervention.

        Returns:
            'captcha' | 'payment' | None
        """
        if not self.current_page:
            return None

        js_detect = """
        () => {
            // 1. CAPTCHA / Bot Mitigation Detection
            const url = window.location.href.toLowerCase();
            const title = document.title.toLowerCase();
            if (url.includes('/sorry/') || title.includes('captcha') || title.includes('recaptcha') || title.includes('hcaptcha') || title.includes('attention required')) {
                return 'captcha';
            }
            
            // Check for visible captcha iframes
            const captchaIframes = document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="turnstile"], iframe[src*="challenge-platform"]');
            for (let iframe of captchaIframes) {
                const rect = iframe.getBoundingClientRect();
                if (rect.width > 5 && rect.height > 5) {
                    const style = window.getComputedStyle(iframe);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                        return 'captcha';
                    }
                }
            }

            // Check for visible captcha widgets
            const captchaWidgets = document.querySelectorAll('.g-recaptcha, .h-captcha, .cf-turnstile, #recaptcha, #h-captcha, #cf-turnstile');
            for (let widget of captchaWidgets) {
                const rect = widget.getBoundingClientRect();
                if (rect.width > 5 && rect.height > 5) {
                    const style = window.getComputedStyle(widget);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                        return 'captcha';
                    }
                }
            }

            // 2. Payment Wall / Credit Card Fields Detection
            if (document.querySelector('iframe[src*="stripe.com"], iframe[src*="paypal.com"]')) {
                return 'payment';
            }
            const ccInputs = document.querySelectorAll('input, select, textarea');
            for (let el of ccInputs) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    const style = window.getComputedStyle(el);
                    if (style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                        const id = (el.id || '').toLowerCase();
                        const name = (el.name || '').toLowerCase();
                        const placeholder = (el.placeholder || '').toLowerCase();
                        const autocomplete = (el.getAttribute('autocomplete') || '').toLowerCase();
                        
                        if (autocomplete.includes('cc-') || 
                            id.includes('cardnumber') || id.includes('card-number') || id.includes('ccnum') ||
                            name.includes('cardnumber') || name.includes('card-number') || name.includes('ccnum') ||
                            id === 'cvv' || id === 'cvc' || name === 'cvv' || name === 'cvc' || placeholder.includes('cvv') || placeholder.includes('cvc') ||
                            id.includes('cardexpiry') || name.includes('cardexpiry')) {
                            return 'payment';
                        }
                    }
                }
            }
            return null;
        }
        """
        try:
            return await self.current_page.evaluate(js_detect)
        except Exception:
            return None

    async def is_google_signin_page(self) -> bool:
        """
        Return True when the current page is a Google account sign-in / auth page.
        Checks both the URL and the page title so it works for accounts.google.com
        and short-link redirects (e.g. forms.gle).
        """
        if not self.current_page:
            return False
        url   = (self.current_page.url or "").lower()
        title = ""
        try:
            title = (await self.current_page.title()).lower()
        except Exception:
            pass
        return (
            "accounts.google.com" in url
            or "signin" in url
            or "sign in" in title
            or "sign in to google" in title
        )

    async def google_login(
        self,
        email: str,
        password: str,
        timeout: int = 15000,
    ) -> bool:
        """
        Attempt to sign in to Google using *email* and *password*.

        Returns True on success, False on failure.
        """
        if not email or not password:
            logger.warning("google_login: email and password must be supplied explicitly")
            return False

        if not self.current_page:
            return False

        try:
            page = self.current_page

            # --- Step 1: Fill in the email field and click Next ---
            email_sel = 'input[type="email"], input#identifierId, input[name="identifier"]'
            await page.wait_for_selector(email_sel, timeout=timeout)
            await page.locator(email_sel).first.fill(email)
            await page.keyboard.press("Enter")
            await asyncio.sleep(2.5)

            # --- Step 2: Fill in the password field and click Next ---
            pwd_sel = 'input[type="password"], input[name="password"]'
            await page.wait_for_selector(pwd_sel, timeout=timeout)
            await page.locator(pwd_sel).first.fill(password)
            await page.keyboard.press("Enter")
            await asyncio.sleep(3.5)


            # --- Step 3: Verify we left the sign-in page ---
            still_signin = await self.is_google_signin_page()
            if still_signin:
                logger.warning("google_login: still on sign-in page after login attempt — credentials may be wrong")
                return False

            logger.info(f"google_login: successfully signed in as {email}")
            return True

        except Exception as exc:
            logger.error(f"google_login failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Convenience wrappers for sub-modules (flat API surface)
    # ------------------------------------------------------------------

    async def select_option(
        self,
        selector: str,
        value: Optional[str] = None,
        label: Optional[str] = None,
        index: Optional[int] = None,
    ) -> bool:
        """Delegate to browser.actions.select_option."""
        from browser import actions
        return await actions.select_option(
            self.current_page, selector,
            value=value, label=label, index=index,
        )

    async def check_checkbox(self, selector: str, checked: bool = True) -> bool:
        """Delegate to browser.actions.check_checkbox."""
        from browser import actions
        return await actions.check_checkbox(self.current_page, selector, checked=checked)

    async def select_radio(self, name: str, value: str) -> bool:
        """Delegate to browser.actions.select_radio."""
        from browser import actions
        return await actions.select_radio(self.current_page, name, value)

    async def open_new_tab(self, url: Optional[str] = None) -> Page:
        """Open a new tab and optionally navigate to url. Updates current_page."""
        if url:
            from backend.browser.security import validate_url_security
            safe, reason, normalized = validate_url_security(url, resolve_dns=True)
            if not safe:
                raise ValueError(f"New tab navigation blocked by security policy: {reason}")
            url = normalized or url
        new_page = await self.tabs.open_new_tab(url)
        self.current_page = new_page
        try:
            from backend.browser.security import install_navigation_guard
            await install_navigation_guard(new_page)
        except Exception:
            pass
        return new_page

    async def switch_tab(self, index: int) -> Optional[Page]:
        """Switch the active tab by zero-based index. Updates current_page."""
        page = await self.tabs.switch_to_index(index)
        if page:
            self.current_page = page
        return page

    async def close_current_tab(self) -> None:
        """Close the current tab and switch to the last remaining tab."""
        if not self.current_page:
            return
        await self.tabs.close_tab(self.current_page)
        pages = self.tabs.list_tabs()
        self.current_page = pages[-1] if pages else None
