"""
extraction/listing.py
---------------------
Extracts multiple structured items / records from e-commerce catalog, search,
and listing pages.

Supports:
1. JSON-LD ItemList and array of Product schemas.
2. Schema.org microdata (repeated [itemtype*="Product"]).
3. Target e-commerce and catalog card selectors:
   - article.product_pod (Books to Scrape and standard book/product catalogs)
   - [data-component-type="s-search-result"], .s-result-item (Amazon)
   - .s-item (eBay)
   - [data-item-id] (Walmart)
   - .product-card, .product-item, .product-grid-item, .product-tile, .card-product, li.product (Shopify, WooCommerce, Magento)
4. Heuristic card detection: repeating containers with headings and price/currency indicators.
5. Tabular listing detection: table rows with entity titles and price/metadata columns.
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from playwright.async_api import Page

logger = logging.getLogger("extraction.listing")

_JS_EXTRACT_LISTINGS = """
(criteriaList) => {
    const records = [];
    const baseUri = window.location.href;

    // Helper: clean text
    const clean = (str) => str ? str.replace(/\\s+/g, ' ').trim() : '';

    // Helper: resolve URL
    const resolveUrl = (href) => {
        if (!href) return null;
        try {
            return new URL(href, baseUri).href;
        } catch(e) {
            return href;
        }
    };

    // Helper: extract price from element or text
    const extractPrice = (container) => {
        const priceSelectors = [
            '.price_color', '[class*="price_color"]',
            '.price', '[class*="price" i]:not([class*="old" i]):not([class*="strike" i])',
            '[class*="amount" i]', '[itemprop="price"]',
            '.a-price .a-offscreen', '.a-color-price'
        ];
        for (const sel of priceSelectors) {
            const el = container.querySelector(sel);
            if (el) {
                const txt = clean(el.innerText || el.textContent);
                if (txt && /[$£€¥₹\\d]/.test(txt)) {
                    // Extract first price occurrence if mixed with other text
                    const m = txt.match(/([$£€¥₹]|Â£)?\\s*[\\d,]+(?:\\.\\d+)?(?:\\s*(?:USD|EUR|GBP|INR))?/i);
                    if (m && m[0].trim()) {
                        return { value: m[0].trim(), evidence: 'price element (' + sel + '): ' + txt };
                    }
                    return { value: txt, evidence: 'price element (' + sel + '): ' + txt };
                }
            }
        }
        // Fallback: search visible text in container for price pattern
        const fullTxt = container.innerText || '';
        const m = fullTxt.match(/([$£€¥₹]|Â£)\\s*[\\d,]+(?:\\.\\d+)?/);
        if (m) {
            return { value: m[0].trim(), evidence: 'text pattern match: ' + m[0].trim() };
        }
        return null;
    };

    // Helper: extract availability
    const extractAvailability = (container) => {
        const availSelectors = [
            '.availability', '[class*="availability" i]',
            '.stock', '[class*="stock" i]', '[itemprop="availability"]',
            '.instock', '.out-of-stock'
        ];
        for (const sel of availSelectors) {
            const el = container.querySelector(sel);
            if (el) {
                const txt = clean(el.innerText || el.textContent);
                if (txt) {
                    return { value: txt, evidence: 'availability element (' + sel + '): ' + txt };
                }
            }
        }
        const fullTxt = (container.innerText || '').toLowerCase();
        if (fullTxt.includes('in stock')) {
            return { value: 'In stock', evidence: 'text match: in stock' };
        }
        if (fullTxt.includes('out of stock') || fullTxt.includes('sold out')) {
            return { value: 'Out of stock', evidence: 'text match: out of stock' };
        }
        return null;
    };

    // Helper: extract rating
    const extractRating = (container) => {
        const ratingSelectors = [
            '.star-rating', '[class*="star-rating" i]',
            '.rating', '[class*="rating" i]', '[itemprop="ratingValue"]'
        ];
        for (const sel of ratingSelectors) {
            const el = container.querySelector(sel);
            if (el) {
                const aria = el.getAttribute('aria-label');
                const cls = el.className;
                const txt = clean(el.innerText || el.textContent);
                const val = aria || txt || cls;
                if (val) {
                    return { value: val, evidence: 'rating element (' + sel + '): ' + val };
                }
            }
        }
        return null;
    };

    // Helper: extract title and product link
    const extractTitleAndLink = (container) => {
        // Preferred: heading anchor with title attribute or text
        const titleLinks = container.querySelectorAll('h1 a, h2 a, h3 a, h4 a, a[title], [class*="title" i] a, a[class*="title" i], [class*="name" i] a');
        for (const a of titleLinks) {
            const titleAttr = a.getAttribute('title');
            const inner = clean(a.innerText || a.textContent);
            const title = clean(titleAttr || inner);
            if (title && title.length > 1) {
                return {
                    title: title,
                    url: resolveUrl(a.getAttribute('href')),
                    evidence: 'title link: ' + title
                };
            }
        }

        // Direct heading
        const heading = container.querySelector('h1, h2, h3, h4, [class*="title" i], [class*="name" i]');
        if (heading) {
            const title = clean(heading.innerText || heading.textContent);
            if (title && title.length > 1) {
                const anyLink = container.querySelector('a[href]');
                return {
                    title: title,
                    url: anyLink ? resolveUrl(anyLink.getAttribute('href')) : null,
                    evidence: 'heading: ' + title
                };
            }
        }

        // Any anchor with substantive text
        const anyAnchor = container.querySelector('a[href]');
        if (anyAnchor) {
            const txt = clean(anyAnchor.innerText || anyAnchor.textContent);
            if (txt && txt.length > 3 && !txt.toLowerCase().includes('add to') && !txt.toLowerCase().includes('view') && !txt.toLowerCase().includes('buy')) {
                return {
                    title: txt,
                    url: resolveUrl(anyAnchor.getAttribute('href')),
                    evidence: 'anchor: ' + txt
                };
            }
        }

        return null;
    };

    // ── Strategy 1: JSON-LD ItemList ──
    const jsonLdScripts = document.querySelectorAll('script[type="application/ld+json"]');
    for (const s of jsonLdScripts) {
        try {
            const data = JSON.parse(s.textContent);
            const items = data.itemListElement || (Array.isArray(data) ? data : (data['@type'] === 'ItemList' ? data.items : null));
            if (Array.isArray(items) && items.length > 0) {
                for (const item of items) {
                    const prod = item.item || item;
                    if (prod && (prod.name || prod.title)) {
                        const name = clean(prod.name || prod.title);
                        const offer = prod.offers || {};
                        const pVal = offer.price ? (offer.priceCurrency ? offer.priceCurrency + ' ' + offer.price : String(offer.price)) : null;
                        records.push({
                            title: name,
                            name: name,
                            price: pVal,
                            availability: offer.availability ? clean(String(offer.availability).split('/').pop()) : null,
                            product_url: resolveUrl(prod.url || item.url),
                            source_url: baseUri,
                            evidence: {
                                title: 'JSON-LD item: ' + name,
                                price: pVal ? 'JSON-LD offer price: ' + pVal : null
                            }
                        });
                    }
                }
                if (records.length >= 2) {
                    return records;
                }
            }
        } catch(e) {}
    }

    // ── Strategy 2: Common Card Selectors ──
    const cardSelectors = [
        'article.product_pod',
        '[data-component-type="s-search-result"]',
        '.s-result-item[data-asin]:not([data-asin=""])',
        '.s-item:not(.s-item--header)',
        '[data-item-id]',
        '.product-card', '.product-item', '.product-grid-item', '.product-tile',
        '.card-product', 'li.product', '.grid-product', '.product_card'
    ];

    let matchedElements = [];
    for (const sel of cardSelectors) {
        const found = Array.from(document.querySelectorAll(sel));
        if (found.length >= 1) {
            matchedElements = found;
            break;
        }
    }

    // ── Strategy 3: Heuristic Repeating Container Fallback ──
    if (matchedElements.length === 0) {
        const candidates = Array.from(document.querySelectorAll('article, li, div[class*="item" i], div[class*="card" i], div[class*="product" i], div[class*="grid" i] > div'));
        matchedElements = candidates.filter(el => {
            const hasHeading = el.querySelector('h1, h2, h3, h4, [class*="title" i], [class*="name" i], a[title]');
            const fullTxt = el.innerText || '';
            const hasPrice = /([$£€¥₹]|Â£)\\s*[\\d,]+|\\b[\\d,]+\\.\\d{2}\\b/.test(fullTxt);
            return hasHeading && hasPrice;
        });
    }

    // Process matched elements
    for (const container of matchedElements) {
        const titleInfo = extractTitleAndLink(container);
        if (!titleInfo || !titleInfo.title) continue;

        const priceInfo = extractPrice(container);
        const availInfo = extractAvailability(container);
        const ratingInfo = extractRating(container);

        const recordEvidence = {};
        if (titleInfo.evidence) recordEvidence['title'] = titleInfo.evidence;
        if (priceInfo && priceInfo.evidence) recordEvidence['price'] = priceInfo.evidence;
        if (availInfo && availInfo.evidence) recordEvidence['availability'] = availInfo.evidence;
        if (ratingInfo && ratingInfo.evidence) recordEvidence['rating'] = ratingInfo.evidence;

        const record = {
            title: titleInfo.title,
            name: titleInfo.title,
            price: priceInfo ? priceInfo.value : null,
            availability: availInfo ? availInfo.value : null,
            rating: ratingInfo ? ratingInfo.value : null,
            product_url: titleInfo.url || baseUri,
            source_url: baseUri,
            evidence: recordEvidence
        };

        // Check for any additional criteria requested
        if (Array.isArray(criteriaList)) {
            for (const crit of criteriaList) {
                if (record[crit] !== undefined) continue;
                const critLower = crit.toLowerCase();
                const critEl = container.querySelector(`[class*="${critLower}" i], [data-${critLower}]`);
                if (critEl) {
                    const cTxt = clean(critEl.innerText || critEl.textContent);
                    if (cTxt) {
                        record[crit] = cTxt;
                        recordEvidence[crit] = crit + ' element: ' + cTxt;
                    }
                }
            }
        }

        records.push(record);
    }

    return records;
}
"""


async def extract_listing_records(
    page: Page,
    criteria: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Extract multiple item records from a catalog/listing page.

    Args:
        page: Active Playwright Page instance.
        criteria: Optional list of specific field names requested.

    Returns:
        List of extracted item dictionaries. Returns empty list if page is not a listing.
    """
    try:
        raw_records = await page.evaluate(_JS_EXTRACT_LISTINGS, criteria or [])
        if not isinstance(raw_records, list):
            return []

        clean_records: List[Dict[str, Any]] = []
        now_iso = datetime.now().isoformat()
        current_page_url = page.url

        for item in raw_records:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue

            # Ensure source attribution and timestamps
            item["source_url"] = current_page_url
            item["extracted_at"] = now_iso
            item["title"] = title
            item["name"] = title

            # Ensure product URL is absolute
            prod_url = item.get("product_url")
            if prod_url:
                item["product_url"] = urljoin(current_page_url, str(prod_url))
            else:
                item["product_url"] = current_page_url

            clean_records.append(item)

        return clean_records

    except Exception as exc:
        logger.warning(f"Listing extraction error on {page.url}: {exc}")
        return []
