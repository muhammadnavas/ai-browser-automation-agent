"""
services/research_normalization.py
----------------------------------
Conservative normalization and deduplication engine for multi-website research.

Provides:
- Conservative normalizers for prices/currencies, measurements/units, dates/timestamps, and text.
- Canonical entity key computation.
- Deduplication across multiple websites with source attribution retention.
- Explicit conflict detection and flagging.
"""

import html
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from models.schemas import (
    ComparisonConflict,
    ExtractedFieldEvidence,
    NormalizedRecord,
)

CURRENCY_SYMBOLS: Dict[str, str] = {
    "$": "USD",
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
    "₹": "INR",
    "C$": "CAD",
    "A$": "AUD",
    "₩": "KRW",
    "CHF": "CHF",
    "R$": "BRL",
}

CURRENCY_CODES = {"USD", "EUR", "GBP", "JPY", "INR", "CAD", "AUD", "KRW", "CHF", "BRL", "CNY", "SEK", "NZD", "SGD"}

# Measurement units
UNITS_REGEX = re.compile(
    r"\b(TB|GB|MB|KB|GHz|MHz|kHz|Hz|kg|g|mg|lbs?|oz|inches|inch|in|\"|cm|mm|m|mAh|Wh|W|kW|V|mV|fps|px|mp)\b",
    re.IGNORECASE,
)

# Standard unit capitalization mapping
UNIT_CANONICAL_MAP = {
    "tb": "TB", "gb": "GB", "mb": "MB", "kb": "KB",
    "ghz": "GHz", "mhz": "MHz", "khz": "kHz", "hz": "Hz",
    "kg": "kg", "g": "g", "mg": "mg", "lb": "lb", "lbs": "lbs", "oz": "oz",
    "in": "in", "inch": "in", "inches": "in", "\"": "in",
    "cm": "cm", "mm": "mm", "m": "m",
    "mah": "mAh", "wh": "Wh", "w": "W", "kw": "kW", "v": "V", "mv": "mV",
    "fps": "fps", "px": "px", "mp": "MP",
}


def normalize_text(value: Any) -> str:
    """Normalize raw text by unescaping HTML and collapsing internal whitespace."""
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def normalize_price(value: Any) -> Optional[Dict[str, Any]]:
    """
    Conservatively normalize price representations.
    Extracts numeric amount and currency without performing speculative currency conversion.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        amount = float(value)
        return {
            "amount": round(amount, 2),
            "currency": "USD",
            "formatted": f"${amount:,.2f}",
            "type": "price",
        }

    raw_str = normalize_text(value)
    if not raw_str:
        return None

    # Clean any mangled currency encoding (e.g. Latin-1 Â£ to £, â‚¬ to €, â‚¹ to ₹)
    raw_str = raw_str.replace("Â£", "£").replace("â‚¬", "€").replace("â‚¹", "₹")

    detected_currency = None
    detected_symbol = None

    # Check 3-letter codes
    for code in CURRENCY_CODES:
        if re.search(rf"\b{code}\b", raw_str, re.IGNORECASE):
            detected_currency = code
            break

    # Check currency symbols
    if not detected_currency:
        for sym, code in CURRENCY_SYMBOLS.items():
            if sym in raw_str:
                detected_symbol = sym
                detected_currency = code
                break

    if not detected_currency:
        detected_currency = "USD"
        detected_symbol = "$"
    elif not detected_symbol:
        detected_symbol = "$" if detected_currency == "USD" else detected_currency + " "

    # Extract numbers with decimal or thousand separators
    # E.g. $1,299.99 or 1299.99 or 1.299,99 or 49
    num_match = re.search(r"(\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?|\d{1,3}(?:\.\d{3})*(?:,\d+)?)", raw_str)
    if not num_match:
        return None

    num_str = num_match.group(1).replace(" ", "")
    # Handle European style (1.299,99)
    if "," in num_str and "." in num_str:
        if num_str.rfind(",") > num_str.rfind("."):
            num_str = num_str.replace(".", "").replace(",", ".")
        else:
            num_str = num_str.replace(",", "")
    elif "," in num_str:
        # Check if comma is decimal (e.g. 49,99) or thousands (1,000)
        parts = num_str.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            num_str = num_str.replace(",", ".")
        else:
            num_str = num_str.replace(",", "")

    try:
        amount = float(num_str)
        symbol = detected_symbol or (detected_currency + " ")
        return {
            "amount": round(amount, 2),
            "currency": detected_currency,
            "formatted": f"{symbol}{amount:,.2f}" if symbol in ("$", "€", "£", "₹", "¥") else f"{detected_currency} {amount:,.2f}",
            "type": "price",
        }
    except ValueError:
        return None


def normalize_measurement(value: Any) -> Optional[Dict[str, Any]]:
    """
    Conservatively normalize numerical measurements and units.
    Does not perform speculative unit conversions across disparate categories.
    """
    if value is None:
        return None

    raw_str = normalize_text(value)
    if not raw_str:
        return None

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*"
        r"(TB|GB|MB|KB|GHz|MHz|kHz|Hz|kg|g|mg|lbs?|oz|inches|inch|in|\"|cm|mm|m|mAh|Wh|W|kW|V|mV|fps|px|mp)\b",
        raw_str,
        re.IGNORECASE,
    )
    if not match:
        return None

    numeric_val = float(match.group(1))
    raw_unit = match.group(2).lower()
    canonical_unit = UNIT_CANONICAL_MAP.get(raw_unit, match.group(2))

    return {
        "value": numeric_val,
        "unit": canonical_unit,
        "formatted": f"{numeric_val:g} {canonical_unit}",
        "type": "measurement",
    }


def normalize_date(value: Any) -> Optional[Dict[str, Any]]:
    """Conservatively parse and normalize dates into ISO YYYY-MM-DD."""
    if value is None:
        return None

    raw_str = normalize_text(value)
    if not raw_str:
        return None

    # Try ISO formats first
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)?)\b", raw_str)
    if iso_match:
        iso_str = iso_match.group(1)[:10]
        return {"iso": iso_str, "formatted": iso_str, "type": "date"}

    date_formats = [
        "%B %d, %Y", "%b %d, %Y",
        "%d %B %Y", "%d %b %Y",
        "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y",
    ]
    for fmt in date_formats:
        try:
            dt = datetime.strptime(raw_str, fmt)
            iso_str = dt.strftime("%Y-%m-%d")
            return {"iso": iso_str, "formatted": iso_str, "type": "date"}
        except ValueError:
            continue

    return None


def normalize_criterion_value(field_name: str, value: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """
    Attempt to normalize a field value according to its name and contents.
    Returns (display_string_or_raw, normalization_metadata_dict).
    """
    if value is None:
        return "N/A", None

    field_lower = field_name.lower()
    str_val = normalize_text(value)

    # 1. Price check
    if any(k in field_lower for k in ("price", "cost", "msrp", "rate", "fee")) or any(s in str_val for s in CURRENCY_SYMBOLS):
        p_res = normalize_price(value)
        if p_res:
            return p_res["formatted"], p_res

    # 2. Measurement check (by field name hint, or fallback for any field)
    m_res = normalize_measurement(value)
    if m_res:
        return m_res["formatted"], m_res

    # 3. Date check
    if any(k in field_lower for k in ("date", "published", "released", "updated", "time")):
        d_res = normalize_date(value)
        if d_res:
            return d_res["formatted"], d_res

    # 4. Standard text
    return str_val, None


GENERIC_MARKETING_TOKENS = {
    "the", "a", "an", "and", "or", "new", "official", "with", "for", "by", "in", "of",
    "noise", "canceling", "cancelling", "wireless", "bluetooth", "edition", "color",
    "headphones", "headphone", "earbuds", "earbud", "smart", "portable", "premium",
    "device", "series", "version", "brand", "original",
}


def compute_canonical_key(name: str, brand: Optional[str] = None, sku: Optional[str] = None) -> str:
    """Generate a stable, lowercase alphanumeric key for record deduplication."""
    if sku and str(sku).strip():
        clean_sku = re.sub(r"[^a-zA-Z0-9]", "", str(sku)).lower()
        if len(clean_sku) >= 4:
            return f"sku_{clean_sku}"

    clean_name = re.sub(r"[^a-zA-Z0-9\s]", "", str(name or "")).lower()
    raw_tokens = clean_name.split()
    meaningful_tokens = [t for t in raw_tokens if t not in GENERIC_MARKETING_TOKENS]

    # Look for alphanumeric model tokens (containing both letters and digits, e.g. wh1000xm5, rtx4090)
    model_tokens = [t for t in meaningful_tokens if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)]

    clean_brand = re.sub(r"[^a-zA-Z0-9]", "", str(brand or "")).lower()
    if not clean_brand and meaningful_tokens:
        clean_brand = meaningful_tokens[0]

    if model_tokens:
        canonical = f"{clean_brand} {' '.join(model_tokens)}".strip()
        return canonical

    canonical = " ".join(meaningful_tokens[:6])
    if clean_brand and clean_brand not in canonical:
        canonical = f"{clean_brand} {canonical}"

    return canonical.strip() or f"record_{uuid.uuid4().hex[:8]}"


def deduplicate_and_compare(
    raw_records: List[Dict[str, Any]],
    criteria: List[str],
) -> Tuple[List[NormalizedRecord], List[ComparisonConflict]]:
    """
    Deduplicate extracted records across websites, retain source attribution,
    and flag conflicting values across sources.

    Args:
        raw_records: List of dicts representing extracted entity data from various sources.
                     Must include 'source_url', 'extracted_at', and entity fields.
        criteria: List of field names / comparison criteria to evaluate.

    Returns:
        Tuple of (List of NormalizedRecord, List of ComparisonConflict)
    """
    record_map: Dict[str, NormalizedRecord] = {}
    all_conflicts: List[ComparisonConflict] = []

    for raw in raw_records:
        source_url = raw.get("source_url", "unknown")
        extracted_at = raw.get("extracted_at", datetime.now().isoformat())
        entity_name = normalize_text(raw.get("name") or raw.get("title") or raw.get("page_title") or "Unnamed Item")
        brand = raw.get("brand")
        sku = raw.get("sku") or raw.get("model")

        canonical_key = compute_canonical_key(entity_name, brand, sku)

        if canonical_key not in record_map:
            # Create new record
            rec_id = f"rec_{uuid.uuid4().hex[:8]}"
            norm_rec = NormalizedRecord(
                record_id=rec_id,
                name=entity_name,
                canonical_key=canonical_key,
                source_urls=[source_url],
                is_duplicate_across_sources=False,
                fields={},
                evidence={},
                conflicts=[],
            )
            record_map[canonical_key] = norm_rec
        else:
            norm_rec = record_map[canonical_key]
            if source_url not in norm_rec.source_urls:
                norm_rec.source_urls.append(source_url)
                norm_rec.is_duplicate_across_sources = True

        # Process each criterion
        for crit in criteria:
            raw_field_val = raw.get(crit)
            evidence_snippet = raw.get(f"{crit}_evidence") or raw.get("evidence", {}).get(crit)

            if raw_field_val is not None:
                norm_val, _ = normalize_criterion_value(crit, raw_field_val)

                if crit not in norm_rec.fields or norm_rec.fields[crit] in ("N/A", None):
                    # First observation or filling in previously missing field
                    norm_rec.fields[crit] = norm_val
                    norm_rec.evidence[crit] = ExtractedFieldEvidence(
                        value=norm_val,
                        raw_value=str(raw_field_val),
                        source_url=source_url,
                        extracted_at=extracted_at,
                        field_name=crit,
                        evidence_text=evidence_snippet,
                        confidence=1.0 if evidence_snippet else 0.85,
                        status="present",
                    )
                else:
                    # Compare with existing value for conflict detection
                    if crit in ("source_url", "source_urls"):
                        norm_rec.fields[crit] = ", ".join(norm_rec.source_urls)
                    else:
                        existing_val = norm_rec.fields[crit]
                        is_conflict = False
                        if (
                            existing_val not in ("N/A", None)
                            and norm_val not in ("N/A", None)
                            and str(existing_val).strip().lower() != str(norm_val).strip().lower()
                        ):
                            s1 = str(existing_val).strip().lower()
                            s2 = str(norm_val).strip().lower()
                            if not (("in stock" in s1 and "in stock" in s2) or ("out of stock" in s1 and "out of stock" in s2)):
                                is_conflict = True

                        if is_conflict:
                            # Conflict detected between two distinct observed values
                            existing_evidence = norm_rec.evidence.get(crit)
                            existing_source = existing_evidence.source_url if existing_evidence else "Source 1"

                            conflict = ComparisonConflict(
                                field=crit,
                                values={
                                    existing_source: existing_val,
                                    source_url: norm_val,
                                },
                                description=f"Conflicting '{crit}' values: '{existing_val}' ({existing_source}) vs '{norm_val}' ({source_url})",
                            )
                            norm_rec.conflicts.append(conflict)
                            all_conflicts.append(conflict)
                            # Retain evidence for current source as well
                            norm_rec.evidence[f"{crit}@{source_url}"] = ExtractedFieldEvidence(
                                value=norm_val,
                                raw_value=str(raw_field_val),
                                source_url=source_url,
                                extracted_at=extracted_at,
                                field_name=crit,
                                evidence_text=evidence_snippet,
                                confidence=0.9,
                                status="present",
                            )
            elif crit not in norm_rec.fields:
                # Value missing so far
                norm_rec.fields[crit] = "N/A"
                norm_rec.evidence[crit] = ExtractedFieldEvidence(
                    value="N/A",
                    source_url=source_url,
                    extracted_at=extracted_at,
                    field_name=crit,
                    status="missing",
                    confidence=0.0,
                )

    return list(record_map.values()), all_conflicts
