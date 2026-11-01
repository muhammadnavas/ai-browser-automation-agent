"""
browser/dom_utils.py
--------------------
Python loader and injector for browser/dom_utils.js.
"""

from pathlib import Path
from playwright.async_api import Page

_JS_FILE = Path(__file__).parent / "dom_utils.js"
_DOM_UTILS_SCRIPT = _JS_FILE.read_text(encoding="utf-8") if _JS_FILE.exists() else ""


async def ensure_dom_utils(page: Page) -> None:
    """Inject window.__ai_dom_utils into the target page if not already present."""
    is_present = await page.evaluate("() => typeof window.__ai_dom_utils !== 'undefined'")
    if not is_present and _DOM_UTILS_SCRIPT:
        await page.evaluate(_DOM_UTILS_SCRIPT)


def get_dom_utils_script() -> str:
    """Return raw Javascript string for script evaluation."""
    return _DOM_UTILS_SCRIPT
