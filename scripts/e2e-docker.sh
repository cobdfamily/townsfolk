#!/usr/bin/env bash
#
# townsfolk full-stack end-to-end test.
#
# Brings up postgis + townsfolk via compose, runs the
# gazetteer container to produce the two JSONs, runs
# the townsfolk-etl to load them into Postgres, then
# curls /v1/lookup against known inputs.
#
# Assumes both gazetteer:latest and townsfolk:latest
# are pullable (or already built locally). Use:
#
#   GAZETTEER_TAG=e2e-local TOWNSFOLK_TAG=e2e-local \
#     scripts/e2e-docker.sh
#
# to run against locally-built tags.
#
# Run-time: ~60-90s (gazetteer phones build is the
# slowest piece; pulls from CNAC + ISED + NRCan).

set -euo pipefail

cd "$(dirname "$0")/.."

TS_PORT="${TOWNSFOLK_HTTP_PORT:-8003}"

cleanup() {
  docker compose --profile jobs down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

ok() {
  echo "PASS: $*"
}

echo "== bring up postgis + townsfolk"
docker compose up -d postgis-townsfolk townsfolk >/dev/null
# Wait for the FastAPI app to answer.
for i in $(seq 1 30); do
  if curl -fsS "http://localhost:${TS_PORT}/" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://localhost:${TS_PORT}/" >/dev/null || fail "townsfolk did not come up"
ok "townsfolk + postgis healthy"

echo "== run gazetteer phones build"
docker compose run --rm gazetteer phones build >/dev/null
docker run --rm -v gazetteer-data:/data alpine \
  sh -c '[ -s /data/telephone-location-data.json ]' \
  || fail "telephone-location-data.json missing or empty"
ok "phones JSON in shared volume"

echo "== run gazetteer places build (best-effort)"
docker compose run --rm gazetteer places build >/dev/null 2>&1 || {
  echo "WARN: places build failed (likely StatCan URL drift)."
  echo "      Continuing without places data; lookup will still"
  echo "      resolve phone -> point but cities_within may be empty."
}

echo "== seed an empty IP-ranges CSV so the ETL doesn't skip the table"
docker run --rm -v gazetteer-data:/data alpine \
  sh -c 'touch /data/dbip-city-lite.csv'

echo "== run townsfolk-etl"
docker compose run --rm townsfolk-etl \
  --phones-json /data/telephone-location-data.json \
  --places-json /data/canadian-places.json \
  --dbip-csv /data/dbip-city-lite.csv \
  >/dev/null
ok "etl loaded"

echo "== /v1/data-version reflects the load"
ver=$(curl -fsS "http://localhost:${TS_PORT}/v1/data-version")
echo "$ver" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); assert b['ok']; \
   assert b['tables']['exchanges']['rows'] > 20000; \
   print('exchanges:', b['tables']['exchanges']['rows'])"
ok "data-version reports rows"

echo "== /v1/lookup phone (Toronto, ON)"
# 416-200 is a real Toronto exchange in CNAC data
# (Bell Mobility). 555 prefixes are reserved for
# fiction/test and aren't reliably published, so
# pick a known-real NPA+NXX. URL-encode the + as
# %2B since curl treats bare + as a space in query
# strings.
toronto=$(curl -fsS "http://localhost:${TS_PORT}/v1/lookup?phone=%2B14162000199")
echo "$toronto" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); \
   assert b['match']['kind']=='phone'; \
   assert b['match']['province']=='ON'; \
   print('point:', b['point']['lat'], b['point']['lng'])"
ok "phone lookup -> ON"

echo "== /v1/lookup phone (non-CA -> Bowen Island fallback)"
# 212 is NYC, never a Canadian NPA, so this should
# always hit the non-CA branch. NXX is irrelevant
# for the fallback path.
fallback=$(curl -fsS "http://localhost:${TS_PORT}/v1/lookup?phone=%2B12120000199")
echo "$fallback" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); \
   assert b['match']['kind']=='fallback'; \
   assert 'Bowen' in b['match']['fallback_city']"
ok "non-CA phone -> Bowen Island fallback"

echo "== /v1/exchanges/204/200 (direct)"
direct=$(curl -fsS "http://localhost:${TS_PORT}/v1/exchanges/204/200")
echo "$direct" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); \
   assert b['npa']=='204'; assert b['nxx']=='200'; \
   assert b['province']=='MB'"
ok "direct exchange lookup"

echo "== X-Request-ID echoed"
rid=$(curl -fsS -H 'X-Request-ID: trace-abc-123' \
  "http://localhost:${TS_PORT}/" -i | tr -d '\r' | grep -i '^x-request-id:')
echo "$rid" | grep -q "trace-abc-123" \
  || fail "X-Request-ID not echoed: $rid"
ok "request-id middleware"

echo
echo "all townsfolk full-stack E2E assertions passed"
