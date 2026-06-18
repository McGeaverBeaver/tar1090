-- tar1090 alerting: rules, MQTT/Home-Assistant settings, and a log of fired alerts.
-- Plain PostgreSQL (JSONB) -- no TimescaleDB/PostGIS needed. These tables are created
-- automatically by tar1090-history-api on startup; this file is kept for reference and
-- for applying them by hand if you prefer (psql -f schema-alerts.sql).

-- One row (id=1) of MQTT broker / Home Assistant settings.
CREATE TABLE IF NOT EXISTS alert_config (
  id               int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled          boolean NOT NULL DEFAULT true,
  mqtt_host        text,
  mqtt_port        int  NOT NULL DEFAULT 1883,
  mqtt_username    text,
  mqtt_password    text,
  mqtt_tls         boolean NOT NULL DEFAULT false,
  base_topic       text NOT NULL DEFAULT 'tar1090',
  ha_discovery     boolean NOT NULL DEFAULT true,
  discovery_prefix text NOT NULL DEFAULT 'homeassistant',
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- One alert rule. conditions/zone/time_window are JSON so the schema stays stable as the
-- UI grows. zone = null (anywhere) | {"type":"circle","lat":..,"lon":..,"radius_m":..}
--                                   | {"type":"polygon","points":[[lat,lon],...]}
-- time_window = null (always) | {"days":[0..6 (0=Sun)],"start":"HH:MM","end":"HH:MM"}
CREATE TABLE IF NOT EXISTS alert_rules (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         text NOT NULL,
  enabled      boolean NOT NULL DEFAULT true,
  conditions   jsonb NOT NULL DEFAULT '{}'::jsonb,
  zone         jsonb,
  time_window  jsonb,
  cooldown_sec int  NOT NULL DEFAULT 1800,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Every time a rule fires we append a row here (also feeds the in-app Alerts log).
CREATE TABLE IF NOT EXISTS alert_log (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id      bigint,
  rule_name    text,
  icao_hex     text,
  callsign     text,
  registration text,
  icao_type    text,
  operator     text,
  military     boolean,
  lat          double precision,
  lon          double precision,
  alt          int,
  squawk       text,
  fired_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS alert_log_fired_idx ON alert_log (fired_at DESC);
