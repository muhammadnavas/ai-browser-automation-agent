"""
agents/plan_cache.py
--------------------
Thread-safe in-memory LRU cache for storing, retrieving, versioning,
and invalidating AI-generated high-level task plans and metadata.
"""

import re
import time
import logging
import threading
from urllib.parse import urlparse, urlunparse
from dataclasses import dataclass, field
from collections import OrderedDict
from typing import List, Dict, Any, Optional

logger = logging.getLogger("PlanCache")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class CachedPlan:
    """Represents a cached high-level execution plan with metadata and version tracking."""
    key: str
    goal: str
    start_url: str
    plan: List[str]
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "llm"                    # "llm" | "heuristic"
    version: int = 1                       # Starts at 1, incremented upon update/replan
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    hit_count: int = 0

    def copy_plan(self) -> List[str]:
        """Return a defensive shallow copy of the milestone steps."""
        return list(self.plan)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize cached entry for inspection, diagnostics, and serialization."""
        return {
            "key": self.key,
            "goal": self.goal,
            "start_url": self.start_url,
            "plan": self.copy_plan(),
            "metadata": dict(self.metadata),
            "source": self.source,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "hit_count": self.hit_count,
        }


@dataclass
class PlanCacheStats:
    """Runtime statistics and telemetry for the plan cache."""
    hits: int = 0
    misses: int = 0
    sets: int = 0
    updates: int = 0
    invalidations: int = 0
    evictions: int = 0
    max_size: int = 256

    @property
    def total_requests(self) -> int:
        return self.hits + self.misses

    @property
    def hit_ratio(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return round(self.hits / self.total_requests, 4)


# ---------------------------------------------------------------------------
# Plan Cache Manager
# ---------------------------------------------------------------------------

class PlanCache:
    """
    Thread-safe, in-memory LRU cache for AI Task Planning milestones.
    Supports deterministic key normalization, plan versioning, defensive copies,
    and LRU capacity eviction.
    """

    def __init__(self, max_size: int = 256, ttl_seconds: Optional[float] = None):
        if max_size < 1:
            raise ValueError("max_size must be at least 1")
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._cache: OrderedDict[str, CachedPlan] = OrderedDict()
        self._lock = threading.RLock()
        self._stats = PlanCacheStats(max_size=max_size)

    # -----------------------------------------------------------------------
    # Key Normalization
    # -----------------------------------------------------------------------

    @staticmethod
    def normalize_goal(goal: str) -> str:
        """
        Normalize a goal prompt:
        - Lowercase
        - Strip quotes, trailing punctuation, and whitespace
        - Collapse multiple spaces into single space
        """
        if not goal:
            return ""
        g = goal.strip().lower()
        # Remove surrounding quotes
        g = g.strip("\"'“”`")
        # Collapse whitespace
        g = re.sub(r"\s+", " ", g)
        # Strip trailing punctuation (e.g. ".", "!", "?", ";", ",")
        g = g.rstrip(".,!?:;")
        return g.strip()

    @staticmethod
    def normalize_url(url: str) -> str:
        """
        Normalize a start URL:
        - Ensure scheme
        - Lowercase scheme and domain
        - Strip default HTTP/HTTPS ports (:80, :443)
        - Strip trailing slashes on empty root paths
        """
        if not url:
            return ""
        clean_url = url.strip()
        if not clean_url or clean_url in ("about:blank",):
            return ""

        clean_lower = clean_url.lower()
        if not (clean_lower.startswith("http://") or clean_lower.startswith("https://")):
            clean_url = "https://" + clean_url

        try:
            parsed = urlparse(clean_url)
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()

            # Remove default ports
            if scheme == "http" and netloc.endswith(":80"):
                netloc = netloc[:-3]
            elif scheme == "https" and netloc.endswith(":443"):
                netloc = netloc[:-4]

            # Strip trailing slash on bare root path
            path = parsed.path
            if path == "/":
                path = ""
            elif path.endswith("/"):
                path = path.rstrip("/")

            normalized = urlunparse((
                scheme,
                netloc,
                path,
                parsed.params,
                parsed.query,
                ""  # drop fragments for planning key
            ))
            return normalized.lower()
        except Exception:
            return clean_url.lower().rstrip("/")

    def generate_key(self, goal: str, start_url: str = "") -> str:
        """Generate a deterministic canonical cache key from goal and start_url."""
        norm_goal = self.normalize_goal(goal)
        norm_url = self.normalize_url(start_url)
        return f"plan:{norm_goal}::{norm_url}"

    # -----------------------------------------------------------------------
    # Cache Operations
    # -----------------------------------------------------------------------

    def get(self, goal: str, start_url: str = "") -> Optional[CachedPlan]:
        """
        Retrieve a cached plan if present and valid.
        On hit: Moves item to MRU position and updates hit counts.
        On miss: Increments miss count.
        """
        key = self.generate_key(goal, start_url)
        with self._lock:
            cached = self._cache.get(key)
            if cached is None:
                self._stats.misses += 1
                logger.debug(f"PlanCache MISS: key='{key}'")
                return None

            # Check optional TTL expiration
            if self._ttl_seconds is not None:
                age = time.time() - cached.created_at
                if age > self._ttl_seconds:
                    logger.debug(f"PlanCache EXPIRED (age={age:.1f}s > {self._ttl_seconds}s): key='{key}'")
                    del self._cache[key]
                    self._stats.misses += 1
                    return None

            # Move to MRU end
            self._cache.move_to_end(key)
            cached.hit_count += 1
            self._stats.hits += 1
            logger.debug(
                f"PlanCache HIT: key='{key}' (v{cached.version}, source={cached.source}, hits={cached.hit_count})"
            )
            return cached

    def set(
        self,
        goal: str,
        start_url: str,
        plan: List[str],
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "llm",
    ) -> CachedPlan:
        """
        Store a generated plan and metadata.
        If cache size exceeds max_size, evicts the oldest (LRU) entry.
        """
        key = self.generate_key(goal, start_url)
        now = time.time()
        plan_copy = list(plan) if plan else []
        meta_copy = dict(metadata) if metadata else {}

        with self._lock:
            # If replacing existing item
            if key in self._cache:
                existing = self._cache[key]
                existing.plan = plan_copy
                existing.metadata = meta_copy
                existing.source = source
                existing.updated_at = now
                self._cache.move_to_end(key)
                self._stats.sets += 1
                return existing

            # If capacity full, evict LRU (first item)
            if len(self._cache) >= self._max_size:
                evicted_key, evicted_item = self._cache.popitem(last=False)
                self._stats.evictions += 1
                logger.debug(f"PlanCache EVICTION (LRU): key='{evicted_key}'")

            cached = CachedPlan(
                key=key,
                goal=goal,
                start_url=start_url,
                plan=plan_copy,
                metadata=meta_copy,
                source=source,
                version=1,
                created_at=now,
                updated_at=now,
                hit_count=0,
            )
            self._cache[key] = cached
            self._stats.sets += 1
            logger.debug(f"PlanCache SET: key='{key}', steps={len(plan_copy)}, source={source}")
            return cached

    def update(
        self,
        goal: str,
        start_url: str,
        new_plan: List[str],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CachedPlan:
        """
        Update/replan an existing entry. Increments the version number and refreshes updated_at.
        If key is not present, creates it with version=1.
        """
        key = self.generate_key(goal, start_url)
        now = time.time()
        plan_copy = list(new_plan) if new_plan else []

        with self._lock:
            if key in self._cache:
                cached = self._cache[key]
                cached.plan = plan_copy
                if metadata is not None:
                    cached.metadata.update(metadata)
                cached.version += 1
                cached.updated_at = now
                self._cache.move_to_end(key)
                self._stats.updates += 1
                logger.info(f"PlanCache UPDATE: key='{key}' -> v{cached.version}, steps={len(plan_copy)}")
                return cached

            # Otherwise set as new
            cached = self.set(goal, start_url, plan_copy, metadata=metadata)
            self._stats.updates += 1
            return cached

    def invalidate(self, goal: str, start_url: str = "") -> bool:
        """Remove a specific entry from the cache. Returns True if removed, False otherwise."""
        key = self.generate_key(goal, start_url)
        with self._lock:
            if key in self._cache:
                del self._cache[key]
                self._stats.invalidations += 1
                logger.debug(f"PlanCache INVALIDATED: key='{key}'")
                return True
            return False

    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            logger.info("PlanCache cleared.")

    def get_stats(self) -> Dict[str, Any]:
        """Return diagnostic statistics and hit ratios."""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._stats.hits,
                "misses": self._stats.misses,
                "total_requests": self._stats.total_requests,
                "hit_ratio": self._stats.hit_ratio,
                "sets": self._stats.sets,
                "updates": self._stats.updates,
                "invalidations": self._stats.invalidations,
                "evictions": self._stats.evictions,
            }

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __contains__(self, goal_or_key: str) -> bool:
        with self._lock:
            if goal_or_key in self._cache:
                return True
            # Also check if it's a goal string
            key = self.generate_key(goal_or_key)
            return key in self._cache


# Global shared singleton instance for application runtime
default_plan_cache = PlanCache()
