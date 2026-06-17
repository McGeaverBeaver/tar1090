#!/usr/bin/env python3
"""tar1090-logger: log aircraft metadata + flight trails into TimescaleDB/PostGIS.

It reads the very same aircraft.json that readsb produces (and that the tar1090
frontend consumes) and writes three tables (see schema.sql):

  * aircraft  -- registry metadata: registration, type, operator, military, ...
  * flights   -- one row per contiguous appearance of an aircraft with a callsign
  * positions -- thinned time-series trail points (a TimescaleDB hypertable)

This is a standalone sidecar. It changes nothing about readsb or the tar1090 web
UI, so the heatmap, ?replay and ?pTracks keep working exactly as before. If this
logger stops, the rest of the system is unaffected.

Configuration is read from the environment (see tar1090-logger.default).
Usage: tar1090-logger.py [SOURCE_DIR]
"""

import gzip
import json
import logging
import os
import signal
import sys
import time
import urllib.request
from datetime import datetime, timezone

import psycopg

log = logging.getLogger("tar1090-logger")

# --- configuration (environment, with defaults) -----------------------------
DB_DSN        = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
LOG_INTERVAL  = float(os.environ.get("LOG_INTERVAL", "5"))
FLIGHT_GAP    = float(os.environ.get("FLIGHT_GAP", "300"))
MAX_POINT_GAP = float(os.environ.get("MAX_POINT_GAP", "15"))
MIN_TRACK_DEG = float(os.environ.get("MIN_TRACK_DEG", "5"))
MIN_ALT_FT    = float(os.environ.get("MIN_ALT_FT", "200"))
MIN_GS_KT     = float(os.environ.get("MIN_GS_KT", "10"))
AIRCRAFT_CSV  = os.environ.get("AIRCRAFT_CSV", "").strip()
# Optional: fetch aircraft.json over HTTP instead of reading a local file. Useful
# when readsb/tar1090 runs in a separate container -- point this at the tar1090 web
# UI (e.g. http://192.168.1.10/data/aircraft.json, or just http://192.168.1.10).
AIRCRAFT_URL  = os.environ.get("AIRCRAFT_URL", "").strip()

# Same candidate list install.sh uses to find a decoder's aircraft.json.
SOURCE_DIRS_FALLBACK = [
    "/run/readsb", "/run/dump1090-fa", "/run/adsbexchange-feed",
    "/run/dump1090", "/run/dump1090-mutability", "/run/skyaware978", "/run/shm",
]

AIRCRAFT_UPSERT = """
INSERT INTO aircraft (icao_hex, registration, icao_type, type_desc, operator,
                      military, interesting, pia, ladd, year, first_seen, last_seen)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (icao_hex) DO UPDATE SET
  registration = COALESCE(EXCLUDED.registration, aircraft.registration),
  icao_type    = COALESCE(EXCLUDED.icao_type,    aircraft.icao_type),
  type_desc    = COALESCE(EXCLUDED.type_desc,    aircraft.type_desc),
  operator     = COALESCE(EXCLUDED.operator,     aircraft.operator),
  military     = EXCLUDED.military,
  interesting  = EXCLUDED.interesting,
  pia          = EXCLUDED.pia,
  ladd         = EXCLUDED.ladd,
  year         = COALESCE(EXCLUDED.year,         aircraft.year),
  first_seen   = LEAST(aircraft.first_seen,      EXCLUDED.first_seen),
  last_seen    = GREATEST(aircraft.last_seen,    EXCLUDED.last_seen)
"""

COPY_POSITIONS = ("COPY positions (time, icao_hex, flight_id, lat, lon, alt_baro, "
                  "on_ground, gs, track, baro_rate, source) FROM STDIN")


# --- small helpers ----------------------------------------------------------
def dt(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def num(v):
    """Return v if it's a real number, else None (readsb sometimes omits fields)."""
    return v if isinstance(v, (int, float)) else None


def intornone(v):
    try:
        return int(v) if v is not None else None
    except (ValueError, TypeError):
        return None


def parse_alt(v):
    """alt_baro is a number in feet, the string 'ground', or missing."""
    if v == "ground":
        return None, True
    if isinstance(v, (int, float)):
        return int(v), False
    return None, False


def angdiff(a, b):
    """Smallest absolute difference between two headings, handling 0/360 wrap."""
    d = abs(a - b) % 360.0
    return d if d <= 180.0 else 360.0 - d


# --- metadata resolution ----------------------------------------------------
def resolve_meta(ac, csv_meta):
    """Pull registry metadata for one aircraft.

    The primary source is the fields readsb injects into aircraft.json when run
    with --db-file (r/t/desc/ownOp/year/dbFlags). If those are absent and an
    AIRCRAFT_CSV fallback was loaded, fill in from there.
    """
    reg = ac.get("r")
    typ = ac.get("t")
    desc = ac.get("desc")
    op = ac.get("ownOp")
    year = ac.get("year")
    dbf = ac.get("dbFlags") or 0

    if csv_meta and (reg is None or typ is None or op is None):
        m = csv_meta.get(ac["hex"].lower().lstrip("~"))
        if m:
            # m is a tuple: (registration, icao_type, type_desc, operator, dbFlags)
            reg = reg or m[0]
            typ = typ or m[1]
            desc = desc or m[2]
            op = op or m[3]
            if not ac.get("dbFlags") and m[4]:
                dbf = m[4]

    return {
        "reg": reg or None,
        "typ": typ or None,
        "desc": desc or None,
        "op": op or None,
        "mil": bool(dbf & 1),
        "intr": bool(dbf & 2),
        "pia": bool(dbf & 4),
        "ladd": bool(dbf & 8),
        "year": intornone(year),
    }


def parse_dbflags(s):
    """tar1090-db encodes dbFlags as a positional binary string: index 0 = military,
    1 = interesting, 2 = pia, 3 = ladd (see html/planeObject.js). e.g. '10' = military.
    Tolerate an already-numeric value too."""
    s = (s or "").strip()
    if not s:
        return 0
    if set(s) <= {"0", "1"}:               # positional bit string ('10', '0010', ...)
        f = 0
        for i, bit in enumerate(("1", "2", "4", "8")):
            if i < len(s) and s[i] == "1":
                f |= int(bit)
        return f
    try:                                   # some sources store a plain integer
        return int(s)
    except ValueError:
        return 0


def load_csv_metadata(path):
    """Loader for a tar1090-db style aircraft CSV (used when readsb is NOT run with
    --db-file). Handles the real wiedehopf format -- headerless, ';'-separated,
    columns: icao;registration;icaotype;dbflags;description;year;operator -- and
    falls back to header-name matching for other CSV shapes. Returns
    {icao_hex: (registration, icao_type, type_desc, operator, dbFlags)}."""
    if not path:
        return {}
    if not os.path.exists(path):
        log.warning("AIRCRAFT_CSV %s not found; skipping fallback metadata", path)
        return {}

    opener = gzip.open if path.endswith(".gz") else open
    out = {}
    _i = sys.intern  # dedupe the very repetitive type/desc/operator strings
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            first = fh.readline()
            fh.seek(0)
            f0 = first.split(";")[0].strip().lstrip("~")
            headerless = ";" in first and len(f0) == 6 and all(c in "0123456789abcdefABCDEF" for c in f0)

            if headerless:
                for line in fh:
                    p = line.rstrip("\n").split(";")
                    if len(p) < 3:
                        continue
                    key = p[0].strip().lower().lstrip("~")
                    if not key:
                        continue
                    reg = (p[1].strip() if len(p) > 1 else "") or None
                    typ = (p[2].strip() if len(p) > 2 else "") or None
                    flags = parse_dbflags(p[3] if len(p) > 3 else "")
                    desc = (p[4].strip() if len(p) > 4 else "") or None
                    op = (p[6].strip() if len(p) > 6 else "") or None
                    out[key] = (reg, _i(typ) if typ else None, _i(desc) if desc else None,
                                _i(op) if op else None, flags)
            else:
                import csv as _csv
                sample = first
                delim = ";" if sample.count(";") >= sample.count(",") else ","
                rdr = _csv.reader(fh, delimiter=delim)
                header = next(rdr, None)
                cols = [h.strip().lower() for h in header] if header else []

                def idx(*names):
                    for n in names:
                        if n in cols:
                            return cols.index(n)
                    return None

                i_icao = idx("icao", "hex", "icao24", "icaohex")
                i_reg = idx("registration", "reg", "r", "tail")
                i_type = idx("icaotype", "type", "t", "typecode", "icao_type")
                i_desc = idx("desc", "description", "type_desc", "model")
                i_op = idx("operator", "ownop", "owner", "airline")
                i_flags = idx("dbflags", "flags")
                if i_icao is None:
                    log.warning("AIRCRAFT_CSV header not recognised (cols=%s); skipping", cols)
                    return {}

                def cell(row, i):
                    return (row[i].strip() or None) if (i is not None and i < len(row)) else None

                for row in rdr:
                    if len(row) <= i_icao:
                        continue
                    key = row[i_icao].strip().lower().lstrip("~")
                    if not key:
                        continue
                    typ, desc, op = cell(row, i_type), cell(row, i_desc), cell(row, i_op)
                    flags = parse_dbflags(row[i_flags]) if (i_flags is not None and i_flags < len(row)) else 0
                    out[key] = (cell(row, i_reg), _i(typ) if typ else None,
                                _i(desc) if desc else None, _i(op) if op else None, flags)

        log.info("loaded %d aircraft from CSV %s", len(out), path)
    except OSError as e:
        log.warning("failed to load AIRCRAFT_CSV %s: %s", path, e)
    return out


# --- per-aircraft analysis (pure, no DB) ------------------------------------
def analyze(ac, now_ts, state, csv_meta):
    """Compute what to write for one aircraft entry. Reads previous in-memory
    state read-only and returns a decision dict; state is mutated by the caller
    only after the transaction commits."""
    hexid = ac.get("hex")
    if not hexid:
        return None
    hexid = hexid.lower()

    seen = ac.get("seen") or 0
    msg_ts = now_ts - seen
    msg_dt = dt(msg_ts)

    meta = resolve_meta(ac, csv_meta)
    ac_row = (hexid, meta["reg"], meta["typ"], meta["desc"], meta["op"],
              meta["mil"], meta["intr"], meta["pia"], meta["ladd"], meta["year"],
              msg_dt, msg_dt)

    callsign = (ac.get("flight") or "").strip() or None
    squawk = ac.get("squawk")
    msgs = intornone(ac.get("messages"))

    lat = num(ac.get("lat"))
    lon = num(ac.get("lon"))
    has_pos = lat is not None and lon is not None
    seen_pos = ac.get("seen_pos")
    alt, on_ground = parse_alt(ac.get("alt_baro", ac.get("altitude")))
    gs = num(ac.get("gs"))
    track = num(ac.get("track"))
    baro_rate = ac.get("baro_rate")
    if baro_rate is None:
        baro_rate = ac.get("geom_rate")
    baro_rate = intornone(baro_rate)
    source = ac.get("type")

    prev = state.get(hexid)
    new_flight = prev is None
    if prev is not None:
        gap = msg_ts - prev["last_msg_ts"]
        if gap > FLIGHT_GAP or (callsign and prev["callsign"] and callsign != prev["callsign"]):
            new_flight = True

    if new_flight:
        start_ts = msg_ts
        cs = callsign
        msg_count = msgs
        max_alt = alt
        prev_flight_id = None
        prev_lp = None
    else:
        start_ts = prev["start_ts"]
        cs = callsign or prev["callsign"]
        msg_count = msgs if msgs is not None else prev["msg_count"]
        max_alt = prev["max_alt"]
        if alt is not None:
            max_alt = alt if max_alt is None else max(max_alt, alt)
        prev_flight_id = prev["flight_id"]
        prev_lp = prev["lp"]

    # Trail thinning: store a point only on meaningful change or after a max gap.
    store_pos = False
    pos = None
    pos_lp = prev_lp
    if has_pos and seen_pos is not None:
        pos_ts = now_ts - seen_pos
        lp = prev_lp
        if lp is None:
            store_pos = True
        else:
            delta = pos_ts - lp["ts"]
            if delta <= 0:
                store_pos = False  # same/old fix, nothing new
            elif delta >= MAX_POINT_GAP:
                store_pos = True
            elif on_ground != lp["ground"]:
                store_pos = True
            elif track is not None and lp["track"] is not None and angdiff(track, lp["track"]) > MIN_TRACK_DEG:
                store_pos = True
            elif alt is not None and lp["alt"] is not None and abs(alt - lp["alt"]) > MIN_ALT_FT:
                store_pos = True
            elif gs is not None and lp["gs"] is not None and abs(gs - lp["gs"]) > MIN_GS_KT:
                store_pos = True
        if store_pos:
            pos = {"time": dt(pos_ts), "lat": lat, "lon": lon, "alt": alt,
                   "ground": on_ground, "gs": gs, "track": track,
                   "baro_rate": baro_rate, "source": source}
            pos_lp = {"ts": pos_ts, "lat": lat, "lon": lon, "alt": alt,
                      "gs": gs, "track": track, "ground": on_ground}

    new_state = {"flight_id": prev_flight_id, "callsign": cs, "start_ts": start_ts,
                 "last_msg_ts": msg_ts, "msg_count": msg_count, "max_alt": max_alt,
                 "lp": pos_lp}

    return {"hex": hexid, "ac_row": ac_row, "new_flight": new_flight,
            "prev_flight_id": prev_flight_id, "callsign": cs, "squawk": squawk,
            "start": dt(start_ts), "end": msg_dt, "msg_count": msg_count,
            "max_alt": max_alt, "store_pos": store_pos, "pos": pos,
            "new_state": new_state}


# --- database writes --------------------------------------------------------
def insert_flight(cur, d):
    cur.execute(
        "INSERT INTO flights (icao_hex, callsign, squawk, start_time, end_time, "
        "msg_count, max_alt) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (d["hex"], d["callsign"], d["squawk"], d["start"], d["end"],
         d["msg_count"], d["max_alt"]))
    return cur.fetchone()[0]


def process(conn, data, state, csv_meta):
    """Apply one aircraft.json snapshot in a single transaction. Returns the
    number of position points written."""
    now_ts = data.get("now")
    if not isinstance(now_ts, (int, float)):
        now_ts = time.time()

    decisions = []
    for ac in data.get("aircraft") or []:
        d = analyze(ac, now_ts, state, csv_meta)
        if d:
            decisions.append(d)
    if not decisions:
        return 0

    pos_rows = []
    with conn.cursor() as cur:
        # aircraft first (flights has an FK to it)
        cur.executemany(AIRCRAFT_UPSERT, [d["ac_row"] for d in decisions])

        for d in decisions:
            d["flight_id"] = insert_flight(cur, d) if d["new_flight"] else d["prev_flight_id"]

        fupd = [(d["end"], d["msg_count"], d["max_alt"], d["flight_id"])
                for d in decisions if not d["new_flight"] and d["flight_id"] is not None]
        if fupd:
            cur.executemany(
                "UPDATE flights SET end_time=%s, msg_count=%s, max_alt=%s WHERE id=%s", fupd)

        for d in decisions:
            if d["store_pos"]:
                p = d["pos"]
                pos_rows.append((p["time"], d["hex"], d["flight_id"], p["lat"], p["lon"],
                                 p["alt"], p["ground"], p["gs"], p["track"],
                                 p["baro_rate"], p["source"]))
        if pos_rows:
            with cur.copy(COPY_POSITIONS) as cp:
                for r in pos_rows:
                    cp.write_row(r)

    conn.commit()

    # Commit succeeded -> it is now safe to advance the in-memory state.
    for d in decisions:
        ns = d["new_state"]
        ns["flight_id"] = d["flight_id"]
        state[d["hex"]] = ns
    return len(pos_rows)


def evict(state, now_ts):
    cutoff = now_ts - max(FLIGHT_GAP * 2, 600)
    for h in [h for h, s in state.items() if s["last_msg_ts"] < cutoff]:
        del state[h]


# --- source discovery / IO --------------------------------------------------
def is_url(s):
    return isinstance(s, str) and (s.startswith("http://") or s.startswith("https://"))


def normalize_url(src):
    """Accept either a full aircraft.json URL or a tar1090 base URL and return the
    full aircraft.json URL (so http://host and http://host/data/aircraft.json both
    work)."""
    if src.endswith(".json") or src.endswith(".json.gz"):
        return src
    return src.rstrip("/") + "/data/aircraft.json"


def detect_source(arg):
    """Return where to read aircraft.json from: an http(s) URL or a directory.
    A URL (from the CLI arg, AIRCRAFT_URL or SOURCE_DIR) wins; otherwise probe the
    usual decoder run dirs for an aircraft.json file."""
    for s in (arg, AIRCRAFT_URL, os.environ.get("SOURCE_DIR", "")):
        if is_url(s):
            return normalize_url(s)

    candidates = []
    if arg:
        candidates.append(arg)
    if os.environ.get("SOURCE_DIR"):
        candidates.append(os.environ["SOURCE_DIR"])
    candidates += SOURCE_DIRS_FALLBACK
    for c in candidates:
        if c and (os.path.exists(os.path.join(c, "aircraft.json"))
                  or os.path.exists(os.path.join(c, "aircraft.json.gz"))):
            return c
    return candidates[0] if candidates else None


def read_aircraft_url(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = resp.read()
    except (OSError, ValueError) as e:
        log.debug("could not fetch %s: %s", url, e)
        return None
    try:
        if url.endswith(".gz") or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, OSError) as e:
        log.debug("could not parse %s: %s", url, e)
        return None


def read_aircraft_json(src):
    if is_url(src):
        return read_aircraft_url(src)
    for name, opener in (("aircraft.json", open), ("aircraft.json.gz", gzip.open)):
        p = os.path.join(src, name)
        if os.path.exists(p):
            try:
                with opener(p, "rt", encoding="utf-8", errors="replace") as fh:
                    return json.load(fh)
            except (ValueError, OSError) as e:
                log.debug("could not read %s: %s", p, e)
                return None
    return None


def connect():
    while True:
        try:
            conn = psycopg.connect(DB_DSN, autocommit=False)
            log.info("connected to database")
            return conn
        except psycopg.Error as e:
            log.warning("database connect failed: %s; retrying in 5s", e)
            time.sleep(5)


# --- main loop --------------------------------------------------------------
_running = True


def _stop(_sig, _frm):
    global _running
    _running = False


def main():
    logging.basicConfig(
        level=os.environ.get("LOGLEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    arg = sys.argv[1] if len(sys.argv) > 1 else None
    src = detect_source(arg)
    log.info("tar1090-logger starting; reading aircraft.json from %s", src)
    csv_meta = load_csv_metadata(AIRCRAFT_CSV)

    state = {}
    conn = None
    points_min = 0
    next_stat = time.time() + 60
    warned_nodata = False

    while _running:
        loop_start = time.monotonic()
        try:
            if conn is None or conn.closed:
                conn = connect()
            data = read_aircraft_json(src)
            if data and "aircraft" in data:
                warned_nodata = False
                points_min += process(conn, data, state, csv_meta)
                evict(state, data.get("now") or time.time())
            elif not warned_nodata:
                log.warning("no aircraft.json from %s yet (is readsb/tar1090 reachable?)", src)
                warned_nodata = True

            if time.time() >= next_stat:
                log.info("%d aircraft in memory, %d points written in last minute",
                         len(state), points_min)
                points_min = 0
                next_stat = time.time() + 60
        except psycopg.Error as e:
            log.warning("database error: %s; reconnecting", e)
            if conn is not None:
                try:
                    conn.rollback()
                except psycopg.Error:
                    pass
                try:
                    conn.close()
                except psycopg.Error:
                    pass
            conn = None
            time.sleep(2)
        except Exception:
            log.exception("unexpected error in main loop")
            time.sleep(2)

        elapsed = time.monotonic() - loop_start
        if _running and elapsed < LOG_INTERVAL:
            time.sleep(LOG_INTERVAL - elapsed)

    if conn is not None:
        try:
            conn.close()
        except psycopg.Error:
            pass
    log.info("tar1090-logger stopped")


if __name__ == "__main__":
    main()
