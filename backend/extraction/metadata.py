"""
extraction/metadata.py
-----------------------
Extracts standard page metadata from a Playwright Page object:
  - <title>, <meta name="description">, canonical URL
  - Open Graph (og:*) and Twitter Card (twitter:*) tags
  - Favicon URL
  - Page language (<html lang>)
  - Last-modified / article:published_time if available
  - <meta name="keywords">
"""

import logging
from typing import Optional, List
from playwright.async_api import Page
from models.schemas import PageMetadata

logger = logging.getLogger("extraction.metadata")


async def extract_metadata(page: Page) -> PageMetadata:
    """
    Scrape all standard metadata from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` instance (already navigated).

    Returns:
        A populated ``PageMetadata`` instance. Missing fields are ``None``.
    """
    try:
        result: dict = await page.evaluate(_JS_EXTRACT_METADATA)
    except Exception as exc:
        logger.warning(f"metadata JS evaluation failed: {exc}")
        result = {}

    # Normalise keywords — may be a comma-separated string
    raw_keywords: str = result.get("keywords", "") or ""
    keywords: List[str] = [
        k.strip() for k in raw_keywords.split(",") if k.strip()
    ]

    # Resolve relative favicon paths to absolute
    favicon = result.get("favicon")
    if favicon and not favicon.startswith("http"):
        try:
            base = page.url.split("/")
            base_url = "/".join(base[:3])  # scheme + host
            favicon = base_url + ("" if favicon.startswith("/") else "/") + favicon
        except Exception:
            pass

    return PageMetadata(
        page_title        = _clean(result.get("page_title")),
        meta_description  = _clean(result.get("meta_description")),
        canonical_url     = _clean(result.get("canonical_url")),
        og_title          = _clean(result.get("og_title")),
        og_description    = _clean(result.get("og_description")),
        og_image          = _clean(result.get("og_image")),
        og_url            = _clean(result.get("og_url")),
        og_type           = _clean(result.get("og_type")),
        twitter_title     = _clean(result.get("twitter_title")),
        twitter_description = _clean(result.get("twitter_description")),
        twitter_image     = _clean(result.get("twitter_image")),
        favicon           = _clean(favicon),
        language          = _clean(result.get("language")),
        last_updated      = _clean(result.get("last_updated")),
        keywords          = keywords,
    )


def _clean(value: Optional[str]) -> Optional[str]:
    """Strip whitespace and return None for empty strings."""
    if value is None:
        return None
    value = str(value).strip()
    return value if value else None


# ---------------------------------------------------------------------------
# JavaScript executed in the browser context
# ---------------------------------------------------------------------------

_JS_EXTRACT_METADATA = """
() => {
    const getMeta = (name) => {
        const el = document.querySelector(
            `meta[name="${name}"], meta[property="${name}"]`
        );
        return el ? (el.getAttribute('content') || '').trim() : null;
    };

    const getLink = (rel) => {
        const el = document.querySelector(`link[rel="${rel}"]`);
        return el ? (el.getAttribute('href') || '').trim() : null;
    };

    // Attempt to find the best favicon
    const faviconSelectors = [
        'link[rel="icon"]',
        'link[rel="shortcut icon"]',
        'link[rel="apple-touch-icon"]',
    ];
    let favicon = null;
    for (const sel of faviconSelectors) {
        const el = document.querySelector(sel);
        if (el && el.href) { favicon = el.href; break; }
    }

    // last_updated: try several sources
    const lastUpdated =
        getMeta('article:modified_time') ||
        getMeta('article:published_time') ||
        getMeta('date') ||
        getMeta('last-modified') ||
        (() => {
            const timeEl = document.querySelector('time[datetime]');
            return timeEl ? timeEl.getAttribute('datetime') : null;
        })();

    return {
        page_title:          document.title || null,
        meta_description:    getMeta('description'),
        canonical_url:       getLink('canonical'),
        og_title:            getMeta('og:title'),
        og_description:      getMeta('og:description'),
        og_image:            getMeta('og:image'),
        og_url:              getMeta('og:url'),
        og_type:             getMeta('og:type'),
        twitter_title:       getMeta('twitter:title'),
        twitter_description: getMeta('twitter:description'),
        twitter_image:       getMeta('twitter:image'),
        favicon:             favicon,
        language:            (document.documentElement.getAttribute('lang') || '').trim() || null,
        last_updated:        lastUpdated,
        keywords:            getMeta('keywords'),
    };
}
"""
