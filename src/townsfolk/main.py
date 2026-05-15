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

from fastapi import FastAPI, HTTPException, Query

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
from .models import (
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
    """
    cfg: Config = load()
    app.state.config = cfg
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


@app.get("/v1/lookup", response_model=LookupResponse, tags=["Lookup"])
async def lookup(
    phone: str | None = Query(default=None),
    ip: str | None = Query(default=None),
    lat: float | None = Query(default=None),
    lng: float | None = Query(default=None),
    radius_km: float | None = Query(default=None, ge=0),
) -> LookupResponse:
    """Three input modes; one envelope. Exactly one of
    phone, ip, or (lat+lng) must be supplied.
    """
    cfg: Config = app.state.config
    if not app.state.db_ready:
        raise HTTPException(503, "database not initialised")

    # Validate input shape -- exactly one mode.
    modes_set = sum([phone is not None, ip is not None, lat is not None or lng is not None])
    if modes_set != 1:
        raise HTTPException(
            400,
            "supply exactly one of phone=, ip=, or lat= + lng=",
        )
    if (lat is None) != (lng is None):
        raise HTTPException(400, "lat and lng must be supplied together")

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
            raise HTTPException(
                404,
                f"no exchange for {parsed.npa}-{parsed.nxx}",
            )
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
            raise HTTPException(404, f"no IP range covers {ip!r}")
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
    cities = await cities_within(lat, lng, radius)
    return LookupResponse(
        input=LookupInput(kind="coords", value=f"{lat},{lng}"),
        point=LookupPoint(lat=lat, lng=lng, source="input"),
        match=CoordsMatch(),
        radius_km=radius,
        cities_within=_to_cities(cities),
    )


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
