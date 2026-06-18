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
import threading
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import psycopg

log = logging.getLogger("tar1090-history-api")

DB_DSN      = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
GLOBE_DIR   = os.environ.get("GLOBE_HISTORY_DIR", "/var/globe_history")
PORT        = int(os.environ.get("HISTORY_API_PORT", "8090"))
BIND        = os.environ.get("HISTORY_API_BIND", "0.0.0.0")
WEB_DIR     = os.environ.get("HISTORY_WEB_DIR",
                             os.path.join(os.path.dirname(os.path.abspath(__file__)), "history"))
MAX_RESULTS = int(os.environ.get("HISTORY_MAX_RESULTS", "2000"))

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
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
        except psycopg.Error:
            global _conn
            if _conn is not None:
                try:
                    _conn.close()
                except psycopg.Error:
                    pass
            _conn = None
            raise


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
        limit = min(int(q.get("limit", [500])[0]), MAX_RESULTS)
    except ValueError:
        limit = 500

    sql = ("SELECT id, icao_hex, callsign, registration, icao_type, operator, military, "
           "start_time, end_time, max_alt, "
           "EXTRACT(EPOCH FROM (end_time - start_time))::int AS duration_s "
           "FROM v_flights WHERE " + " AND ".join(where) +
           " ORDER BY start_time DESC LIMIT %s")
    params.append(limit)

    rows = query(sql, params)
    for r in rows:
        r["start"] = ms(r.pop("start_time"))
        r["end"] = ms(r.pop("end_time"))
        r["military"] = bool(r["military"])
    return {"flights": rows, "from": ms(t_from), "to": ms(t_to), "count": len(rows)}


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


def _heatmap_points(hexid, lo, hi, pad=120):
    try:
        target = int(hexid, 16) & 0xFFFFFF
    except ValueError:
        return []
    lo_p, hi_p, step = lo - pad, hi + pad, 1800
    chunks = []
    base = int(lo_p) // step * step
    while base <= hi_p:
        d = datetime.fromtimestamp(base, tz=timezone.utc)
        idx = 2 * d.hour + (1 if d.minute >= 30 else 0)
        chunks.append((base, os.path.join(
            GLOBE_DIR, f"{d.year:04d}", f"{d.month:02d}", f"{d.day:02d}",
            "heatmap", f"{idx:02d}.bin.ttf")))
        base += step

    out = []
    for cbase, path in chunks:
        if not os.path.exists(path):
            continue
        try:
            raw = _read_bytes_maybe_gzip(path)
        except (OSError, ValueError) as e:
            log.warning("heatmap read failed %s: %s", path, e)
            continue
        if not raw or len(raw) % 16:
            continue
        n = len(raw) // 4
        a = struct.unpack("<%di" % n, raw)
        sl = [i for i in range(0, n, 4) if a[i] == HEAT_MAGIC]
        if not sl:
            continue
        ival = (a[sl[0] + 3] & 0xFFFF) / 1000.0 or 15.0
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


def trace(q):
    hexid = (q.get("hex", [""])[0] or "").strip().lower().lstrip("~")
    if not hexid or any(c not in "0123456789abcdef" for c in hexid):
        return {"hex": hexid, "points": [], "error": "bad hex"}
    t_from = parse_time(q.get("start", [None])[0], None)
    t_to   = parse_time(q.get("end", [None])[0], None)

    # which UTC day file(s) does this flight touch
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
    pad = 120  # seconds of slack so we don't clip the ends of the leg

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
            points.append([int(t * 1000), round(e[1], 5), round(e[2], 5),
                           alt if isinstance(alt, (int, float)) else None])
    points.sort(key=lambda p: p[0])
    source = "trace"
    # No per-aircraft trace file (these need READSB_ENABLE_TRACES, and today's live trace
    # isn't archived into globe_history yet) -> fall back to the heatmap chunks readsb
    # always writes. Same positions the native replay uses, so existing history works.
    if not points and lo is not None and hi is not None:
        points = _heatmap_points(hexid, lo, hi, pad)
        source = "heatmap"
    return {"hex": hexid, "points": points, "files": files, "source": source}


# --- HTTP -------------------------------------------------------------------
ROUTES = {"/api/search": search, "/api/options": options, "/api/trace": trace}
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
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
