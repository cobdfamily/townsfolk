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
