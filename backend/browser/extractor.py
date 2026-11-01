"""
browser/extractor.py
---------------------
Core Information Extraction Engine for the AI Browser Automation Agent.

The ``InfoExtractor`` class is the single public entry-point for structured
data extraction. It:

  1. Auto-detects the page category using a scored heuristic system.
  2. Delegates to the appropriate category-specific extractor helper.
  3. Always extracts page-level metadata (Open Graph, meta tags, etc.).
  4. Returns a standardised ``ExtractionResult`` JSON-serialisable object.

Usage::

    from browser.extractor import InfoExtractor
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.goto("https://www.amazon.com/dp/B09G9FPHY6")

        extractor = InfoExtractor()
        result = await extractor.extract(page)
        print(result.model_dump_json(indent=2))
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from playwright.async_api import Page

from models.schemas import (
    ExtractionResult,
    PageCategory,
    PageMetadata,
)
from extraction.metadata import extract_metadata
from extraction.product  import extract_product
from extraction.news     import extract_news
from extraction.search   import extract_search_results
from extraction.contact  import extract_contact_info
from extraction.tables   import extract_tables
from extraction.images   import extract_images

logger = logging.getLogger("browser.extractor")


class InfoExtractor:
    """
    Reusable Information Extraction Engine backed by Playwright.

    Attributes:
        wait_for_idle: If True, waits for network to become idle before
                       extracting — helps with JS-heavy SPAs.
        idle_timeout:  Timeout in ms to wait for network idle.
    """

    def __init__(
        self,
        wait_for_idle: bool = False,
        idle_timeout:  int  = 5000,
    ) -> None:
        self.wait_for_idle = wait_for_idle
        self.idle_timeout  = idle_timeout

    # ------------------------------------------------------------------
    # Primary entry-point
    # ------------------------------------------------------------------

    async def extract(
        self,
        page: Page,
        category: Optional[PageCategory] = None,
    ) -> ExtractionResult:
        """
        Extract structured information from the active Playwright page.

        Args:
            page:     An active Playwright ``Page`` (already navigated).
            category: Force a specific category. ``None`` = auto-detect.

        Returns:
            A fully-populated ``ExtractionResult`` ready for serialisation.
        """
        errors: List[str] = []

        # Optional idle wait for SPAs / lazy-rendered content
        if self.wait_for_idle:
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=self.idle_timeout
                )
            except Exception:
                pass  # Timeout is acceptable — proceed with current DOM state

        # 1. Page metadata (always extracted)
        metadata: PageMetadata
        try:
            metadata = await extract_metadata(page)
        except Exception as exc:
            logger.error(f"Metadata extraction failed: {exc}")
            metadata = PageMetadata()
            errors.append(f"Metadata extraction failed: {exc}")

        page_url   = page.url
        page_title = metadata.page_title

        # 2. Category detection (or honour forced category)
        if category is None:
            try:
                category = await self.detect_category(page, metadata)
            except Exception as exc:
                logger.error(f"Category detection failed: {exc}")
                category = PageCategory.GENERIC
                errors.append(f"Category detection failed: {exc}")

        logger.info(f"Extracting [{category.value}] from {page_url}")

        # 3. Category-specific extraction
        data: Any = None
        try:
            data = await self._dispatch(page, category)
        except Exception as exc:
            logger.error(f"Category extraction failed [{category.value}]: {exc}")
            errors.append(f"Extraction failed for category '{category.value}': {exc}")

        return ExtractionResult(
            url          = page_url,
            page_title   = page_title,
            category     = category,
            extracted_at = datetime.now(timezone.utc).isoformat(),
            metadata     = metadata,
            data         = _serialise(data),
            errors       = errors,
        )

    # ------------------------------------------------------------------
    # Category dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, page: Page, category: PageCategory) -> Any:
        """Route to the correct extractor based on detected category."""
        if category == PageCategory.PRODUCT:
            return await extract_product(page)
        if category == PageCategory.NEWS:
            return await extract_news(page)
        if category == PageCategory.SEARCH_RESULTS:
            return await extract_search_results(page)
        if category == PageCategory.CONTACT:
            return await extract_contact_info(page)
        if category == PageCategory.TABLE:
            return await extract_tables(page)
        if category == PageCategory.IMAGE_GALLERY:
            return await extract_images(page)
        # GENERIC — return basic metadata only; no specialised data
        return {}

    # ------------------------------------------------------------------
    # Category Detection
    # ------------------------------------------------------------------

    async def detect_category(
        self,
        page: Page,
        metadata: Optional[PageMetadata] = None,
    ) -> PageCategory:
        """
        Automatically detect the page category using a multi-signal scoring system.

        Scoring rules:
          Each potential category accumulates points from URL patterns,
          JSON-LD types, meta tags, and DOM structure signals.
          The category with the highest score wins.
          Ties favour: product > news > search > contact > table > image_gallery.

        Args:
            page:     Active Playwright page.
            metadata: Pre-extracted PageMetadata (optional, avoids re-extraction).

        Returns:
            The detected ``PageCategory``.
        """
        url = page.url.lower()
        scores: Dict[str, int] = {c.value: 0 for c in PageCategory}

        # --- URL-based signals ---
        _url_signals(url, scores)

        # --- Meta tag signals ---
        if metadata:
            _meta_signals(metadata, scores)
        else:
            try:
                md = await extract_metadata(page)
                _meta_signals(md, scores)
            except Exception:
                pass

        # --- JSON-LD type signals ---
        try:
            jsonld_types = await _get_jsonld_types(page)
            _jsonld_signals(jsonld_types, scores)
        except Exception:
            pass

        # --- DOM structure signals ---
        try:
            dom_signals = await _get_dom_signals(page)
            _dom_score(dom_signals, scores)
        except Exception:
            pass

        logger.debug(f"Category scores for {page.url}: {scores}")

        # Select winner (exclude GENERIC from competition)
        candidates = {k: v for k, v in scores.items() if k != PageCategory.GENERIC.value}
        if not candidates or max(candidates.values()) == 0:
            return PageCategory.GENERIC

        winner = max(candidates, key=lambda k: candidates[k])
        return PageCategory(winner)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _url_signals(url: str, scores: Dict[str, int]) -> None:
    """Score based on URL path patterns."""
    product_patterns = [
        r"/dp/", r"/product/", r"/item/", r"/p/[\w-]+",
        r"/shop/", r"pdp", r"/sku/", r"/buy/",
    ]
    news_patterns = [
        r"/article/", r"/news/", r"/story/", r"/post/",
        r"/blog/", r"/press-release/", r"/opinion/",
    ]
    search_patterns = [
        r"[?&]q=", r"[?&]query=", r"[?&]search=", r"/search[?/]",
        r"[?&]s=", r"[?&]keyword",
    ]
    contact_patterns = [
        r"/contact", r"/about", r"/about-us", r"/get-in-touch",
        r"/reach-us", r"/support", r"/help",
    ]

    for pattern in product_patterns:
        if re.search(pattern, url):
            scores[PageCategory.PRODUCT.value] += 3

    for pattern in news_patterns:
        if re.search(pattern, url):
            scores[PageCategory.NEWS.value] += 3

    for pattern in search_patterns:
        if re.search(pattern, url):
            scores[PageCategory.SEARCH_RESULTS.value] += 3

    for pattern in contact_patterns:
        if re.search(pattern, url):
            scores[PageCategory.CONTACT.value] += 3


def _meta_signals(metadata: PageMetadata, scores: Dict[str, int]) -> None:
    """Score based on Open Graph og:type and meta keywords."""
    og_type = (metadata.og_type or "").lower()

    if "product" in og_type:
        scores[PageCategory.PRODUCT.value] += 3
    if "article" in og_type or "news" in og_type or "blog" in og_type:
        scores[PageCategory.NEWS.value] += 3

    # Keywords hint
    keywords_str = " ".join(metadata.keywords).lower()
    if any(k in keywords_str for k in ["price", "buy", "cart", "shop", "product"]):
        scores[PageCategory.PRODUCT.value] += 1
    if any(k in keywords_str for k in ["news", "article", "press", "editorial"]):
        scores[PageCategory.NEWS.value] += 1


def _jsonld_signals(jsonld_types: List[str], scores: Dict[str, int]) -> None:
    """Score based on JSON-LD @type values found on the page."""
    for t in jsonld_types:
        t_lower = t.lower()
        if "product" in t_lower:
            scores[PageCategory.PRODUCT.value] += 4
        if any(x in t_lower for x in ["article", "newsarticle", "blogposting", "report"]):
            scores[PageCategory.NEWS.value] += 4
        if any(x in t_lower for x in ["searchresultspage", "searchaction"]):
            scores[PageCategory.SEARCH_RESULTS.value] += 4
        if any(x in t_lower for x in ["localbusiness", "organization", "person", "contactpage"]):
            scores[PageCategory.CONTACT.value] += 4


def _dom_score(signals: Dict[str, Any], scores: Dict[str, int]) -> None:
    """Score based on DOM structure counts."""
    # Search result cards
    result_count = signals.get("result_cards", 0)
    if result_count >= 3:
        scores[PageCategory.SEARCH_RESULTS.value] += min(result_count, 10)

    # Product signals
    if signals.get("has_add_to_cart"):
        scores[PageCategory.PRODUCT.value] += 4
    if signals.get("has_price"):
        scores[PageCategory.PRODUCT.value] += 2

    # Article signals
    if signals.get("has_article_tag"):
        scores[PageCategory.NEWS.value] += 2
    if signals.get("has_time_datetime"):
        scores[PageCategory.NEWS.value] += 2
    if signals.get("has_byline"):
        scores[PageCategory.NEWS.value] += 1

    # Contact signals
    if signals.get("has_email_pattern"):
        scores[PageCategory.CONTACT.value] += 2
    if signals.get("has_phone_pattern"):
        scores[PageCategory.CONTACT.value] += 2

    # Table signals
    table_count = signals.get("tables_with_headers", 0)
    if table_count >= 1:
        scores[PageCategory.TABLE.value] += table_count * 2

    # Image gallery signals — raised threshold to avoid misclassifying image-heavy news homepages
    img_count = signals.get("large_image_count", 0)
    if img_count >= 20:
        score_val = 3
        # Suppress gallery score if strong article signals present
        if signals.get("has_article_tag") or signals.get("has_byline"):
            score_val = 1
        scores[PageCategory.IMAGE_GALLERY.value] += score_val

    # Boost news when article elements present (prevents image gallery from winning on homepages)
    if signals.get("has_article_tag") and signals.get("has_byline"):
        scores[PageCategory.NEWS.value] += 3


# ---------------------------------------------------------------------------
# Browser-side helpers
# ---------------------------------------------------------------------------

async def _get_jsonld_types(page: Page) -> List[str]:
    """Extract all JSON-LD @type values from the page."""
    raw: List[str] = await page.evaluate("""
        () => Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).map(s => s.textContent)
    """)
    types: List[str] = []
    for block in raw:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        for item in items:
            if isinstance(item, dict):
                t = item.get("@type", "")
                if isinstance(t, list):
                    types.extend(t)
                elif t:
                    types.append(t)
    return types


_DOM_SIGNALS_JS = """
() => {
    // Search result card selectors (cross-engine)
    const resultCards =
        document.querySelectorAll('.g, .b_algo, .result, [data-testid="result"]').length;

    // Product signals
    const hasAddToCart = !!document.querySelector(
        '[id*="add-to-cart"], [class*="add-to-cart"], [class*="addToCart"], button[data-action*="cart"]'
    );
    const priceEl = document.querySelector(
        '[class*="price"], [id*="price"], [itemprop="price"], .a-price'
    );
    const hasPrice = !!priceEl;

    // Article signals
    const hasArticleTag = !!document.querySelector('article, [role="article"]');
    const hasTimeDatetime = !!document.querySelector('time[datetime]');
    const hasbyline = !!document.querySelector(
        '.byline, .author, [class*="byline"], [rel="author"], [itemprop="author"]'
    );

    // Contact signals
    const bodyText = document.body ? document.body.innerText : '';
    const hasEmailPattern = /@[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}/.test(bodyText);
    const hasPhonePattern = /\\+?[\\d][\\s.\\-]?\\(?\\d{3}\\)?[\\s.\\-]?\\d{3}[\\s.\\-]?\\d{4}/.test(bodyText);

    // Table signals
    const tablesWithHeaders = Array.from(document.querySelectorAll('table')).filter(
        t => t.querySelector('th')
    ).length;

    // Image signals — count content images > 100px
    const largeImageCount = Array.from(document.querySelectorAll('img')).filter(
        img => (img.naturalWidth || img.width) > 100
    ).length;

    return {
        result_cards:       resultCards,
        has_add_to_cart:    hasAddToCart,
        has_price:          hasPrice,
        has_article_tag:    hasArticleTag,
        has_time_datetime:  hasTimeDatetime,
        has_byline:         hasbyline,
        has_email_pattern:  hasEmailPattern,
        has_phone_pattern:  hasPhonePattern,
        tables_with_headers: tablesWithHeaders,
        large_image_count:  largeImageCount,
    };
}
"""

async def _get_dom_signals(page: Page) -> Dict[str, Any]:
    return await page.evaluate(_DOM_SIGNALS_JS)


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _serialise(data: Any) -> Any:
    """Convert Pydantic models / lists thereof to plain dicts for JSON output."""
    if data is None or isinstance(data, (str, int, float, bool, dict)):
        return data
    if isinstance(data, list):
        return [_serialise(item) for item in data]
    if hasattr(data, "model_dump"):
        return data.model_dump()
    return data
