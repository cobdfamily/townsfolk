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
# Run-time: ~2-3 min (gazetteer phones build +
# db-ip-city-lite COPY load are the slowest pieces;
# both pull from real upstream feeds).

set -euo pipefail

cd "$(dirname "$0")/.."

TS_PORT="${TOWNSFOLK_HTTP_PORT:-8003}"
TMPDIR="$(mktemp -d)"

cleanup() {
  docker compose --profile jobs down -v >/dev/null 2>&1 || true
  rm -rf "$TMPDIR"
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

echo "== fetch db-ip-city-lite (current month, gz)"
# db-ip publishes the CSV gz monthly; the URL embeds
# the current year-month. They keep the previous
# month available too, so we fall back to last month
# if the current release isn't out yet (releases drop
# on the 1st but propagate to the CDN over the day).
MONTH="$(date -u +%Y-%m)"
DBIP_URL="https://download.db-ip.com/free/dbip-city-lite-${MONTH}.csv.gz"
if ! curl -fsSLo "$TMPDIR/dbip.csv.gz" "$DBIP_URL"; then
  # date -v works on BSD/macOS, GNU date wants -d.
  PREV="$(date -u -v-1m +%Y-%m 2>/dev/null \
    || date -u -d 'last month' +%Y-%m)"
  DBIP_URL="https://download.db-ip.com/free/dbip-city-lite-${PREV}.csv.gz"
  curl -fsSLo "$TMPDIR/dbip.csv.gz" "$DBIP_URL" \
    || fail "could not fetch db-ip-city-lite from $DBIP_URL"
fi
ok "db-ip-city-lite downloaded ($(du -h $TMPDIR/dbip.csv.gz | awk '{print $1}'))"

echo "== decompress dbip-city-lite into shared volume"
# Pipe through an alpine container: the gz lives on
# the host TMPDIR, the decompressed CSV needs to land
# in the gazetteer-data named volume the ETL reads
# from. Saves us a host-side gunzip dep on whatever
# machine runs the script.
docker run --rm -i -v gazetteer-data:/out alpine \
  sh -c 'gunzip -c > /out/dbip-city-lite.csv' \
  < "$TMPDIR/dbip.csv.gz"
docker run --rm -v gazetteer-data:/out alpine \
  sh -c '[ -s /out/dbip-city-lite.csv ]' \
  || fail "decompressed dbip-city-lite.csv empty"
ok "dbip-city-lite.csv in shared volume"

echo "== run townsfolk-etl"
# COPY-based loader: ~30s for the full ~3M-row db-ip
# file. executemany would push this past 10 min.
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
   assert b['tables']['places']['rows'] > 20000; \
   assert b['tables']['ip_ranges']['rows'] > 1000000; \
   print('rows:', \
         'exchanges=' + str(b['tables']['exchanges']['rows']), \
         'places=' + str(b['tables']['places']['rows']), \
         'ip_ranges=' + str(b['tables']['ip_ranges']['rows']))"
ok "data-version reports rows across all three tables"

echo "== /v1/lookup phone (Toronto, ON) + cities_within"
# 416-200 is a real Toronto exchange in CNAC data
# (Bell Mobility). 555 prefixes are reserved for
# fiction/test and aren't reliably published, so
# pick a known-real NPA+NXX. URL-encode the + as
# %2B since curl treats bare + as a space in query
# strings.
#
# Also: with places now loaded, cities_within
# should return real GTA municipalities for a
# 100km Toronto query (Mississauga, Brampton,
# Hamilton, Oshawa, etc.). The assertion is loose
# (>= 10) because CGN merges + place granularity
# make an exact count brittle.
toronto=$(curl -fsS "http://localhost:${TS_PORT}/v1/lookup?phone=%2B14162000199")
echo "$toronto" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); \
   assert b['match']['kind']=='phone'; \
   assert b['match']['province']=='ON'; \
   n = len(b['cities_within']); \
   assert n >= 10, f'expected >= 10 cities within 100km of Toronto, got {n}'; \
   print('point:', b['point']['lat'], b['point']['lng'], 'cities_within:', n)"
ok "phone lookup -> ON + cities_within populated"

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

echo "== /v1/lookup ip (8.8.8.8 -> US)"
# Google Public DNS. db-ip-city-lite has resolved
# this to US/Mountain View (or US/various depending
# on the month) since 2018. Asserting on country=US
# is stable; city varies between releases.
ipres=$(curl -fsS "http://localhost:${TS_PORT}/v1/lookup?ip=8.8.8.8")
echo "$ipres" | python3 -c \
  "import sys,json; b=json.load(sys.stdin); \
   assert b['match']['kind']=='ip'; \
   assert b['match']['country']=='US', \
     f'expected US, got {b[\"match\"][\"country\"]!r}'; \
   print('ip:', b['match']['ip'], 'city:', b['match'].get('city'), \
         'country:', b['match']['country'])"
ok "ip lookup -> US"

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
