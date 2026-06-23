# tar1090 all-in-one image (tar1090 GUI + readsb + logger)

One container that does what your old image did **plus** logs to a database. It is the
actively-maintained [`sdr-enthusiasts/docker-tar1090`](https://github.com/sdr-enthusiasts/docker-tar1090)
image (the successor to the deprecated `mikenye/tar1090`) with the
[`tar1090-logger`](../database/tar1090-logger.py) baked in as an extra service.

- **Same web GUI, same env vars** — `BEASTHOST`, `MLATHOST`, `LAT`, `LONG`,
  `HEYWHATSTHAT_PANORAMA_ID`, etc. all behave exactly as before. Heatmap, `?replay`,
  `?pTracks` unchanged.
- **Logger built in** — reads the local `aircraft.json` and writes aircraft / flights /
  trail data to **your existing TimescaleDB**.
- **Postgres and Grafana are NOT bundled.** You point the logger at your own database
  and connect your own Grafana to that same database. Nothing to persist in this
  container.

```
        ┌─────────────── tar1090-allinone container ───────────────┐
BEAST ─►│ readsb ─► aircraft.json ─► tar1090 web GUI (:80)          │
        │                    └─────► tar1090-logger ──┐            │
        └──────────────────────────────────────────────┼───────────┘
                                                        ▼
                              your existing TimescaleDB ◄── your existing Grafana
```

## Image

Built and published by [`.github/workflows/build-allinone.yml`](../.github/workflows/build-allinone.yml):

```
ghcr.io/mcgeaverbeaver/tar1090-allinone:latest
```

## One-time: the database

The logger writes to an **external** TimescaleDB + PostGIS database. Two ways to get one.

### Option A — dedicated TimescaleDB container (recommended)

A separate container that bundles PostgreSQL + TimescaleDB + PostGIS. Nothing to
install, nothing to change on any existing database, and its lifecycle is independent
of the tar1090 container (pulling tar1090 updates can never touch your history).

```bash
# 1) the database (persistent volume on real disk; pick any LAN IP / path you like)
docker run -d --name tar1090-db --restart unless-stopped \
  -e POSTGRES_PASSWORD=STRONGPASS -e TZ=America/New_York \
  -v /mnt/cache/appdata/tar1090/pgdata:/home/postgres/pgdata/data \
  timescale/timescaledb-ha:pg16
# (Unraid: if it fails with "permission denied" on pgdata, the image runs as uid 1000:
#  chmod -R 777 /mnt/cache/appdata/tar1090/pgdata  -- or use a named volume.)

# 2) create the database + role, then apply the schema (pulled out of the tar1090 image)
docker exec -e PGPASSWORD=STRONGPASS tar1090-db \
  psql -h 127.0.0.1 -U postgres -v ON_ERROR_STOP=1 \
    -c "CREATE DATABASE tar1090;" \
    -c "CREATE ROLE tar1090 LOGIN SUPERUSER PASSWORD 'STRONGPASS';"
docker cp tar1090:/usr/local/share/tar1090/schema.sql /tmp/schema.sql   # from the running app container
docker cp /tmp/schema.sql tar1090-db:/tmp/schema.sql
docker exec -e PGPASSWORD=STRONGPASS tar1090-db \
  psql -h 127.0.0.1 -U postgres -d tar1090 -v ON_ERROR_STOP=1 -f /tmp/schema.sql
```

Then set on the tar1090 container:
`TAR1090_DB_DSN=host=<db-ip> port=5432 dbname=tar1090 user=tar1090 password=STRONGPASS`.

### Option B — your own existing PostgreSQL

Only if it already has (or you're willing to add) the **TimescaleDB** and **PostGIS**
extensions. PostGIS is a per-database `CREATE EXTENSION` (no restart); TimescaleDB needs
`shared_preload_libraries = 'timescaledb'` + a server restart. See
[`../database/README.md`](../database/README.md) for the install steps, then:

```bash
sudo -u postgres psql -c "CREATE ROLE tar1090 LOGIN PASSWORD 'changeme';"
sudo -u postgres psql -c "CREATE DATABASE tar1090 OWNER tar1090;"
sudo -u postgres psql -d tar1090 \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
psql "host=DB_IP dbname=tar1090 user=tar1090 password=changeme" -f database/schema.sql
```

To pull `schema.sql` out of the image instead of cloning the repo:
```bash
docker create --name x ghcr.io/mcgeaverbeaver/tar1090-allinone:latest
docker cp x:/usr/local/share/tar1090/schema.sql ./schema.sql
docker rm x
```

## Run the container

Take the `docker run` you already use for tar1090 and just (a) change the image and
(b) add `TAR1090_DB_DSN` pointing at your database:

```bash
docker run -d --name tar1090 --restart unless-stopped \
  -e BEASTHOST=192.168.4.20 -e MLATHOST=192.168.4.20 \
  -e LAT=XX.XXX -e LONG=-YY.YYY \
  -e TZ=America/New_York \
  -e TAR1090_DB_DSN="host=DB_IP port=5432 dbname=tar1090 user=tar1090 password=changeme" \
  -v /mnt/cache/appdata/tar1090/globe_history:/var/globe_history \
  -p 80:80 \
  ghcr.io/mcgeaverbeaver/tar1090-allinone:latest
```

Replace `DB_IP` with your database's address (and pick any host path you like for the
volume).

> **Important — persist the trails, or you'll get "no trail on disk".**
> There are two separate stores. The flight **index** (callsign, duration, max alt — the
> table rows) is written to your **database** and always survives. The actual **trail**
> lives in readsb's `globe_history` **inside the container** at `/var/globe_history`.
> If you don't mount that on a real volume (the `-v` line above), **every container
> recreate — including pulling a new image — wipes all collected trails**, while the
> index in your database stays. You then see flights listed with a duration but no
> trail. Mount the volume once and trails persist across updates. (Trails only exist from
> when collection started with the volume mounted — older flights won't backfill.)

### Unraid

Edit your existing tar1090 template: set **Repository** to
`ghcr.io/mcgeaverbeaver/tar1090-allinone:latest`, then add one **Variable** and one
**Path**:

| Type | Key / Container path | Value / Host path |
|------|----------------------|-------------------|
| Variable | `TAR1090_DB_DSN` | `host=192.168.10.245 port=5432 dbname=tar1090 user=tar1090 password=changeme` |
| Path | `/var/globe_history` | `/mnt/cache/appdata/tar1090/globe_history` |

The **Path** is what keeps your trails: without it, every template update / container
recreate wipes the collected `globe_history` (see the warning above) and flights show
with no trail. Leave all your existing `BEASTHOST` / `LAT` / `LONG` / etc. variables as
they are. You do **not** need the `pgdata` volume or the separate `host`/`user`/`password`
variables from earlier attempts — remove those.

## Verify

```bash
docker logs -f tar1090 | grep tar1090-logger
# expect: "starting; DB=host=... source=/run/readsb"
#         "connected to database"
#         "<n> aircraft in memory, <m> points written in last minute"
```
Then on the DB:
```sql
SELECT count(*) FROM positions;
SELECT * FROM v_flights ORDER BY start_time DESC LIMIT 10;
```

## Connect your Grafana

Point your Grafana at the **same database** (it does not talk to this container):
add a PostgreSQL datasource (tick **TimescaleDB**) for `DB_IP:5432` / `tar1090`, then
import [`../database/grafana/dashboard.json`](../database/grafana/dashboard.json). Full
steps (provisioning file or manual UI, and the datasource-uid note) are in
[`../database/README.md`](../database/README.md#6-grafana--historical-search--map-trail-replay).

## Config / tuning

All logger knobs are environment variables (defaults baked into the image; see
[`../database/tar1090-logger.default`](../database/tar1090-logger.default) for meanings):

| Var | Default | Purpose |
|-----|---------|---------|
| `TAR1090_DB_DSN` | *(unset → logger idle)* | your external TimescaleDB connection string |
| `ENABLE_LOGGER` | `true` | set `false` to run the GUI only |
| `LOG_INTERVAL` | `5` | seconds between samples of `aircraft.json` |
| `FLIGHT_GAP` | `300` | seconds of silence that ends one flight |
| `MAX_POINT_GAP` / `MIN_TRACK_DEG` / `MIN_ALT_FT` / `MIN_GS_KT` | `15` / `5` / `200` / `10` | trail thinning |
| `AIRCRAFT_CSV` | bundled `aircraft.csv.gz` | fallback metadata if readsb has no `--db-file` |

If `TAR1090_DB_DSN` is unset the logger simply idles and logs a hint — the tar1090 web
GUI still runs normally.
