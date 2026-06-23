#!/usr/bin/env python3
"""tar1090 history API — a tiny read-only HTTP service that powers the custom
history search & replay page.

It does two things the static tar1090 frontend can't:
  * /api/search  — query the aircraft/flights INDEX (Postgres) for "every appearance
                   of callsign/registration/type/operator/military in a date range".
  * /api/trace   — read that flight's actual trail on demand from the globe_history
                   trace files readsb already writes (no positions are duplicated).
It also serves the search page itself (index.html) so everything is same-origin.

Stdlib only (plus psycopg, already used by the logger). Read-only; intended for a
trusted LAN. Configure via environment (see tar1090-history-api.default).
"""

import gzip
import json
import logging
import os
import struct
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg
from psycopg.types.json import Jsonb

# sibling helper (alerting MQTT); importable because we add our own dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tar1090_mqtt as mq        # noqa: E402
try:
    import tar1090_heatmap_import as hm        # noqa: E402  (the Settings "Historical import" job)
except Exception as _e:          # pragma: no cover -- import feature just disabled if missing
    hm = None
    logging.getLogger("tar1090-history-api").warning("heatmap importer unavailable: %s", _e)

log = logging.getLogger("tar1090-history-api")

DB_DSN      = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
GLOBE_DIR   = os.environ.get("GLOBE_HISTORY_DIR", "/var/globe_history")
# readsb's live run dir (same container) -- holds traces/<xx>/trace_full_<hex>.json for
# aircraft it's currently tracking, used to draw the trail of a still-active flight.
RUN_DIR     = os.environ.get("READSB_RUN_DIR", os.environ.get("SOURCE_DIR", "/run/readsb"))
PORT        = int(os.environ.get("HISTORY_API_PORT", "8090"))
BIND        = os.environ.get("HISTORY_API_BIND", "0.0.0.0")
WEB_DIR     = os.environ.get("HISTORY_WEB_DIR",
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "history"))
MAX_RESULTS = int(os.environ.get("HISTORY_MAX_RESULTS", "2000"))
# "Show all trails" caps: how many flights to draw at once, and how many distinct 30-min
# heatmap chunks we'll read for one overview request (bounds work for wide time ranges).
MAX_TRAILS       = int(os.environ.get("HISTORY_MAX_TRAILS", "300"))
MAX_TRACE_CHUNKS = int(os.environ.get("HISTORY_MAX_TRACE_CHUNKS", "400"))
# A flight is "active" (still in the air / being tracked) if the logger has refreshed its
# end_time within this many seconds -- its trail is still being recorded, so it may be
# incomplete or not yet archived to globe_history.
ACTIVE_WINDOW_SEC = int(os.environ.get("HISTORY_ACTIVE_WINDOW_SEC", "120"))

# --- database ---------------------------------------------------------------
_conn = None
_lock = threading.Lock()


def db():
    """One shared autocommit connection, reconnected on demand."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DB_DSN, autocommit=True)
    return _conn


def query(sql, params):
    with _lock:
        try:
            cur = db().execute(sql, params)
            cols = [d.name for d in cur.description] if cur.description else []
            rows = cur.fetchall() if cur.description else []
            return [dict(zip(cols, row)) for row in rows]
        except psycopg.Error:
            global _conn
            if _conn is not None:
                try:
                    _conn.close()
                except psycopg.Error:
                    pass
            _conn = None
            raise


# Write that may return rows (RETURNING) or nothing; same reconnect-on-error as query().
execute = query


def ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


# --- search -----------------------------------------------------------------
def parse_time(v, default):
    """Accept epoch ms, epoch s, or ISO 8601; return aware UTC datetime."""
    if v is None or v == "":
        return default
    try:
        n = float(v)
        if n > 1e12:      # ms
            n /= 1000.0
        return datetime.fromtimestamp(n, tz=timezone.utc)
    except (ValueError, TypeError):
        pass
    try:
        s = v.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return default


def search(q):
    now = datetime.now(timezone.utc)
    t_from = parse_time(q.get("from", [None])[0], now - timedelta(hours=24))
    t_to   = parse_time(q.get("to", [None])[0], now)

    where = ["start_time <= %s AND end_time >= %s"]   # flight overlaps the window
    params = [t_to, t_from]

    def like(field, key):
        val = (q.get(key, [""])[0] or "").strip()
        if val:
            where.append(f"{field} ILIKE %s")
            params.append("%" + val + "%")

    like("callsign", "callsign")
    like("registration", "registration")
    like("icao_type", "type")
    like("operator", "operator")

    free = (q.get("q", [""])[0] or "").strip()
    if free:
        where.append("(callsign ILIKE %s OR registration ILIKE %s OR icao_type ILIKE %s "
                     "OR operator ILIKE %s OR icao_hex ILIKE %s)")
        params += ["%" + free + "%"] * 5

    mil = (q.get("mil", ["any"])[0] or "any").lower()
    if mil == "mil":
        where.append("military")
    elif mil == "civ":
        where.append("NOT military")

    try:
        limit = min(max(1, int(q.get("limit", [500])[0])), MAX_RESULTS)
    except ValueError:
        limit = 500
    try:
        offset = max(0, int(q.get("offset", [0])[0]))
    except ValueError:
        offset = 0

    where_sql = " AND ".join(where)
    total = query("SELECT count(*) AS n FROM v_flights WHERE " + where_sql, list(params))[0]["n"]

    sql = ("SELECT id, icao_hex, callsign, registration, icao_type, operator, military, "
           "start_time, end_time, max_alt, "
           "EXTRACT(EPOCH FROM (end_time - start_time))::int AS duration_s, "
           "(end_time >= now() - make_interval(secs => %s)) AS active "
           "FROM v_flights WHERE " + where_sql +
           " ORDER BY start_time DESC LIMIT %s OFFSET %s")

    rows = query(sql, [ACTIVE_WINDOW_SEC] + list(params) + [limit, offset])
    for r in rows:
        r["start"] = ms(r.pop("start_time"))
        r["end"] = ms(r.pop("end_time"))
        r["military"] = bool(r["military"])
        r["active"] = bool(r["active"])
    return {"flights": rows, "from": ms(t_from), "to": ms(t_to),
            "count": len(rows), "total": total, "limit": limit, "offset": offset}


def options(q):
    field = (q.get("field", [""])[0] or "").strip()
    col = {"callsign": "callsign", "registration": "registration",
           "type": "icao_type", "operator": "operator"}.get(field)
    if not col:
        return {"values": []}
    now = datetime.now(timezone.utc)
    t_from = parse_time(q.get("from", [None])[0], now - timedelta(days=7))
    t_to   = parse_time(q.get("to", [None])[0], now)
    rows = query(
        f"SELECT DISTINCT {col} AS v FROM v_flights WHERE {col} IS NOT NULL AND {col} <> '' "
        "AND start_time <= %s AND end_time >= %s ORDER BY 1 LIMIT 5000", [t_to, t_from])
    return {"values": [r["v"] for r in rows]}


# --- trail (read from globe_history) ----------------------------------------
def _read_json_maybe_gzip(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8", "replace"))


# --- heatmap fallback (readsb's globe_history/.../heatmap/NN.bin.ttf chunks) ----------
# readsb ALWAYS writes these (they power the heatmap + replay), unlike the per-aircraft
# trace_full files which need READSB_ENABLE_TRACES. Each chunk is a 30-min file of
# 16-byte int32 records [hex(+flags), lat*1e6, lon*1e6, gs<<16|alt]; time "slices" (one
# per `ival` seconds) are delimited by HEAT_MAGIC. We read the chunks covering the leg,
# pick out one hex, and timestamp each fix from its slice index -- the same data the
# native replay animates from, just slightly coarser than a full trace.
HEAT_MAGIC = 0xe7f7c9d


def _read_bytes_maybe_gzip(path):
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return raw


def _heat_alt(f3):
    a = f3 & 0xFFFF
    if a & 0x8000:
        a -= 0x10000
    if a == -123:        # on ground
        return 0
    if a == -124:        # altitude unknown
        return None
    return a * 25        # stored in units of 25 ft


def _cbase_path(cbase):
    """Filesystem path of the 30-min heatmap chunk whose UTC window starts at epoch cbase."""
    d = datetime.fromtimestamp(cbase, tz=timezone.utc)
    idx = 2 * d.hour + (1 if d.minute >= 30 else 0)
    return os.path.join(GLOBE_DIR, f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}",
                        "heatmap", f"{idx:02d}.bin.ttf")


def _parse_heat_chunk(path):
    """Return (ints, n, slices, ival) for a heatmap chunk, or None if unreadable/empty."""
    try:
        raw = _read_bytes_maybe_gzip(path)
    except (OSError, ValueError) as e:
        log.warning("heatmap read failed %s: %s", path, e)
        return None
    if not raw or len(raw) % 16:
        return None
    n = len(raw) // 4
    a = struct.unpack("<%di" % n, raw)
    sl = [i for i in range(0, n, 4) if a[i] == HEAT_MAGIC]
    if not sl:
        return None
    ival = (a[sl[0] + 3] & 0xFFFF) / 1000.0 or 15.0
    return a, n, sl, ival


def _chunk_bases(lo, hi, step=1800):
    """Epoch starts of every 30-min chunk overlapping [lo, hi]."""
    out, base = [], int(lo) // step * step
    while base <= hi:
        out.append(base)
        base += step
    return out


def _heatmap_points(hexid, lo, hi, pad=120):
    try:
        target = int(hexid, 16) & 0xFFFFFF
    except ValueError:
        return []
    lo_p, hi_p = lo - pad, hi + pad
    out = []
    for cbase in _chunk_bases(lo_p, hi_p):
        path = _cbase_path(cbase)
        if not os.path.exists(path):
            continue
        parsed = _parse_heat_chunk(path)
        if not parsed:
            continue
        a, n, sl, ival = parsed
        for si, spos in enumerate(sl):
            t = cbase + si * ival
            if t < lo_p or t > hi_p:
                continue
            j = spos + 4
            while j < n and a[j] != HEAT_MAGIC:
                if (a[j] & 0xFFFFFF) == target:
                    lat, lon = a[j + 1] / 1e6, a[j + 2] / 1e6
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                        out.append([int(t * 1000), round(lat, 5), round(lon, 5),
                                    _heat_alt(a[j + 3])])
                j += 4
    out.sort(key=lambda p: p[0])
    return out


# Turn one readsb trace_full doc ({"timestamp": base, "trace": [[dt, lat, lon, alt, ...]]})
# into [ms, lat, lon, alt] points, clipped to [lo, hi] epoch seconds (either may be None).
def _trace_doc_points(doc, lo, hi, pad=120):
    out = []
    base = doc.get("timestamp") or 0
    for e in doc.get("trace") or []:
        if len(e) < 4 or e[1] is None or e[2] is None:
            continue
        t = base + e[0]
        if lo is not None and t < lo - pad:
            continue
        if hi is not None and t > hi + pad:
            continue
        alt = e[3]
        if alt == "ground":
            alt = 0
        out.append([int(t * 1000), round(e[1], 5), round(e[2], 5),
                    alt if isinstance(alt, (int, float)) else None])
    return out


# Read a flight's still-live trail straight from readsb's run dir. readsb writes TWO files per
# active aircraft: trace_full_<hex>.json (the whole flight, rewritten occasionally) and
# trace_recent_<hex>.json (the last few minutes, rewritten constantly). The main tar1090 map
# merges both; we do too, otherwise the freshest end of a live trail is missing. Lets an
# in-progress ("LIVE") flight show its trail-so-far before it's archived to globe_history.
# Returns clipped, de-duplicated, time-sorted points.
def _live_trace_points(hexid, t_from, t_to, pad=120):
    base = os.path.join(RUN_DIR, "traces", hexid[-2:])
    lo = t_from.timestamp() if t_from else None
    hi = t_to.timestamp() if t_to else None
    merged = {}
    for name in (f"trace_full_{hexid}.json", f"trace_recent_{hexid}.json"):
        path = os.path.join(base, name)
        if not os.path.exists(path):
            continue
        try:
            doc = _read_json_maybe_gzip(path)
        except (OSError, ValueError) as e:
            log.warning("live trace read failed %s: %s", path, e)
            continue
        for p in _trace_doc_points(doc, lo, hi, pad):
            merged[p[0]] = p                      # key by ms timestamp; recent extends full
    return sorted(merged.values(), key=lambda p: p[0])


# Read a flight's per-aircraft trace_full_<hex>.json from globe_history for the UTC day(s)
# it spans, clipped to [t_from, t_to] (aware datetimes; either may be None). Returns
# (points, files_read). This is the sharp, preferred source written when READSB_ENABLE_TRACES
# is on -- shared by both the single-flight (trace) and batch (traces) readers.
def _trace_file_points(hexid, t_from, t_to, pad=120):
    days = []
    if t_from and t_to:
        d = datetime(t_from.year, t_from.month, t_from.day, tzinfo=timezone.utc)
        last = datetime(t_to.year, t_to.month, t_to.day, tzinfo=timezone.utc)
        while d <= last:
            days.append(d)
            d += timedelta(days=1)
    else:
        days = [datetime.now(timezone.utc)]

    lo = t_from.timestamp() if t_from else None
    hi = t_to.timestamp() if t_to else None

    points = []
    files = 0
    for day in days:
        path = os.path.join(GLOBE_DIR, f"{day.year:04d}", f"{day.month:02d}", f"{day.day:02d}",
                            "traces", hexid[-2:], f"trace_full_{hexid}.json")
        if not os.path.exists(path):
            continue
        try:
            doc = _read_json_maybe_gzip(path)
        except (OSError, ValueError) as e:
            log.warning("trace read failed %s: %s", path, e)
            continue
        files += 1
        points.extend(_trace_doc_points(doc, lo, hi, pad))
    points.sort(key=lambda p: p[0])
    return points, files


def trace(q):
    hexid = (q.get("hex", [""])[0] or "").strip().lower().lstrip("~")
    if not hexid or any(c not in "0123456789abcdef" for c in hexid):
        return {"hex": hexid, "points": [], "error": "bad hex"}
    t_from = parse_time(q.get("start", [None])[0], None)
    t_to   = parse_time(q.get("end", [None])[0], None)

    points, files = _trace_file_points(hexid, t_from, t_to)
    source = "trace"
    # Still-active flight whose trail isn't archived to globe_history yet -> read readsb's
    # live trace from its run dir so the in-progress trail still draws.
    if not points:
        live = _live_trace_points(hexid, t_from, t_to)
        if live:
            points, source = live, "live"
    # No trace file at all -> fall back to the heatmap chunks readsb always writes (same
    # positions the native replay uses), so existing history still works.
    if not points and t_from and t_to:
        points = _heatmap_points(hexid, t_from.timestamp(), t_to.timestamp())
        source = "heatmap"
    return {"hex": hexid, "points": points, "files": files, "source": source}


def traces(q):
    """Batch trail reader for the "show all / multiple" overview: given flight ids, hand back
    every flight's trail. Each flight prefers its own sharp per-aircraft trace_full file (same
    source the single-flight reader uses); flights that don't have one fall back to the
    heatmap chunks, which are read ONCE each and shared across those flights."""
    raw = (q.get("ids", [""])[0] or "").strip()
    if not raw:
        return {"flights": {}, "chunks": 0}
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()][:MAX_TRAILS]
    except ValueError:
        return {"error": "bad ids"}
    if not ids:
        return {"flights": {}, "chunks": 0}

    rows = query("SELECT id, icao_hex, start_time, end_time FROM v_flights WHERE id = ANY(%s)",
                 [ids])
    pad = 120
    out = {}
    by_hex = {}        # hex_low24 -> [(flight_id, lo, hi), ...]  (only flights w/o a trace file)
    bases = set()
    for r in rows:
        hexid = (r["icao_hex"] or "").strip().lower().lstrip("~")
        if not hexid or any(c not in "0123456789abcdef" for c in hexid):
            continue
        # Prefer the same per-aircraft trace file the single-flight view uses.
        pts, _ = _trace_file_points(hexid, r["start_time"], r["end_time"], pad)
        if not pts:
            # Still-active flight -> use readsb's live run-dir trace for its trail-so-far.
            pts = _live_trace_points(hexid, r["start_time"], r["end_time"], pad)
        if pts:
            out[r["id"]] = pts
            continue
        # No trace file for this flight -> queue it for the shared heatmap pass.
        target = int(hexid, 16) & 0xFFFFFF
        lo = r["start_time"].timestamp() - pad
        hi = r["end_time"].timestamp() + pad
        by_hex.setdefault(target, []).append((r["id"], lo, hi))
        bases.update(_chunk_bases(lo, hi))

    if len(bases) > MAX_TRACE_CHUNKS:
        # Too many heatmap chunks to scan, but still return any trace-file trails we have.
        for v in out.values():
            v.sort(key=lambda p: p[0])
        return {"flights": out, "truncated": True, "chunks": len(bases),
                "max_chunks": MAX_TRACE_CHUNKS}

    for cbase in sorted(bases):
        path = _cbase_path(cbase)
        if not os.path.exists(path):
            continue
        parsed = _parse_heat_chunk(path)
        if not parsed:
            continue
        a, n, sl, ival = parsed
        for si, spos in enumerate(sl):
            t = cbase + si * ival
            tm = int(t * 1000)
            j = spos + 4
            while j < n and a[j] != HEAT_MAGIC:
                wins = by_hex.get(a[j] & 0xFFFFFF)
                if wins:
                    lat, lon = a[j + 1] / 1e6, a[j + 2] / 1e6
                    if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                        pt = None
                        for fid, lo, hi in wins:
                            if lo <= t <= hi:
                                if pt is None:
                                    pt = [tm, round(lat, 5), round(lon, 5), _heat_alt(a[j + 3])]
                                out.setdefault(fid, []).append(pt)
                j += 4
    for v in out.values():
        v.sort(key=lambda p: p[0])
    return {"flights": out, "chunks": len(bases), "truncated": False}


# --- live ('Live' tab) ------------------------------------------------------
_SITE = "unset"
def _site_location():
    """Receiver location for the live distance column. Prefer explicit SITE_LAT/SITE_LON,
    fall back to the LAT/LONG env the ADS-B images already take, then readsb's
    receiver.json if it exposes a position. Cached -- it never changes at runtime."""
    global _SITE
    if _SITE != "unset":
        return _SITE
    for la, lo in (("SITE_LAT", "SITE_LON"), ("LAT", "LONG"), ("LAT", "LON")):
        try:
            _SITE = {"lat": float(os.environ[la]), "lon": float(os.environ[lo])}
            return _SITE
        except (KeyError, ValueError):
            continue
    try:
        rj = _read_json_maybe_gzip(os.path.join(RUN_DIR, "receiver.json"))
        if rj.get("lat") is not None and rj.get("lon") is not None:
            _SITE = {"lat": float(rj["lat"]), "lon": float(rj["lon"])}
            return _SITE
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        pass
    _SITE = None
    return _SITE



# readsb continuously rewrites aircraft.json in its run dir with every aircraft it
# currently sees. We pass a slimmed snapshot (positioned aircraft only) to the Live
# map, which polls this once a second.
def live(q):
    path = os.path.join(RUN_DIR, "aircraft.json")
    try:
        doc = _read_json_maybe_gzip(path)
    except FileNotFoundError:
        return {"now": None, "aircraft": [], "count_total": 0, "count_pos": 0,
                "error": f"live feed not found ({path}) — is readsb running in this container?"}
    acs = doc.get("aircraft") or []
    out = []
    for a in acs:
        if a.get("lat") is None or a.get("lon") is None:
            continue
        out.append({
            "hex": a.get("hex"), "flight": (a.get("flight") or "").strip(),
            "r": a.get("r"), "t": a.get("t"), "desc": a.get("desc"),
            "operator": None, "military": False,
            "lat": a.get("lat"), "lon": a.get("lon"),
            "alt": a.get("alt_baro"), "alt_geom": a.get("alt_geom"),
            "gs": a.get("gs"), "track": a.get("track"), "baro_rate": a.get("baro_rate"),
            "squawk": a.get("squawk"), "category": a.get("category"),
            "seen": a.get("seen"), "seen_pos": a.get("seen_pos"), "rssi": a.get("rssi"),
        })
    # readsb's aircraft.json rarely carries registration/type, so fill those (and the
    # military flag) from our own aircraft index -- the same source the History table uses.
    hexes = [a["hex"] for a in out if a.get("hex")]
    if hexes:
        try:
            meta = {r["icao_hex"]: r for r in query(
                "SELECT icao_hex, registration, icao_type, type_desc, operator, military "
                "FROM aircraft WHERE icao_hex = ANY(%s)", (hexes,))}
            for a in out:
                m = meta.get(a["hex"])
                if not m:
                    continue
                a["r"] = a["r"] or m["registration"]
                a["t"] = a["t"] or m["icao_type"]
                a["desc"] = a["desc"] or m["type_desc"]
                a["operator"] = m["operator"]
                a["military"] = bool(m["military"])
        except Exception:                                # noqa: BLE001 -- DB down: still serve live
            log.warning("live: aircraft-index enrichment skipped (db unavailable)")
    return {"now": doc.get("now"), "messages": doc.get("messages"), "site": _site_location(),
            "count_total": len(acs), "count_pos": len(out), "aircraft": out}


# --- alerts -----------------------------------------------------------------
ALERT_DDL = """
CREATE TABLE IF NOT EXISTS alert_config (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  enabled boolean NOT NULL DEFAULT true,
  mqtt_host text, mqtt_port int NOT NULL DEFAULT 1883,
  mqtt_username text, mqtt_password text, mqtt_tls boolean NOT NULL DEFAULT false,
  base_topic text NOT NULL DEFAULT 'tar1090',
  ha_discovery boolean NOT NULL DEFAULT true,
  discovery_prefix text NOT NULL DEFAULT 'homeassistant',
  updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS alert_rules (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL, enabled boolean NOT NULL DEFAULT true,
  conditions jsonb NOT NULL DEFAULT '{}'::jsonb, zone jsonb, time_window jsonb,
  cooldown_sec int NOT NULL DEFAULT 1800,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS alert_log (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rule_id bigint, rule_name text, icao_hex text, callsign text, registration text,
  icao_type text, operator text, military boolean,
  lat double precision, lon double precision, alt int, squawk text,
  fired_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS alert_log_fired_idx ON alert_log (fired_at DESC);
"""
_alerts_ready = False


def _ensure_alerts():
    global _alerts_ready
    if not _alerts_ready:
        execute(ALERT_DDL, ())
        _alerts_ready = True


def alerts_rules_get(q):
    _ensure_alerts()
    return {"rules": query("SELECT id, name, enabled, conditions, zone, time_window, "
                           "cooldown_sec FROM alert_rules ORDER BY id", ())}


def alerts_rule_save(body):
    _ensure_alerts()
    name = (body.get("name") or "").strip()
    if not name:
        return {"error": "name is required"}
    cond = Jsonb(body.get("conditions") or {})
    zone = Jsonb(body["zone"]) if body.get("zone") else None
    win  = Jsonb(body["time_window"]) if body.get("time_window") else None
    cd   = int(body.get("cooldown_sec") or 1800)
    enabled = bool(body.get("enabled", True))
    rid = body.get("id")
    if rid:
        execute("UPDATE alert_rules SET name=%s, enabled=%s, conditions=%s, zone=%s, "
                "time_window=%s, cooldown_sec=%s, updated_at=now() WHERE id=%s",
                (name, enabled, cond, zone, win, cd, int(rid)))
        return {"ok": True, "id": int(rid)}
    rows = execute("INSERT INTO alert_rules (name, enabled, conditions, zone, time_window, "
                   "cooldown_sec) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
                   (name, enabled, cond, zone, win, cd))
    return {"ok": True, "id": rows[0]["id"]}


def alerts_rule_delete(body):
    _ensure_alerts()
    if not body.get("id"):
        return {"error": "id required"}
    execute("DELETE FROM alert_rules WHERE id=%s", (int(body["id"]),))
    return {"ok": True}


def alerts_config_get(q):
    _ensure_alerts()
    rows = query("SELECT enabled, mqtt_host, mqtt_port, mqtt_username, mqtt_password, "
                 "mqtt_tls, base_topic, ha_discovery, discovery_prefix "
                 "FROM alert_config WHERE id=1", ())
    if not rows:
        return {"config": {"enabled": True, "mqtt_port": 1883, "mqtt_tls": False,
                           "base_topic": "tar1090", "ha_discovery": True,
                           "discovery_prefix": "homeassistant", "has_password": False}}
    c = rows[0]
    c["has_password"] = bool(c.pop("mqtt_password", None))   # never expose the password
    return {"config": c}


def alerts_config_save(body):
    _ensure_alerts()
    cur = query("SELECT mqtt_password FROM alert_config WHERE id=1", ())
    pw = body.get("mqtt_password")
    if pw in (None, ""):                                     # blank -> keep the stored password
        pw = cur[0]["mqtt_password"] if cur else None
    execute("INSERT INTO alert_config (id, enabled, mqtt_host, mqtt_port, mqtt_username, "
            "mqtt_password, mqtt_tls, base_topic, ha_discovery, discovery_prefix, updated_at) "
            "VALUES (1,%s,%s,%s,%s,%s,%s,%s,%s,%s, now()) "
            "ON CONFLICT (id) DO UPDATE SET enabled=EXCLUDED.enabled, mqtt_host=EXCLUDED.mqtt_host, "
            "mqtt_port=EXCLUDED.mqtt_port, mqtt_username=EXCLUDED.mqtt_username, "
            "mqtt_password=EXCLUDED.mqtt_password, mqtt_tls=EXCLUDED.mqtt_tls, "
            "base_topic=EXCLUDED.base_topic, ha_discovery=EXCLUDED.ha_discovery, "
            "discovery_prefix=EXCLUDED.discovery_prefix, updated_at=now()",
            (bool(body.get("enabled", True)), body.get("mqtt_host") or None,
             int(body.get("mqtt_port") or 1883), body.get("mqtt_username") or None, pw,
             bool(body.get("mqtt_tls", False)), body.get("base_topic") or "tar1090",
             bool(body.get("ha_discovery", True)), body.get("discovery_prefix") or "homeassistant"))
    return {"ok": True}


def alerts_test(body):
    _ensure_alerts()
    rows = query("SELECT enabled, mqtt_host, mqtt_port, mqtt_username, mqtt_password, "
                 "mqtt_tls, base_topic FROM alert_config WHERE id=1", ())
    if not rows or not rows[0].get("mqtt_host"):
        return {"ok": False, "error": "save an MQTT broker host first"}
    cfg = rows[0]
    ok, err = mq.publish_once(cfg, (cfg.get("base_topic") or "tar1090") + "/test",
                              {"test": True, "msg": "tar1090 alert test",
                               "time": datetime.now(timezone.utc).isoformat()})
    return {"ok": ok, "error": err}


def alerts_log_get(q):
    _ensure_alerts()
    try:
        limit = min(int(q.get("limit", [100])[0]), 1000)
    except ValueError:
        limit = 100
    where, params = "", []
    since = q.get("since", [None])[0]
    if since:
        try:
            params.append(int(since))
            where = "WHERE id > %s"
        except ValueError:
            pass
    params.append(limit)
    rows = query("SELECT id, rule_id, rule_name, icao_hex, callsign, registration, icao_type, "
                 "operator, military, lat, lon, alt, squawk, fired_at FROM alert_log "
                 + where + " ORDER BY fired_at DESC LIMIT %s", params)
    for r in rows:
        r["fired_at"] = ms(r["fired_at"])
    return {"alerts": rows}


# --- historical import (heatmap .ttf -> searchable index) -------------------
# A resumable background job: walk globe_history heatmap chunks and fill the aircraft/flights
# index (see tar1090_heatmap_import). Progress + a checkpoint live in import_state so the
# Settings tab can show it and so a crash / container restart picks up where it left off.
IMPORT_DDL = """
CREATE TABLE IF NOT EXISTS import_state (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  status text NOT NULL DEFAULT 'idle',             -- idle|running|done|error|cancelled
  globe_dir text,
  last_cbase double precision NOT NULL DEFAULT 0,  -- checkpoint: resume from this chunk epoch
  chunks_total int NOT NULL DEFAULT 0, chunks_done int NOT NULL DEFAULT 0,
  flights_added bigint NOT NULL DEFAULT 0, fixes_seen bigint NOT NULL DEFAULT 0,
  message text, started_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO import_state (id, status) VALUES (1, 'idle') ON CONFLICT DO NOTHING;
"""
_import_ready = False
_import_lock = threading.Lock()
_import_thread = None
_import_cancel = threading.Event()


def _ensure_import():
    global _import_ready
    if not _import_ready:
        execute(IMPORT_DDL, ())
        _import_ready = True


def _default_csv():
    csv = os.environ.get("AIRCRAFT_CSV", "").strip()
    if csv:
        return csv
    bundled = "/usr/local/share/tar1090/aircraft.csv.gz"     # present in the all-in-one image
    return bundled if os.path.exists(bundled) else ""


def _import_worker(globe):
    last = [0.0]

    def progress(s):
        now = time.monotonic()
        if now - last[0] < 1.5 and s["chunks_done"] < s["chunks_total"]:
            return                                           # throttle UI writes
        last[0] = now
        try:
            execute("UPDATE import_state SET chunks_total=%s, chunks_done=%s, flights_added=%s, "
                    "fixes_seen=%s, last_cbase=%s, updated_at=now() WHERE id=1",
                    (s["chunks_total"], s["chunks_done"], s["flights"], s["fixes"], s["checkpoint"]))
        except psycopg.Error:
            pass

    imp = None
    try:
        meta = hm.load_csv_metadata(_default_csv())
        row = query("SELECT last_cbase FROM import_state WHERE id=1", ())
        resume_from = float(row[0]["last_cbase"]) if row and row[0]["last_cbase"] else 0.0
        conn = psycopg.connect(DB_DSN)
        conn.autocommit = True
        try:
            imp = hm.Importer(conn, meta, resume_from=resume_from,
                              cancel=_import_cancel.is_set, on_progress=progress)
            result = imp.run(globe)
        finally:
            conn.close()
        msg = ("stopped — press Start to resume" if result == "cancelled"
               else f"imported {imp.n_flights} flight legs from {imp.n_points} fixes")
        execute("UPDATE import_state SET status=%s, message=%s, chunks_total=%s, chunks_done=%s, "
                "flights_added=%s, fixes_seen=%s, last_cbase=%s, updated_at=now() WHERE id=1",
                ("cancelled" if result == "cancelled" else "done", msg, imp.total,
                 imp.skipped + imp.processed, imp.n_flights, imp.n_points, imp.committed_checkpoint))
    except Exception as e:                               # noqa: BLE001
        log.exception("historical import failed")
        try:
            execute("UPDATE import_state SET status='error', message=%s, updated_at=now() WHERE id=1",
                    (str(e),))
        except psycopg.Error:
            pass


def _launch_import(globe):
    global _import_thread
    _import_cancel.clear()
    _import_thread = threading.Thread(target=_import_worker, args=(globe,),
                                      name="heatmap-import", daemon=True)
    _import_thread.start()


def import_status(q):
    _ensure_import()
    rows = query("SELECT status, globe_dir, last_cbase, chunks_total, chunks_done, flights_added, "
                 "fixes_seen, message, started_at, updated_at FROM import_state WHERE id=1", ())
    r = dict(rows[0]) if rows else {"status": "idle"}
    r["started_at"] = ms(r.get("started_at"))
    r["updated_at"] = ms(r.get("updated_at"))
    r["running"] = bool(_import_thread and _import_thread.is_alive())
    r["default_globe_dir"] = GLOBE_DIR
    if not r.get("globe_dir"):
        r["globe_dir"] = GLOBE_DIR
    r["available"] = hm is not None
    return r


def import_start(body):
    if hm is None:
        return {"error": "the heatmap importer is not installed on this server"}
    with _import_lock:
        if _import_thread and _import_thread.is_alive():
            return {"error": "an import is already running"}
        globe = (body.get("globe_dir") or GLOBE_DIR or "/var/globe_history").strip()
        if not os.path.isdir(globe):
            return {"error": "path not found on the server: " + globe}
        _ensure_import()
        if body.get("restart"):
            execute("UPDATE import_state SET status='running', globe_dir=%s, last_cbase=0, "
                    "chunks_total=0, chunks_done=0, flights_added=0, fixes_seen=0, message=NULL, "
                    "started_at=now(), updated_at=now() WHERE id=1", (globe,))
        else:
            execute("UPDATE import_state SET status='running', globe_dir=%s, message=NULL, "
                    "started_at=COALESCE(started_at, now()), updated_at=now() WHERE id=1", (globe,))
        _launch_import(globe)
    return import_status({})


def import_stop(body):
    _import_cancel.set()
    return {"ok": True}


def _resume_import_on_start():
    """If the row says an import was running when the process died, continue from checkpoint."""
    if hm is None:
        return
    _ensure_import()
    row = query("SELECT status, globe_dir FROM import_state WHERE id=1", ())
    if row and row[0]["status"] == "running":
        globe = row[0]["globe_dir"] or GLOBE_DIR
        if os.path.isdir(globe):
            log.info("resuming interrupted historical import from checkpoint (%s)", globe)
            _launch_import(globe)
        else:
            execute("UPDATE import_state SET status='error', message=%s, updated_at=now() WHERE id=1",
                    ("globe_dir not found on restart: " + str(globe),))


# --- HTTP -------------------------------------------------------------------
ROUTES = {"/api/search": search, "/api/options": options,
          "/api/trace": trace, "/api/traces": traces, "/api/live": live,
          "/api/alerts/rules": alerts_rules_get, "/api/alerts/config": alerts_config_get,
          "/api/alerts/log": alerts_log_get, "/api/import/status": import_status}
POST_ROUTES = {"/api/alerts/rules": alerts_rule_save, "/api/alerts/rules/delete": alerts_rule_delete,
               "/api/alerts/config": alerts_config_save, "/api/alerts/test": alerts_test,
               "/api/import/start": import_start, "/api/import/stop": import_stop}
CONTENT = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
           ".css": "text/css", ".json": "application/json", ".ico": "image/x-icon"}


class Handler(BaseHTTPRequestHandler):
    server_version = "tar1090-history/1.0"

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path in ROUTES:
            try:
                self._send(200, ROUTES[path](parse_qs(u.query)))
            except psycopg.Error as e:
                self._send(503, {"error": "database unavailable", "detail": str(e)})
            except Exception as e:                       # noqa: BLE001
                log.exception("handler error")
                self._send(500, {"error": str(e)})
            return
        self._serve_static(path)

    do_HEAD = do_GET

    def do_POST(self):
        fn = POST_ROUTES.get(urlparse(self.path).path)
        if not fn:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw.decode("utf-8")) if raw else {}
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
        except (ValueError, TypeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        try:
            self._send(200, fn(body))
        except psycopg.Error as e:
            self._send(503, {"error": "database unavailable", "detail": str(e)})
        except Exception as e:                           # noqa: BLE001
            log.exception("post handler error")
            self._send(500, {"error": str(e)})

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = os.path.normpath(path).lstrip("/\\")
        full = os.path.join(WEB_DIR, rel)
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
            self._send(404, {"error": "not found"})
            return
        ctype = CONTENT.get(os.path.splitext(full)[1], "application/octet-stream")
        with open(full, "rb") as fh:
            self._send(200, fh.read(), ctype)

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("tar1090-history-api on %s:%d  (globe_history=%s, web=%s)", BIND, PORT, GLOBE_DIR, WEB_DIR)
    try:
        _ensure_alerts()                                 # create alert tables up front (best effort)
        _resume_import_on_start()                        # continue an interrupted import, if any
    except psycopg.Error as e:
        log.warning("tables not created yet (DB down?): %s -- will retry on first use", e)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
