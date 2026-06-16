-- Example queries against the tar1090 database.
-- Run with:  psql "$TAR1090_DB_DSN" -f example_queries.sql   (or paste individually)

-- Every military aircraft seen in the last 7 days
SELECT registration, icao_type, callsign, start_time, duration
FROM v_flights
WHERE military AND start_time > now() - interval '7 days'
ORDER BY start_time DESC;

-- All A320s seen this month
SELECT DISTINCT a.icao_hex, a.registration, a.operator
FROM aircraft a
WHERE a.icao_type = 'A320'
  AND a.last_seen > date_trunc('month', now());

-- Busiest operators in the last 24 hours
SELECT operator, count(*) AS flights
FROM v_flights
WHERE start_time > now() - interval '24 hours' AND operator IS NOT NULL
GROUP BY operator
ORDER BY flights DESC
LIMIT 20;

-- The full trail (path) of one flight, ready to plot (lat/lon over time)
SELECT time, lat, lon, alt_baro, gs, track
FROM positions
WHERE flight_id = :flight_id          -- replace with an id from v_flights
ORDER BY time;

-- The same trail as a single GeoJSON LineString (PostGIS)
SELECT ST_AsGeoJSON(ST_MakeLine(geom ORDER BY time)) AS geojson
FROM positions
WHERE flight_id = :flight_id;

-- Aircraft that passed within 10 km of a point (e.g. your home) today (PostGIS)
SELECT DISTINCT a.registration, a.icao_type, a.operator, a.military
FROM positions p
JOIN aircraft a USING (icao_hex)
WHERE p.time > now() - interval '24 hours'
  AND ST_DWithin(p.geom::geography,
                 ST_SetSRID(ST_MakePoint(-0.45, 51.47), 4326)::geography,  -- lon,lat
                 10000)
ORDER BY a.registration;

-- Daily counts: distinct aircraft and military fraction (TimescaleDB time_bucket)
SELECT time_bucket('1 day', start_time) AS day,
       count(*)                           AS flights,
       count(*) FILTER (WHERE military)   AS military_flights
FROM v_flights
GROUP BY day
ORDER BY day DESC;
