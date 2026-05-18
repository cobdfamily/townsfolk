"""Redis-backed cache for /v1/lookup envelopes.

Failure-mode discipline: Redis is a soft dep. Every
call here swallows connection errors and falls back
to the no-cache path. Losing the cache makes lookups
slower; it never breaks them. This matches the
broader townsfolk pattern (degraded boot when the DB
pool fails to init, brian's pattern).

Keys live under the `lookup:` prefix so an operator
can `redis-cli --scan --pattern 'lookup:*'` to
inspect what's cached. The cache key encodes the
normalised input (E.164 for phones, rounded coords
for coords) + the radius, since the response embeds
the cities_within list which depends on radius.

No explicit invalidation -- TTL covers it. Bump
config.cache_ttl_seconds higher (24h, 7d) once the
ETL signals cache-bust on TRUNCATE + reload.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis.asyncio as redis_asyncio


logger = logging.getLogger("townsfolk.cache")


# Module-level singleton, same pattern as db._POOL.
# init_cache() at lifespan startup; close_cache() at
# shutdown.
_CLIENT: redis_asyncio.Redis | None = None


async def init_cache(url: str) -> None:
    """Connect + ping. If url is empty or Redis is
    unreachable, log and leave _CLIENT as None --
    callers degrade transparently."""
    global _CLIENT
    if not url:
        logger.info("cache disabled (TOWNSFOLK_REDIS_URL empty)")
        _CLIENT = None
        return
    try:
        client = redis_asyncio.from_url(url, decode_responses=True)
        await client.ping()
        _CLIENT = client
        logger.info("cache connected: %s", url)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cache init failed (%s); serving uncached", exc,
        )
        _CLIENT = None


async def close_cache() -> None:
    global _CLIENT
    if _CLIENT is not None:
        try:
            await _CLIENT.aclose()
        except Exception:  # noqa: BLE001
            pass
        _CLIENT = None


def is_ready() -> bool:
    """Used by /v1/health/dependencies + the metrics
    gauge so operators can see "cache up" at a
    glance."""
    return _CLIENT is not None


async def get_lookup(key: str) -> dict[str, Any] | None:
    """Returns the cached envelope as a dict, or None
    on miss / cache-down. Caller is responsible for
    re-hydrating into the Pydantic model."""
    if _CLIENT is None:
        return None
    try:
        raw = await _CLIENT.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache get failed: %s", exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        # Bad payload in the cache -- treat as miss
        # and let the next set() overwrite it.
        logger.warning("cache decode failed for %s: %s", key, exc)
        return None


async def set_lookup(
    key: str, value: dict[str, Any], ttl_seconds: int,
) -> None:
    """Best-effort write. Errors logged, never
    raised."""
    if _CLIENT is None:
        return
    try:
        await _CLIENT.set(key, json.dumps(value), ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache set failed: %s", exc)


def lookup_cache_key(
    *,
    phone_e164: str | None,
    ip: str | None,
    lat: float | None,
    lng: float | None,
    radius_km: float,
) -> str | None:
    """Build a deterministic cache key. Returns None
    when no cacheable input was supplied.

    For phones we use the E.164 form (caller passes
    the already-parsed value), so `4162000199` and
    `+1 416-200-0199` and `+14162000199` share a
    slot. For coords we round to 4 decimals
    (~11 metre resolution) so near-identical
    sequential clicks on a map share a slot.
    Radius is part of the key because the response
    body includes cities_within, which depends on
    it.
    """
    rkey = f"r{int(radius_km)}"
    if phone_e164 is not None:
        return f"lookup:phone:{phone_e164}:{rkey}"
    if ip is not None:
        return f"lookup:ip:{ip.strip()}:{rkey}"
    if lat is not None and lng is not None:
        return f"lookup:coords:{round(lat, 4)}:{round(lng, 4)}:{rkey}"
    return None
