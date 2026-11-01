"""
browser/stealth.py
-------------------
Stealth & Anti-Bot Evasion utilities for Playwright browser contexts.

Mocking navigator properties, WebGL fingerprinting, permissions, and vendor tags
to prevent automated bot detection on Cloudflare, Akamai, and Google SERPs.
"""

import logging
from typing import Dict, Optional
from playwright.async_api import BrowserContext

logger = logging.getLogger("browser.stealth")

# Stealth injection script evaluated on every new page creation
_STEALTH_JS = """
(() => {
    // 1. Override navigator.webdriver
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
    });

    // 2. Mock navigator.languages & plugins
    Object.defineProperty(navigator, 'languages', {
        get: () => ['en-US', 'en'],
    });

    Object.defineProperty(navigator, 'plugins', {
        get: () => [1, 2, 3, 4, 5],
    });

    // 3. Mock WebGL Vendor & Renderer
    try {
        const getParameter = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(parameter) {
            // UNMASKED_VENDOR_WEBGL
            if (parameter === 37445) return 'Google Inc. (NVIDIA)';
            // UNMASKED_RENDERER_WEBGL
            if (parameter === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Direct3D11 vs_5_0 ps_5_0)';
            return getParameter.apply(this, arguments);
        };
    } catch (e) {}

    // 4. Mock window.chrome runtime
    if (!window.chrome) {
        window.chrome = {
            runtime: {},
            loadTimes: function() {},
            csi: function() {},
            app: {},
        };
    }

    // 5. Mock Permissions API
    if (navigator.permissions && navigator.permissions.query) {
        const originalQuery = navigator.permissions.query;
        navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
})();
"""


async def apply_stealth_to_context(context: BrowserContext) -> None:
    """Inject stealth evasion script to run before any page script executes."""
    try:
        await context.add_init_script(_STEALTH_JS)
        logger.debug("Stealth scripts injected into BrowserContext")
    except Exception as exc:
        logger.warning(f"Failed to apply stealth evasion scripts: {exc}")


def build_proxy_config(
    server: Optional[str] = None,
    username: Optional[str] = None,
    password: Optional[str] = None,
    bypass: Optional[str] = None
) -> Optional[Dict[str, str]]:
    """Build proxy settings dictionary for Playwright context launch options."""
    if not server:
        return None
    
    cfg = {"server": server}
    if username:
        cfg["username"] = username
    if password:
        cfg["password"] = password
    if bypass:
        cfg["bypass"] = bypass
    return cfg
