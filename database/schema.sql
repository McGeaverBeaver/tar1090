-- tar1090 database schema: aircraft registry + flights + time-series trail points.
--
-- Target: PostgreSQL 14+ with the TimescaleDB and PostGIS extensions.
-- Apply with:  psql "<your DSN>" -f schema.sql
--
-- This is an ADDITIVE component. It does not change anything about how readsb or
-- the tar1090 frontend work; the heatmap, ?replay and ?pTracks keep working as-is.
-- It is fed by database/tar1090-logger.py, which reads the same aircraft.json that
-- readsb already produces.

CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fast ILIKE '%term%' search (used by Grafana)

-- ---------------------------------------------------------------------------
-- aircraft: one slowly-changing row per ICAO 24-bit address (the "registry").
-- Populated from the metadata readsb injects into aircraft.json when run with
-- --db-file (r / t / desc / ownOp / year / dbFlags). Kept forever (no retention).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aircraft (
    icao_hex     text PRIMARY KEY,                  -- lowercase 24-bit ICAO hex
    registration text,                              -- aircraft.json .r   (tail number)
    icao_type    text,                              -- aircraft.json .t   (e.g. A320)
    type_desc    text,                              -- aircraft.json .desc
    operator     text,                              -- aircraft.json .ownOp (airline / owner)
    military     boolean NOT NULL DEFAULT false,    -- dbFlags & 1
    interesting  boolean NOT NULL DEFAULT false,    -- dbFlags & 2
    pia          boolean NOT NULL DEFAULT false,    -- dbFlags & 4  (Privacy ICAO Address)
    ladd         boolean NOT NULL DEFAULT false,    -- dbFlags & 8  (Limiting Aircraft Data Displayed)
    year         int,                               -- aircraft.json .year
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS aircraft_military_idx ON aircraft (military) WHERE military;
CREATE INDEX IF NOT EXISTS aircraft_type_idx     ON aircraft (icao_type);
CREATE INDEX IF NOT EXISTS aircraft_operator_idx ON aircraft (operator);
-- trigram indexes so the Grafana search filters (ILIKE '%term%') stay fast
CREATE INDEX IF NOT EXISTS aircraft_reg_trgm_idx ON aircraft USING gin (registration gin_trgm_ops);
CREATE INDEX IF NOT EXISTS aircraft_op_trgm_idx  ON aircraft USING gin (operator gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- flights: one contiguous appearance of an aircraft with a given callsign.
-- A new row is started when the callsign changes or the aircraft reappears
-- after a gap (see FLIGHT_GAP in tar1090-logger.default). Kept forever.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flights (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icao_hex   text NOT NULL REFERENCES aircraft (icao_hex),
    callsign   text,                                -- aircraft.json .flight (trimmed)
    squawk     text,
    start_time timestamptz NOT NULL,
    end_time   timestamptz NOT NULL,
    msg_count  bigint,
    max_alt    int,
    UNIQUE (icao_hex, start_time)
);
CREATE INDEX IF NOT EXISTS flights_callsign_idx ON flights (callsign, start_time DESC);
CREATE INDEX IF NOT EXISTS flights_hex_idx      ON flights (icao_hex, start_time DESC);
CREATE INDEX IF NOT EXISTS flights_start_idx    ON flights (start_time DESC);
CREATE INDEX IF NOT EXISTS flights_cs_trgm_idx  ON flights USING gin (callsign gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- positions: the time-series trail points that power replay / spatial queries.
-- One row per (thinned) position sample. This is the high-volume table, so it
-- is a TimescaleDB hypertable with compression + a retention policy.
-- The geom column is generated from lon/lat so the logger never has to build it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    time      timestamptz NOT NULL,
    icao_hex  text NOT NULL,
    flight_id bigint,
    lat       double precision NOT NULL,
    lon       double precision NOT NULL,
    alt_baro  int,                                  -- feet, NULL when on the ground
    on_ground boolean NOT NULL DEFAULT false,
    gs        real,                                 -- ground speed, knots
    track     real,                                 -- degrees
    baro_rate int,                                  -- vertical rate, ft/min
    source    text,                                 -- adsb_icao / mlat / tisb / mode_s ...
    geom      geometry(Point, 4326)
              GENERATED ALWAYS AS (ST_SetSRID(ST_MakePoint(lon, lat), 4326)) STORED
);

SELECT create_hypertable('positions', 'time',
                         chunk_time_interval => INTERVAL '1 day',
                         if_not_exists => TRUE);

CREATE INDEX IF NOT EXISTS positions_hex_time_idx    ON positions (icao_hex, time DESC);
CREATE INDEX IF NOT EXISTS positions_flight_time_idx ON positions (flight_id, time);
CREATE INDEX IF NOT EXISTS positions_geom_idx        ON positions USING gist (geom);

-- Compress chunks older than 2 days, grouped by aircraft (big space win).
ALTER TABLE positions SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'icao_hex',
    timescaledb.compress_orderby   = 'time DESC'
);
SELECT add_compression_policy('positions', INTERVAL '2 days', if_not_exists => TRUE);

-- Retention: drop raw position rows after 30 days. Change the interval (or run
-- remove_retention_policy('positions')) to keep more / forever. Aircraft and
-- flights rows are never auto-deleted.
SELECT add_retention_policy('positions', INTERVAL '30 days', if_not_exists => TRUE);

-- ---------------------------------------------------------------------------
-- Convenience view: human-readable flight list joined to registry metadata.
-- Used by the Grafana dashboard and ad-hoc queries.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_flights AS
SELECT f.id,
       f.icao_hex,
       f.callsign,
       a.registration,
       a.icao_type,
       a.type_desc,
       a.operator,
       a.military,
       f.squawk,
       f.start_time,
       f.end_time,
       (f.end_time - f.start_time) AS duration,
       f.max_alt,
       f.msg_count
FROM flights f
JOIN aircraft a USING (icao_hex);
