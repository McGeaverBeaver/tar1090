#!/usr/bin/env python3
"""backfill_globe_history: import readsb's existing globe_history traces into the DB.

readsb (run with --write-globe-history /var/globe_history) already stores complete,
de-duplicated per-aircraft flight paths as trace_full_<icao>.json files. This one-time
importer walks those files and populates the same aircraft / flights / positions tables
the live logger uses, so you get history back to whenever globe-history was enabled --
not just from the moment tar1090-logger started.

Each trace point is the array readsb/tar1090 use (see html/planeObject.js):
  [0]=time_offset [1]=lat [2]=lon [3]=alt(or "ground") [4]=gs [5]=track
  [6]=flags (&1 stale, &2 leg_marker) [7]=baro_rate [8]=source [9]=type ...
The wrapper object carries the base "timestamp" plus registry metadata (r/t/desc/ownOp/
dbFlags/year). A leg_marker (flags & 2) starts a new flight leg.

Usage:  backfill_globe_history.py [GLOBE_HISTORY_DIR]   (default /var/globe_history)
        DB connection comes from $TAR1090_DB_DSN (same as the logger).
Re-running is safe for flights (UNIQUE icao_hex,start_time -> skipped) but will duplicate
position rows, so run it once.
"""

import glob
import gzip
import json
import logging
import os
import sys
from datetime import datetime, timezone

import psycopg

log = logging.getLogger("backfill")

DB_DSN = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")

AIRCRAFT_UPSERT = """
INSERT INTO aircraft (icao_hex, registration, icao_type, type_desc, operator,
                      military, interesting, pia, ladd, year, first_seen, last_seen)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (icao_hex) DO UPDATE SET
  registration = COALESCE(EXCLUDED.registration, aircraft.registration),
  icao_type    = COALESCE(EXCLUDED.icao_type,    aircraft.icao_type),
  type_desc    = COALESCE(EXCLUDED.type_desc,    aircraft.type_desc),
  operator     = COALESCE(EXCLUDED.operator,     aircraft.operator),
  military     = aircraft.military OR EXCLUDED.military,
  year         = COALESCE(EXCLUDED.year,         aircraft.year),
  first_seen   = LEAST(aircraft.first_seen,      EXCLUDED.first_seen),
  last_seen    = GREATEST(aircraft.last_seen,    EXCLUDED.last_seen)
"""

COPY_POSITIONS = ("COPY positions (time, icao_hex, flight_id, lat, lon, alt_baro, "
                  "on_ground, gs, track, baro_rate, source) FROM STDIN")


def dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def parse_alt(v):
    if v == "ground":
        return None, True
    if isinstance(v, (int, float)):
        return int(v), False
    return None, False


def num(v):
    return v if isinstance(v, (int, float)) else None


def icao_from(data, path):
    icao = data.get("icao")
    if icao:
        return icao.lower()
    base = os.path.basename(path)
    for pre in ("trace_full_", "trace_recent_"):
        if base.startswith(pre):
            return base[len(pre):].split(".")[0].lower()
    return None


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        return json.load(fh)


def import_file(cur, path):
    """Import one trace file. Returns (flights, positions) inserted."""
    try:
        data = load(path)
    except (ValueError, OSError) as e:
        log.warning("skip %s: %s", path, e)
        return 0, 0

    icao = icao_from(data, path)
    trace = data.get("trace")
    if not icao or not trace:
        return 0, 0

    base = data.get("timestamp")
    if not isinstance(base, (int, float)):
        return 0, 0

    dbf = data.get("dbFlags") or 0
    # Split the trace into legs on the leg_marker flag (point[6] & 2).
    legs = []
    cur_leg = []
    for p in trace:
        if not p or len(p) < 3:
            continue
        flags = p[6] if len(p) > 6 and isinstance(p[6], int) else 0
        if cur_leg and (flags & 2):
            legs.append(cur_leg)
            cur_leg = []
        cur_leg.append(p)
    if cur_leg:
        legs.append(cur_leg)
    if not legs:
        return 0, 0

    # Registry row spanning the whole file.
    all_times = [base + p[0] for leg in legs for p in leg if isinstance(p[0], (int, float))]
    first_seen, last_seen = dt(min(all_times)), dt(max(all_times))
    cur.execute(AIRCRAFT_UPSERT, (
        icao, data.get("r") or None, data.get("t") or None, data.get("desc") or None,
        data.get("ownOp") or None, bool(dbf & 1), bool(dbf & 2), bool(dbf & 4),
        bool(dbf & 8),
        (int(data["year"]) if str(data.get("year") or "").isdigit() else None),
        first_seen, last_seen))

    callsign = (data.get("flight") or "").strip() or None
    n_flights = n_pos = 0
    for leg in legs:
        times = [base + p[0] for p in leg if isinstance(p[0], (int, float))]
        if not times:
            continue
        alts = [parse_alt(p[3] if len(p) > 3 else None)[0] for p in leg]
        max_alt = max([a for a in alts if a is not None], default=None)
        cur.execute(
            "INSERT INTO flights (icao_hex, callsign, squawk, start_time, end_time, "
            "msg_count, max_alt) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (icao_hex, start_time) DO NOTHING RETURNING id",
            (icao, callsign, None, dt(min(times)), dt(max(times)), len(leg), max_alt))
        row = cur.fetchone()
        if row is None:
            continue  # leg already imported on a previous run
        flight_id = row[0]
        n_flights += 1

        rows = []
        for p in leg:
            if not isinstance(p[0], (int, float)) or num(p[1]) is None or num(p[2]) is None:
                continue
            alt, on_ground = parse_alt(p[3] if len(p) > 3 else None)
            rows.append((
                dt(base + p[0]), icao, flight_id, p[1], p[2], alt, on_ground,
                num(p[4]) if len(p) > 4 else None,
                num(p[5]) if len(p) > 5 else None,
                int(p[7]) if len(p) > 7 and isinstance(p[7], (int, float)) else None,
                p[8] if len(p) > 8 and isinstance(p[8], str) else None))
        if rows:
            with cur.copy(COPY_POSITIONS) as cp:
                for r in rows:
                    cp.write_row(r)
            n_pos += len(rows)
    return n_flights, n_pos


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = sys.argv[1] if len(sys.argv) > 1 else "/var/globe_history"
    if not os.path.isdir(root):
        log.error("globe_history dir not found: %s", root)
        sys.exit(1)

    paths = glob.glob(os.path.join(root, "**", "trace_full_*.json"), recursive=True)
    paths += glob.glob(os.path.join(root, "**", "trace_full_*.json.gz"), recursive=True)
    log.info("found %d trace files under %s", len(paths), root)

    conn = psycopg.connect(DB_DSN, autocommit=False)
    tot_f = tot_p = done = 0
    try:
        for path in paths:
            try:
                with conn.cursor() as cur:
                    f, p = import_file(cur, path)
                conn.commit()
                tot_f += f
                tot_p += p
            except psycopg.Error as e:
                conn.rollback()
                log.warning("db error on %s: %s", path, e)
            done += 1
            if done % 500 == 0:
                log.info("%d/%d files, %d flights, %d positions", done, len(paths), tot_f, tot_p)
    finally:
        conn.close()
    log.info("done: %d files, %d flights, %d positions imported", len(paths), tot_f, tot_p)


if __name__ == "__main__":
    main()
