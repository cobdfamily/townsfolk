"""Runtime configuration for townsfolk.

Reads TOWNSFOLK_* env vars (and the standard
DATABASE_URL) into a typed Settings singleton. The
service is small enough to keep all knobs in one
file -- no nested namespaces.

Deployment shape: behind Traefik in the same docker-
compose as the PostGIS container, so DATABASE_URL is
the container hostname (postgres-townsfolk:5432
typical) and the service trusts whatever network
it's on.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    """Single source of truth for everything operator-
    controllable. Built once from env at boot."""

    database_url: str
    bowen_island_lat: float
    bowen_island_lng: float
    radius_default_km: float
    radius_ceiling_km: float
    # Empty string disables the cache entirely; the
    # service still serves, just slower. Set to the
    # Redis URL (typically redis://redis-townsfolk:
    # 6379/0 inside docker-compose).
    redis_url: str
    # 1 hour by default. ETL is nightly TRUNCATE +
    # reload, so a 1h TTL means stale data is bounded
    # by ~1h post-ETL. Bump higher (24h, 7d) once the
    # ETL bumps an explicit cache-bust on completion.
    cache_ttl_seconds: int


def load() -> Config:
    return Config(
        database_url=os.environ.get(
            "DATABASE_URL",
            "postgresql://townsfolk:townsfolk@localhost:5432/townsfolk",
        ),
        # Bowen Island, BC -- the sane fallback for
        # non-CA phone numbers. Coordinates picked at
        # the village centre (Snug Cove ferry
        # terminal area). Operator can override via
        # env if they want a different "I don't know"
        # answer.
        bowen_island_lat=float(
            os.environ.get("TOWNSFOLK_FALLBACK_LAT", "49.385"),
        ),
        bowen_island_lng=float(
            os.environ.get("TOWNSFOLK_FALLBACK_LNG", "-123.358"),
        ),
        radius_default_km=float(
            os.environ.get("TOWNSFOLK_RADIUS_DEFAULT_KM", "100"),
        ),
        # Hard ceiling per the design discussion --
        # protects against scrape-by-one-query abuse.
        # Bumping past this would let a caller pull
        # most of southern Ontario or the entire
        # Lower Mainland in a single hit.
        radius_ceiling_km=float(
            os.environ.get("TOWNSFOLK_RADIUS_CEILING_KM", "500"),
        ),
        redis_url=os.environ.get("TOWNSFOLK_REDIS_URL", ""),
        cache_ttl_seconds=int(
            os.environ.get("TOWNSFOLK_CACHE_TTL_SECONDS", "3600"),
        ),
    )
