# Container image for the tar1090-logger sidecar.
#
# Small Python image that reads readsb's aircraft.json and writes aircraft /
# flights / trail data into TimescaleDB+PostGIS. It does not bundle readsb or a
# database -- point it at an existing aircraft.json (mounted) and database (DSN).
#
# Build:  docker build -t tar1090-logger ./database
# Run (read aircraft.json over HTTP from an existing tar1090 container/host):
#         docker run --rm \
#           -e TAR1090_DB_DSN="host=DB_IP dbname=tar1090 user=tar1090 password=..." \
#           -e AIRCRAFT_URL="http://TAR1090_IP/data/aircraft.json" \
#           ghcr.io/mcgeaverbeaver/tar1090-logger:latest
# Run (read aircraft.json from a mounted local run dir instead):
#         docker run --rm \
#           -e TAR1090_DB_DSN="host=DB_IP dbname=tar1090 user=tar1090 password=..." \
#           -v /run/readsb:/run/readsb:ro \
#           ghcr.io/mcgeaverbeaver/tar1090-logger:latest
FROM python:3.12-slim

WORKDIR /app

# psycopg[binary] bundles libpq, so no extra apt packages are required.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY tar1090-logger.py backfill_globe_history.py ./

# Drop privileges; the logger only reads aircraft.json and talks to the DB.
RUN useradd --system --no-create-home --uid 1000 logger
USER logger

# Override at runtime with -e / --env-file. You MUST set TAR1090_DB_DSN (a single
# libpq connection string) to point at your TimescaleDB; the default host below is
# just a placeholder. For the data source, either set AIRCRAFT_URL (HTTP) or mount
# a run dir -- if neither is set the logger auto-detects /run/readsb, /run/dump1090-fa.
ENV TAR1090_DB_DSN="host=db port=5432 dbname=tar1090 user=tar1090 password=changeme"

ENTRYPOINT ["python3", "tar1090-logger.py"]
