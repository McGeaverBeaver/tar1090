# tar1090 all-in-one: the regular tar1090 web GUI + readsb, with the tar1090-logger
# baked in so the same container also logs aircraft / flights / trails to your
# EXTERNAL TimescaleDB. Postgres and Grafana are NOT bundled -- you point the logger
# at your existing database (TAR1090_DB_DSN) and connect your own Grafana to it.
#
# This extends the actively-maintained sdr-enthusiasts tar1090 image (the successor
# to the deprecated mikenye/tar1090), so every existing env var -- BEASTHOST,
# MLATHOST, LAT, LONG, etc. -- and the web GUI behave exactly as before. We only add
# one extra s6 service (the logger).
#
# Build context is the REPO ROOT (so we can reuse database/tar1090-logger.py and
# database/schema.sql as the single source of truth):
#   docker build -f allinone/Dockerfile -t tar1090-allinone .
FROM ghcr.io/sdr-enthusiasts/docker-tar1090:latest

# --- Python + psycopg3 (logger/history/alerts) + paho-mqtt (alerts) ----------
# Prefer the distro packages; fall back to self-contained wheels via pip.
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends python3 ca-certificates; \
    if ! apt-get install -y --no-install-recommends python3-psycopg python3-paho-mqtt; then \
        apt-get install -y --no-install-recommends python3-pip; \
        pip3 install --no-cache-dir --break-system-packages "psycopg[binary]>=3.1" paho-mqtt; \
    fi; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# --- the logger, history API + page, alert engine, and schemas (from database/) ---
COPY database/tar1090-logger.py      /usr/local/share/tar1090/tar1090-logger.py
COPY database/tar1090-history-api.py /usr/local/share/tar1090/tar1090-history-api.py
COPY database/tar1090-alerter.py     /usr/local/share/tar1090/tar1090-alerter.py
COPY database/tar1090_mqtt.py        /usr/local/share/tar1090/tar1090_mqtt.py
COPY database/tar1090_heatmap_import.py /usr/local/share/tar1090/tar1090_heatmap_import.py
COPY database/backfill_heatmap.py       /usr/local/share/tar1090/backfill_heatmap.py
COPY database/backfill_globe_history.py /usr/local/share/tar1090/backfill_globe_history.py
COPY database/history/               /usr/local/share/tar1090/history/
COPY database/schema.sql             /usr/local/share/tar1090/schema.sql
COPY database/schema-index.sql       /usr/local/share/tar1090/schema-index.sql
COPY database/schema-alerts.sql      /usr/local/share/tar1090/schema-alerts.sql

# Bundle the aircraft metadata CSV so registration/type/operator/military resolve
# even when readsb is not run with --db-file. Fail the build if it can't be fetched
# or isn't a valid (>1MB) gzip, so we never ship an image without working metadata.
RUN curl -fsSL --retry 5 --retry-delay 2 -o /usr/local/share/tar1090/aircraft.csv.gz \
      https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz \
 && gzip -t /usr/local/share/tar1090/aircraft.csv.gz \
 && [ "$(stat -c%s /usr/local/share/tar1090/aircraft.csv.gz)" -gt 1000000 ] \
 && echo "bundled aircraft.csv.gz ($(stat -c%s /usr/local/share/tar1090/aircraft.csv.gz) bytes)"

# --- s6 services: logger + history/replay API + alert engine ----------------
COPY allinone/rootfs/ /
RUN chmod +x /etc/s6-overlay/s6-rc.d/tar1090-logger/run \
             /etc/s6-overlay/s6-rc.d/tar1090-history-api/run \
             /etc/s6-overlay/s6-rc.d/tar1090-alerter/run

# The history search & replay page (and Alerts/Settings tabs) is served on port 8090:
#   http://<host>:8090/   (History | Alerts | Settings)
EXPOSE 8090

# Logger + history-API tunables (see database/tar1090-logger.default).
# TAR1090_DB_DSN is intentionally left UNSET: set it to your database to enable logging
# and search (the web GUI runs regardless). e.g.
#   -e TAR1090_DB_DSN="host=192.168.1.50 port=5432 dbname=tar1090 user=tar1090 password=secret"
# LOG_POSITIONS=false: with the history page, trails are read from globe_history, so the
# logger only maintains the slim aircraft/flights index (apply schema-index.sql -- plain
# PostgreSQL, no TimescaleDB/PostGIS needed). Set true to also fill a positions table.
#
# Trails: the replay page reconstructs each trail from readsb's heatmap chunks
# (globe_history/.../heatmap/NN.bin.ttf), which readsb ALWAYS writes -- the same data the
# native ?replay animates from -- so it works against whatever history you already have,
# with no extra setting. For sharper trails on NEW flights you can additionally set
# READSB_ENABLE_TRACES=true (writes a per-aircraft trace_full_*.json the API prefers when
# present); we leave it OFF by default because it's one file per aircraft per day and the
# only way to bound it, MAX_GLOBE_HISTORY, also deletes the older heatmap history you want
# to keep. Persist /var/globe_history on a real volume.
# Alerts: the alert engine watches the live feed and publishes matches over MQTT to Home
# Assistant (configure the broker + rules in the Alerts/Settings tabs; tables auto-create
# in your database). It idles until a broker + rules are set. Set ENABLE_ALERTS=false to
# turn the service off entirely.
ENV ENABLE_LOGGER=true \
    ENABLE_HISTORY_API=true \
    ENABLE_ALERTS=true \
    LOG_POSITIONS=false \
    LOG_INTERVAL=5 \
    FLIGHT_GAP=300 \
    MAX_POINT_GAP=15 \
    MIN_TRACK_DEG=5 \
    MIN_GS_KT=10 \
    MIN_ALT_FT=200 \
    GLOBE_HISTORY_DIR=/var/globe_history \
    HISTORY_API_PORT=8090 \
    ALERT_INTERVAL=5 \
    ALERT_HOLD_SEC=60
