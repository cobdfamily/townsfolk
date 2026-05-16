"""Nightly ETL.

Inputs (paths default to env vars; --flags override):
  TOWNSFOLK_PHONES_JSON       ./telephone-location-data.json
  TOWNSFOLK_PLACES_JSON       ./canadian-places.json
  TOWNSFOLK_DBIP_CITY_LITE    ./dbip-city-lite.csv

(The older TOWNSFOLK_FIREHOSE_JSON / TOWNSFOLK_
GAZETTEER_JSON env names are honored as fallbacks
for cron operators who haven't migrated yet.)

Database:
  DATABASE_URL                same as the service

Flow (idempotent, safe to re-run):
  1. Apply schema.sql (CREATE TABLE IF NOT EXISTS;
     also CREATE EXTENSION postgis).
  2. TRUNCATE all three tables.
  3. Bulk-load each from its JSON / CSV.
  4. ANALYZE so the planner has stats.

The ETL is a separate process from the service so a
load failure doesn't crash the running webserver --
the service keeps serving against the previous
generation of data until the next successful ETL.

Run order:
  gazetteer phones build      -> ./telephone-location-data.json
  gazetteer places build      -> ./canadian-places.json
  curl db-ip lite             -> ./dbip-city-lite.csv
  uv run townsfolk-load       -> bulk-load + analyze
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import asyncpg


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database-url",
        default=os.environ.get(
            "DATABASE_URL",
            "postgresql://townsfolk:townsfolk@localhost:5432/townsfolk",
        ),
    )
    parser.add_argument(
        "--phones-json",
        default=os.environ.get(
            "TOWNSFOLK_PHONES_JSON",
            os.environ.get(
                "TOWNSFOLK_FIREHOSE_JSON",
                "./telephone-location-data.json",
            ),
        ),
    )
    parser.add_argument(
        "--places-json",
        default=os.environ.get(
            "TOWNSFOLK_PLACES_JSON",
            os.environ.get(
                "TOWNSFOLK_GAZETTEER_JSON",
                "./canadian-places.json",
            ),
        ),
    )
    parser.add_argument(
        "--dbip-csv",
        default=os.environ.get(
            "TOWNSFOLK_DBIP_CITY_LITE", "./dbip-city-lite.csv",
        ),
    )
    args = parser.parse_args()

    schema_path = Path(__file__).parent / "schema.sql"
    schema_sql = schema_path.read_text()

    conn = await asyncpg.connect(args.database_url)
    try:
        # 1. Schema (no-op when already present).
        await conn.execute(schema_sql)

        # 2. Reset.
        await conn.execute(
            "TRUNCATE exchanges, places, ip_ranges RESTART IDENTITY",
        )

        # 3a. gazetteer phones -> exchanges
        if Path(args.phones_json).exists():
            await _load_exchanges(conn, args.phones_json)

        # 3b. gazetteer places -> places
        if Path(args.places_json).exists():
            await _load_places(conn, args.places_json)

        # 3c. db-ip -> ip_ranges
        if Path(args.dbip_csv).exists():
            await _load_ip_ranges(conn, args.dbip_csv)

        # 4. Stats for the planner.
        await conn.execute("ANALYZE exchanges, places, ip_ranges")
    finally:
        await conn.close()


async def _load_exchanges(conn: asyncpg.Connection, path: str) -> None:
    """`gazetteer phones` JSON is an array; each record
    carries a GeoJSON Point with coordinates:
    [lng, lat]."""
    data = json.loads(Path(path).read_text())
    rows = []
    for r in data:
        coords = (r.get("location") or {}).get("coordinates") or [None, None]
        if not coords[0] or not coords[1]:
            continue
        rows.append(
            (
                str(r.get("npa") or ""),
                str(r.get("nxx") or ""),
                r.get("exchangeArea") or "",
                r.get("region") or "",
                r.get("company"),
                f"POINT({coords[0]} {coords[1]})",
            ),
        )
    await conn.executemany(
        """
        INSERT INTO exchanges (npa, nxx, exchange_area, province, carrier, point)
        VALUES ($1, $2, $3, $4, $5, ST_SetSRID($6::geography, 4326))
        ON CONFLICT (npa, nxx) DO NOTHING
        """,
        rows,
    )


async def _load_places(conn: asyncpg.Connection, path: str) -> None:
    """`gazetteer places` JSON shape -- see cli/gazetteer."""
    data = json.loads(Path(path).read_text())
    rows = []
    for r in data:
        lat = r.get("latitude") or 0.0
        lng = r.get("longitude") or 0.0
        if not lat or not lng:
            continue
        rows.append(
            (
                r.get("_id") or "",
                r.get("name") or "",
                r.get("province") or "",
                f"POINT({lng} {lat})",
                r.get("population"),
                r.get("conceptType"),
                r.get("cgnId"),
                r.get("sgcCode"),
            ),
        )
    await conn.executemany(
        """
        INSERT INTO places
          (id, name, province, point, population, concept_type, cgn_id, sgc_code)
        VALUES ($1, $2, $3, ST_SetSRID($4::geography, 4326), $5, $6, $7, $8)
        ON CONFLICT (id) DO NOTHING
        """,
        rows,
    )


async def _load_ip_ranges(conn: asyncpg.Connection, path: str) -> None:
    """db-ip city-lite CSV. Columns (positional, no
    header):
      0 start_ip, 1 end_ip, 2 continent, 3 country_iso,
      4 region, 5 city, 6 latitude, 7 longitude.

    Loads via Postgres COPY (binary text-CSV mode)
    instead of executemany -- the city-lite file is
    ~3M rows and executemany would take 10+ minutes;
    COPY pushes it through in ~30 seconds. We filter
    and reorder the columns in Python, then stream
    the result into COPY as in-memory CSV. inet and
    geography parsing both happen server-side: the
    `point` column accepts plain WKT text and
    defaults to SRID 4326 for the geography type.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    with Path(path).open() as fh:
        reader = csv.reader(fh)
        for row in reader:
            if len(row) < 8:
                continue
            try:
                lat = float(row[6])
                lng = float(row[7])
            except ValueError:
                continue
            writer.writerow(
                [
                    row[0],
                    row[1],
                    row[5] or "",
                    row[4] or "",
                    row[3] or "??",
                    f"POINT({lng} {lat})",
                ],
            )

    # asyncpg's copy_to_table treats a bytes/str
    # `source` as a path, so wrap the buffered CSV
    # in a BytesIO and pass the file-like instead.
    binbuf = io.BytesIO(buf.getvalue().encode("utf-8"))
    await conn.copy_to_table(
        "ip_ranges",
        source=binbuf,
        columns=["start_ip", "end_ip", "city", "province", "country", "point"],
        format="csv",
    )


def run() -> None:
    """Entry point: `python -m townsfolk.load` or
    `uv run python scripts/load.py`."""
    asyncio.run(main())


if __name__ == "__main__":
    run()
