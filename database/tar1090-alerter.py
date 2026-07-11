#!/usr/bin/env python3
"""tar1090 alert engine.

Polls the same live aircraft.json readsb produces, evaluates the alert rules stored in
the database (match conditions + optional zone + optional time window + per-rule cooldown)
and, when one fires, logs it and publishes it over MQTT to Home Assistant (HA MQTT
Discovery binary_sensor + a JSON event topic). Aircraft metadata (type / registration /
operator / military) that isn't in aircraft.json is joined from the `aircraft` table the
logger maintains, so type-based rules (e.g. "A321") work.

Stdlib + psycopg + paho-mqtt. Runs independently; if MQTT isn't configured it just logs.
"""

import fnmatch
import json
import logging
import math
import os
import time
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone

import psycopg

import tar1090_mqtt as mq
import airshow_types
import maneuver
import patterns

log = logging.getLogger("tar1090-alerter")

DB_DSN        = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
INTERVAL      = float(os.environ.get("ALERT_INTERVAL", "5"))
HOLD_SEC      = int(os.environ.get("ALERT_HOLD_SEC", "60"))         # how long the HA sensor stays ON
MAN_HISTORY_SEC = int(os.environ.get("ALERT_MANEUVER_HISTORY_SEC", "300"))  # altitude history kept per a/c
MAN_DEADBAND_FT = int(os.environ.get("ALERT_MANEUVER_DEADBAND_FT", "75"))   # ignore wiggles below this
# orbit/survey patterns build up far slower than aerobatics (a mapping leg alone can run several
# minutes), so pattern rules get a longer rolling history than the maneuver detector needs.
PAT_HISTORY_SEC = int(os.environ.get("ALERT_PATTERN_HISTORY_SEC", "1800"))
FETCH_PHOTO   = os.environ.get("ALERT_FETCH_PHOTO", "true").strip().lower() != "false"
AIRCRAFT_URL  = os.environ.get("ALERT_AIRCRAFT_URL") or os.environ.get("AIRCRAFT_URL")
AIRCRAFT_JSON = os.environ.get("ALERT_AIRCRAFT_JSON")
CANDIDATE_DIRS = ["/run/readsb", "/run/dump1090-fa", "/run/dump1090",
                  "/run/adsbexchange-feed", "/run/dump1090-mutability", "/run/skyaware978"]
PS_API = "https://api.planespotters.net/pub/photos/hex/"
# planespotters sits behind Cloudflare and answers 403 to the default urllib User-Agent,
# so identify ourselves like a real client (override with ALERT_PHOTO_UA if needed).
PHOTO_UA = os.environ.get(
    "ALERT_PHOTO_UA", "tar1090-alerter/1.0 (+https://github.com/McGeaverBeaver/tar1090)")


# --- database ---------------------------------------------------------------
_conn = None


def db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(DB_DSN, autocommit=True)
    return _conn


def fetch(sql, params=()):
    cur = db().execute(sql, params)
    cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# --- live feed --------------------------------------------------------------
def _aircraft_path():
    if AIRCRAFT_JSON and os.path.exists(AIRCRAFT_JSON):
        return AIRCRAFT_JSON
    for d in CANDIDATE_DIRS:
        p = os.path.join(d, "aircraft.json")
        if os.path.exists(p):
            return p
    return None


def read_aircraft():
    """Return the parsed aircraft.json (live), or None if it can't be read this tick."""
    try:
        if AIRCRAFT_URL:
            with urllib.request.urlopen(AIRCRAFT_URL, timeout=4) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        p = _aircraft_path()
        if not p:
            return None
        with open(p, "rb") as fh:
            return json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError) as e:
        log.debug("aircraft.json read failed: %s", e)
        return None


def planes_from(doc):
    """Normalise aircraft.json entries to dicts with a position and a clean hex/callsign."""
    out = []
    for a in (doc.get("aircraft") or []):
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            continue
        hexid = (a.get("hex") or "").strip().lower()
        if not hexid:
            continue
        alt = a.get("alt_baro")
        if alt == "ground":
            alt = 0
        out.append({
            "hex": hexid,
            "callsign": (a.get("flight") or "").strip(),
            "registration": a.get("r"),
            "icao_type": a.get("t"),
            "operator": a.get("ownOp"),
            "military": None,
            "lat": lat, "lon": lon,
            "alt": alt if isinstance(alt, (int, float)) else None,
            "gs": a.get("gs"), "track": a.get("track"),
            "squawk": a.get("squawk"),
        })
    return out


def join_metadata(planes):
    """Fill type/registration/operator/military from the aircraft table where missing."""
    need = [p["hex"] for p in planes if not p["icao_type"] or p["military"] is None]
    if not need:
        return
    try:
        rows = fetch("SELECT icao_hex, registration, icao_type, operator, military "
                     "FROM aircraft WHERE icao_hex = ANY(%s)", (need,))
    except psycopg.Error as e:
        log.debug("metadata join failed: %s", e)
        return
    meta = {r["icao_hex"]: r for r in rows}
    for p in planes:
        m = meta.get(p["hex"])
        if not m:
            continue
        p["registration"] = p["registration"] or m["registration"]
        p["icao_type"] = p["icao_type"] or m["icao_type"]
        p["operator"] = p["operator"] or m["operator"]
        if p["military"] is None:
            p["military"] = m["military"]


# --- aerobatic maneuvering detection ----------------------------------------
class AltTracker:
    """Rolling per-aircraft position+altitude history. The shared maneuver detector reads it to
    spot aerobatic flying live, and the shared pattern classifier reads it to spot surveillance
    orbits / aerial-photography survey grids -- both judged the same way the history backfill
    judges archived trails."""

    def __init__(self, keep_sec=max(MAN_HISTORY_SEC, PAT_HISTORY_SEC)):
        self.keep = keep_sec
        self.hist = {}                      # hex -> deque[(t_epoch, alt_ft, lat, lon)]

    def update(self, planes, now):
        for p in planes:
            if p["alt"] is None:
                continue
            dq = self.hist.get(p["hex"])
            if dq is None:
                dq = deque()
                self.hist[p["hex"]] = dq
            dq.append((now, p["alt"], p["lat"], p["lon"]))
            cutoff = now - self.keep
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        for h in list(self.hist):           # forget aircraft gone for a whole window
            dq = self.hist[h]
            if not dq or now - dq[-1][0] > self.keep:
                del self.hist[h]

    def points(self, hexid, window_sec):
        """The (t, alt, lat, lon) samples kept for an aircraft over the last window_sec seconds."""
        dq = self.hist.get(hexid)
        if not dq:
            return []
        now = dq[-1][0]
        return [s for s in dq if s[0] >= now - window_sec]


# Admin-added non-aerobatic types (from the history-api's airshow_custom table), refreshed ~30s.
EXTRA_EXCLUDE = set()
_extra_exclude_at = [0.0]


def load_extra_exclude():
    now = time.time()
    if now - _extra_exclude_at[0] < 30:
        return
    _extra_exclude_at[0] = now
    global EXTRA_EXCLUDE
    try:
        rows = fetch("SELECT config FROM airshow_custom WHERE id=1")
    except psycopg.Error:
        return                                  # table not created yet (history-api owns it)
    cfg = (rows[0]["config"] if rows else {}) or {}
    vals = cfg.get("extra_exclude") or []
    s = set()
    for x in (vals if isinstance(vals, list) else [vals]):
        for tok in str(x).replace(",", " ").split():
            if tok.strip():
                s.add(tok.strip().upper())
    EXTRA_EXCLUDE = s


# --- matching ---------------------------------------------------------------
def _txt_match(pattern, value):
    if not pattern:
        return True
    if value is None:
        return False
    value = str(value).lower()
    for tok in str(pattern).split(","):
        tok = tok.strip().lower()
        if not tok:
            continue
        if "*" in tok or "?" in tok:
            if fnmatch.fnmatch(value, tok):
                return True
        elif tok in value:
            return True
    return False


def _type_match(pattern, value):
    """Type is matched per-token as equality or wildcard (A32* ), not substring."""
    if not pattern:
        return True
    if value is None:
        return False
    value = str(value).lower()
    for tok in str(pattern).split(","):
        tok = tok.strip().lower()
        if tok and fnmatch.fnmatch(value, tok):
            return True
    return False


def _squawk_match(pattern, value):
    if not pattern:
        return True
    if value is None:
        return False
    value = str(value)
    for tok in str(pattern).split(","):
        tok = tok.strip().lower()
        if tok == "emergency" and value in ("7500", "7600", "7700"):
            return True
        if tok and tok == value:
            return True
    return False


def matches_conditions(cond, p, tracker=None):
    if not cond:
        return True
    if not _txt_match(cond.get("callsign"), p["callsign"]):
        return False
    if not _type_match(cond.get("icao_type"), p["icao_type"]):
        return False
    if not _txt_match(cond.get("registration"), p["registration"]):
        return False
    if not _txt_match(cond.get("operator"), p["operator"]):
        return False
    if not _squawk_match(cond.get("squawk"), p["squawk"]):
        return False
    if cond.get("military") and not p["military"]:
        return False
    amin, amax = cond.get("alt_min"), cond.get("alt_max")
    if (amin is not None or amax is not None):
        if p["alt"] is None:
            return False
        if amin is not None and p["alt"] < amin:
            return False
        if amax is not None and p["alt"] > amax:
            return False
    # air-show aircraft categories (match the curated ICAO-type lists)
    cats = cond.get("airshow")
    if cats:
        toks = airshow_types.types_for(cats)
        if not toks or not _type_match(",".join(toks), p["icao_type"]):
            return False
    # aerobatic maneuvering: confined box + steep vertical rates + repeated reversals, and the
    # aircraft must be a plausible air-show type (not a trainer/tourer/heli just doing steep work)
    man = cond.get("maneuver")
    if man:
        if tracker is None:
            return False
        if not airshow_types.maneuver_plausible(p["icao_type"], EXTRA_EXCLUDE):
            return False
        pts = tracker.points(p["hex"], float(man.get("window_sec") or 180))
        m = maneuver.metrics([(t, al, la, lo) for (t, al, la, lo) in pts])
        if not maneuver.is_air_show(m, min_span=man.get("alt_span_min_ft"),
                                    min_reversals=man.get("reversals_min")):
            return False
    # surveillance / aerial photography: orbit (sustained same-direction circling over a point)
    # and/or survey (parallel "lawnmower" mapping legs), judged on the rolling live trail with
    # the same geometry the history backfill uses (patterns.py)
    pat = cond.get("pattern")
    if pat:
        if tracker is None:
            return False
        kinds = [k for k in (pat.get("kinds") or []) if k in ("orbit", "survey")]
        if not kinds:
            kinds = ["orbit", "survey"]
        pts = tracker.points(p["hex"], float(pat.get("window_sec") or 900))
        hits = patterns.detect(pts, kinds,
                               min_loops=pat.get("orbit_min_loops"),
                               min_legs=pat.get("survey_min_legs"))
        if not hits:
            return False
        p["_pattern"] = "; ".join(f"{k}: {v}" for k, v in sorted(hits.items()))
    return True


def _haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _point_in_poly(lat, lon, pts):
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        yi, xi = pts[i][0], pts[i][1]
        yj, xj = pts[j][0], pts[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def in_zone(zone, p):
    if not zone:
        return True
    t = zone.get("type")
    if t == "circle":
        try:
            return _haversine_m(p["lat"], p["lon"], zone["lat"], zone["lon"]) <= zone["radius_m"]
        except (KeyError, TypeError):
            return True
    if t == "polygon":
        pts = zone.get("points") or []
        return _point_in_poly(p["lat"], p["lon"], pts) if len(pts) >= 3 else True
    return True


def in_window(win, now):
    if not win:
        return True
    days = win.get("days")
    if days:
        dow = (now.weekday() + 1) % 7        # python Mon=0..Sun=6 -> 0=Sun..6=Sat
        if dow not in days:
            return False
    start, end = win.get("start"), win.get("end")
    if start and end:
        cur = now.hour * 60 + now.minute
        s = int(start[:2]) * 60 + int(start[3:5])
        e = int(end[:2]) * 60 + int(end[3:5])
        return (s <= cur <= e) if s <= e else (cur >= s or cur <= e)
    return True


# --- firing -----------------------------------------------------------------
def _thumb_src(ph):
    # planespotters returns a small "thumbnail" and a larger "thumbnail_large"; prefer the
    # larger one for the Home Assistant card, falling back to the small one (the same photo
    # the history page shows).
    for key in ("thumbnail_large", "thumbnail"):
        t = ph.get(key)
        src = t.get("src") if isinstance(t, dict) else t
        if src:
            return src
    return None


def _ps_get(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": PHOTO_UA, "Accept": "application/json", "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def fetch_photo(p):
    if not FETCH_PHOTO or not p["hex"] or p["hex"].startswith("~"):
        return None
    base = PS_API + p["hex"].upper()
    qs = []
    if p["registration"]:
        qs.append("reg=" + urllib.parse.quote(str(p["registration"])))
    if p["icao_type"]:
        qs.append("icaoType=" + urllib.parse.quote(str(p["icao_type"])))
    try:
        data = _ps_get(base + ("?" + "&".join(qs) if qs else ""))
        ph = (data.get("photos") or [None])[0]
        if not ph and qs:                          # reg/type hint matched nothing -> try hex only
            ph = (_ps_get(base).get("photos") or [None])[0]
    except Exception as e:                          # noqa: BLE001 -- photo is best-effort
        log.info("photo lookup failed for %s: %s", p["hex"], e)   # 403 = UA/IP block, timeout = egress
        return None
    return _thumb_src(ph) if ph else None


def log_alert(rule, p):
    try:
        db().execute(
            "INSERT INTO alert_log (rule_id, rule_name, icao_hex, callsign, registration, "
            "icao_type, operator, military, lat, lon, alt, squawk) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (rule["id"], rule["name"], p["hex"], p["callsign"] or None, p["registration"],
             p["icao_type"], p["operator"], bool(p["military"]) if p["military"] is not None else None,
             p["lat"], p["lon"], p["alt"], p["squawk"]))
    except psycopg.Error as e:
        log.warning("alert_log insert failed: %s", e)


class Mqtt:
    """Long-lived MQTT client that tracks config + discovery and pulses rule sensors."""
    def __init__(self):
        self.client = None
        self.sig = None
        self.published = {}          # rule_id -> name (for discovery)
        self.pending_off = {}        # state_topic -> epoch to send OFF

    def _config_sig(self, cfg):
        return (cfg.get("mqtt_host"), cfg.get("mqtt_port"), cfg.get("mqtt_username"),
                cfg.get("mqtt_password"), cfg.get("mqtt_tls"))

    def ensure(self, cfg):
        if not (mq.available() and cfg.get("enabled") and cfg.get("mqtt_host")):
            self.stop()
            return False
        sig = self._config_sig(cfg)
        if self.client and sig == self.sig:
            return True
        self.stop()
        try:
            c = mq.make_client(cfg, "tar1090-alerter")
            c.reconnect_delay_set(min_delay=1, max_delay=30)
            c.connect_async(cfg["mqtt_host"], int(cfg.get("mqtt_port") or 1883), keepalive=30)
            c.loop_start()
            self.client, self.sig, self.published = c, sig, {}
            log.info("MQTT connecting to %s:%s", cfg["mqtt_host"], cfg.get("mqtt_port") or 1883)
            return True
        except Exception as e:                # noqa: BLE001
            log.warning("MQTT connect failed: %s", e)
            self.client = None
            return False

    def stop(self):
        if self.client:
            try:
                self.client.loop_stop()
                self.client.disconnect()
            except Exception:
                pass
        self.client, self.sig = None, None

    def sync_discovery(self, cfg, rules):
        if not self.client or not cfg.get("ha_discovery"):
            return
        want = {r["id"]: r["name"] for r in rules}
        for rid, name in want.items():
            if self.published.get(rid) != name:
                self.client.publish(mq.discovery_topic(cfg, rid),
                                    json.dumps(mq.discovery_payload(cfg, rid, name, HOLD_SEC)),
                                    qos=1, retain=True)
        for rid in list(self.published):
            if rid not in want:               # rule deleted -> remove the HA entity
                self.client.publish(mq.discovery_topic(cfg, rid), "", qos=1, retain=True)
        self.published = want

    def fire(self, cfg, rule, payload):
        if not self.client:
            return
        self.client.publish(mq.event_topic(cfg), json.dumps(payload), qos=1, retain=False)
        st = mq.state_topic(cfg, rule["id"])
        self.client.publish(st, "ON", qos=1, retain=False)
        self.client.publish(mq.attributes_topic(cfg, rule["id"]), json.dumps(payload), qos=1, retain=False)
        self.pending_off[st] = time.time() + HOLD_SEC

    def flush_off(self):
        if not self.client:
            return
        now = time.time()
        for st, when in list(self.pending_off.items()):
            if now >= when:
                self.client.publish(st, "OFF", qos=1, retain=False)
                del self.pending_off[st]


def build_payload(rule, p):
    return {
        "rule_id": rule["id"], "rule": rule["name"],
        "hex": p["hex"], "callsign": p["callsign"] or None,
        "registration": p["registration"], "type": p["icao_type"], "operator": p["operator"],
        "military": bool(p["military"]) if p["military"] is not None else None,
        "lat": p["lat"], "lon": p["lon"], "alt": p["alt"], "gs": p["gs"],
        "track": p["track"], "squawk": p["squawk"],
        # what the pattern condition saw (e.g. "orbit: 2.5 loops over 3.1 km box"), when the
        # rule has one -- guarded so another rule's stashed detection can't leak in
        "pattern": p.get("_pattern") if (rule.get("conditions") or {}).get("pattern") else None,
        "photo": fetch_photo(p),
        "time": datetime.now(timezone.utc).isoformat(),
    }


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    log.info("tar1090-alerter starting (interval=%ss)", INTERVAL)
    mqtt_mgr = Mqtt()
    tracker = AltTracker()
    cooldowns = {}        # (rule_id, hex) -> last fired epoch

    while True:
        loop_start = time.time()
        try:
            cfg_rows = fetch("SELECT * FROM alert_config WHERE id = 1")
            cfg = cfg_rows[0] if cfg_rows else {"enabled": True, "base_topic": "tar1090",
                                                "ha_discovery": True, "discovery_prefix": "homeassistant"}
            rules = fetch("SELECT id, name, conditions, zone, time_window, cooldown_sec "
                          "FROM alert_rules WHERE enabled ORDER BY id")
        except psycopg.Error as e:
            log.warning("DB unavailable: %s", e)
            time.sleep(min(INTERVAL * 2, 30))
            continue

        load_extra_exclude()        # admin-added non-aerobatic types (so maneuver rules match the UI)

        mqtt_up = mqtt_mgr.ensure(cfg)
        if mqtt_up:
            mqtt_mgr.sync_discovery(cfg, rules)

        if rules:
            doc = read_aircraft()
            if doc:
                planes = planes_from(doc)
                join_metadata(planes)
                now_local = datetime.now()
                now = time.time()
                tracker.update(planes, now)        # feed altitude history for maneuver detection
                for p in planes:
                    for rule in rules:
                        if not matches_conditions(rule["conditions"], p, tracker):
                            continue
                        if not in_zone(rule["zone"], p):
                            continue
                        if not in_window(rule["time_window"], now_local):
                            continue
                        key = (rule["id"], p["hex"])
                        cd = rule.get("cooldown_sec") or 0
                        if now - cooldowns.get(key, 0) < cd:
                            continue
                        cooldowns[key] = now
                        payload = build_payload(rule, p)
                        log_alert(rule, p)
                        if mqtt_up:
                            mqtt_mgr.fire(cfg, rule, payload)
                        log.info("ALERT '%s' <- %s %s %s @ %.4f,%.4f",
                                 rule["name"], p["hex"], p["callsign"] or "", p["icao_type"] or "",
                                 p["lat"], p["lon"])
                # prune old cooldowns (>6h)
                for k, v in list(cooldowns.items()):
                    if now - v > 21600:
                        del cooldowns[k]

        mqtt_mgr.flush_off()
        time.sleep(max(0.5, INTERVAL - (time.time() - loop_start)))


if __name__ == "__main__":
    main()
