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

## One-time: prepare your external database

On your existing PostgreSQL server (needs the **TimescaleDB** and **PostGIS**
extensions — see [`../database/README.md`](../database/README.md) for installing them):

```bash
# create role + db (once)
sudo -u postgres psql -c "CREATE ROLE tar1090 LOGIN PASSWORD 'changeme';"
sudo -u postgres psql -c "CREATE DATABASE tar1090 OWNER tar1090;"
sudo -u postgres psql -d tar1090 \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;" \
  -c "CREATE EXTENSION IF NOT EXISTS postgis;" \
  -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
# apply the schema (grab it from this repo, or copy it out of the image)
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
  -p 80:80 \
  ghcr.io/mcgeaverbeaver/tar1090-allinone:latest
```

Replace `DB_IP` with your database's address. That's the only new thing versus a plain
tar1090 container. Keep your usual volumes (e.g. globe_history) if you use them.

### Unraid

Edit your existing tar1090 template: set **Repository** to
`ghcr.io/mcgeaverbeaver/tar1090-allinone:latest`, then add one **Variable**:

| Key | Value |
|-----|-------|
| `TAR1090_DB_DSN` | `host=192.168.10.245 port=5432 dbname=tar1090 user=tar1090 password=changeme` |

Leave all your existing `BEASTHOST` / `LAT` / `LONG` / etc. variables as they are. You
do **not** need the `pgdata` volume or the separate `host`/`user`/`password` variables
from earlier attempts — remove those.

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
