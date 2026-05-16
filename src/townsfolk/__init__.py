"""townsfolk -- location lookup service.

Surface:

  GET    /                  liveness
  GET    /v1/health         aggregated (self + DB)
  GET    /v1/lookup         the only real endpoint
                            ?phone=+1NPANXXXXXX
                            ?ip=A.B.C.D
                            ?lat=X.X&lng=Y.Y
                            [radius_km=100, max 500]

Lives at location.openapis.ca behind Traefik; the
backend is network-trusted (no per-token auth).
PostGIS sibling container holds three tables loaded
nightly by an ETL job:

  exchanges      from firehose JSON -- NPA+NXX ->
                 city + point
  places         from gazetteer JSON -- full
                 Canadian settlement catalog with
                 population
  ip_ranges      from db-ip lite -- IP block ->
                 representative city + point

Each lookup mode resolves the input to a single point,
then runs `places ST_DWithin point :radius` to return
the cities-within-radius. Non-Canadian phone numbers
fall back to Bowen Island (49.385, -123.358) -- by
design, a sane default for our deployment.
"""

__version__ = "0.4.0"
