"""
extraction/__init__.py
----------------------
Public API for the Information Extraction helpers package.

Each sub-module exposes a single async extraction function that accepts a
Playwright ``Page`` object and returns a typed Pydantic model (or list thereof).

Usage::

    from extraction import (
        extract_metadata,
        extract_product,
        extract_news,
        extract_search_results,
        extract_contact_info,
        extract_tables,
        extract_images,
    )
"""

import inspect
import logging
from typing import Any, Callable, Dict, Optional

from extraction.metadata import extract_metadata
from extraction.product  import extract_product
from extraction.news     import extract_news
from extraction.search   import extract_search_results
from extraction.contact  import extract_contact_info
from extraction.tables   import extract_tables
from extraction.images   import extract_images
from extraction.listing  import extract_listing_records
from services.cache_service import CacheService

logger = logging.getLogger("extraction")


async def cached_extraction(
    cache_service: Optional[CacheService],
    agent_version: str,
    extractor_fn: Callable[..., Any],
    url: str,
    dom_fingerprint: Optional[str] = None,
    task_or_goal: str = "",
    extraction_context: str = "general",
    *args,
    **kwargs,
) -> Any:
    """
    Execute an extraction function with Redis caching for intermediate extraction results.

    Checks Redis for a cached extraction result matching the URL, state fingerprint,
    and extraction context. On HIT, returns the cached result. On MISS or invalid entry,
    executes extractor_fn(*args, **kwargs), sanitizes the result, and caches it in Redis.
    """
    if not cache_service:
        logger.info(f"Fresh extraction performed for URL '{url}'")
        if inspect.iscoroutinefunction(extractor_fn):
            return await extractor_fn(*args, **kwargs)
        return extractor_fn(*args, **kwargs)

    norm_url = cache_service.normalize_url(url)
    context_str = f"{task_or_goal}:{extraction_context}"
    redis_key = cache_service.generate_key(
        cache_type="extraction",
        agent_version=agent_version,
        task_or_goal=context_str,
        url=norm_url,
        dom_fingerprint=dom_fingerprint,
    )

    try:
        def validate_extraction(cached_obj: Any) -> bool:
            if not isinstance(cached_obj, dict):
                return False
            if "result" not in cached_obj:
                return False
            if cached_obj.get("state_fingerprint") != dom_fingerprint:
                logger.info("Extraction cache validation failed: state fingerprint mismatch")
                return False
            cached_url = cached_obj.get("url", "")
            if cache_service.normalize_url(cached_url) != norm_url:
                logger.info("Extraction cache validation failed: URL mismatch")
                return False
            if str(cached_obj.get("agent_version", "")).strip().lower() != agent_version.strip().lower():
                return False
            if cached_obj.get("extraction_context") != extraction_context:
                logger.info("Extraction cache validation failed: extraction context mismatch")
                return False
            return True

        cached_payload = await cache_service.get_validated(redis_key, validator=validate_extraction)
        if cached_payload is not None:
            logger.info(f"Extraction cache HIT for URL '{url}' (key='{redis_key}')")
            return cached_payload.get("result")
        else:
            logger.info(f"Extraction cache MISS for URL '{url}' (key='{redis_key}')")
    except Exception as exc:
        logger.warning(f"Extraction cache lookup failed (Redis failure, non-fatal): {exc}")

    # Fresh extraction
    logger.info(f"Fresh extraction performed for URL '{url}'")
    if inspect.iscoroutinefunction(extractor_fn):
        raw_result = await extractor_fn(*args, **kwargs)
    else:
        raw_result = extractor_fn(*args, **kwargs)

    # Convert Pydantic model / dict to serializable structure
    result_to_cache = raw_result
    if hasattr(raw_result, "model_dump"):
        result_to_cache = raw_result.model_dump()
    elif hasattr(raw_result, "dict"):
        result_to_cache = raw_result.dict()

    try:
        cache_payload = {
            "url": norm_url,
            "state_fingerprint": dom_fingerprint,
            "agent_version": agent_version,
            "extraction_context": extraction_context,
            "result": result_to_cache,
        }
        sanitized_payload = cache_service.sanitize_sensitive_data(cache_payload)
        logger.info(f"Extraction cache SET for URL '{url}' (key='{redis_key}')")
        await cache_service.set(redis_key, sanitized_payload, ttl=600)
    except Exception as exc:
        logger.warning(f"Extraction cache SET failed (Redis failure, non-fatal): {exc}")

    return raw_result


__all__ = [
    "extract_metadata",
    "extract_product",
    "extract_news",
    "extract_search_results",
    "extract_contact_info",
    "extract_tables",
    "extract_images",
    "extract_listing_records",
    "cached_extraction",
]
