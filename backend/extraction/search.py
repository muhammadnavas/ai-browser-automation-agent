"""
extraction/search.py
--------------------
Extracts structured search result listings from a Playwright Page.

Supported search engines:
  - Google (standard web results + AI overview detection)
  - Bing
  - DuckDuckGo
  - Yahoo Search
  - Generic fallback (ranked <h3>/<h2> + sibling anchor + snippet)

Returns a list of ``SearchResult`` objects ordered by position.
"""

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from playwright.async_api import Page
from models.schemas import SearchResult

logger = logging.getLogger("extraction.search")


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_search_results(page: Page) -> List[SearchResult]:
    """
    Extract an ordered list of search result items from the current page.

    Args:
        page: An active Playwright ``Page`` (already navigated to a SERP).

    Returns:
        List of ``SearchResult`` objects, one per result card.
    """
    url = page.url.lower()
    raw_results: List[Dict[str, Any]] = []

    try:
        if "google." in url:
            raw_results = await _extract_google(page)
        elif "bing.com" in url:
            raw_results = await _extract_bing(page)
        elif "duckduckgo.com" in url:
            raw_results = await _extract_duckduckgo(page)
        elif "yahoo.com/search" in url:
            raw_results = await _extract_yahoo(page)
        else:
            raw_results = await _extract_generic(page)

        # Fallback if engine-specific returned nothing
        if not raw_results:
            raw_results = await _extract_generic(page)
    except Exception as exc:
        logger.warning(f"Primary search extraction failed: {exc}, trying generic.")
        try:
            raw_results = await _extract_generic(page)
        except Exception as exc2:
            logger.error(f"Generic search extraction also failed: {exc2}")

    results: List[SearchResult] = []
    for i, item in enumerate(raw_results, start=1):
        item_url = item.get("url") or ""
        domain = _extract_domain(item_url)
        results.append(SearchResult(
            position    = i,
            title       = _clean(item.get("title")),
            description = _clean(item.get("description")),
            url         = item_url or None,
            thumbnail   = item.get("thumbnail") or None,
            domain      = domain or None,
        ))

    return results


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------

_GOOGLE_JS = """
() => {
    const results = [];

    // Standard web result cards — selector works for most Google layouts
    const cards = document.querySelectorAll('#search .g, #rso > div > .g, div[data-sokoban-container]');
    cards.forEach(card => {
        const linkEl   = card.querySelector('a[href]');
        const titleEl  = card.querySelector('h3');
        const snippetEl = card.querySelector('.VwiC3b, .s3v9rd, [data-snf]');
        const thumbEl  = card.querySelector('img');

        const url = linkEl ? linkEl.href : null;
        if (!url || url.startsWith('javascript') || url.includes('/search?')) return;

        results.push({
            url:         url,
            title:       titleEl   ? titleEl.innerText.trim()   : null,
            description: snippetEl ? snippetEl.innerText.trim() : null,
            thumbnail:   thumbEl   ? thumbEl.src                 : null,
        });
    });

    return results;
}
"""

async def _extract_google(page: Page) -> List[Dict]:
    return await page.evaluate(_GOOGLE_JS) or []


# ---------------------------------------------------------------------------
# Bing
# ---------------------------------------------------------------------------

_BING_JS = """
() => {
    const results = [];
    document.querySelectorAll('.b_algo').forEach(card => {
        const linkEl    = card.querySelector('h2 a, .b_title a');
        const snippetEl = card.querySelector('.b_caption p, .b_algoSlug');
        const thumbEl   = card.querySelector('img');

        results.push({
            url:         linkEl    ? linkEl.href              : null,
            title:       linkEl    ? linkEl.innerText.trim()  : null,
            description: snippetEl ? snippetEl.innerText.trim() : null,
            thumbnail:   thumbEl   ? thumbEl.src               : null,
        });
    });
    return results;
}
"""

async def _extract_bing(page: Page) -> List[Dict]:
    return await page.evaluate(_BING_JS) or []


# ---------------------------------------------------------------------------
# DuckDuckGo
# ---------------------------------------------------------------------------

_DDG_JS = """
() => {
    const results = [];
    document.querySelectorAll('[data-testid="result"], .result').forEach(card => {
        const linkEl    = card.querySelector('[data-testid="result-title-a"], .result__title a, a');
        const snippetEl = card.querySelector('[data-result="snippet"], .result__snippet');
        const thumbEl   = card.querySelector('img');

        results.push({
            url:         linkEl    ? linkEl.href                : null,
            title:       linkEl    ? linkEl.innerText.trim()    : null,
            description: snippetEl ? snippetEl.innerText.trim() : null,
            thumbnail:   thumbEl   ? thumbEl.src                : null,
        });
    });
    return results;
}
"""

async def _extract_duckduckgo(page: Page) -> List[Dict]:
    return await page.evaluate(_DDG_JS) or []


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------

_YAHOO_JS = """
() => {
    const results = [];
    document.querySelectorAll('#web .algo, .Sr').forEach(card => {
        const linkEl    = card.querySelector('h3 a, .title a');
        const snippetEl = card.querySelector('.compText p, .s-desc');

        results.push({
            url:         linkEl    ? linkEl.href                : null,
            title:       linkEl    ? linkEl.innerText.trim()    : null,
            description: snippetEl ? snippetEl.innerText.trim() : null,
            thumbnail:   null,
        });
    });
    return results;
}
"""

async def _extract_yahoo(page: Page) -> List[Dict]:
    return await page.evaluate(_YAHOO_JS) or []


# ---------------------------------------------------------------------------
# Generic fallback
# ---------------------------------------------------------------------------

_GENERIC_JS = """
() => {
    const results = [];
    const candidates = document.querySelectorAll('h3 a, h2 a');
    const seen = new Set();

    candidates.forEach(anchor => {
        const url = anchor.href;
        if (!url || url.startsWith('javascript') || seen.has(url)) return;
        seen.add(url);

        const title = anchor.innerText.trim();
        if (!title) return;

        // Look for a snippet in siblings or parent
        let snippet = null;
        const parent = anchor.closest('li, div, article, section');
        if (parent) {
            const pEl = parent.querySelector('p');
            snippet = pEl ? pEl.innerText.trim().slice(0, 300) : null;
        }

        results.push({ url, title, description: snippet, thumbnail: null });
    });

    return results.slice(0, 20);
}
"""

async def _extract_generic(page: Page) -> List[Dict]:
    return await page.evaluate(_GENERIC_JS) or []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_domain(url: str) -> Optional[str]:
    try:
        return urlparse(url).netloc.lstrip("www.")
    except Exception:
        return None


def _clean(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
