"""
services/cache_service.py

Reusable, production-ready async Redis Cache Service for AI Browser Automation Agent.

Requirements satisfied:
1. Async Redis client from redis.asyncio for FastAPI/asyncio compatibility.
2. Core methods: connect(), close(), get(key), set(key, value, ttl), delete(key), get_validated(key, validator).
3. Non-fatal Redis operations: logs warnings on failures and falls back gracefully so normal flow continues.
4. TTL / expiration support on cached items.
5. Deterministic SHA-256 cache-key generation from cache type, agent version, normalized task, normalized URL, and optional DOM fingerprint.
6. Hashed key material (SHA-256) to ensure raw task text or sensitive values are never stored in key names.
7. Recursive sanitization of sensitive fields (passwords, tokens, cookies, API keys, card numbers, CVVs, PINs) before caching.
8. Comprehensive logging for hits, misses, sets, malformed entries/expiration, invalidation, and connection failures.
9. get_validated() retrieves, validates, returns only valid values, and auto-invalidates failed items.
10. Reusable and decoupled from TaskManager and AIAgentPlanner.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import inspect
import json
import logging
import os
import re
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple, Union

from redis.asyncio import Redis, from_url
from redis.exceptions import ConnectionError, RedisError, TimeoutError

from backend.security import (
    is_sensitive_key as sec_is_sensitive_key,
    redact_text as sec_redact_text,
    redact_sensitive_data as sec_redact_sensitive_data,
)

logger = logging.getLogger("services.cache_service")


class CacheService:
    """Reusable asynchronous Redis caching service with recursive sanitization,

    deterministic hashing, and non-fatal fallback handling.
    """

    # -----------------------------------------------------------------------
    # Sensitive field detection patterns
    # -----------------------------------------------------------------------
    _SENSITIVE_KEY_TOKENS: Set[str] = {
        "password",
        "passwd",
        "pwd",
        "passphrase",
        "secret",
        "token",
        "jwt",
        "bearer",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "cookie",
        "cookies",
        "session",
        "sessionid",
        "session_id",
        "apikey",
        "api_key",
        "cvv",
        "cvv2",
        "cvc",
        "cvc2",
        "pin",
        "pincode",
        "pin_code",
        "pan",
    }

    _SENSITIVE_KEY_COMPOUND_PATTERNS: Tuple[re.Pattern, ...] = (
        re.compile(r"api[_-]?key", re.IGNORECASE),
        re.compile(r"private[_-]?key", re.IGNORECASE),
        re.compile(r"client[_-]?secret", re.IGNORECASE),
        re.compile(r"secret[_-]?key", re.IGNORECASE),
        re.compile(r"app[_-]?secret", re.IGNORECASE),
        re.compile(r"card[_-]?(?:num|number)", re.IGNORECASE),
        re.compile(r"credit[_-]?card", re.IGNORECASE),
        re.compile(r"debit[_-]?card", re.IGNORECASE),
        re.compile(r"cc[_-]?(?:num|number)", re.IGNORECASE),
        re.compile(r"security[_-]?code", re.IGNORECASE),
        re.compile(r"card[_-]?code", re.IGNORECASE),
        re.compile(r"pin[_-]?code", re.IGNORECASE),
        re.compile(r"access[_-]?token", re.IGNORECASE),
        re.compile(r"refresh[_-]?token", re.IGNORECASE),
        re.compile(r"id[_-]?token", re.IGNORECASE),
        re.compile(r"auth[_-]?token", re.IGNORECASE),
        re.compile(r"session[_-]?id", re.IGNORECASE),
        re.compile(r"session[_-]?token", re.IGNORECASE),
        re.compile(r"account[_-]?number", re.IGNORECASE),
    )

    _VALUE_PATTERNS: List[Tuple[re.Pattern, str]] = [
        # Credit card numbers: 13-19 digits with optional spaces or dashes
        (re.compile(r"\b(?:\d{4}[ -]?){3}\d{1,7}\b"), "[REDACTED_CARD]"),
        # JWT tokens (header.payload.signature starting with eyJ)
        (
            re.compile(
                r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_\-+/=]{10,}\b"
            ),
            "[REDACTED_JWT]",
        ),
        # Bearer tokens in text
        (
            re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE),
            "Bearer [REDACTED]",
        ),
        # API key prefixes (e.g. OpenAI sk-, Google AIza, GitHub tokens)
        (
            re.compile(
                r"\b(sk-[a-zA-Z0-9]{20,}|AIza[0-9A-Za-z-_]{35}|ghp_[a-zA-Z0-9]{36}|gho_[a-zA-Z0-9]{36})\b"
            ),
            "[REDACTED_API_KEY]",
        ),
        # Query parameter / form fields containing sensitive keys in strings
        (
            re.compile(
                r"((?:password|passwd|pwd|token|access_token|api[_-]?key|secret|cvv|pin)=)([^&\s]+)",
                re.IGNORECASE,
            ),
            r"\1[REDACTED]",
        ),
    ]

    def __init__(
        self,
        redis_url: Optional[str] = None,
        default_ttl: int = 3600,
        key_prefix: str = "agent_cache",
        max_connections: int = 10,
        socket_timeout: float = 5.0,
        socket_connect_timeout: float = 5.0,
        connect_cooldown_seconds: float = 60.0,
        client: Optional[Redis] = None,
    ) -> None:
        """Initialize the CacheService.

        Args:
            redis_url: Redis connection URL (e.g. 'redis://localhost:6379/0').
                       Defaults to REDIS_URL env variable or 'redis://localhost:6379/0'.
            default_ttl: Default expiration TTL in seconds (default: 3600s / 1 hour).
            key_prefix: Global key namespace prefix.
            max_connections: Maximum connection pool size.
            socket_timeout: Socket read/write timeout in seconds.
            socket_connect_timeout: Socket connection timeout in seconds.
            connect_cooldown_seconds: Cooldown interval before retrying a failed Redis connection.
            client: Optional pre-configured redis.asyncio.Redis instance.
        """
        raw_env_url = (redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")).strip()
        redis_enabled_env = os.getenv("REDIS_ENABLED", "true").strip().lower()
        self.is_disabled = (
            redis_enabled_env in ("false", "0", "no", "off")
            or raw_env_url.lower() in ("none", "disabled", "off", "false", "")
        )
        self.redis_url = "" if self.is_disabled else raw_env_url
        try:
            self.default_ttl = int(os.getenv("CACHE_DEFAULT_TTL", str(default_ttl)))
        except ValueError:
            self.default_ttl = default_ttl

        self.key_prefix = key_prefix
        try:
            self.max_connections = int(os.getenv("REDIS_MAX_CONNECTIONS", str(max_connections)))
        except ValueError:
            self.max_connections = max_connections
        try:
            self.socket_timeout = float(os.getenv("REDIS_SOCKET_TIMEOUT", str(socket_timeout if socket_timeout != 2.0 else 5.0)))
        except ValueError:
            self.socket_timeout = 5.0
        try:
            self.socket_connect_timeout = float(os.getenv("REDIS_CONNECT_TIMEOUT", str(socket_connect_timeout if socket_connect_timeout != 2.0 else 5.0)))
        except ValueError:
            self.socket_connect_timeout = 5.0
        try:
            self.connect_cooldown_seconds = float(os.getenv("REDIS_CONNECT_COOLDOWN", str(connect_cooldown_seconds)))
        except ValueError:
            self.connect_cooldown_seconds = connect_cooldown_seconds

        self._client: Optional[Redis] = client
        self._is_available: bool = client is not None and not self.is_disabled
        self._lock: asyncio.Lock = asyncio.Lock()
        self._last_connect_attempt: float = 0.0
        self._last_connection_warning_at: float = 0.0

    # -----------------------------------------------------------------------
    # Connection Lifecycle
    # -----------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return True if Redis is currently marked as connected and available."""
        return self._client is not None and self._is_available

    @property
    def safe_redis_url(self) -> str:
        """Return the Redis connection URL with password redacted for safe logging."""
        try:
            parsed = urlparse(self.redis_url)
            if parsed.password:
                netloc = f"{parsed.username or ''}:[REDACTED]@{parsed.hostname or 'localhost'}:{parsed.port or 6379}"
                return urlunparse(
                    (
                        parsed.scheme,
                        netloc,
                        parsed.path,
                        parsed.params,
                        parsed.query,
                        parsed.fragment,
                    )
                )
        except Exception:
            pass
        return self.redis_url

    async def connect(self, force: bool = False) -> bool:
        """Establish an asynchronous connection to the Redis server.

        If Redis is unreachable or connection fails, logs a warning and returns
        False without raising an exception (non-fatal failure mode).

        Args:
            force: If True, bypasses the connection retry cooldown timer.

        Returns:
            bool: True if connection is healthy, False otherwise.
        """
        if self.is_disabled or not self.redis_url:
            return False

        now = time.time()
        if (
            not force
            and not self._is_available
            and (now - self._last_connect_attempt < self.connect_cooldown_seconds)
        ):
            return False

        async with self._lock:
            self._last_connect_attempt = time.time()
            if self._client is not None and self._is_available:
                try:
                    await self._client.ping()
                    return True
                except Exception:
                    self._is_available = False

            try:
                if self._client is None or not self._is_available:
                    if self._client is not None:
                        try:
                            if hasattr(self._client, "aclose"):
                                await self._client.aclose()
                            elif hasattr(self._client, "close"):
                                await self._client.close()
                        except Exception:
                            pass
                        self._client = None

                    self._client = from_url(
                        self.redis_url,
                        max_connections=self.max_connections,
                        socket_timeout=self.socket_timeout,
                        socket_connect_timeout=self.socket_connect_timeout,
                        retry_on_timeout=True,
                        health_check_interval=30,
                        socket_keepalive=True,
                        decode_responses=True,
                    )

                # Validate connection with PING
                ping_started = time.perf_counter()
                await self._client.ping()
                self._is_available = True
                logger.info("Redis connected: endpoint=%s ping_ms=%.1f", self.safe_redis_url, (time.perf_counter() - ping_started) * 1000)
                return True

            except Exception as exc:
                # Redis is an optional cache: keep the application running without
                # repeatedly emitting the same warning while the endpoint is unreachable.
                warning_now = time.time()
                if warning_now - self._last_connection_warning_at >= self.connect_cooldown_seconds:
                    logger.warning(
                        "Redis connection failed: endpoint=%s error_type=%s; using cache bypass mode. "
                        "Check REDIS_URL, service availability, DNS, and outbound TLS connectivity.",
                        self.safe_redis_url, type(exc).__name__,
                    )
                    self._last_connection_warning_at = warning_now
                self._is_available = False
                if self._client is not None:
                    try:
                        if hasattr(self._client, "aclose"):
                            await self._client.aclose()
                        elif hasattr(self._client, "close"):
                            await self._client.close()
                    except Exception:
                        pass
                    self._client = None
                return False

    async def close(self) -> None:
        """Gracefully close the Redis client and connection pool."""
        async with self._lock:
            if self._client is not None:
                try:
                    if hasattr(self._client, "aclose"):
                        await self._client.aclose()
                    elif hasattr(self._client, "close"):
                        await self._client.close()

                    if hasattr(self._client, "connection_pool") and hasattr(
                        self._client.connection_pool, "disconnect"
                    ):
                        await self._client.connection_pool.disconnect()
                    logger.info("Redis connection closed")
                except Exception as exc:
                    logger.warning("Redis close failed: error_type=%s", type(exc).__name__)
                finally:
                    self._client = None
                    self._is_available = False

    async def _get_client(self) -> Optional[Redis]:
        """Internal helper to retrieve active Redis client or attempt lazy connect."""
        if self._client is not None and self._is_available:
            return self._client

        # Attempt auto-connect
        connected = await self.connect()
        if connected and self._client is not None:
            return self._client
        return None

    # -----------------------------------------------------------------------
    # Cache Key Generation & Normalization
    # -----------------------------------------------------------------------

    def normalize_task(self, task_or_goal: str) -> str:
        """Normalize a task or goal string for deterministic key hashing.

        - Converts to lowercase.
        - Collapses multiple whitespace characters to single space.
        - Strips leading and trailing whitespace and punctuation.
        """
        if not task_or_goal or not isinstance(task_or_goal, str):
            return ""

        norm = task_or_goal.strip().lower()
        norm = re.sub(r"\s+", " ", norm)
        norm = norm.strip("\"'.,;:")
        return norm

    def normalize_url(self, url: str) -> str:
        """Normalize a URL for deterministic caching.

        - Normalizes scheme and host to lowercase.
        - Removes default ports (:80, :443).
        - Collapses duplicate slashes and strips trailing slashes on non-root paths.
        - Sorts query parameters deterministically by key and value.
        - Drops URL fragments.
        """
        if not url or not isinstance(url, str):
            return ""

        clean_url = url.strip()
        if not clean_url:
            return ""

        # Prepend scheme if missing for proper URL parsing
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", clean_url):
            clean_url = "https://" + clean_url

        try:
            parsed = urlparse(clean_url)
            scheme = parsed.scheme.lower()
            netloc = parsed.netloc.lower()

            # Remove standard default ports
            if scheme == "http" and netloc.endswith(":80"):
                netloc = netloc[:-3]
            elif scheme == "https" and netloc.endswith(":443"):
                netloc = netloc[:-4]

            # Normalize path
            path = parsed.path
            if path:
                path = re.sub(r"/+", "/", path)
                if path != "/" and path.endswith("/"):
                    path = path.rstrip("/")

            # Deterministically sort query parameters
            query = ""
            if parsed.query:
                query_params = parse_qsl(parsed.query, keep_blank_values=True)
                sorted_params = sorted(query_params, key=lambda x: (x[0], x[1]))
                query = urlencode(sorted_params)

            normalized = urlunparse(
                (
                    scheme,
                    netloc,
                    path,
                    parsed.params,
                    query,
                    "",  # Fragments excluded
                )
            )
            return normalized
        except Exception:
            return clean_url.lower().rstrip("/")

    def normalize_dom_fingerprint(self, dom_fingerprint: Optional[str]) -> str:
        """Normalize DOM / page-state fingerprint string."""
        if not dom_fingerprint or not isinstance(dom_fingerprint, str):
            return ""
        return dom_fingerprint.strip()

    def generate_key(
        self,
        cache_type: str,
        agent_version: str,
        task_or_goal: str,
        url: str = "",
        dom_fingerprint: Optional[str] = None,
        history_hash: Optional[str] = None,
    ) -> str:
        """Generate a deterministic, secure SHA-256 hashed cache key.

        Components hashed:
        - cache_type (e.g. 'plan', 'dom', 'extraction', 'action')
        - agent_version (e.g. '1.0.0', 'v1')
        - normalized task/goal
        - normalized URL
        - optional normalized DOM fingerprint
        - optional history hash

        Raw task text and sensitive inputs are NEVER placed directly in Redis keys.

        Args:
            cache_type: Type identifier of the cached data.
            agent_version: Version identifier of the AI agent/planner.
            task_or_goal: Task objective or goal string.
            url: Optional page URL.
            dom_fingerprint: Optional DOM structural hash or fingerprint.
            history_hash: Optional hash of the agent's recent action trajectory.

        Returns:
            str: Deterministic hashed cache key format '<prefix>:<type>:<version>:<sha256>'.
        """
        norm_type = (cache_type or "general").strip().lower()
        norm_version = (agent_version or "v1").strip().lower()
        norm_task = self.normalize_task(task_or_goal)
        norm_url = self.normalize_url(url)
        norm_dom = self.normalize_dom_fingerprint(dom_fingerprint)
        norm_history = history_hash or ""

        # Build canonical payload material for SHA-256 digest
        key_material = f"{norm_type}|{norm_version}|{norm_task}|{norm_url}|{norm_dom}|{norm_history}"
        digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()

        return f"{self.key_prefix}:{norm_type}:{norm_version}:{digest}"

    # -----------------------------------------------------------------------
    # Sensitive Data Sanitization
    # -----------------------------------------------------------------------

    def is_sensitive_key(self, key_name: str) -> bool:
        """Check if a dictionary key name represents sensitive data.

        Handles snake_case, camelCase, kebab-case, and PascalCase.
        Delegates to centralized security module while respecting legacy class attributes.
        """
        if sec_is_sensitive_key(key_name):
            return True

        if not key_name or not isinstance(key_name, (str, int, float)):
            return False

        key_str = str(key_name)
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_str).lower()
        normalized = re.sub(r"[^a-z0-9]", "_", normalized)
        parts = set(filter(None, normalized.split("_")))

        if parts & self._SENSITIVE_KEY_TOKENS:
            return True

        for pattern in self._SENSITIVE_KEY_COMPOUND_PATTERNS:
            if pattern.search(normalized):
                return True

        return False

    def sanitize_string(self, text: str) -> str:
        """Sanitize sensitive patterns (credit cards, JWTs, API keys, tokens) in string values."""
        if not isinstance(text, str):
            return text

        result = sec_redact_text(text)
        for pattern, replacement in self._VALUE_PATTERNS:
            result = pattern.sub(replacement, result)
        return result

    def sanitize_sensitive_data(
        self, data: Any, _visited: Optional[Set[int]] = None
    ) -> Any:
        """Recursively sanitize sensitive fields (passwords, auth tokens, cookies,

        API keys, payment cards, CVVs, PINs) before caching.

        Delegates to the centralized security module.
        """
        return sec_redact_sensitive_data(data, mask_tier3=False, _visited=_visited)

    # -----------------------------------------------------------------------
    # JSON Serialization Helper
    # -----------------------------------------------------------------------

    @staticmethod
    def _json_serializer(obj: Any) -> Any:
        """Custom JSON serializer for datetime, UUID, sets, bytes, and custom objects."""
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        if isinstance(obj, uuid.UUID):
            return str(obj)
        if isinstance(obj, set):
            return list(obj)
        if isinstance(obj, bytes):
            return obj.decode("utf-8", errors="replace")
        if hasattr(obj, "model_dump") and callable(obj.model_dump):
            return obj.model_dump()
        if hasattr(obj, "dict") and callable(obj.dict):
            return obj.dict()
        if hasattr(obj, "__dict__"):
            return obj.__dict__
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    # -----------------------------------------------------------------------
    # Core Cache Operations
    # -----------------------------------------------------------------------

    async def get(self, key: str) -> Optional[Any]:
        """Retrieve and deserialize a cached entry from Redis.

        Args:
            key: The Redis cache key.

        Returns:
            The deserialized value if found, or None on cache miss, malformed entry,
            or Redis error.
        """
        if not key:
            return None

        client = await self._get_client()
        if client is None:
            logger.debug(f"Cache MISS (Redis unavailable): key='{key}'")
            return None

        try:
            raw_data = await client.get(key)
            if raw_data is None:
                logger.info(f"Cache MISS: key='{key}'")
                return None

            if isinstance(raw_data, bytes):
                raw_data = raw_data.decode("utf-8")

            try:
                deserialized = json.loads(raw_data)
                logger.info(f"Cache HIT: key='{key}'")
                return deserialized
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
                logger.warning(
                    f"Cache MALFORMED entry for key='{key}': {exc}. Auto-invalidating corrupted entry."
                )
                await self.delete(key)
                return None

        except (RedisError, ConnectionError, TimeoutError, OSError, Exception) as exc:
            logger.warning(f"Redis get operation failed for key='{key}': {exc}")
            self._is_available = False
            return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[int] = None,
    ) -> bool:
        """Sanitize, serialize, and store a value in Redis with an expiration TTL.

        Args:
            key: The Redis cache key.
            value: The data to store. Will be recursively sanitized for sensitive fields.
            ttl: Expiration time in seconds. Defaults to self.default_ttl if None.

        Returns:
            bool: True if stored successfully, False otherwise (non-fatal).
        """
        if not key or value is None:
            return False

        effective_ttl = ttl if ttl is not None else self.default_ttl
        if effective_ttl <= 0:
            logger.warning(
                f"Cache SET ignored for key='{key}': TTL ({effective_ttl}) is <= 0"
            )
            return False

        client = await self._get_client()
        if client is None:
            logger.debug(f"Cache SET bypassed (Redis unavailable): key='{key}'")
            return False

        try:
            sanitized_value = self.sanitize_sensitive_data(value)
            payload = json.dumps(sanitized_value, default=self._json_serializer)
            await client.set(key, payload, ex=effective_ttl)
            logger.info(f"Cache SET: key='{key}', ttl={effective_ttl}s")
            return True
        except (RedisError, ConnectionError, TimeoutError, OSError, Exception) as exc:
            logger.warning(f"Redis set operation failed for key='{key}': {exc}")
            self._is_available = False
            return False

    async def delete(self, key: str) -> bool:
        """Delete / invalidate a cached entry in Redis.

        Args:
            key: The Redis cache key to delete.

        Returns:
            bool: True if deleted or acknowledged, False on failure.
        """
        if not key:
            return False

        client = await self._get_client()
        if client is None:
            return False

        try:
            deleted_count = await client.delete(key)
            logger.info(
                f"Cache INVALIDATED: key='{key}' (deleted_count={deleted_count})"
            )
            return True
        except (RedisError, ConnectionError, TimeoutError, OSError, Exception) as exc:
            logger.warning(f"Redis delete operation failed for key='{key}': {exc}")
            self._is_available = False
            return False

    async def get_validated(
        self,
        key: str,
        validator: Callable[[Any], Union[bool, Coroutine[Any, Any, bool]]],
    ) -> Optional[Any]:
        """Retrieve a cached value and validate it against a custom validator function.

        Behavior:
        - Retrieves the cached value via get(key).
        - If not found or Redis is down, returns None.
        - Runs the supplied validator (sync or async callable).
        - If validator returns True, returns the cached value.
        - If validator returns False or raises an exception, automatically deletes
          the invalid cache entry via delete(key) and returns None.

        Args:
            key: The Redis cache key.
            validator: A sync or async callable accepting the cached value and returning bool.

        Returns:
            The validated cached value if valid, None otherwise.
        """
        cached_value = await self.get(key)
        if cached_value is None:
            return None

        try:
            if inspect.iscoroutinefunction(validator):
                is_valid = await validator(cached_value)
            else:
                result = validator(cached_value)
                if inspect.isawaitable(result):
                    is_valid = await result
                else:
                    is_valid = bool(result)
        except Exception as exc:
            logger.warning(
                f"Cache validation error for key='{key}': {exc}. Invalidation triggered."
            )
            is_valid = False

        if is_valid:
            logger.debug(f"Cache validation succeeded for key='{key}'")
            return cached_value

        logger.info(
            f"Cache validation failed for key='{key}'. Auto-invalidating entry."
        )
        await self.delete(key)
        return None


# Default singleton instance for convenient application-wide import
default_cache_service = CacheService()
