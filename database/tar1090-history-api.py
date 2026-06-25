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
import base64
import hashlib
import hmac
import secrets
import urllib.request
import urllib.error
from http.cookies import SimpleCookie
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, urlencode, quote

import psycopg
from psycopg.types.json import Jsonb

# sibling helper (alerting MQTT); importable because we add our own dir to sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tar1090_mqtt as mq        # noqa: E402
import airshow_types             # noqa: E402
import maneuver                  # noqa: E402
import patterns                  # noqa: E402
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
MAX_TRAILS       = int(os.environ.get("HISTORY_MAX_TRAILS", "2000"))
MAX_TRACE_CHUNKS = int(os.environ.get("HISTORY_MAX_TRACE_CHUNKS", "400"))
# A flight is "active" (still in the air / being tracked) if the logger has refreshed its
# end_time within this many seconds -- its trail is still being recorded, so it may be
# incomplete or not yet archived to globe_history.
ACTIVE_WINDOW_SEC = int(os.environ.get("HISTORY_ACTIVE_WINDOW_SEC", "120"))

# --- OIDC / Authentik single sign-on (optional) -----------------------------
# Off by default; set OIDC_ENABLED=true to require login on the whole report site.
# Authorization-Code flow with PKCE, confidential client. Two roles, mapped from the
# user's Authentik groups: "admin" (full access) and "viewer" (read-only -- no Alerts,
# no Settings, no alert creation). Sessions are stateless, signed cookies (no DB/store).
#
#   OIDC_ENABLED         "true" to turn it on
#   OIDC_ISSUER          e.g. https://authentik.example.com/application/o/<app-slug>/
#   OIDC_CLIENT_ID       provider client id
#   OIDC_CLIENT_SECRET   provider client secret (confidential client)
#   OIDC_REDIRECT_URL    e.g. https://reports.example.com/oidc/callback
#                        (optional; otherwise derived from the request Host/X-Forwarded-*)
#   OIDC_ADMIN_GROUP     Authentik group name granting the admin role  (default tar1090-admins)
#   OIDC_VIEWER_GROUP    Authentik group name granting the viewer role (default empty =
#                        any authenticated user is a viewer)
#   OIDC_SCOPES          default "openid profile email groups"  (the groups scope is required)
#   OIDC_SESSION_SECRET  HMAC key for the session cookie (defaults to the client secret)
#   OIDC_SESSION_TTL     session lifetime, seconds (default 28800 = 8h)
#   OIDC_COOKIE_SECURE   "true" (default) sets the Secure flag -- serve over HTTPS
OIDC_ENABLED        = os.environ.get("OIDC_ENABLED", "false").lower() == "true"
OIDC_ISSUER         = os.environ.get("OIDC_ISSUER", "").rstrip("/")
OIDC_CLIENT_ID      = os.environ.get("OIDC_CLIENT_ID", "")
OIDC_CLIENT_SECRET  = os.environ.get("OIDC_CLIENT_SECRET", "")
OIDC_REDIRECT_URL   = os.environ.get("OIDC_REDIRECT_URL", "")
OIDC_ADMIN_GROUP    = os.environ.get("OIDC_ADMIN_GROUP", "tar1090-admins")
OIDC_VIEWER_GROUP   = os.environ.get("OIDC_VIEWER_GROUP", "")
OIDC_SCOPES         = os.environ.get("OIDC_SCOPES", "openid profile email groups")
OIDC_SESSION_TTL    = int(os.environ.get("OIDC_SESSION_TTL", "28800"))
OIDC_COOKIE_SECURE  = os.environ.get("OIDC_COOKIE_SECURE", "true").lower() == "true"
OIDC_SESSION_SECRET = (os.environ.get("OIDC_SESSION_SECRET")
                       or OIDC_CLIENT_SECRET or secrets.token_hex(32)).encode()
OIDC_LOGOUT_REDIRECT = os.environ.get("OIDC_LOGOUT_REDIRECT", "")
# Some reverse proxies / WAFs (Cloudflare, nginx) 403 the default "Python-urllib" agent, so we
# send a normal one for the back-channel calls (discovery / token / userinfo). Override if needed.
OIDC_USER_AGENT = os.environ.get("OIDC_USER_AGENT", "Mozilla/5.0 (compatible; tar1090-history-api)")

SESSION_COOKIE = "tar1090_session"
TX_COOKIE      = "tar1090_oidc_tx"
# Anything under these is admin-only; viewers get 403 (and the UI hides it too).
ADMIN_PREFIXES = ("/api/alerts", "/api/import", "/api/patterns/build", "/api/airshow/",
                  "/api/users", "/api/settings/global")
# settings.html is reachable by viewers (their Preferences tab); its admin sub-tabs are hidden in
# the UI and the admin APIs above stay server-gated.
ADMIN_PAGES    = ("/alerts.html",)


def _b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> str:
    sig = hmac.new(OIDC_SESSION_SECRET, payload, hashlib.sha256).digest()
    return _b64u(payload) + "." + _b64u(sig)


def _unsign(token: str):
    try:
        p_b64, sig_b64 = token.split(".", 1)
        payload = _b64u_dec(p_b64)
        expected = hmac.new(OIDC_SESSION_SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64u_dec(sig_b64)):
            return None
        data = json.loads(payload)
        if data.get("exp") and time.time() > data["exp"]:
            return None
        return data
    except Exception:                                # noqa: BLE001
        return None


def _jwt_payload(jwt: str):
    # Decode (not signature-verify) the id_token. We received it over a direct, server-to-server
    # TLS call to the token endpoint, so per the OIDC spec the back-channel transport authenticates
    # it; we still validate iss/aud/exp/nonce below.
    try:
        return json.loads(_b64u_dec(jwt.split(".")[1]))
    except Exception:                                # noqa: BLE001
        return None


def _pkce():
    verifier = _b64u(secrets.token_bytes(40))
    challenge = _b64u(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def _role_for(groups):
    groups = groups or []
    if OIDC_ADMIN_GROUP and OIDC_ADMIN_GROUP in groups:
        return "admin"
    if not OIDC_VIEWER_GROUP or OIDC_VIEWER_GROUP in groups:
        return "viewer"
    return None                                      # authenticated but not in an allowed group


def _is_admin_path(path):
    return path.startswith(ADMIN_PREFIXES) or path in ADMIN_PAGES


_oidc_meta = None
# Authorization codes are single-use. Reverse proxies (or a HEAD probe) can deliver the callback
# twice; we serialize callbacks and remember a code's result briefly so a duplicate delivery
# re-issues the same session instead of failing the second exchange with invalid_grant.
_code_lock = threading.Lock()
_code_done = {}      # code -> (expires_at, session_cookie_value, next_url)


def oidc_meta():
    global _oidc_meta
    if _oidc_meta is None:
        url = OIDC_ISSUER + "/.well-known/openid-configuration"
        req = urllib.request.Request(url, headers={"User-Agent": OIDC_USER_AGENT,
                                                   "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                _oidc_meta = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"GET {url} returned {e.code} {e.reason}") from e
    return _oidc_meta


def _oidc_post_form(url, data):
    body = urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={"Accept": "application/json",
                                                          "User-Agent": OIDC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _oidc_get_json(url, bearer):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + bearer,
                                               "Accept": "application/json",
                                               "User-Agent": OIDC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


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

    # pattern view: only flights tagged by the backfill (Settings -> Build flight patterns)
    want_pattern = (q.get("pattern", [""])[0] or "").strip().lower()
    if want_pattern:
        _ensure_patterns()
        where.append("EXISTS (SELECT 1 FROM flight_patterns fp "
                     "WHERE fp.flight_id = v_flights.id AND fp.pattern = %s)")
        params.append(want_pattern)

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

    pattern_sel = (", (SELECT fp.detail FROM flight_patterns fp WHERE fp.flight_id = v_flights.id "
                   "AND fp.pattern = %s) AS pattern_reason" if want_pattern else "")
    sel_params = [ACTIVE_WINDOW_SEC] + ([want_pattern] if want_pattern else [])
    sql = ("SELECT id, icao_hex, callsign, registration, icao_type, operator, military, "
           "start_time, end_time, max_alt, "
           "EXTRACT(EPOCH FROM (end_time - start_time))::int AS duration_s, "
           "(end_time >= now() - make_interval(secs => %s)) AS active" + pattern_sel +
           " FROM v_flights WHERE " + where_sql +
           " ORDER BY start_time DESC LIMIT %s OFFSET %s")

    rows = query(sql, sel_params + list(params) + [limit, offset])
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


# --- users (login log + block list) -----------------------------------------
# Every successful OIDC sign-in is recorded here, keyed by the stable OIDC subject. An admin
# can flip `blocked` from the Settings -> Users tab to deny someone: blocked subjects are kept
# in a short-lived cache and rejected on every request (active sessions die within ~20s) and
# their next sign-in is refused at the callback.
USERS_DDL = """
CREATE TABLE IF NOT EXISTS app_users (
  sub         text PRIMARY KEY,
  email       text,
  name        text,
  role        text,
  blocked     boolean NOT NULL DEFAULT false,
  login_count int NOT NULL DEFAULT 0,
  first_seen  timestamptz NOT NULL DEFAULT now(),
  last_seen   timestamptz NOT NULL DEFAULT now());
"""
_users_ready = False


def _ensure_users():
    global _users_ready
    if not _users_ready:
        execute(USERS_DDL, ())
        _users_ready = True


_blocked = {"subs": set(), "ts": 0.0}


def _blocked_subs():
    """Set of blocked OIDC subjects, refreshed every ~20s. Fails open (keeps the last known
    set) if the DB is briefly unavailable, so a blip can't lock everyone out."""
    now = time.time()
    if now - _blocked["ts"] > 20:
        try:
            _ensure_users()
            _blocked["subs"] = {r["sub"] for r in query("SELECT sub FROM app_users WHERE blocked", ())}
        except psycopg.Error:
            pass
        _blocked["ts"] = now
    return _blocked["subs"]


def _record_login(sub, email, name, role):
    """Upsert a user on successful auth; return True if they're blocked. Blocked users still get
    last_seen bumped (so the attempt shows up) but no login_count increment and no session."""
    _ensure_users()
    rows = execute(
        "INSERT INTO app_users (sub, email, name, role, login_count, first_seen, last_seen) "
        "VALUES (%s, %s, %s, %s, 1, now(), now()) "
        "ON CONFLICT (sub) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name, "
        "  role = EXCLUDED.role, last_seen = now(), "
        "  login_count = app_users.login_count + CASE WHEN app_users.blocked THEN 0 ELSE 1 END "
        "RETURNING blocked",
        [sub, email, name, role])
    blocked = bool(rows and rows[0].get("blocked"))
    if blocked:
        _blocked["subs"].add(sub)
    return blocked


def users_get(q):
    _ensure_users()
    rows = query("SELECT sub, email, name, role, blocked, login_count, first_seen, last_seen "
                 "FROM app_users ORDER BY last_seen DESC", ())
    for r in rows:
        r["first_seen"] = ms(r["first_seen"])
        r["last_seen"] = ms(r["last_seen"])
    return {"users": rows}


def users_block(body):
    _ensure_users()
    sub = (body.get("sub") or "").strip()
    if not sub:
        return {"error": "sub required"}
    blocked = bool(body.get("blocked"))
    execute("UPDATE app_users SET blocked = %s WHERE sub = %s", [blocked, sub])
    _blocked["ts"] = 0.0          # force a cache refresh on the next request
    return {"ok": True, "sub": sub, "blocked": blocked}


# --- settings (admin global defaults + per-user preferences) -----------------
# app_settings (id=1) holds the site-wide defaults an admin sets; user_prefs holds each viewer's
# personal overrides (keyed by OIDC subject, or "local" when login is off). The Live page merges
# global defaults with the user's prefs to decide which toggle buttons show, their initial state,
# units, and visible columns.
DEFAULT_SETTINGS = {
    "buttons":  {"labels": True, "trails": True, "ground": True, "fit": True, "rings": True},  # which toggle buttons show
    "defaults": {"labels": False, "trails": False, "ground": True, "rings": False},             # their initial on/off state
    "units":    {"speed": "kt", "alt": "ft", "dist": "nm"},
    "cols":     {"t": True, "alt": True, "gs": True, "squawk": True, "dist": True},
    "hist_cols": {"active": True, "callsign": True, "registration": True, "icao_type": True,
                  "military": True, "start": True, "duration_s": True, "max_alt": True, "operator": True},
    "map":      {"base": "osm"},                        # base layer: osm | dark | light | sat
    "rings":    {"distances": [100, 150, 200]},         # site range rings, in the chosen distance unit
    "sizes":    {"icon": 1.0, "label": 1.0},            # aircraft icon / label scale
    "allow_user_prefs": True,                           # let viewers override the above
}
SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS app_settings (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  settings jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO app_settings (id, settings) VALUES (1, '{}'::jsonb) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS user_prefs (
  sub text PRIMARY KEY,
  prefs jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now());
"""
_settings_ready = False


def _ensure_settings():
    global _settings_ready
    if not _settings_ready:
        execute(SETTINGS_DDL, ())
        _settings_ready = True


def settings_global_get():
    _ensure_settings()
    rows = query("SELECT settings FROM app_settings WHERE id=1", ())
    return (rows[0]["settings"] if rows and rows[0]["settings"] else {})


def settings_global_save(body):
    s = body.get("settings")
    if not isinstance(s, dict):
        return {"error": "settings object required"}
    _ensure_settings()
    execute("UPDATE app_settings SET settings=%s, updated_at=now() WHERE id=1", [Jsonb(s)])
    return {"ok": True}


def user_prefs_get(sub):
    _ensure_settings()
    rows = query("SELECT prefs FROM user_prefs WHERE sub=%s", [sub or "local"])
    return (rows[0]["prefs"] if rows and rows[0]["prefs"] else {})


def user_prefs_save(sub, body):
    p = body.get("prefs")
    if not isinstance(p, dict):
        return {"error": "prefs object required"}
    _ensure_settings()
    execute("INSERT INTO user_prefs (sub, prefs, updated_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (sub) DO UPDATE SET prefs=EXCLUDED.prefs, updated_at=now()",
            [sub or "local", Jsonb(p)])
    return {"ok": True}


# --- historical import (heatmap .ttf -> searchable index) -------------------
# A resumable background job: walk globe_history heatmap chunks and fill the aircraft/flights
# index (see tar1090_heatmap_import). Progress + a checkpoint live in import_state so the
# Settings tab can show it and so a crash / container restart picks up where it left off.
IMPORT_DDL = """
CREATE TABLE IF NOT EXISTS import_state (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  status text NOT NULL DEFAULT 'idle',             -- live state only: idle|running
  globe_dir text,
  last_cbase double precision NOT NULL DEFAULT 0,  -- checkpoint: resume from this chunk epoch
  chunks_total int NOT NULL DEFAULT 0, chunks_done int NOT NULL DEFAULT 0,
  flights_added bigint NOT NULL DEFAULT 0, fixes_seen bigint NOT NULL DEFAULT 0,
  message text, started_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO import_state (id, status) VALUES (1, 'idle') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS import_runs (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  globe_dir text, status text,                     -- outcome: done|cancelled|error
  chunks_total int, chunks_done int,
  flights_added bigint, fixes_seen bigint, message text,
  started_at timestamptz, finished_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS import_runs_finished_idx ON import_runs (finished_at DESC);
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


# Copy the current import_state into the import_runs history with the given final outcome, then
# return the live state to a clean 'idle' (counters zeroed). last_cbase is kept so "Start /
# resume import" can still pick up where a stopped/finished run left off; "Restart from scratch"
# is what zeroes the checkpoint.
def _archive_and_idle(status, message=None):
    # Archive the finished run -- but skip an identical run logged in the last 2 minutes, so
    # repeated restarts or back-to-back "Start/resume" clicks don't spam the history.
    execute("INSERT INTO import_runs (globe_dir, status, chunks_total, chunks_done, "
            "flights_added, fixes_seen, message, started_at) "
            "SELECT s.globe_dir, %s, s.chunks_total, s.chunks_done, s.flights_added, s.fixes_seen, "
            "COALESCE(%s, s.message), s.started_at FROM import_state s WHERE s.id=1 "
            "AND NOT EXISTS (SELECT 1 FROM import_runs r WHERE r.finished_at > now() - interval '2 minutes' "
            "  AND r.status = %s AND r.chunks_done = s.chunks_done AND r.flights_added = s.flights_added "
            "  AND r.fixes_seen = s.fixes_seen)",
            (status, message, status))
    execute("UPDATE import_state SET status='idle', chunks_total=0, chunks_done=0, "
            "flights_added=0, fixes_seen=0, message=NULL, started_at=NULL, updated_at=now() "
            "WHERE id=1", ())


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
        fin = "cancelled" if result == "cancelled" else "done"
        msg = ("stopped — press Start to resume" if result == "cancelled"
               else f"imported {imp.n_flights} flight legs from {imp.n_points} fixes")
        # Record the final tallies on the live row, then archive it to history and clear to idle.
        execute("UPDATE import_state SET message=%s, chunks_total=%s, chunks_done=%s, "
                "flights_added=%s, fixes_seen=%s, last_cbase=%s, updated_at=now() WHERE id=1",
                (msg, imp.total, imp.skipped + imp.processed, imp.n_flights, imp.n_points,
                 imp.committed_checkpoint))
        _archive_and_idle(fin)
    except Exception as e:                               # noqa: BLE001
        log.exception("historical import failed")
        try:
            _archive_and_idle("error", str(e))
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
    running = bool(_import_thread and _import_thread.is_alive())
    r["running"] = running
    if not running:                                  # live row only ever reports idle/running
        r["status"] = "idle"
    r["started_at"] = ms(r.get("started_at"))
    r["updated_at"] = ms(r.get("updated_at"))
    r["default_globe_dir"] = GLOBE_DIR
    if not r.get("globe_dir"):
        r["globe_dir"] = GLOBE_DIR
    r["available"] = hm is not None
    runs = query("SELECT id, globe_dir, status, chunks_total, chunks_done, flights_added, "
                 "fixes_seen, message, started_at, finished_at FROM import_runs "
                 "ORDER BY finished_at DESC LIMIT 20", ())
    for h in runs:
        h["started_at"] = ms(h.get("started_at"))
        h["finished_at"] = ms(h.get("finished_at"))
    r["runs"] = runs
    # Live ingestion freshness: the bundled tar1090-logger writes flights continuously, so the
    # most recent end_time tells us real-time logging is alive (no import needed for that).
    try:
        ig = query("SELECT max(end_time) AS last, "
                   "count(*) FILTER (WHERE start_time >= now() - interval '1 hour') AS last_hour "
                   "FROM flights WHERE start_time >= now() - interval '6 hours'", ())
        r["ingest_last_seen"] = ms(ig[0]["last"]) if ig else None
        r["ingest_last_hour"] = (ig[0]["last_hour"] if ig else 0) or 0
    except psycopg.Error:
        r["ingest_last_seen"], r["ingest_last_hour"] = None, 0
    return r


def import_clear_history(body):
    _ensure_import()
    execute("DELETE FROM import_runs", ())
    return {"ok": True}


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
    """On startup, settle whatever the import row was left in: a genuinely-interrupted 'running'
    job resumes from its checkpoint; any leftover terminal state (done/cancelled/error) is moved
    into the run history so the live state starts clean and idle."""
    if hm is None:
        return
    _ensure_import()
    row = query("SELECT status, globe_dir FROM import_state WHERE id=1", ())
    if not row:
        return
    status = row[0]["status"]
    if status == "running":
        globe = row[0]["globe_dir"] or GLOBE_DIR
        if os.path.isdir(globe):
            log.info("resuming interrupted historical import from checkpoint (%s)", globe)
            _launch_import(globe)
        else:
            _archive_and_idle("error", "globe_dir not found on restart: " + str(globe))
    elif status != "idle":
        # Leftover done/cancelled/error from before this build (or a prior version): archive + clear.
        _archive_and_idle(status)


# --- flight-pattern backfill ------------------------------------------------
# A one-time (re-runnable) scan of the existing flight index that tags which past flights match an
# "interesting" flying pattern: air show (known type, or aerobatic maneuvering), surveillance/orbit
# (circling), or aerial survey/mapping (lawnmower grid). Geometry comes from each flight's trail
# (patterns.py). Results live in flight_patterns (one row per flight+pattern) so the History pattern
# views show all of history instantly server-side instead of detecting per-page in the browser.
PATTERN_DDL = """
CREATE TABLE IF NOT EXISTS flight_patterns (
  flight_id bigint NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
  pattern text NOT NULL,                            -- 'airshow' | 'orbit' | 'survey'
  detail text,                                      -- e.g. 'maneuver', '3 loops', '6 legs'
  built_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (flight_id, pattern));
CREATE INDEX IF NOT EXISTS flight_patterns_pat_idx ON flight_patterns (pattern);
CREATE TABLE IF NOT EXISTS pattern_build (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  status text NOT NULL DEFAULT 'idle',             -- idle | running
  scanned int NOT NULL DEFAULT 0, total int NOT NULL DEFAULT 0, found int NOT NULL DEFAULT 0,
  message text, started_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO pattern_build (id, status) VALUES (1, 'idle') ON CONFLICT DO NOTHING;
"""
_pattern_ready = False
_pattern_lock = threading.Lock()
_pattern_thread = None
_pattern_cancel = threading.Event()


def _ensure_patterns():
    global _pattern_ready
    if not _pattern_ready:
        execute(PATTERN_DDL, ())
        _pattern_ready = True


def _flight_track(hexid, start_dt, end_dt):
    """A flight's trail as (t_sec, alt_ft, lat, lon) -- trace files first, heatmap as fallback."""
    pts, _ = _trace_file_points(hexid, start_dt, end_dt)
    if not pts:
        pts = _heatmap_points(hexid, start_dt.timestamp(), end_dt.timestamp())
    return [(p[0] / 1000.0, p[3], p[1], p[2]) for p in pts if p[3] is not None]


_AIRSHOW_TYPESET = set(t.upper() for t in airshow_types.all_types())

# --- admin-customisable air-show config (DB-backed, merged with the built-in lists) ----------
AIRSHOW_CUSTOM_DDL = """
CREATE TABLE IF NOT EXISTS airshow_custom (
  id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  config jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now());
INSERT INTO airshow_custom (id) VALUES (1) ON CONFLICT DO NOTHING;
"""
_airshow_custom_ready = False
_airshow_custom_cache = {"t": 0.0, "v": None}


def _ensure_airshow_custom():
    global _airshow_custom_ready
    if not _airshow_custom_ready:
        execute(AIRSHOW_CUSTOM_DDL, ())
        _airshow_custom_ready = True


def _norm_types(val):
    """Accept a list or a free-text blob; return a set of UPPERCASE type tokens."""
    if isinstance(val, str):
        val = [val]
    out = set()
    for x in (val or []):
        for tok in str(x).replace(",", " ").split():
            t = tok.strip().upper()
            if t:
                out.add(t)
    return out


def _norm_watch(val):
    out = []
    for w in (val or []):
        if not isinstance(w, dict):
            continue
        reg = (w.get("reg") or "").strip().upper()
        hexid = (w.get("hex") or "").strip().lower().lstrip("~")
        note = (w.get("note") or "").strip()[:80]
        if reg or hexid:
            out.append({"reg": reg, "hex": hexid, "note": note})
    return out


def _airshow_custom():
    """Effective air-show config (built-in merged with the admin edits), cached ~20s."""
    now = time.time()
    if _airshow_custom_cache["v"] is not None and now - _airshow_custom_cache["t"] < 20:
        return _airshow_custom_cache["v"]
    try:
        _ensure_airshow_custom()
        row = query("SELECT config FROM airshow_custom WHERE id=1", ())
        cfg = (row[0]["config"] if row else {}) or {}
    except psycopg.Error:
        cfg = {}
    extra_types = _norm_types(cfg.get("extra_types"))
    extra_exclude = _norm_types(cfg.get("extra_exclude"))
    watch = _norm_watch(cfg.get("watch"))
    eff = {"extra_types": extra_types, "extra_exclude": extra_exclude, "watch": watch,
           "watch_regs": {w["reg"] for w in watch if w["reg"]},
           "watch_hex": {w["hex"] for w in watch if w["hex"]},
           "type_set": _AIRSHOW_TYPESET | extra_types}
    _airshow_custom_cache["t"], _airshow_custom_cache["v"] = now, eff
    return eff


def _classify_flight(icao_type, registration, hexid, track, eff):
    """{pattern: detail} for one flight: geometric patterns + air-show-by-type + watchlist, with a
    type gate so a non-aerobatic aircraft (e.g. a Cessna 172 doing steep turns) isn't an air show."""
    found = dict(patterns.classify(track)) if track else {}
    ty = (icao_type or "").upper()
    if "airshow" in found and not airshow_types.maneuver_plausible(ty, eff["extra_exclude"]):
        del found["airshow"]                                            # maneuvering, but not aerobatic
    if airshow_types.is_airshow_type(ty, eff["extra_types"]):
        found["airshow"] = "both" if found.get("airshow") else "type"   # curated / extra air-show type
    reg = (registration or "").upper()
    hx = (hexid or "").lower().lstrip("~")
    if (reg and reg in eff["watch_regs"]) or (hx and hx in eff["watch_hex"]):
        found["airshow"] = "watch"                                      # admin watchlist -> always air show
    return found


def _pattern_worker(t_from, t_to):
    wconn = None
    try:
        wconn = psycopg.connect(DB_DSN, autocommit=True)
        eff = _airshow_custom()        # admin extra/excluded types + watchlist, merged with built-ins
        cur = wconn.execute("SELECT f.id, f.icao_hex, a.registration, a.icao_type, f.start_time, f.end_time "
                            "FROM flights f JOIN aircraft a USING (icao_hex) "
                            "WHERE f.start_time <= %s AND f.end_time >= %s "
                            "ORDER BY f.start_time DESC", (t_to, t_from))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        total = len(rows)
        wconn.execute("UPDATE pattern_build SET total=%s, scanned=0, found=0, message=NULL, "
                      "updated_at=now() WHERE id=1", (total,))
        found, last = 0, [0.0]
        outcome = "done"
        for i, r in enumerate(rows):
            if _pattern_cancel.is_set():
                outcome = "cancelled"
                break
            track = _flight_track(r["icao_hex"], r["start_time"], r["end_time"])
            hits = _classify_flight(r["icao_type"], r["registration"], r["icao_hex"], track, eff)
            for pattern, detail in hits.items():
                wconn.execute(
                    "INSERT INTO flight_patterns (flight_id, pattern, detail) VALUES (%s,%s,%s) "
                    "ON CONFLICT (flight_id, pattern) DO UPDATE SET detail=EXCLUDED.detail, built_at=now()",
                    (r["id"], pattern, detail))
            if hits:
                found += 1
            now = time.monotonic()
            if now - last[0] > 1.0 or i == total - 1:
                last[0] = now
                wconn.execute("UPDATE pattern_build SET scanned=%s, found=%s, updated_at=now() "
                              "WHERE id=1", (i + 1, found))
        msg = ("stopped" if outcome == "cancelled"
               else f"tagged {found} flight(s) from {total} scanned")
        wconn.execute("UPDATE pattern_build SET status='idle', found=%s, message=%s, updated_at=now() "
                      "WHERE id=1", (found, msg))
    except Exception as e:                               # noqa: BLE001
        log.exception("flight-pattern build failed")
        try:
            (wconn or db()).execute("UPDATE pattern_build SET status='idle', message=%s, "
                                    "updated_at=now() WHERE id=1", (str(e),))
        except psycopg.Error:
            pass
    finally:
        if wconn is not None:
            try:
                wconn.close()
            except psycopg.Error:
                pass


def pattern_build_status(q):
    _ensure_patterns()
    rows = query("SELECT status, scanned, total, found, message, started_at, updated_at "
                 "FROM pattern_build WHERE id=1", ())
    r = dict(rows[0]) if rows else {"status": "idle"}
    r["running"] = bool(_pattern_thread and _pattern_thread.is_alive())
    if not r["running"]:
        r["status"] = "idle"
    r["started_at"] = ms(r.get("started_at"))
    r["updated_at"] = ms(r.get("updated_at"))
    counts = query("SELECT pattern, count(*) AS n FROM flight_patterns GROUP BY pattern", ())
    r["by_pattern"] = {c["pattern"]: c["n"] for c in counts}
    r["flagged_total"] = sum(c["n"] for c in counts)
    return r


def pattern_build_start(body):
    global _pattern_thread
    with _pattern_lock:
        if _pattern_thread and _pattern_thread.is_alive():
            return {"error": "a build is already running"}
        _ensure_patterns()
        now = datetime.now(timezone.utc)
        t_from = parse_time((body or {}).get("from"), datetime(2000, 1, 1, tzinfo=timezone.utc))
        t_to = parse_time((body or {}).get("to"), now)
        if (body or {}).get("clear"):
            execute("TRUNCATE flight_patterns", ())
        _pattern_cancel.clear()
        execute("UPDATE pattern_build SET status='running', scanned=0, total=0, found=0, "
                "message=NULL, started_at=now(), updated_at=now() WHERE id=1", ())
        _pattern_thread = threading.Thread(target=_pattern_worker, args=(t_from, t_to),
                                           name="pattern-build", daemon=True)
        _pattern_thread.start()
    return pattern_build_status({})


def pattern_build_stop(body):
    _pattern_cancel.set()
    return {"ok": True}


# --- HTTP -------------------------------------------------------------------
def airshow_get(q):
    """Air-show aircraft types + non-aerobatic exclusions + watchlist (built-in merged with admin
    edits) -- served to the Live filter and the Alerts editor."""
    eff = _airshow_custom()
    return {"categories": [{"key": k, "label": v["label"], "desc": v.get("desc", ""), "types": v["types"]}
                           for k, v in airshow_types.AIRSHOW_TYPES.items()],
            "all": sorted(eff["type_set"]),
            "non_aerobatic": sorted(airshow_types.NON_AEROBATIC | eff["extra_exclude"]),
            "watch": [{"reg": w["reg"], "hex": w["hex"]} for w in eff["watch"]]}


def airshow_custom_get(q):
    """Admin view of the customisable air-show config + the built-in lists for reference."""
    _ensure_airshow_custom()
    row = query("SELECT config FROM airshow_custom WHERE id=1", ())
    cfg = (row[0]["config"] if row else {}) or {}
    return {"config": {"extra_types": cfg.get("extra_types") or [],
                       "extra_exclude": cfg.get("extra_exclude") or [],
                       "watch": _norm_watch(cfg.get("watch"))},
            "builtin": {"categories": [{"key": k, "label": v["label"], "types": v["types"]}
                                       for k, v in airshow_types.AIRSHOW_TYPES.items()],
                        "non_aerobatic": sorted(airshow_types.NON_AEROBATIC)}}


def airshow_custom_save(body):
    _ensure_airshow_custom()
    cfg = {"extra_types": sorted(_norm_types((body or {}).get("extra_types"))),
           "extra_exclude": sorted(_norm_types((body or {}).get("extra_exclude"))),
           "watch": _norm_watch((body or {}).get("watch"))}
    execute("UPDATE airshow_custom SET config=%s, updated_at=now() WHERE id=1", (Jsonb(cfg),))
    _airshow_custom_cache["v"] = None        # invalidate so the next read reflects the edit
    return {"ok": True, "config": cfg}


def airshow_scan(q):
    """Find likely air-show aircraft already in the (global) aircraft DB -- by air-show type match,
    and/or the worldwide military / 'interesting' flags -- so the admin can pin specific ones to the
    watchlist. Registry-agnostic: it uses the aircraft your receiver has actually seen, anywhere."""
    eff = _airshow_custom()
    use_types = (q.get("types", ["1"])[0] != "0")
    use_mil = (q.get("military", ["1"])[0] != "0")
    use_int = (q.get("interesting", ["0"])[0] == "1")
    conds, params = [], []
    if use_types and eff["type_set"]:
        conds.append("upper(icao_type) = ANY(%s)")
        params.append(sorted(eff["type_set"]))
    if use_mil:
        conds.append("military")
    if use_int:
        conds.append("interesting")
    if not conds:
        return {"aircraft": [], "count": 0}
    rows = query("SELECT icao_hex, registration, icao_type, type_desc, operator, military, interesting, "
                 "last_seen FROM aircraft WHERE (" + " OR ".join(conds) + ") "
                 "ORDER BY last_seen DESC NULLS LAST LIMIT 500", params)
    for r in rows:
        r["last_seen"] = ms(r.get("last_seen"))
        r["military"] = bool(r["military"])
        r["interesting"] = bool(r["interesting"])
    return {"aircraft": rows, "count": len(rows)}


def patterns_get(q):
    """Catalogue of detectable flight patterns, for the History pattern picker."""
    return {"patterns": patterns.CATALOG}


ROUTES = {"/api/search": search, "/api/options": options,
          "/api/trace": trace, "/api/traces": traces, "/api/live": live,
          "/api/airshow": airshow_get, "/api/airshow/custom": airshow_custom_get,
          "/api/airshow/scan": airshow_scan,
          "/api/patterns": patterns_get, "/api/patterns/build-status": pattern_build_status,
          "/api/alerts/rules": alerts_rules_get, "/api/alerts/config": alerts_config_get,
          "/api/alerts/log": alerts_log_get, "/api/import/status": import_status,
          "/api/users": users_get}
POST_ROUTES = {"/api/alerts/rules": alerts_rule_save, "/api/alerts/rules/delete": alerts_rule_delete,
               "/api/alerts/config": alerts_config_save, "/api/alerts/test": alerts_test,
               "/api/import/start": import_start, "/api/import/stop": import_stop,
               "/api/import/clear-history": import_clear_history,
               "/api/patterns/build": pattern_build_start, "/api/patterns/build-stop": pattern_build_stop,
               "/api/airshow/custom": airshow_custom_save,
               "/api/users/block": users_block, "/api/settings/global": settings_global_save}
CONTENT = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
           ".css": "text/css", ".json": "application/json", ".ico": "image/x-icon",
           ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json",
           ".png": "image/png"}


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

    # ---- OIDC: cookies, session, flow -------------------------------------
    def _cookie(self, name):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            ck = SimpleCookie(raw)
            return ck[name].value if name in ck else None
        except Exception:                            # noqa: BLE001
            return None

    def _session(self):
        c = self._cookie(SESSION_COOKIE)
        s = _unsign(c) if c else None
        if s and s.get("sub") in _blocked_subs():     # cut a blocked user's live session
            return None
        return s

    def _cookie_str(self, name, value, max_age):
        parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax",
                 "Max-Age=0" if max_age == 0 else f"Max-Age={int(max_age)}"]
        if OIDC_COOKIE_SECURE:
            parts.append("Secure")
        return "; ".join(parts)

    def _redirect(self, location, cookies=None):
        self.send_response(302)
        self.send_header("Location", location)
        for (n, v, age) in (cookies or []):
            self.send_header("Set-Cookie", self._cookie_str(n, v, age))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _redirect_uri(self):
        if OIDC_REDIRECT_URL:
            return OIDC_REDIRECT_URL
        proto = self.headers.get("X-Forwarded-Proto") or ("https" if OIDC_COOKIE_SECURE else "http")
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
                or f"localhost:{PORT}")
        return f"{proto}://{host}/oidc/callback"

    def _me(self):
        if not OIDC_ENABLED:
            return {"enabled": False, "authenticated": True, "role": "admin"}
        s = self._session()
        if not s:
            return {"enabled": True, "authenticated": False, "role": None}
        return {"enabled": True, "authenticated": True, "role": s.get("role"),
                "name": s.get("name"), "email": s.get("email"), "sub": s.get("sub")}

    def _oidc_login(self, u):
        nxt = parse_qs(u.query).get("next", ["/"])[0]
        if not nxt.startswith("/"):
            nxt = "/"
        verifier, challenge = _pkce()
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        tx = _sign(json.dumps({"s": state, "n": nonce, "v": verifier, "next": nxt,
                               "exp": time.time() + 600}).encode())
        try:
            meta = oidc_meta()
        except Exception as e:                       # noqa: BLE001
            self._send(502, {"error": "OIDC discovery failed: " + str(e)})
            return
        params = {"response_type": "code", "client_id": OIDC_CLIENT_ID,
                  "redirect_uri": self._redirect_uri(), "scope": OIDC_SCOPES,
                  "state": state, "nonce": nonce,
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        self._redirect(meta["authorization_endpoint"] + "?" + urlencode(params),
                       cookies=[(TX_COOKIE, tx, 600)])

    def _oidc_callback(self, u):
        q = parse_qs(u.query)
        if "error" in q:
            log.warning("oidc callback error: %s", q.get("error_description", q["error"])[0])
            self._redirect("/denied?reason=token", cookies=[(TX_COOKIE, "", 0)])
            return
        code = q.get("code", [None])[0]
        tx = self._cookie(TX_COOKIE)
        txd = _unsign(tx) if tx else None
        if not code or not txd or txd.get("s") != q.get("state", [None])[0]:
            self._redirect("/denied?reason=state", cookies=[(TX_COOKIE, "", 0)])
            return
        # Serialize per-code so a duplicate callback delivery reuses the first result instead of
        # trying to redeem the (now-consumed) single-use code again.
        with _code_lock:
            now = time.time()
            for k in [k for k, v in _code_done.items() if v[0] < now]:
                del _code_done[k]
            cached = _code_done.get(code)
            if cached:
                self._redirect(cached[2], cookies=[(SESSION_COOKIE, cached[1], OIDC_SESSION_TTL),
                                                   (TX_COOKIE, "", 0)])
                return
            try:
                meta = oidc_meta()
                tok = _oidc_post_form(meta["token_endpoint"], {
                    "grant_type": "authorization_code", "code": code,
                    "redirect_uri": self._redirect_uri(),
                    "client_id": OIDC_CLIENT_ID, "client_secret": OIDC_CLIENT_SECRET,
                    "code_verifier": txd["v"]})
            except urllib.error.HTTPError as e:
                log.warning("oidc token exchange failed: %s", e.read().decode()[:300])
                self._redirect("/denied?reason=token", cookies=[(TX_COOKIE, "", 0)])
                return
            except Exception as e:                       # noqa: BLE001
                log.warning("oidc token exchange failed: %s", e)
                self._redirect("/denied?reason=token", cookies=[(TX_COOKIE, "", 0)])
                return
            claims = _jwt_payload(tok.get("id_token", "")) or {}
            aud = claims.get("aud")
            aud = aud if isinstance(aud, list) else [aud]
            if (claims.get("nonce") != txd["n"]
                    or claims.get("iss", "").rstrip("/") != OIDC_ISSUER
                    or OIDC_CLIENT_ID not in aud
                    or claims.get("exp", 0) < time.time()):
                self._redirect("/denied?reason=idtoken", cookies=[(TX_COOKIE, "", 0)])
                return
            groups = claims.get("groups") or []
            name = claims.get("name") or claims.get("preferred_username")
            email = claims.get("email")
            access = tok.get("access_token")
            if access and meta.get("userinfo_endpoint"):
                try:
                    ui = _oidc_get_json(meta["userinfo_endpoint"], access)
                    groups = ui.get("groups") or groups
                    name = name or ui.get("name") or ui.get("preferred_username")
                    email = email or ui.get("email")
                except Exception:                        # noqa: BLE001
                    pass
            role = _role_for(groups)
            if role is None:
                log.info("oidc: %s authenticated but in no authorized group (%s)", email or name, groups)
                self._redirect("/denied?reason=group", cookies=[(TX_COOKIE, "", 0)])
                return
            # Record the sign-in (drives the Settings -> Users tab) and honour the block list.
            sub = claims.get("sub")
            try:
                if sub and _record_login(sub, email, name, role):
                    log.info("oidc: blocked user %s tried to sign in", email or name or sub)
                    self._redirect("/denied?reason=blocked", cookies=[(TX_COOKIE, "", 0)])
                    return
            except psycopg.Error as e:
                log.warning("user login record failed: %s", e)     # don't block sign-in on a DB blip
            sess = _sign(json.dumps({"sub": claims.get("sub"), "name": name, "email": email,
                                     "role": role, "exp": time.time() + OIDC_SESSION_TTL}).encode())
            nxt = txd.get("next", "/")
            _code_done[code] = (now + 120, sess, nxt)
        self._redirect(nxt, cookies=[(SESSION_COOKIE, sess, OIDC_SESSION_TTL), (TX_COOKIE, "", 0)])

    def _oidc_logout(self):
        # Clear our session and land on the branded "signed out" splash (no auto sign-in).
        loc = OIDC_LOGOUT_REDIRECT or "/login?loggedout=1"
        self._redirect(loc, cookies=[(SESSION_COOKIE, "", 0)])

    def _gate(self, path, is_api):
        """When OIDC is on, require a valid session and the admin role for admin paths.
        Returns True to continue handling, or False if it already sent a response/redirect."""
        if not OIDC_ENABLED:
            return True
        sess = self._session()
        if sess is None:
            if is_api:
                self._send(401, {"error": "authentication required"})
            else:
                self._redirect("/login?next=" + quote(self.path))
            return False
        if sess.get("role") != "admin" and _is_admin_path(path):
            if is_api:
                self._send(403, {"error": "forbidden: this action requires the admin role"})
            else:
                self._redirect("/")
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        if path == "/api/me":                        # always reachable (drives the UI)
            self._send(200, self._me())
            return
        if OIDC_ENABLED:
            if path in ("/login", "/login.html"):     # branded sign-in splash (public)
                self._serve_static("/login.html")
                return
            if path in ("/denied", "/denied.html"):    # friendly access-denied page (public)
                self._serve_static("/denied.html")
                return
            if path in ("/manifest.webmanifest", "/sw.js", "/icon.svg", "/icon-maskable.svg"):
                self._serve_static(path)               # PWA shell assets (public, non-sensitive)
                return
        if OIDC_ENABLED and self.command == "GET":    # flow is GET-only (a HEAD must not redeem the code)
            if path == "/oidc/login":
                self._oidc_login(u)
                return
            if path == "/oidc/callback":
                self._oidc_callback(u)
                return
            if path == "/oidc/logout":
                self._oidc_logout()
                return
        if not self._gate(path, path.startswith("/api/")):
            return
        if path == "/api/settings":                  # site defaults + this user's prefs (any signed-in user)
            s = self._session() or {}
            try:
                self._send(200, {"builtin": DEFAULT_SETTINGS, "global": settings_global_get(),
                                 "prefs": user_prefs_get(s.get("sub")),
                                 "role": s.get("role") if OIDC_ENABLED else "admin"})
            except psycopg.Error as e:
                self._send(503, {"error": "database unavailable", "detail": str(e)})
            return
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
        path = urlparse(self.path).path
        if not self._gate(path, True):               # admin paths gated; others just need a session
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
        if path == "/api/settings/prefs":            # save the signed-in user's own preferences
            fn = lambda b: user_prefs_save((self._session() or {}).get("sub"), b)
        else:
            fn = POST_ROUTES.get(path)
        if not fn:
            self._send(404, {"error": "not found"})
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
            path = "/live.html"
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
    if OIDC_ENABLED:
        missing = [k for k, v in (("OIDC_ISSUER", OIDC_ISSUER), ("OIDC_CLIENT_ID", OIDC_CLIENT_ID),
                                  ("OIDC_CLIENT_SECRET", OIDC_CLIENT_SECRET)) if not v]
        if missing:
            log.error("OIDC_ENABLED but missing required config: %s -- login will fail", ", ".join(missing))
        log.info("OIDC login required: issuer=%s admin_group=%s viewer_group=%s",
                 OIDC_ISSUER, OIDC_ADMIN_GROUP, OIDC_VIEWER_GROUP or "(any authenticated)")
    else:
        log.info("OIDC disabled (OIDC_ENABLED=false) -- the report site is open to anyone who can reach it")
    try:
        _ensure_alerts()                                 # create alert tables up front (best effort)
        _resume_import_on_start()                        # continue an interrupted import, if any
    except psycopg.Error as e:
        log.warning("tables not created yet (DB down?): %s -- will retry on first use", e)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
