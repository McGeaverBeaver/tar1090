-- tar1090 history INDEX schema (slim) — plain PostgreSQL, no TimescaleDB / PostGIS.
--
-- This is the "search index" half of the history feature: it stores one row per
-- aircraft (registry metadata) and one row per flight (a contiguous appearance with a
-- callsign), so you can search "every time callsign/registration/type X appeared in a
-- date range". The actual trails are NOT stored here — they are read on demand from
-- the globe_history trace files readsb already writes (the history API does that).
--
-- Because there is no time-series positions table, this needs only stock PostgreSQL
-- plus pg_trgm (for fast substring search). Apply with:
--   psql "<your DSN>" -f schema-index.sql
--
-- (schema.sql is the older, self-contained variant that ALSO logs every position into
--  a TimescaleDB hypertable. Use this slim one with the history-API + search page.)

CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- fast ILIKE '%term%' search

-- ---------------------------------------------------------------------------
-- aircraft: one slowly-changing row per ICAO 24-bit address (the registry).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aircraft (
    icao_hex     text PRIMARY KEY,                  -- lowercase 24-bit ICAO hex
    registration text,                              -- tail number
    icao_type    text,                              -- e.g. A320
    type_desc    text,                              -- human description
    operator     text,                              -- airline / owner
    military     boolean NOT NULL DEFAULT false,
    interesting  boolean NOT NULL DEFAULT false,
    pia          boolean NOT NULL DEFAULT false,    -- Privacy ICAO Address
    ladd         boolean NOT NULL DEFAULT false,    -- Limiting Aircraft Data Displayed
    year         int,
    first_seen   timestamptz NOT NULL,
    last_seen    timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS aircraft_mil_idx  ON aircraft (military) WHERE military;
CREATE INDEX IF NOT EXISTS aircraft_type_idx ON aircraft (icao_type);
CREATE INDEX IF NOT EXISTS aircraft_reg_trgm ON aircraft USING gin (registration gin_trgm_ops);
CREATE INDEX IF NOT EXISTS aircraft_op_trgm  ON aircraft USING gin (operator gin_trgm_ops);

-- ---------------------------------------------------------------------------
-- flights: one contiguous appearance of an aircraft with a callsign.
-- This is the searchable, per-appearance index. start_time/end_time tell the API
-- which globe_history UTC day file(s) to read for the trail.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS flights (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    icao_hex   text NOT NULL REFERENCES aircraft(icao_hex),
    callsign   text,
    squawk     text,
    start_time timestamptz NOT NULL,
    end_time   timestamptz NOT NULL,
    msg_count  bigint,
    max_alt    int,
    UNIQUE (icao_hex, start_time)
);
CREATE INDEX IF NOT EXISTS flights_callsign_time ON flights (callsign, start_time DESC);
CREATE INDEX IF NOT EXISTS flights_hex_time      ON flights (icao_hex, start_time DESC);
CREATE INDEX IF NOT EXISTS flights_time          ON flights (start_time DESC);
CREATE INDEX IF NOT EXISTS flights_callsign_trgm ON flights USING gin (callsign gin_trgm_ops);

-- Convenience view joining the two (used by the API / Grafana).
CREATE OR REPLACE VIEW v_flights AS
SELECT f.id, f.icao_hex, f.callsign,
       a.registration, a.icao_type, a.type_desc, a.operator, a.military,
       f.squawk, f.start_time, f.end_time,
       (f.end_time - f.start_time) AS duration,
       f.max_alt, f.msg_count
FROM flights f
JOIN aircraft a USING (icao_hex);
