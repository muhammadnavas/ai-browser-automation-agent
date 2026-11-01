"""
extraction/product.py
---------------------
Extracts structured product / e-commerce listing information from a Playwright
Page object.

Extraction strategy (in priority order):
  1. JSON-LD <script type="application/ld+json"> with @type "Product"
  2. Schema.org microdata  ([itemprop="name"], [itemprop="price"], …)
  3. Site-specific selectors  (Amazon, Flipkart, eBay, Walmart, Shopify)
  4. Generic heuristic fallback  (h1 for name, price patterns in visible text)

All strategies are tried and results are merged, with higher-priority sources
taking precedence for non-empty fields.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page
from models.schemas import ProductInfo

logger = logging.getLogger("extraction.product")

# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_product(page: Page) -> ProductInfo:
    """
    Extract product information from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` (already navigated).

    Returns:
        A ``ProductInfo`` instance with all available fields populated.
    """
    errors: List[str] = []
    merged: Dict[str, Any] = {}

    # --- Strategy 1: JSON-LD ---
    try:
        ld_data = await _extract_jsonld_product(page)
        _merge(merged, ld_data)
    except Exception as exc:
        errors.append(f"JSON-LD extraction failed: {exc}")
        logger.debug(f"JSON-LD error: {exc}")

    # --- Strategy 2: Schema.org microdata ---
    try:
        micro_data = await _extract_microdata(page)
        _merge(merged, micro_data, overwrite=False)
    except Exception as exc:
        errors.append(f"Microdata extraction failed: {exc}")
        logger.debug(f"Microdata error: {exc}")

    # --- Strategy 3: Site-specific selectors ---
    try:
        site_data = await _extract_site_specific(page)
        _merge(merged, site_data, overwrite=False)
    except Exception as exc:
        errors.append(f"Site-specific extraction failed: {exc}")
        logger.debug(f"Site-specific error: {exc}")

    # --- Strategy 4: Generic heuristic fallback ---
    try:
        generic_data = await _extract_generic(page)
        _merge(merged, generic_data, overwrite=False)
    except Exception as exc:
        errors.append(f"Generic extraction failed: {exc}")
        logger.debug(f"Generic error: {exc}")

    # --- Compute discount percentage if prices available ---
    current  = _parse_price(merged.get("current_price"))
    original = _parse_price(merged.get("original_price"))
    if current and original and original > current:
        merged["discount_percentage"] = round((original - current) / original * 100, 1)

    # --- Fill product_url from page URL if missing ---
    if not merged.get("product_url"):
        merged["product_url"] = page.url

    if errors:
        logger.debug(f"Product extraction errors (non-fatal): {errors}")

    return ProductInfo(**{k: v for k, v in merged.items() if v is not None and v != [] and v != {}})


# ---------------------------------------------------------------------------
# Strategy 1 — JSON-LD
# ---------------------------------------------------------------------------

async def _extract_jsonld_product(page: Page) -> Dict[str, Any]:
    """Parse <script type='application/ld+json'> blocks for Product schema."""
    raw_blocks: List[str] = await page.evaluate("""
        () => Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).map(s => s.textContent)
    """)

    for block in raw_blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue

        # Handle @graph arrays
        items = data if isinstance(data, list) else [data]
        if "@graph" in data:
            items = data["@graph"]

        for item in items:
            if not isinstance(item, dict):
                continue
            type_val = item.get("@type", "")
            if isinstance(type_val, list):
                type_val = " ".join(type_val)
            if "Product" not in type_val:
                continue

            # Offers block
            offers = item.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            # Aggregate rating
            agg = item.get("aggregateRating", {})
            rating      = _safe_float(agg.get("ratingValue"))
            review_count = _safe_int(agg.get("reviewCount") or agg.get("ratingCount"))

            # Images — can be str or list
            raw_images = item.get("image", [])
            if isinstance(raw_images, str):
                raw_images = [raw_images]
            elif isinstance(raw_images, dict):
                raw_images = [raw_images.get("url", "")]

            # Specifications from additionalProperty
            specs: Dict[str, str] = {}
            for prop in item.get("additionalProperty", []):
                if isinstance(prop, dict) and prop.get("name"):
                    specs[prop["name"]] = str(prop.get("value", ""))

            return {
                "product_name":  item.get("name"),
                "brand":         _brand_name(item.get("brand")),
                "description":   _clean_text(item.get("description")),
                "current_price": _price_str(offers.get("price"), offers.get("priceCurrency")),
                "original_price": None,
                "availability":  _availability(offers.get("availability", "")),
                "rating":        rating,
                "review_count":  review_count,
                "images":        [img for img in raw_images if img],
                "category":      item.get("category"),
                "sku":           item.get("sku"),
                "upc":           item.get("gtin13") or item.get("gtin"),
                "specifications": specs,
            }
    return {}


# ---------------------------------------------------------------------------
# Strategy 2 — Schema.org Microdata
# ---------------------------------------------------------------------------

_MICRODATA_JS = """
() => {
    const getItemprop = (scope, name) => {
        const el = scope.querySelector(`[itemprop="${name}"]`);
        if (!el) return null;
        return el.getAttribute('content') || el.getAttribute('href') || el.innerText || null;
    };
    const getAllItemprop = (scope, name) => {
        return Array.from(scope.querySelectorAll(`[itemprop="${name}"]`)).map(el =>
            el.getAttribute('src') || el.getAttribute('href') || el.getAttribute('content') || el.innerText || ''
        ).filter(Boolean);
    };

    const scope = document.querySelector('[itemtype*="Product"]') || document;
    if (!scope) return {};

    const offersEl = scope.querySelector('[itemtype*="Offer"]') || scope;
    const ratingEl = scope.querySelector('[itemtype*="AggregateRating"]') || scope;

    return {
        product_name:  getItemprop(scope, 'name'),
        brand:         getItemprop(scope, 'brand'),
        description:   getItemprop(scope, 'description'),
        current_price: getItemprop(offersEl, 'price'),
        currency:      getItemprop(offersEl, 'priceCurrency'),
        availability:  getItemprop(offersEl, 'availability'),
        rating:        getItemprop(ratingEl, 'ratingValue'),
        review_count:  getItemprop(ratingEl, 'reviewCount'),
        images:        getAllItemprop(scope, 'image'),
        sku:           getItemprop(scope, 'sku'),
    };
}
"""

async def _extract_microdata(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_MICRODATA_JS)
    if not raw or not any(raw.values()):
        return {}

    currency = raw.get("currency", "")
    price_raw = raw.get("current_price")
    current_price = _price_str(price_raw, currency) if price_raw else None

    return {
        "product_name":  _clean_text(raw.get("product_name")),
        "brand":         _clean_text(raw.get("brand")),
        "description":   _clean_text(raw.get("description")),
        "current_price": current_price,
        "availability":  _availability(raw.get("availability") or ""),
        "rating":        _safe_float(raw.get("rating")),
        "review_count":  _safe_int(raw.get("review_count")),
        "images":        raw.get("images", []),
        "sku":           raw.get("sku"),
    }


# ---------------------------------------------------------------------------
# Strategy 3 — Site-specific selectors
# ---------------------------------------------------------------------------

_SITE_SELECTORS_JS = """
() => {
    const text = sel => {
        const el = document.querySelector(sel);
        return el ? (el.innerText || el.textContent || '').trim() : null;
    };
    const attr = (sel, attribute) => {
        const el = document.querySelector(sel);
        return el ? el.getAttribute(attribute) : null;
    };
    const allAttr = (sel, attribute) =>
        Array.from(document.querySelectorAll(sel))
             .map(el => el.getAttribute(attribute))
             .filter(Boolean);

    const host = window.location.hostname;

    // Amazon
    if (host.includes('amazon')) {
        return {
            product_name:   text('#productTitle'),
            brand:          text('#bylineInfo') || text('.po-brand .a-span9'),
            current_price:  text('.a-price .a-offscreen') || text('#priceblock_ourprice') || text('#priceblock_dealprice'),
            original_price: text('.a-text-price .a-offscreen') || text('#priceblock_listprice'),
            availability:   text('#availability span'),
            rating:         attr('.a-icon-star .a-icon-alt', 'innerText') || text('.a-icon-star .a-icon-alt'),
            review_count:   text('#acrCustomerReviewText'),
            images:         allAttr('.imgTagWrapper img, #altImages img', 'src').filter(u => u && !u.includes('spinner')),
            description:    text('#productDescription p') || text('#feature-bullets .a-list-item'),
            category:       text('#wayfinding-breadcrumbs_container .a-list-item:last-child'),
        };
    }

    // eBay
    if (host.includes('ebay')) {
        return {
            product_name:   text('.x-item-title .ux-textspans') || text('#itemTitle'),
            current_price:  text('.x-price-primary .ux-textspans') || text('#prcIsum'),
            original_price: text('.x-price-was .ux-textspans'),
            availability:   text('.d-quantity__availability') || text('#qtySubTxt'),
            rating:         text('.ux-seller-section__item--stats'),
            images:         allAttr('.ux-image-carousel-item img', 'src'),
            description:    text('#viTabs_0_is'),
            category:       text('.breadcrumb li:last-child a'),
        };
    }

    // Flipkart
    if (host.includes('flipkart')) {
        return {
            product_name:   text('.B_NuCI') || text('h1.yhB1nd'),
            brand:          text('.G6XhRU') || text('._2erKSe'),
            current_price:  text('._30jeq3._16Jk6d') || text('._1vC4OE._3qQ9m1'),
            original_price: text('._3I9_wc._2p6lqe') || text('._27UcVY'),
            availability:   text('._16FRp0') || text('._1dVbu9'),
            rating:         text('._3LWZlK'),
            review_count:   text('._2_R_DZ'),
            images:         allAttr('._396cs4, ._2r_T1I', 'src'),
            description:    text('._1mXcCf.RmoJUa') || text('._1AN87F'),
        };
    }

    // Walmart
    if (host.includes('walmart')) {
        return {
            product_name:   text('[itemprop="name"]') || text('h1.prod-ProductTitle'),
            current_price:  text('[itemprop="price"]') || text('.price-characteristic'),
            availability:   text('[itemprop="availability"]') || text('.prod-ProductOffer-oosMsg'),
            rating:         text('.stars-reviews-count-node'),
            images:         allAttr('.prod-hero-image img', 'src'),
            description:    text('[itemprop="description"]') || text('.about-desc'),
        };
    }

    // Shopify generic detection
    if (document.querySelector('.product-single__title, .product__title')) {
        return {
            product_name:   text('.product-single__title') || text('.product__title'),
            current_price:  text('.product__price') || text('.price__regular'),
            original_price: text('.product__price--compare') || text('.price__sale'),
            availability:   text('.product__availability'),
            images:         allAttr('.product__media img, .product-single__photo', 'src'),
            description:    text('.product__description') || text('.product-single__description'),
        };
    }

    return {};
}
"""

async def _extract_site_specific(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_SITE_SELECTORS_JS)
    if not raw:
        return {}

    # Parse rating — often "4.5 out of 5 stars"
    rating_raw = raw.get("rating", "")
    rating = None
    if rating_raw:
        m = re.search(r"(\d+(\.\d+)?)", str(rating_raw))
        if m:
            rating = _safe_float(m.group(1))

    # Parse review_count — "1,234 ratings"
    review_raw = raw.get("review_count", "")
    review_count = None
    if review_raw:
        m = re.search(r"([\d,]+)", str(review_raw))
        if m:
            review_count = _safe_int(m.group(1).replace(",", ""))

    return {
        "product_name":   _clean_text(raw.get("product_name")),
        "brand":          _clean_text(raw.get("brand")),
        "description":    _clean_text(raw.get("description")),
        "current_price":  _clean_text(raw.get("current_price")),
        "original_price": _clean_text(raw.get("original_price")),
        "availability":   _clean_text(raw.get("availability")),
        "rating":         rating,
        "review_count":   review_count,
        "images":         [i for i in (raw.get("images") or []) if i],
        "category":       _clean_text(raw.get("category")),
    }


# ---------------------------------------------------------------------------
# Strategy 4 — Generic heuristic fallback
# ---------------------------------------------------------------------------

_GENERIC_JS = r"""
() => {
    const h1 = document.querySelector('h1');
    const name = h1 ? h1.innerText.trim() : null;

    // Price pattern scan in visible text
    const body = document.body.innerText;
    const priceMatch = body.match(/[$€£₹¥]\s?[\d,]+(\.\d{1,2})?/);
    const price = priceMatch ? priceMatch[0] : null;

    // All reasonably-sized images
    const images = Array.from(document.querySelectorAll('img'))
        .filter(img => img.naturalWidth > 100 || img.width > 100)
        .map(img => img.src)
        .filter(Boolean)
        .slice(0, 10);

    return { product_name: name, current_price: price, images };
}
"""

async def _extract_generic(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_GENERIC_JS)
    return {
        "product_name":  _clean_text(raw.get("product_name")),
        "current_price": _clean_text(raw.get("current_price")),
        "images":        raw.get("images", []),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merge(base: dict, update: dict, overwrite: bool = True) -> None:
    """Merge update into base, skipping None/empty values."""
    for key, val in update.items():
        if val is None or val == "" or val == []:
            continue
        if overwrite or key not in base or base[key] is None:
            base[key] = val


def _brand_name(brand_field: Any) -> Optional[str]:
    if isinstance(brand_field, dict):
        return brand_field.get("name")
    if isinstance(brand_field, str):
        return brand_field
    return None


def _price_str(price: Any, currency: Optional[str] = None) -> Optional[str]:
    if price is None:
        return None
    price_str = str(price).strip()
    if not price_str:
        return None
    if currency and not any(c in price_str for c in "$€£₹¥"):
        return f"{currency} {price_str}"
    return price_str


def _availability(raw: str) -> Optional[str]:
    if not raw:
        return None
    raw = raw.lower()
    if "instock" in raw or "in stock" in raw or "in_stock" in raw:
        return "In Stock"
    if "outofstock" in raw or "out of stock" in raw:
        return "Out of Stock"
    if "preorder" in raw or "pre-order" in raw:
        return "Pre-Order"
    if "discontinued" in raw:
        return "Discontinued"
    return raw.strip().title() or None


def _safe_float(val: Any) -> Optional[float]:
    try:
        return float(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> Optional[int]:
    try:
        return int(str(val).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _parse_price(price_str: Optional[str]) -> Optional[float]:
    if not price_str:
        return None
    m = re.search(r"[\d,]+(\.\d{1,2})?", str(price_str).replace(",", ""))
    if m:
        return _safe_float(m.group(0))
    return None


def _clean_text(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
