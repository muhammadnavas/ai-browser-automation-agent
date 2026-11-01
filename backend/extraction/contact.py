"""
extraction/contact.py
---------------------
Extracts structured contact / business information from a Playwright Page.

Extraction strategy:
  1. Schema.org JSON-LD (LocalBusiness, Organization, Person)
  2. Schema.org microdata  ([itemprop="telephone"], [itemprop="email"], …)
  3. Regex scan over visible page text for emails and phone numbers
  4. Social media link detection from all <a href> elements
  5. Google Maps / Apple Maps link detection
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from playwright.async_api import Page
from models.schemas import ContactInfo

logger = logging.getLogger("extraction.contact")

# ---------------------------------------------------------------------------
# Regex Patterns
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

PHONE_RE = re.compile(
    r"""
    (?:
        (?:\+?1[\s.\-]?)?                      # optional country code +1
        (?:\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4})  # (NXX) NXX-XXXX
        |
        \+?\d{1,3}[\s.\-]?\(?\d{1,4}\)?[\s.\-]?\d{1,4}[\s.\-]?\d{1,9}
    )
    """,
    re.VERBOSE,
)

# Social platforms to detect from href
SOCIAL_PLATFORMS = {
    "linkedin":  "linkedin.com",
    "twitter":   "twitter.com",
    "x":         "x.com",
    "facebook":  "facebook.com",
    "instagram": "instagram.com",
    "youtube":   "youtube.com",
    "github":    "github.com",
    "tiktok":    "tiktok.com",
    "pinterest": "pinterest.com",
}

MAPS_PATTERNS = [
    "maps.google",
    "goo.gl/maps",
    "maps.apple.com",
    "google.com/maps",
]


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

async def extract_contact_info(page: Page) -> ContactInfo:
    """
    Extract contact / business information from the current Playwright page.

    Args:
        page: An active Playwright ``Page`` (already navigated).

    Returns:
        A ``ContactInfo`` instance with all available fields populated.
    """
    merged: Dict[str, Any] = {
        "email_addresses": [],
        "phone_numbers":   [],
        "social_links":    {},
    }
    errors: List[str] = []

    # Strategy 1: JSON-LD
    try:
        _merge(merged, await _extract_jsonld_contact(page))
    except Exception as exc:
        errors.append(f"JSON-LD: {exc}")

    # Strategy 2: Microdata
    try:
        _merge(merged, await _extract_microdata(page), overwrite=False)
    except Exception as exc:
        errors.append(f"Microdata: {exc}")

    # Strategy 3: Regex scan on visible text
    try:
        emails, phones = await _regex_scan(page)
        _extend_list(merged, "email_addresses", emails)
        _extend_list(merged, "phone_numbers",   phones)
    except Exception as exc:
        errors.append(f"Regex scan: {exc}")

    # Strategy 4: Social + Maps links
    try:
        socials, maps_link = await _extract_links(page)
        for platform, url in socials.items():
            if platform not in merged["social_links"]:
                merged["social_links"][platform] = url
        if maps_link and not merged.get("google_maps_link"):
            merged["google_maps_link"] = maps_link
    except Exception as exc:
        errors.append(f"Link scan: {exc}")

    # Deduplicate lists
    merged["email_addresses"] = _dedupe(merged.get("email_addresses", []))
    merged["phone_numbers"]   = _dedupe(merged.get("phone_numbers", []))

    if errors:
        logger.debug(f"Contact extraction non-fatal errors: {errors}")

    return ContactInfo(**{k: v for k, v in merged.items() if v is not None})


# ---------------------------------------------------------------------------
# Strategy 1 — JSON-LD
# ---------------------------------------------------------------------------

async def _extract_jsonld_contact(page: Page) -> Dict[str, Any]:
    raw_blocks: List[str] = await page.evaluate("""
        () => Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).map(s => s.textContent)
    """)

    contact_types = {"LocalBusiness", "Organization", "Person", "Corporation",
                     "Restaurant", "Hotel", "Store", "MedicalOrganization"}

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
            if not any(t in item_type for t in contact_types):
                continue

            # Address
            addr_raw = item.get("address", {})
            address = None
            if isinstance(addr_raw, str):
                address = addr_raw
            elif isinstance(addr_raw, dict):
                parts = [
                    addr_raw.get("streetAddress", ""),
                    addr_raw.get("addressLocality", ""),
                    addr_raw.get("addressRegion", ""),
                    addr_raw.get("postalCode", ""),
                    addr_raw.get("addressCountry", ""),
                ]
                address = ", ".join(p for p in parts if p) or None

            # Emails and phones — can be string or list
            email_raw = item.get("email", [])
            emails = _as_list(email_raw)

            phone_raw = item.get("telephone", [])
            phones = _as_list(phone_raw)

            # Social profiles from sameAs
            same_as = _as_list(item.get("sameAs", []))
            social_links: Dict[str, str] = {}
            for link in same_as:
                platform = _detect_platform(link)
                if platform:
                    social_links[platform] = link

            return {
                "company_name":    _clean(item.get("name")),
                "email_addresses": emails,
                "phone_numbers":   phones,
                "physical_address": address,
                "website":         _clean(item.get("url")),
                "social_links":    social_links,
            }
    return {}


# ---------------------------------------------------------------------------
# Strategy 2 — Microdata
# ---------------------------------------------------------------------------

_MICRODATA_JS = """
() => {
    const scope = document.querySelector(
        '[itemtype*="LocalBusiness"], [itemtype*="Organization"], [itemtype*="Person"]'
    ) || document;

    const get = name => {
        const el = scope.querySelector(`[itemprop="${name}"]`);
        return el ? (el.getAttribute('content') || el.getAttribute('href') || el.innerText || '').trim() : null;
    };
    const getAll = name =>
        Array.from(scope.querySelectorAll(`[itemprop="${name}"]`))
            .map(el => (el.getAttribute('content') || el.getAttribute('href') || el.innerText || '').trim())
            .filter(Boolean);

    // Address parts
    const addr = scope.querySelector('[itemtype*="PostalAddress"]') || scope;
    const addrParts = [
        get.call({}, 'streetAddress')  || (addr.querySelector('[itemprop="streetAddress"]') || {}).innerText || '',
        get.call({}, 'addressLocality') || (addr.querySelector('[itemprop="addressLocality"]') || {}).innerText || '',
        get.call({}, 'addressRegion')  || (addr.querySelector('[itemprop="addressRegion"]') || {}).innerText || '',
        get.call({}, 'postalCode')     || (addr.querySelector('[itemprop="postalCode"]') || {}).innerText || '',
    ].filter(Boolean);

    return {
        company_name:     get('name'),
        email_addresses:  getAll('email'),
        phone_numbers:    getAll('telephone'),
        physical_address: addrParts.join(', ') || null,
        website:          get('url'),
    };
}
"""

async def _extract_microdata(page: Page) -> Dict[str, Any]:
    raw = await page.evaluate(_MICRODATA_JS)
    if not raw or not any(raw.values()):
        return {}
    return {k: (_clean(v) if isinstance(v, str) else v) for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Strategy 3 — Regex scan
# ---------------------------------------------------------------------------

async def _regex_scan(page: Page) -> tuple:
    visible_text: str = await page.evaluate(
        "() => document.body ? document.body.innerText : ''"
    )

    emails = list({m.lower() for m in EMAIL_RE.findall(visible_text)})
    # Filter out common false positives (image filenames, etc.)
    emails = [e for e in emails if not e.endswith((".png", ".jpg", ".gif", ".svg"))]

    phone_matches = PHONE_RE.findall(visible_text)
    phones = list({p.strip() for p in phone_matches if len(re.sub(r"\D", "", p)) >= 7})

    return emails, phones


# ---------------------------------------------------------------------------
# Strategy 4 — Link scan
# ---------------------------------------------------------------------------

async def _extract_links(page: Page) -> tuple:
    hrefs: List[str] = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
                   .map(a => a.href)
                   .filter(Boolean)
    """)

    social_links: Dict[str, str] = {}
    maps_link: Optional[str] = None

    for href in hrefs:
        href_lower = href.lower()

        # Social
        for platform, domain in SOCIAL_PLATFORMS.items():
            if domain in href_lower and platform not in social_links:
                social_links[platform] = href

        # Maps
        if not maps_link and any(p in href_lower for p in MAPS_PATTERNS):
            maps_link = href

    return social_links, maps_link


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_platform(url: str) -> Optional[str]:
    url_lower = url.lower()
    for platform, domain in SOCIAL_PLATFORMS.items():
        if domain in url_lower:
            return platform
    return None


def _as_list(val: Any) -> List[str]:
    if isinstance(val, list):
        return [str(v).strip() for v in val if v]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []


def _extend_list(base: dict, key: str, items: List[str]) -> None:
    existing = set(base.get(key, []))
    for item in items:
        if item and item not in existing:
            base.setdefault(key, []).append(item)
            existing.add(item)


def _dedupe(lst: List[str]) -> List[str]:
    seen: set = set()
    result: List[str] = []
    for item in lst:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _merge(base: dict, update: dict, overwrite: bool = True) -> None:
    for key, val in update.items():
        if val is None or val == "" or val == [] or val == {}:
            continue
        if key in ("email_addresses", "phone_numbers"):
            _extend_list(base, key, val if isinstance(val, list) else [val])
        elif key == "social_links" and isinstance(val, dict):
            for platform, link in val.items():
                base["social_links"].setdefault(platform, link)
        else:
            if overwrite or key not in base or not base.get(key):
                base[key] = val


def _clean(val: Any) -> Optional[str]:
    if val is None:
        return None
    text = re.sub(r"\s+", " ", str(val)).strip()
    return text if text else None
