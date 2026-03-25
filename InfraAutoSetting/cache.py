from __future__ import annotations

import json
import logging

from redis import Redis

from runtime_bus import create_redis

logger = logging.getLogger("infra_auto_setting.cache")

# Redis key prefix — all infra script cache entries live under this namespace.
_KEY_PREFIX = "infra:script_cache:"

# Default TTL: 7 days in seconds.
_DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60


class ScriptCache:
    """Redis-backed cache for generated infra bootstrap scripts.

    Cache key: human-readable concatenation of (package_manager, sudo_allowed,
    sorted components, sorted versions, log paths) — directly inspectable in
    Redis without decoding.  Entries are stored with a server-side TTL so stale
    scripts are automatically evicted.

    Shared across agent processes, so cached scripts are reusable across
    concurrent / sequential runs without file-locking concerns.
    """

    def __init__(self, redis_client: Redis | None = None, ttl_seconds: int = _DEFAULT_TTL_SECONDS) -> None:
        self._redis = redis_client or create_redis()
        self._ttl = ttl_seconds

    # ------------------------------------------------------------------ #
    # public API                                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def build_key(
        components: list[str],
        resolved_versions: dict[str, str],
        package_manager: str,
        sudo_allowed: str,
        log_base_dir: str,
        log_gc_dir: str,
        log_app_dir: str,
    ) -> str:
        """Return a human-readable, deterministic cache key for the given parameters.

        Format:
            {package_manager}:{sudo_allowed}:{components}:{versions}:{log_dirs}

        Each segment uses a consistent separator so the key is directly
        inspectable in Redis (e.g. ``KEYS infra:script_cache:apt:yes:*``).
        """
        components_seg = ",".join(sorted(c.strip().lower() for c in components))
        versions_seg = ",".join(f"{k}={v}" for k, v in sorted(resolved_versions.items()))
        log_seg = f"{log_base_dir}|{log_gc_dir}|{log_app_dir}"
        return f"{package_manager}:{sudo_allowed}:{components_seg}:{versions_seg}:{log_seg}"

    def get(self, key: str) -> str | None:
        """Return the cached script for *key*, or ``None`` on miss / expiry.

        TTL is managed by Redis — expired keys are simply absent.
        """
        redis_key = _KEY_PREFIX + key
        try:
            value = self._redis.get(redis_key)
        except Exception:
            logger.warning("redis GET failed for key %s; treating as cache miss", key[:12], exc_info=True)
            return None
        if value is None:
            return None
        # value is already a str because create_redis() sets decode_responses=True
        try:
            entry = json.loads(value)
            return entry.get("script")
        except (json.JSONDecodeError, TypeError):
            return value  # legacy: raw script string without JSON wrapper

    def put(self, key: str, script: str, meta: dict | None = None) -> None:
        """Store *script* under *key* with the configured TTL."""
        redis_key = _KEY_PREFIX + key
        entry = json.dumps(
            {"script": script, **(meta or {})},
            ensure_ascii=False,
        )
        try:
            self._redis.setex(redis_key, self._ttl, entry)
        except Exception:
            logger.warning("redis SETEX failed for key %s; cache write skipped", key[:12], exc_info=True)
