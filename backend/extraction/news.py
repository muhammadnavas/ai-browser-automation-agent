"""
extraction/news.py
------------------
Extracts structured news / editorial article information from a Playwright Page.

Extraction strategy (in priority order):
  1. JSON-LD <script type="application/ld+json"> with @type Article/NewsArticle
  2. Schema.org microdata  ([itemprop="headline"], [itemprop="author"], …)
  3. HTML5 semantic fallback  (<article>, <time>, byline patterns)
  4. Open Graph fallback      (og:title, og:description, og:image)
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page
from models.schemas import NewsArticle

logger = logging.getLogger("extraction.news")

# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_news(page: Page) -> NewsArticle:
    """
    Extract article information from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` (already navigated).

    Returns:
        A ``NewsArticle`` instance with all available fields populated.
    """
    merged: Dict[str, Any] = {}
    errors: List[str] = []

    # Strategy 1: JSON-LD
    try:
        _merge(merged, await _extract_jsonld_article(page))
    except Exception as exc:
        errors.append(f"JSON-LD: {exc}")

    # Strategy 2: Microdata
    try:
        _merge(merged, await _extract_microdata(page), overwrite=False)
    except Exception as exc:
        errors.append(f"Microdata: {exc}")

    # Strategy 3: HTML semantic
    try:
        _merge(merged, await _extract_semantic(page), overwrite=False)
    except Exception as exc:
        errors.append(f"Semantic: {exc}")

    # Strategy 4: Open Graph fallback
    try:
        _merge(merged, await _extract_og_fallback(page), overwrite=False)
    except Exception as exc:
        errors.append(f"OG fallback: {exc}")

    if not merged.get("article_url"):
        merged["article_url"] = page.url

    if not merged.get("news_source"):
        try:
            from urllib.parse import urlparse
            merged["news_source"] = urlparse(page.url).netloc
        except Exception:
            pass

    if errors:
        logger.debug(f"News extraction non-fatal errors: {errors}")

    return NewsArticle(**{k: v for k, v in merged.items() if v is not None and v != []})


# ---------------------------------------------------------------------------
# Strategy 1 — JSON-LD
# ---------------------------------------------------------------------------

async def _extract_jsonld_article(page: Page) -> Dict[str, Any]:
    raw_blocks: List[str] = await page.evaluate("""
        () => Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).map(s => s.textContent)
    """)

    article_types = {"Article", "NewsArticle", "BlogPosting", "Report", "SocialMediaPosting"}

    for block in raw_blocks:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue

        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]

        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            if isinstance(item_type, list):
                item_type = " ".join(item_type)
            if not any(t in item_type for t in article_types):
                continue

            # Author — may be Person dict or string
            author_raw = item.get("author", "")
            if isinstance(author_raw, list):
                author_raw = author_raw[0] if author_raw else ""
            author = author_raw.get("name") if isinstance(author_raw, dict) else str(author_raw)

            # Publisher
            pub_raw = item.get("publisher", {})
            publisher = pub_raw.get("name") if isinstance(pub_raw, dict) else str(pub_raw or "")

            # Keywords
            kw_raw = item.get("keywords", "")
            if isinstance(kw_raw, list):
                tags = [str(k).strip() for k in kw_raw if k]
            elif isinstance(kw_raw, str):
                tags = [k.strip() for k in kw_raw.split(",") if k.strip()]
            else:
                tags = []

            # Featured image
            image_raw = item.get("image", {})
            if isinstance(image_raw, dict):
                featured_image = image_raw.get("url") or image_raw.get("@id")
            elif isinstance(image_raw, list):
                first = image_raw[0] if image_raw else {}
                featured_image = first.get("url") if isinstance(first, dict) else str(first)
            else:
                featured_image = str(image_raw) if image_raw else None

            # Article body — truncate at 5000 chars for sanity
            body = _clean_text(item.get("articleBody", ""))
            if body and len(body) > 5000:
                body = body[:5000] + "…"

            return {
                "headline":         _clean_text(item.get("headline") or item.get("name")),
                "summary":          _clean_text(item.get("description") or item.get("abstract")),
                "article_content":  body,
                "author":           _clean_text(author),
                "publication_date": item.get("datePublished") or item.get("dateCreated"),
                "news_source":      publisher or None,
                "tags":             tags,
                "featured_image":   featured_image,
                "article_url":      item.get("url") or item.get("mainEntityOfPage", {}).get("@id"),
                "section":          item.get("articleSection"),
            }
    return {}


# ---------------------------------------------------------------------------
# Strategy 2 — Schema.org Microdata
# ---------------------------------------------------------------------------

_MICRODATA_JS = """
() => {
    const scope = document.querySelector('[itemtype*="Article"], [itemtype*="BlogPosting"]') || document;
    const get = name => {
        const el = scope.querySelector(`[itemprop="${name}"]`);
        return el ? (el.getAttribute('content') || el.getAttribute('datetime') || el.innerText || '').trim() : null;
    };
    const getAll = name =>
        Array.from(scope.querySelectorAll(`[itemprop="${name}"]`))
            .map(el => (el.getAttribute('content') || el.innerText || '').trim())
            .filter(Boolean);

    return {
        headline:         get('headline') || get('name'),
        summary:          get('description') || get('abstract'),
        article_content:  get('articleBody'),
        author:           get('author'),
        publication_date: get('datePublished') || get('dateCreated'),
        featured_image:   get('image'),
        tags:             getAll('keywords'),
        section:          get('articleSection'),
    };
}
"""

async def _extract_microdata(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_MICRODATA_JS)
    if not raw or not any(raw.values()):
        return {}
    return {k: _clean_text(v) if isinstance(v, str) else v for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Strategy 3 — HTML5 Semantic
# ---------------------------------------------------------------------------

_SEMANTIC_JS = r"""
() => {
    // Headline
    const headline =
        (document.querySelector('h1.article-title, h1.entry-title, h1.post-title, article h1, h1') || {}).innerText;

    // Article body — prefer <article>, then .article-body, then <main>
    const articleEl =
        document.querySelector('article .article-body, .article-body, .entry-content, .post-content, article, main');
    let articleContent = null;
    if (articleEl) {
        // Remove scripts, styles, nav, header, footer, ads
        const clone = articleEl.cloneNode(true);
        ['script','style','nav','header','footer','aside','.ad','[class*="ad-"]','[id*="ad-"]'].forEach(sel => {
            clone.querySelectorAll(sel).forEach(e => e.remove());
        });
        articleContent = clone.innerText.replace(/\\n{3,}/g, '\\n\\n').trim();
        if (articleContent.length > 5000) articleContent = articleContent.slice(0, 5000) + '…';
    }

    // Author — byline patterns
    const authorEl = document.querySelector(
        '[rel="author"], .author-name, .byline-name, .article-author, .byline, [itemprop="author"]'
    );
    const author = authorEl ? authorEl.innerText.replace(/^(by|written by)\s+/i, '').trim() : null;

    // Date
    const timeEl = document.querySelector('time[datetime], .published-date, .article-date, .entry-date, time');
    const dateStr = timeEl
        ? (timeEl.getAttribute('datetime') || timeEl.innerText || '').trim()
        : null;

    // Tags
    const tagEls = document.querySelectorAll(
        '[rel="tag"], .tag a, .tags a, .article-tags a, .post-tags a, .category a'
    );
    const tags = Array.from(tagEls).map(el => el.innerText.trim()).filter(Boolean);

    // Featured image
    const imgEl = document.querySelector(
        '.featured-image img, .article-featured-image img, .post-thumbnail img, article img'
    );
    const featuredImage = imgEl ? (imgEl.src || imgEl.getAttribute('data-src')) : null;

    // Summary — first <p> inside article
    const firstP = articleEl ? articleEl.querySelector('p') : null;
    const summary = firstP ? firstP.innerText.trim().slice(0, 300) : null;

    return { headline, article_content: articleContent, author, publication_date: dateStr,
             tags, featured_image: featuredImage, summary };
}
"""

async def _extract_semantic(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_SEMANTIC_JS)
    if not raw:
        return {}
    result = {}
    for k, v in raw.items():
        if isinstance(v, str):
            result[k] = _clean_text(v)
        else:
            result[k] = v
    return result


# ---------------------------------------------------------------------------
# Strategy 4 — Open Graph fallback
# ---------------------------------------------------------------------------

async def _extract_og_fallback(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate("""
        () => {
            const g = n => {
                const el = document.querySelector(`meta[property="${n}"], meta[name="${n}"]`);
                return el ? el.getAttribute('content') : null;
            };
            return {
                headline:       g('og:title'),
                summary:        g('og:description'),
                featured_image: g('og:image'),
                article_url:    g('og:url'),
                publication_date: g('article:published_time'),
            };
        }
    """)
    return {k: v for k, v in (raw or {}).items() if v}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _merge(base: dict, update: dict, overwrite: bool = True) -> None:
    for key, val in update.items():
        if val is None or val == "" or val == []:
            continue
        if overwrite or key not in base or not base[key]:
            base[key] = val


def _clean_text(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
