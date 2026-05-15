"""PostGIS queries for the three resolver paths plus
the shared cities-within-radius query.

Connection management uses a single asyncpg pool
created at app startup. The schema is loaded
separately by the ETL job (see scripts/load.py) so
the service code doesn't need to know about CREATE
TABLE; it just queries what's there.

Schema assumed (created by scripts/schema.sql):

  exchanges      npa text, nxx text, exchange_area
                 text, province text, carrier text,
                 point geography(Point), PRIMARY KEY
                 (npa, nxx)

  places         id text PRIMARY KEY, name text,
                 province text, point geography(Point),
                 population int NULL, concept_type
                 text NULL

  ip_ranges      id bigserial PRIMARY KEY, start_ip
                 inet, end_ip inet, city text,
                 province text, country text,
                 point geography(Point), accuracy_radius
                 numeric NULL, range inet4range GENERATED

  All three carry a GiST index on point /
  inet4range. Refresh = TRUNCATE + bulk-load nightly.
"""

from __future__ import annotations

from typing import Any

import asyncpg


_POOL: asyncpg.Pool | None = None


async def init_pool(database_url: str) -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        _POOL = await asyncpg.create_pool(database_url, min_size=1, max_size=8)
    return _POOL


async def close_pool() -> None:
    global _POOL
    if _POOL is not None:
        await _POOL.close()
        _POOL = None


def pool() -> asyncpg.Pool:
    if _POOL is None:
        raise RuntimeError("db pool not initialised; call init_pool first")
    return _POOL


async def lookup_exchange(npa: str, nxx: str) -> dict[str, Any] | None:
    """Match a NANP NPA+NXX against the firehose-loaded
    exchanges table. Returns the matched row or None."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT npa, nxx, exchange_area, province, carrier,
                   ST_Y(point::geometry) AS lat,
                   ST_X(point::geometry) AS lng
            FROM exchanges
            WHERE npa = $1 AND nxx = $2
            """,
            npa,
            nxx,
        )
        return dict(row) if row else None


async def lookup_ip(ip: str) -> dict[str, Any] | None:
    """Find the IP range containing the supplied
    address. The `range` column is a generated
    inet4range / inet6range with a GiST index, so this
    is an indexed lookup."""
    async with pool().acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT city, province, country, accuracy_radius,
                   ST_Y(point::geometry) AS lat,
                   ST_X(point::geometry) AS lng
            FROM ip_ranges
            WHERE range @> $1::inet
            LIMIT 1
            """,
            ip,
        )
        return dict(row) if row else None


async def cities_within(
    lat: float, lng: float, radius_km: float, limit: int = 100,
) -> list[dict[str, Any]]:
    """Geographic radius query against the places
    table. Distance is in metres internally (geography
    type); we feed km * 1000 to ST_DWithin and return
    km in the output for caller convenience.
    """
    async with pool().acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT name, province, population, concept_type,
                   ST_Y(point::geometry) AS lat,
                   ST_X(point::geometry) AS lng,
                   ST_Distance(
                       point,
                       ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography
                   ) / 1000.0 AS distance_km
            FROM places
            WHERE ST_DWithin(
                point,
                ST_SetSRID(ST_MakePoint($2, $1), 4326)::geography,
                $3
            )
            ORDER BY distance_km
            LIMIT $4
            """,
            lat,
            lng,
            radius_km * 1000.0,
            limit,
        )
        return [dict(r) for r in rows]
