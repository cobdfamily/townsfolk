-- townsfolk schema. Three tables, each refreshed
-- nightly by the ETL job. No mutable state -- a
-- failed mid-load is recoverable by re-running the
-- ETL; we don't migrate, we truncate + reload.

CREATE EXTENSION IF NOT EXISTS postgis;

-- Phone exchanges. Loaded from firehose's
-- telephone-location-data.json (one row per NPA+NXX
-- that resolved to a city).
CREATE TABLE IF NOT EXISTS exchanges (
    npa            text NOT NULL,
    nxx            text NOT NULL,
    exchange_area  text NOT NULL,
    province       text NOT NULL,
    carrier        text,
    point          geography(Point, 4326) NOT NULL,
    PRIMARY KEY (npa, nxx)
);
CREATE INDEX IF NOT EXISTS exchanges_point_gist
    ON exchanges USING GIST (point);


-- Place catalog. Loaded from gazetteer's
-- canadian-places.json.
CREATE TABLE IF NOT EXISTS places (
    id            text PRIMARY KEY,
    name          text NOT NULL,
    province      text NOT NULL,
    point         geography(Point, 4326) NOT NULL,
    population    int,
    concept_type  text,
    cgn_id        text,
    sgc_code      text
);
CREATE INDEX IF NOT EXISTS places_point_gist
    ON places USING GIST (point);
CREATE INDEX IF NOT EXISTS places_name_lower_idx
    ON places (LOWER(name), province);


-- IP ranges. Loaded from db-ip lite. Stored as plain
-- inet pairs; containment is queried as
-- `WHERE start_ip <= $1 AND end_ip >= $1`. db-ip
-- ships separate v4 and v6 files; both feed this one
-- table.
--
-- We deliberately avoid the `ip4r` extension's
-- `inetrange` + GiST index trick because postgis/
-- postgis:16-3.4 (the base image we ship) doesn't
-- include ip4r. A B-tree on (start_ip, end_ip) is
-- good enough for the db-ip lite scale (~3M ranges).
CREATE TABLE IF NOT EXISTS ip_ranges (
    id              bigserial PRIMARY KEY,
    start_ip        inet NOT NULL,
    end_ip          inet NOT NULL,
    city            text,
    province        text,
    country         text NOT NULL,
    point           geography(Point, 4326) NOT NULL,
    accuracy_radius numeric
);
-- B-tree on start_ip alone is enough: the lookup
-- pattern is "find the row whose start_ip is the
-- largest <= queried ip, then verify end_ip >= ip".
-- See db.py::lookup_ip.
CREATE INDEX IF NOT EXISTS ip_ranges_start_ip_idx
    ON ip_ranges (start_ip);
