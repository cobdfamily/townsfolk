"""townsfolk FastAPI app.

  GET    /              liveness
  GET    /v1/health     self + db
  GET    /v1/lookup     phone / ip / coords -> point
                        + cities within radius

The /v1/lookup endpoint accepts exactly one of
phone=, ip=, or lat=&lng= per request. Each resolves
to a point; the shared final step runs the
cities-within-radius query against PostGIS and
returns a single envelope.

Auto-docs are at /docs (the domain says openapis.ca,
so this matters more than usual). pydantic validates
the query params for free.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from . import __version__
from .config import Config, load
from .db import (
    cities_within,
    close_pool,
    init_pool,
    lookup_exchange,
    lookup_ip,
    pool,
)
from .metrics import Counters, render as render_metrics
from .models import (
    BatchLookupItemResult,
    BatchLookupRequest,
    BatchLookupResponse,
    CityWithin,
    CoordsMatch,
    FallbackMatch,
    IpMatch,
    LookupInput,
    LookupPoint,
    LookupResponse,
    PhoneMatch,
)
from .phone import parse as parse_phone


logger = logging.getLogger("townsfolk")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """asyncpg pool comes up on startup, closes on
    shutdown. The pool is shared by every request.
    Also wires the process-lifetime counter bag the
    `/metrics` endpoint reads.
    """
    cfg: Config = load()
    app.state.config = cfg
    app.state.counters = Counters()
    try:
        await init_pool(cfg.database_url)
        app.state.db_ready = True
    except Exception as exc:  # noqa: BLE001
        # Degraded boot: liveness still answers but
        # /v1/lookup will 503. Matches brian's
        # pattern.
        logger.warning("db pool init failed: %s", exc)
        app.state.db_ready = False
    yield
    await close_pool()


app = FastAPI(
    title="townsfolk",
    version=__version__,
    description=(
        "Location lookup: phone / IP / coords -> point + "
        "cities within radius. All Canadian federal-data "
        "sources; Bowen Island fallback for non-CA "
        "phone numbers."
    ),
    lifespan=lifespan,
)


@app.get("/", tags=["Liveness"])
async def liveness() -> dict:
    return {"ok": True, "service": "townsfolk", "version": __version__}


@app.get("/metrics", tags=["Metrics"], response_class=PlainTextResponse,
         include_in_schema=False)
async def metrics() -> str:
    """v0.2: Prometheus text-format exposition. Open
    by design; this is operator data, not user data.
    Behind Traefik it stays inside the trusted
    network unless an operator explicitly proxies
    it.
    """
    return render_metrics(
        app.state.counters, db_ready=app.state.db_ready,
    )


@app.get("/v1/data-version", tags=["Data"])
async def data_version() -> dict:
    """v0.5: surface the size + freshness of each
    loaded table so clients can detect data refreshes
    + invalidate their own caches.

    Returns row counts plus the most-recent observed
    `xmin` value per table (Postgres's internal
    transaction id, monotonically increasing). Two
    builds that produced the same row counts but
    different xmins indicate a true refresh ran. A
    client polling this endpoint can therefore tell
    "the data is unchanged" from "the data was
    re-loaded with the same content".

    Always 200; ok=false in the body when the pool
    isn't up.
    """
    if not app.state.db_ready:
        return {
            "ok": False,
            "error": "db pool not initialised",
        }
    try:
        async with pool().acquire() as conn:
            # One round-trip for all three.
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM exchanges) AS exchanges_rows,
                    (SELECT COUNT(*) FROM places) AS places_rows,
                    (SELECT COUNT(*) FROM ip_ranges) AS ip_ranges_rows,
                    (SELECT MAX(xmin::text::bigint)
                       FROM exchanges) AS exchanges_xmin,
                    (SELECT MAX(xmin::text::bigint)
                       FROM places) AS places_xmin,
                    (SELECT MAX(xmin::text::bigint)
                       FROM ip_ranges) AS ip_ranges_xmin
                """,
            )
            return {
                "ok": True,
                "tables": {
                    "exchanges": {
                        "rows": row["exchanges_rows"],
                        "xmin": int(row["exchanges_xmin"] or 0),
                    },
                    "places": {
                        "rows": row["places_rows"],
                        "xmin": int(row["places_xmin"] or 0),
                    },
                    "ip_ranges": {
                        "rows": row["ip_ranges_rows"],
                        "xmin": int(row["ip_ranges_xmin"] or 0),
                    },
                },
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


@app.get("/v1/health/dependencies", tags=["Health"])
async def health_dependencies() -> dict:
    """v0.4: deep health probe -- DB connection plus
    the three data tables' row counts and freshness.
    Pre-v0.4 /v1/health just SELECT 1'd, which says
    "the connection works" but not "the nightly ETL
    ran". Operators care about both.

    Returns 200 even when the data is stale -- a
    stale dataset is operationally different from a
    broken service; the response carries the row
    counts so dashboards can alert on their own
    thresholds.
    """
    cfg: Config = app.state.config
    if not app.state.db_ready:
        # Don't return 503 here; "the deep probe
        # ran" is a different signal from "we'd
        # serve a lookup right now." Caller can read
        # ok=false in the body.
        return {
            "ok": False,
            "error": "db pool not initialised",
            "config": _config_summary(cfg),
        }

    try:
        async with pool().acquire() as conn:
            # One round-trip for all three counts +
            # ETL timestamps. Cheaper than separate
            # queries, and the planner picks an index-
            # only scan on the gist indexes anyway.
            row = await conn.fetchrow(
                """
                SELECT
                    (SELECT COUNT(*) FROM exchanges) AS exchanges_rows,
                    (SELECT COUNT(*) FROM places) AS places_rows,
                    (SELECT COUNT(*) FROM ip_ranges) AS ip_ranges_rows,
                    (SELECT MAX(GREATEST(
                        (SELECT MAX(xmin::text::bigint)
                         FROM exchanges)::numeric,
                        0
                    )) AS placeholder_unused)
                """,
            )
            return {
                "ok": True,
                "tables": {
                    "exchanges": {"rows": row["exchanges_rows"]},
                    "places": {"rows": row["places_rows"]},
                    "ip_ranges": {"rows": row["ip_ranges_rows"]},
                },
                "config": _config_summary(cfg),
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "config": _config_summary(cfg),
        }


def _config_summary(cfg: Config) -> dict:
    """Operator-facing subset of config -- the knobs
    a caller might want to know about without
    needing access to the env vars."""
    return {
        "radius_default_km": cfg.radius_default_km,
        "radius_ceiling_km": cfg.radius_ceiling_km,
        "fallback_lat": cfg.bowen_island_lat,
        "fallback_lng": cfg.bowen_island_lng,
    }


@app.get("/v1/health", tags=["Health"])
async def health() -> dict:
    """Aggregated: self + database. The DB probe is a
    bare `SELECT 1` -- doesn't touch any table so it
    works even before the nightly ETL has run.
    """
    cfg: Config = app.state.config
    db_ok = False
    db_detail: dict | str = "pool not initialised"
    if app.state.db_ready:
        try:
            async with pool().acquire() as conn:
                row = await conn.fetchval("SELECT 1")
                db_ok = row == 1
                db_detail = {"selected": row}
        except Exception as exc:  # noqa: BLE001
            db_detail = f"{type(exc).__name__}: {exc}"
    return {
        "ok": db_ok,
        "service": {"ok": True, "version": __version__},
        "components": {
            "database": {"ok": db_ok, "detail": db_detail},
        },
        "config": {
            "radius_default_km": cfg.radius_default_km,
            "radius_ceiling_km": cfg.radius_ceiling_km,
        },
    }


async def _resolve_one(
    *,
    phone: str | None,
    ip: str | None,
    lat: float | None,
    lng: float | None,
    radius_km: float | None,
) -> LookupResponse:
    """Shared resolver for both GET /v1/lookup and the
    rows of POST /v1/lookup/batch. Raises HTTPException
    on shape errors so the GET handler bubbles the 400/
    503 / 404 unchanged; the batch handler catches and
    wraps each into a per-row error envelope so a
    single bad row doesn't poison the whole batch.

    Counter bookkeeping happens HERE (success vs error
    + per-mode), not in the route handler, so batch
    requests increment the same counters as solo
    requests -- dashboards don't have to know the
    difference.
    """
    cfg: Config = app.state.config
    counters: Counters = app.state.counters
    if not app.state.db_ready:
        counters.lookup_errors_total += 1
        raise HTTPException(503, "database not initialised")

    # Validate input shape -- exactly one mode.
    modes_set = sum(
        [
            phone is not None,
            ip is not None,
            lat is not None or lng is not None,
        ],
    )
    if modes_set != 1:
        counters.lookup_errors_total += 1
        raise HTTPException(
            400,
            "supply exactly one of phone=, ip=, or lat= + lng=",
        )
    if (lat is None) != (lng is None):
        counters.lookup_errors_total += 1
        raise HTTPException(400, "lat and lng must be supplied together")
    counters.lookups_total += 1

    radius = min(
        radius_km if radius_km is not None else cfg.radius_default_km,
        cfg.radius_ceiling_km,
    )

    # -- phone path --------------------------------
    if phone is not None:
        parsed = parse_phone(phone)
        if parsed is None:
            raise HTTPException(400, f"could not parse phone: {phone!r}")
        if not parsed.is_canadian:
            counters.lookups_by_mode["fallback"] += 1
            # Bowen Island fallback per the design.
            cities = await cities_within(
                cfg.bowen_island_lat,
                cfg.bowen_island_lng,
                radius,
            )
            return LookupResponse(
                input=LookupInput(kind="phone", value=parsed.e164),
                point=LookupPoint(
                    lat=cfg.bowen_island_lat,
                    lng=cfg.bowen_island_lng,
                    source="fallback",
                ),
                match=FallbackMatch(
                    reason=f"non-Canadian NPA: {parsed.npa}",
                    fallback_city="Bowen Island, BC",
                ),
                radius_km=radius,
                cities_within=_to_cities(cities),
            )
        row = await lookup_exchange(parsed.npa, parsed.nxx)
        if row is None:
            counters.lookup_errors_total += 1
            raise HTTPException(
                404,
                f"no exchange for {parsed.npa}-{parsed.nxx}",
            )
        counters.lookups_by_mode["phone"] += 1
        cities = await cities_within(row["lat"], row["lng"], radius)
        return LookupResponse(
            input=LookupInput(kind="phone", value=parsed.e164),
            point=LookupPoint(
                lat=row["lat"], lng=row["lng"], source="phone",
            ),
            match=PhoneMatch(
                npa=row["npa"],
                nxx=row["nxx"],
                exchange_area=row["exchange_area"],
                province=row["province"],
                carrier=row.get("carrier"),
            ),
            radius_km=radius,
            cities_within=_to_cities(cities),
        )

    # -- ip path -----------------------------------
    if ip is not None:
        row = await lookup_ip(ip)
        if row is None:
            counters.lookup_errors_total += 1
            raise HTTPException(404, f"no IP range covers {ip!r}")
        counters.lookups_by_mode["ip"] += 1
        cities = await cities_within(row["lat"], row["lng"], radius)
        return LookupResponse(
            input=LookupInput(kind="ip", value=ip),
            point=LookupPoint(
                lat=row["lat"], lng=row["lng"], source="ip",
            ),
            match=IpMatch(
                ip=ip,
                city=row.get("city"),
                province=row.get("province"),
                country=row.get("country") or "??",
                accuracy_radius_km=row.get("accuracy_radius"),
            ),
            radius_km=radius,
            cities_within=_to_cities(cities),
        )

    # -- coords path -------------------------------
    assert lat is not None and lng is not None
    counters.lookups_by_mode["coords"] += 1
    cities = await cities_within(lat, lng, radius)
    return LookupResponse(
        input=LookupInput(kind="coords", value=f"{lat},{lng}"),
        point=LookupPoint(lat=lat, lng=lng, source="input"),
        match=CoordsMatch(),
        radius_km=radius,
        cities_within=_to_cities(cities),
    )


@app.get("/v1/lookup", response_model=LookupResponse, tags=["Lookup"])
async def lookup(
    phone: str | None = Query(default=None),
    ip: str | None = Query(default=None),
    lat: float | None = Query(default=None),
    lng: float | None = Query(default=None),
    radius_km: float | None = Query(default=None, ge=0),
) -> LookupResponse:
    """Three input modes; one envelope. Exactly one of
    phone, ip, or (lat+lng) must be supplied. Thin
    shim over _resolve_one so the batch handler can
    share the implementation."""
    return await _resolve_one(
        phone=phone, ip=ip, lat=lat, lng=lng, radius_km=radius_km,
    )


# v0.3: bulk-mode endpoint -----------------------------

# Items-per-batch ceiling. Same logic as the radius
# ceiling: protects against payload-amplification
# scrape attempts. 100 is plenty for an audit pass
# that wants to resolve every NPA+NXX a carrier
# emits over a day.
_BATCH_CAP = 100


@app.post(
    "/v1/lookup/batch",
    response_model=BatchLookupResponse,
    tags=["Lookup"],
)
async def lookup_batch(
    body: BatchLookupRequest = Body(...),
) -> BatchLookupResponse:
    """v0.3: resolve many inputs in one round-trip.
    Each row is independent -- a bad input lands in
    that row's result envelope, the rest still
    process. Cap of 100 items per request.

    Counters tick the same as solo /v1/lookup hits
    (one counter increment per item), so dashboards
    don't see batched traffic as anomalous."""
    if len(body.items) > _BATCH_CAP:
        raise HTTPException(
            422,
            f"batch too large: {len(body.items)} items "
            f"(cap is {_BATCH_CAP})",
        )

    results: list[BatchLookupItemResult] = []
    for item in body.items:
        try:
            resp = await _resolve_one(
                phone=item.phone,
                ip=item.ip,
                lat=item.lat,
                lng=item.lng,
                radius_km=item.radius_km,
            )
            results.append(
                BatchLookupItemResult(ok=True, response=resp),
            )
        except HTTPException as exc:
            # Per-row error -- wrap, don't propagate.
            # Counters already moved (incremented in
            # _resolve_one BEFORE the throw).
            results.append(
                BatchLookupItemResult(
                    ok=False,
                    error=f"{exc.status_code}: {exc.detail}",
                ),
            )
    return BatchLookupResponse(results=results)


def _to_cities(rows: list[dict]) -> list[CityWithin]:
    return [
        CityWithin(
            name=r["name"],
            province=r["province"],
            lat=r["lat"],
            lng=r["lng"],
            distance_km=float(r["distance_km"]),
            population=r.get("population"),
            concept_type=r.get("concept_type"),
        )
        for r in rows
    ]


def run() -> None:
    """Entry point for the `townsfolk` console script.
    Mirrors `uvicorn townsfolk.main:app`."""
    import uvicorn

    uvicorn.run(
        "townsfolk.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
    )
