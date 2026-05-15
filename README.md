# townsfolk

[![license](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

Location lookup service. Lives at
`https://location.openapis.ca/v1/lookup`. One
endpoint, three input modes, one envelope:

```
GET /v1/lookup?phone=+14165550199
GET /v1/lookup?ip=24.84.123.45
GET /v1/lookup?lat=43.6&lng=-79.4
```

All three resolve to a point, then return the cities
within `radius_km` (default 100, hard ceiling 500).

## Architecture

Three nightly inputs, one PostGIS, one FastAPI:

```
firehose build  -> telephone-location-data.json  ┐
gazetteer build -> canadian-places.json          ├─> ETL -> PostGIS
curl db-ip lite -> dbip-city-lite.csv            ┘             |
                                                               v
                       Traefik -> townsfolk (FastAPI) <--------+
                              location.openapis.ca/v1/lookup
```

The service itself owns no source data -- it ingests
the outputs of `firehose` (phone) and `gazetteer`
(cities) plus the db-ip lite IP feed. ETL runs
TRUNCATE + bulk-load, no migrations.

## Run locally

```sh
docker compose up -d
# wait for postgis healthcheck, then in another shell:
TOWNSFOLK_FIREHOSE_JSON=/path/to/telephone-location-data.json \
TOWNSFOLK_GAZETTEER_JSON=/path/to/canadian-places.json \
TOWNSFOLK_DBIP_CITY_LITE=/path/to/dbip-city-lite.csv \
DATABASE_URL=postgresql://townsfolk:changeme-dev-db@localhost:5432/townsfolk \
  uv run python scripts/load.py
curl http://localhost:8003/v1/lookup?phone=+14165550199 | jq
```

Auto-docs at <http://localhost:8003/docs>.

## Endpoints

| Method | Path           | Description                                          |
| ------ | -------------- | ---------------------------------------------------- |
| GET    | `/`            | Liveness.                                            |
| GET    | `/v1/health`   | Self + DB pool health.                               |
| GET    | `/v1/lookup`   | Phone / IP / coords -> point + cities within radius. |

## Behaviour

| Input            | What happens                                                                                                                |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `phone=`         | Parsed to NANP; NPA must be Canadian. Non-CA NPAs fall back to **Bowen Island, BC** (49.385, -123.358) -- design decision. |
| `ip=`            | Lookup against db-ip lite ranges (PostGIS `inet` containment).                                                              |
| `lat=&lng=`      | Used as the query point directly.                                                                                           |

Every response carries the same `LookupResponse`
envelope: `input`, `point`, `match`, `radius_km`,
`cities_within[]`.

## Config

| Env var                      | Default                                          |
| ---------------------------- | ------------------------------------------------ |
| `DATABASE_URL`               | `postgresql://townsfolk:townsfolk@localhost:5432/townsfolk` |
| `TOWNSFOLK_FALLBACK_LAT`     | `49.385` (Bowen Island)                          |
| `TOWNSFOLK_FALLBACK_LNG`     | `-123.358` (Bowen Island)                        |
| `TOWNSFOLK_RADIUS_DEFAULT_KM`| `100`                                            |
| `TOWNSFOLK_RADIUS_CEILING_KM`| `500`                                            |

## Status

v0.1 -- scaffold. Service compiles, schema valid,
ETL has the shape. Real-data smoke tests + Traefik
integration land in v0.2.
