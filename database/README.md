# tar1090 database logging (TimescaleDB + PostGIS)

This is an **optional, additive** component. It logs the aircraft you see —
**callsign, registration, airline/operator, type, and a military flag** — plus the
**trails/paths** they fly, into a real database you can query with SQL and visualise
(e.g. trail replay) in Grafana.

It does **not** change readsb or the tar1090 web frontend in any way. The live map,
the **heatmap** (`?heatmap=…`), the built-in **replay** (`?replay=…`) and `?pTracks`
all keep working exactly as before. A small sidecar service
([`tar1090-logger.py`](tar1090-logger.py)) reads the *same* `aircraft.json` that readsb
already produces and writes it to the database. If the logger stops, nothing else is
affected.

```
readsb ──> aircraft.json ──> tar1090 frontend   (unchanged: live / replay / heatmap)
   │                    └──> tar1090.sh          (unchanged: pTracks chunks)
   └──> /var/globe_history/traces  ──(one-time backfill)──┐
                                                          ▼
              tar1090-logger.py ──reads aircraft.json──> TimescaleDB + PostGIS ──> Grafana
```

## What gets stored

| Table | Contents | Retention |
|-------|----------|-----------|
| `aircraft` | one row per ICAO hex: registration, type, type description, operator/airline, **military** flag, year | forever |
| `flights` | one row per contiguous appearance with a callsign: callsign, squawk, start/end time, max altitude | forever |
| `positions` | time-series trail points (lat/lon/alt/speed/track + PostGIS `geom`) — a TimescaleDB hypertable | 30 days (configurable) |

## Prerequisites

1. **readsb writing the metadata into `aircraft.json`.** Start readsb with a database
   file so it injects `r`/`t`/`desc`/`ownOp`/`dbFlags` per aircraft (this is the same
   setup used for tail numbers in the web UI). In `/etc/default/readsb`:
   ```
   wget -O /usr/local/share/tar1090/aircraft.csv.gz \
        https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz
   # add to the decoder options:
   --db-file /usr/local/share/tar1090/aircraft.csv.gz
   ```
   (If you can't, set `AIRCRAFT_CSV=` in the logger config and it resolves metadata
   itself — see [`tar1090-logger.default`](tar1090-logger.default).)

2. **(For backfill) globe-history enabled** — you likely already have this if you use
   the heatmap. In `/etc/default/readsb`: `--write-globe-history /var/globe_history --heatmap 30`.

3. **PostgreSQL 14+ with TimescaleDB and PostGIS.**

## 1. Install the database

**Option A — Docker (everything bundled):**
```bash
docker run -d --name tar1090-db -p 5432:5432 \
  -e POSTGRES_PASSWORD=changeme -e POSTGRES_DB=tar1090 -e POSTGRES_USER=tar1090 \
  -v tar1090-pgdata:/home/postgres/pgdata/data \
  timescale/timescaledb-ha:pg16          # includes TimescaleDB + PostGIS
```

**Option B — apt onto an existing PostgreSQL 16:**
```bash
# PostGIS
sudo apt-get install -y postgresql-16-postgis-3
# TimescaleDB
echo "deb https://packagecloud.io/timescale/timescaledb/ubuntu/ $(lsb_release -cs) main" \
  | sudo tee /etc/apt/sources.list.d/timescaledb.list
wget --quiet -O - https://packagecloud.io/timescale/timescaledb/gpgkey \
  | sudo gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg
sudo apt-get update && sudo apt-get install -y timescaledb-2-postgresql-16
sudo timescaledb-tune --quiet --yes        # sets shared_preload_libraries
sudo systemctl restart postgresql
```

> **Important:** keep the database on real disk (the default `/var/lib/postgresql`).
> Do **not** put it under `/run`, which is tmpfs and is wiped on reboot.

## 2. Create the role, database and schema

```bash
sudo -u postgres psql -c "CREATE ROLE tar1090 LOGIN PASSWORD 'changeme';"
sudo -u postgres psql -c "CREATE DATABASE tar1090 OWNER tar1090;"
# Extensions must be installed by a superuser (postgis/timescaledb are not "trusted").
sudo -u postgres psql -d tar1090 \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
# Now create the tables/indexes/policies as the owner. (The CREATE EXTENSION lines in
# schema.sql are no-ops here since the extensions already exist.)
psql "host=127.0.0.1 dbname=tar1090 user=tar1090 password=changeme" -f schema.sql
```
(Docker option A: its `POSTGRES_USER` is a superuser, so you can skip the separate
extension step and just run the `schema.sql` line.)

## 3. Install and run the logger

```bash
pip3 install -r requirements.txt                      # psycopg 3
sudo cp tar1090-logger.py /usr/local/share/tar1090/
sudo cp tar1090-logger.default /etc/default/tar1090-logger   # edit TAR1090_DB_DSN
sudo cp tar1090-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tar1090-logger
journalctl -u tar1090-logger -f                       # watch it log points
```

### Alternative: run the logger in a container

The image is built and published to GHCR by `.github/workflows/build-logger.yml`.

> **The logger image is NOT tar1090.** It does not serve a web UI, run readsb, or
> contain a database. It is a tiny sidecar that reads an existing `aircraft.json` and
> writes to an existing database. So you run **three** containers:
>
> | Container | Image | Role |
> |-----------|-------|------|
> | tar1090 / readsb | your existing tar1090 image | produces `aircraft.json`, serves the map — **unchanged** |
> | database | `timescale/timescaledb-ha:pg16` | stores the data (needs a persistent volume) |
> | logger | `ghcr.io/mcgeaverbeaver/tar1090-logger` | reads `aircraft.json`, writes to the DB |
>
> Env vars like `BEASTHOST`, `MLATHOST`, `LAT`, `LONG`, `TCP_PORT_80` belong on your
> **tar1090** container and do nothing on the logger. The logger reads exactly **one**
> connection variable, `TAR1090_DB_DSN` (a full libpq string) — individual `host` /
> `user` / `password` vars are ignored.

The logger needs to reach `aircraft.json`. In a multi-container setup the easy way is
over HTTP from your tar1090 web UI (no volume juggling) via `AIRCRAFT_URL`:

```bash
# 1) database (persistent volume on real disk, NOT /run)
docker run -d --name tar1090-db --restart unless-stopped \
  -e POSTGRES_PASSWORD=changeme -e POSTGRES_DB=tar1090 -e POSTGRES_USER=tar1090 \
  -v /path/on/host/pgdata:/home/postgres/pgdata/data \
  timescale/timescaledb-ha:pg16

# apply the schema once (POSTGRES_USER here is a superuser, so extensions just work
# and the tables end up owned by tar1090, which is what the logger connects as)
docker cp schema.sql tar1090-db:/tmp/schema.sql
docker exec -e PGPASSWORD=changeme tar1090-db \
  psql -h 127.0.0.1 -U tar1090 -d tar1090 -f /tmp/schema.sql

# 2) logger -- point it at the DB (TAR1090_DB_DSN) and tar1090's aircraft.json (AIRCRAFT_URL)
docker run -d --name tar1090-logger --restart unless-stopped \
  -e TAR1090_DB_DSN="host=DB_IP port=5432 dbname=tar1090 user=tar1090 password=changeme" \
  -e AIRCRAFT_URL="http://TAR1090_IP/data/aircraft.json" \
  ghcr.io/mcgeaverbeaver/tar1090-logger:latest
```

Replace `DB_IP` with the database container's address and `TAR1090_IP` with your
tar1090 container's address. (`AIRCRAFT_URL` also accepts just `http://TAR1090_IP` —
it appends `/data/aircraft.json`. Some images serve it at `/tar1090/data/aircraft.json`;
open it in a browser to confirm the path.)

If instead the logger runs on the **same host** as readsb, skip `AIRCRAFT_URL` and mount
the run dir read-only so it reads the file directly:

```bash
docker run -d --name tar1090-logger --restart unless-stopped \
  -e TAR1090_DB_DSN="host=DB_IP dbname=tar1090 user=tar1090 password=changeme" \
  -v /run/readsb:/run/readsb:ro \
  ghcr.io/mcgeaverbeaver/tar1090-logger:latest
```

Build the image locally instead of pulling with `docker build -t tar1090-logger ./database`.

> **Common error:** `database connect failed: failed to resolve host 'db'` means
> `TAR1090_DB_DSN` was not set, so the image fell back to its placeholder default
> (`host=db`). Set `TAR1090_DB_DSN` to your real database connection string.

## 4. Verify

```sql
-- it should be a hypertable with compression + retention policies
\d+ positions
SELECT * FROM timescaledb_information.jobs;

-- data flowing in:
SELECT count(*) FROM positions;
SELECT icao_hex, registration, icao_type, operator, military FROM aircraft LIMIT 10;
SELECT * FROM v_flights ORDER BY start_time DESC LIMIT 10;
```
More ready-made queries (military this week, all A320s, trail of a flight, proximity
search) are in [`example_queries.sql`](example_queries.sql).

## 5. (Optional) Backfill existing history

Import the per-aircraft paths readsb already wrote, so your history goes back further
than logger start. Run **once** (it duplicates position rows if re-run):
```bash
TAR1090_DB_DSN="host=127.0.0.1 dbname=tar1090 user=tar1090 password=changeme" \
  python3 backfill_globe_history.py /var/globe_history
```

## 6. Grafana — historical search + map trail replay

This is where you **search the logged history**. (tar1090's own Search box keeps
working too, but it only searches aircraft currently loaded in the browser; the
browser can't query the database directly, so historical search lives in Grafana.)

- Add the datasource: copy [`grafana/datasource.yml`](grafana/datasource.yml) to
  `/etc/grafana/provisioning/datasources/` (edit host/password) and restart Grafana,
  or add a PostgreSQL datasource by hand (tick "TimescaleDB").
- Import [`grafana/dashboard.json`](grafana/dashboard.json) — the **"tar1090 - search
  & trails"** dashboard.

Using it as a search tool:

- Set the **time range** (top right) to the period you want to search.
- Type into the filter boxes at the top — **Callsign, Registration, Type, Operator**
  (all are case-insensitive substring matches) and toggle **Military** = Any / Military
  / Civil. Leave a box empty to ignore it. The trigram indexes from `schema.sql` keep
  these fast.
- The **Search results** table updates live; the stat panels show how many flights /
  aircraft / military matched.
- **Click a callsign** in the results (or pick from the **Flight** dropdown, which is
  itself filtered by your search) to draw that flight's **trail** on the Geomap panel.

For ad-hoc questions beyond the dashboard, use Grafana's Explore view or `psql` with the
recipes in [`example_queries.sql`](example_queries.sql). tar1090's built-in `?replay`
and heatmap continue to work independently of all of this.

## Tuning

All knobs are in `/etc/default/tar1090-logger`:

- `LOG_INTERVAL` — how often `aircraft.json` is sampled (default 5 s).
- `MAX_POINT_GAP`, `MIN_TRACK_DEG`, `MIN_ALT_FT`, `MIN_GS_KT` — **trail thinning**: a
  point is stored only on meaningful change or after the max gap. This keeps the
  `positions` table small while preserving trail shape; loosen them for smaller DB,
  tighten for smoother trails.
- `FLIGHT_GAP` — gap (seconds) that ends one flight and starts the next (default 300).

**Retention** lives in the database, not the config. To keep more than 30 days, change
the interval in `schema.sql` or run:
```sql
SELECT remove_retention_policy('positions');
SELECT add_retention_policy('positions', INTERVAL '365 days');   -- e.g. one year
```
Older chunks are automatically compressed after 2 days regardless.
