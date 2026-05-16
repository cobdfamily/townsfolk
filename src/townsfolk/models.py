"""Public response shapes for /v1/lookup.

The envelope is the same regardless of input mode --
only the `input` and `match` blocks change. Callers
get a uniform shape they can parse without branching
on which lookup mode they used.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LookupPoint(BaseModel):
    lat: float
    lng: float
    # "phone" / "ip" / "input" -- where this point
    # came from. Lets the caller distinguish
    # "geocoded from input coords (exact)" from
    # "best guess based on phone exchange (city-
    # level)".
    source: Literal["phone", "ip", "input", "fallback"]


class CityWithin(BaseModel):
    name: str
    province: str
    lat: float
    lng: float
    distance_km: float
    # null when the catalog doesn't know the
    # population (CGN-only entry, no StatCan match).
    population: int | None = None
    concept_type: str | None = None


class PhoneMatch(BaseModel):
    kind: Literal["phone"] = "phone"
    npa: str
    nxx: str
    exchange_area: str
    province: str
    carrier: str | None = None


class IpMatch(BaseModel):
    kind: Literal["ip"] = "ip"
    ip: str
    city: str | None = None
    province: str | None = None
    country: str
    # Reported accuracy radius from db-ip; null when
    # the source didn't supply one.
    accuracy_radius_km: float | None = None


class CoordsMatch(BaseModel):
    kind: Literal["coords"] = "coords"


class FallbackMatch(BaseModel):
    kind: Literal["fallback"] = "fallback"
    reason: str
    fallback_city: str


Match = PhoneMatch | IpMatch | CoordsMatch | FallbackMatch


class LookupInput(BaseModel):
    kind: Literal["phone", "ip", "coords"]
    value: str


class LookupResponse(BaseModel):
    input: LookupInput
    point: LookupPoint
    match: Match
    radius_km: float
    cities_within: list[CityWithin] = Field(default_factory=list)


# v0.3: bulk-mode shapes -------------------------------


class BatchLookupItem(BaseModel):
    """One row of a batch. Same per-input contract as
    the GET /v1/lookup query string: supply exactly
    one of phone / ip / (lat + lng). radius_km is
    per-item so a single batch can mix narrow and
    wide queries (e.g. an audit pass that wants
    different radii per call type)."""

    phone: str | None = None
    ip: str | None = None
    lat: float | None = None
    lng: float | None = None
    radius_km: float | None = None


class BatchLookupRequest(BaseModel):
    """The batch envelope. Items list is capped at
    100 per request (see config; matches the radius
    ceiling pattern -- protects against payload-
    amplification abuse)."""

    items: list[BatchLookupItem] = Field(default_factory=list)


class BatchLookupItemResult(BaseModel):
    """Per-row response. Either a successful
    LookupResponse OR an error envelope so a single
    bad row doesn't poison the whole batch (the way
    HTTP-status non-200s would). The endpoint stays
    200 OK as long as the request shape itself was
    valid."""

    ok: bool
    response: LookupResponse | None = None
    error: str | None = None


class BatchLookupResponse(BaseModel):
    results: list[BatchLookupItemResult]
