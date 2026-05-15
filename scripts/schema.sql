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


-- IP ranges. Loaded from db-ip lite. We store the
-- ranges as inet pairs + a generated inet4range/
-- inet6range so the @> containment query uses a
-- GiST index. db-ip ships separate v4 and v6 files;
-- both feed this one table.
CREATE TABLE IF NOT EXISTS ip_ranges (
    id              bigserial PRIMARY KEY,
    start_ip        inet NOT NULL,
    end_ip          inet NOT NULL,
    range           inetrange GENERATED ALWAYS AS
                    (inetrange(start_ip, end_ip, '[]'))
                    STORED,
    city            text,
    province        text,
    country         text NOT NULL,
    point           geography(Point, 4326) NOT NULL,
    accuracy_radius numeric
);
-- Note: inetrange is provided by the `ip4r`
-- extension. If your PostGIS image doesn't carry
-- ip4r, swap this for `int8range` + a custom
-- ip-to-int8 cast at ingest time. The query
-- pattern stays the same shape.
CREATE INDEX IF NOT EXISTS ip_ranges_range_gist
    ON ip_ranges USING GIST (range);
