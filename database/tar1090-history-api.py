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
    return {"hex": hexid, "points": points, "files": files}


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
