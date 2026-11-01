"""
extraction/images.py
--------------------
Extracts all meaningful images from a Playwright Page, including:
  - Standard <img src> and lazy-loaded <img data-src>
  - High-resolution URLs from srcset attributes
  - <picture><source> elements
  - <figure><figcaption> captions
  - Alt text and associated page URL
  - Natural width/height filtering (removes tracking pixels < 5×5)

Returns a list of ``ImageInfo`` objects.
"""

import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page
from models.schemas import ImageInfo

logger = logging.getLogger("extraction.images")

# Minimum image dimension threshold (pixels) — filters tracking pixels
MIN_DIMENSION = 5
# Minimum reasonable image size to include (heuristic)
MIN_CONTENT_SIZE = 50


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_images(page: Page) -> List[ImageInfo]:
    """
    Extract all meaningful images from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` (already navigated).

    Returns:
        List of ``ImageInfo`` objects, deduped by image URL.
    """
    try:
        raw_images: List[Dict[str, Any]] = await page.evaluate(_JS_EXTRACT_IMAGES)
    except Exception as exc:
        logger.error(f"Image extraction JS failed: {exc}")
        return []

    page_url = page.url
    seen_urls: set = set()
    results: List[ImageInfo] = []

    for raw in raw_images:
        # Resolve the best URL
        src = (
            raw.get("best_src")
            or raw.get("src")
            or raw.get("data_src")
            or ""
        ).strip()

        if not src or src.startswith("data:"):
            continue

        # Skip tracking pixels
        w = raw.get("width") or 0
        h = raw.get("height") or 0
        if (w > 0 and w < MIN_DIMENSION) or (h > 0 and h < MIN_DIMENSION):
            continue

        # Skip placeholder / spinner images
        src_lower = src.lower()
        if any(kw in src_lower for kw in ("spinner", "placeholder", "blank.gif", "pixel.gif", "1x1")):
            continue

        # Deduplicate
        if src in seen_urls:
            continue
        seen_urls.add(src)

        # Thumbnail — pick smaller of src/data-src; for now same as image
        thumbnail = raw.get("thumbnail") or src

        results.append(ImageInfo(
            image_url = src,
            thumbnail = thumbnail,
            alt_text  = _clean(raw.get("alt")),
            caption   = _clean(raw.get("caption")),
            page_url  = page_url,
            width     = int(w) if w else None,
            height    = int(h) if h else None,
        ))

    logger.debug(f"Extracted {len(results)} images from {page_url}")
    return results


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

_JS_EXTRACT_IMAGES = """
() => {
    const results = [];

    // Helper: pick best URL from srcset (highest width descriptor)
    const bestSrcSet = (srcset) => {
        if (!srcset) return null;
        const parts = srcset.split(',').map(s => s.trim().split(/\\s+/));
        let best = null, bestW = 0;
        parts.forEach(([url, descriptor]) => {
            if (!url) return;
            const w = descriptor ? parseInt(descriptor) : 0;
            if (w > bestW) { bestW = w; best = url; }
        });
        return best || (parts[0] ? parts[0][0] : null);
    };

    document.querySelectorAll('img').forEach(img => {
        const src      = img.getAttribute('src') || '';
        const dataSrc  = img.getAttribute('data-src')
                      || img.getAttribute('data-lazy-src')
                      || img.getAttribute('data-original')
                      || '';

        // srcset on <img> or on parent <picture><source>
        let srcsetUrl = null;
        const imgSrcset = img.getAttribute('srcset');
        if (imgSrcset) srcsetUrl = bestSrcSet(imgSrcset);

        if (!srcsetUrl) {
            const picture = img.closest('picture');
            if (picture) {
                const source = picture.querySelector('source[srcset]');
                if (source) srcsetUrl = bestSrcSet(source.getAttribute('srcset'));
            }
        }

        const best_src = srcsetUrl || dataSrc || src;

        // Caption from <figure><figcaption>
        const figure = img.closest('figure');
        const caption = figure
            ? (figure.querySelector('figcaption') || {}).innerText || null
            : null;

        // Dimensions
        const width  = img.naturalWidth  || img.width  || parseInt(img.getAttribute('width'))  || 0;
        const height = img.naturalHeight || img.height || parseInt(img.getAttribute('height')) || 0;

        results.push({
            src,
            data_src:  dataSrc || null,
            best_src:  best_src || null,
            thumbnail: src || null,
            alt:       img.getAttribute('alt') || null,
            caption:   caption ? caption.trim() : null,
            width,
            height,
        });
    });

    // Also collect background-image URLs from common image container elements
    const bgSelectors = '.hero-image, .banner-image, [class*="cover"], [class*="thumbnail"]';
    document.querySelectorAll(bgSelectors).forEach(el => {
        const style = window.getComputedStyle(el).backgroundImage;
        const match = style && style.match(/url\\(["']?([^"')]+)["']?\\)/);
        if (match && match[1]) {
            results.push({
                src:       match[1],
                data_src:  null,
                best_src:  match[1],
                thumbnail: match[1],
                alt:       el.getAttribute('aria-label') || null,
                caption:   null,
                width:     0,
                height:    0,
            });
        }
    });

    return results;
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
