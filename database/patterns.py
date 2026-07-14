"""Flight-pattern classifier.

Given a track (time-ordered points), recognise "interesting" flying patterns from trail geometry,
the same way the air-show detector spots aerobatics. Shared by the backfill (history) and usable
live. Patterns:

  * airshow    -- aerobatic maneuvering (delegated to maneuver.py: confined box, steep rates,
                  repeated vertical reversals).
  * orbit      -- surveillance / circling: sustained same-direction turning (several full loops),
                  confined, near level. Police / news helicopters, ISR, traffic & pipeline patrol.
  * survey     -- aerial mapping / photography "lawnmower": many parallel back-and-forth legs along
                  one axis at near-constant altitude, criss-crossing an area.

classify() returns {pattern_key: detail_string} for whatever it recognises. Thresholds are env
overridable. Detection quality follows trail resolution -- coarse heatmap sampling reads turning &
vertical rates less sharply than dense trace files.
"""

import math
import os

from maneuver import metrics as _maneuver_metrics, is_air_show as _is_air_show, _haversine_km


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# orbit / surveillance -- deliberately strict: a genuine surveillance orbit is many laps of
# tight, level, same-direction circling over one spot. Brief loop bursts (steep-turn practice,
# a flight-school lesson wandering a practice area, a couple of circuits at a field) must NOT
# read as surveillance, so the loop count demands sustained circling and the box/span/
# consistency gates require it to be tight, flat and one-directional.
ORBIT_MIN_LOOPS   = _envf("PATTERN_ORBIT_MIN_LOOPS", 3.5)    # net full circles, same direction
ORBIT_CONSISTENCY = _envf("PATTERN_ORBIT_CONSISTENCY", 0.8)   # share of turning in one direction
ORBIT_MAX_BOX_KM  = _envf("PATTERN_ORBIT_MAX_BOX_KM", 6.0)
ORBIT_MAX_SPAN_FT = _envf("PATTERN_ORBIT_MAX_SPAN_FT", 1800)

# survey / lawnmower
SURVEY_MIN_LEGS    = _envf("PATTERN_SURVEY_MIN_LEGS", 4)      # parallel passes
SURVEY_CONCENTR    = _envf("PATTERN_SURVEY_CONCENTRATION", 0.55)  # path share on one folded axis
SURVEY_MAX_SPAN_FT = _envf("PATTERN_SURVEY_MAX_SPAN_FT", 1200)
SURVEY_MIN_BOX_KM  = _envf("PATTERN_SURVEY_MIN_BOX_KM", 1.5)
SURVEY_MIN_PATH_RATIO = _envf("PATTERN_SURVEY_PATH_RATIO", 2.5)  # path length vs box diagonal


def _bearing(lat1, lon1, lat2, lon2):
    r = math.radians
    dl = r(lon2 - lon1)
    y = math.sin(dl) * math.cos(r(lat2))
    x = math.cos(r(lat1)) * math.sin(r(lat2)) - math.sin(r(lat1)) * math.cos(r(lat2)) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _features(points):
    """Geometry summary of a track, or None if too short/sparse to judge."""
    pts = [(float(t), float(al), la, lo) for (t, al, la, lo) in points if al is not None]
    if len(pts) < 8:
        return None
    pts.sort(key=lambda p: p[0])
    alts = [p[1] for p in pts]
    lats = [p[2] for p in pts]
    lons = [p[3] for p in pts]
    span = max(alts) - min(alts)
    box = _haversine_km((min(lats), min(lons)), (max(lats), max(lons)))

    # per-segment bearing + length, skipping sub-30 m jitter
    segs = []
    for i in range(1, len(pts)):
        d = _haversine_km((pts[i - 1][2], pts[i - 1][3]), (pts[i][2], pts[i][3]))
        if d < 0.03:
            continue
        segs.append((_bearing(pts[i - 1][2], pts[i - 1][3], pts[i][2], pts[i][3]), d))
    if len(segs) < 6:
        return None
    path = sum(d for _, d in segs)

    cum = abs_turn = 0.0
    for i in range(1, len(segs)):
        dt = ((segs[i][0] - segs[i - 1][0] + 540) % 360) - 180    # signed [-180,180]
        cum += dt
        abs_turn += abs(dt)
    loops = abs(cum) / 360.0
    consistency = abs(cum) / abs_turn if abs_turn > 0 else 0.0

    # dominant travel axis (bearings folded mod 180, distance-weighted) -> lawnmower axis
    bins = [0.0] * 18      # 10-degree bins over 0..180
    for b, d in segs:
        bins[int((b % 180) / 10) % 18] += d
    axis = (max(range(18), key=lambda i: bins[i]) * 10 + 5)
    # project each segment onto that axis; count direction runs ("legs") and the along-axis share.
    # This counts parallel passes regardless of how each U-turn is sampled (the perpendicular
    # turn segments project ~0 and are skipped), unlike a per-step heading-flip count.
    along = 0.0
    legs, cur = 0, 0
    for b, d in segs:
        p = math.cos(math.radians(b - axis)) * d
        along += abs(p)
        if abs(p) < 0.03:                      # perpendicular hop / turn -> ignore
            continue
        s = 1 if p > 0 else -1
        if s != cur:
            legs += 1
            cur = s
    concentration = (along / path) if path else 0.0

    return {"n": len(pts), "span": round(span), "box_km": round(box, 1), "path_km": round(path, 1),
            "loops": round(loops, 2), "consistency": round(consistency, 2),
            "abs_turn": round(abs_turn), "legs": legs,
            "concentration": round(concentration, 2),
            "path_ratio": round(path / box, 1) if box > 0.05 else 0.0}


def is_orbit(f, min_loops=None):
    """min_loops lets a caller (e.g. an alert rule) tighten/loosen the main knob."""
    return bool(f and f["loops"] >= (ORBIT_MIN_LOOPS if min_loops is None else min_loops)
               and f["consistency"] >= ORBIT_CONSISTENCY
               and 0 < f["box_km"] <= ORBIT_MAX_BOX_KM and f["span"] <= ORBIT_MAX_SPAN_FT)


def is_survey(f, min_legs=None):
    """min_legs lets a caller (e.g. an alert rule) tighten/loosen the main knob."""
    return bool(f and f["legs"] >= (SURVEY_MIN_LEGS if min_legs is None else min_legs)
               and f["concentration"] >= SURVEY_CONCENTR
               and f["loops"] < 1.5 and f["span"] <= SURVEY_MAX_SPAN_FT
               and f["box_km"] >= SURVEY_MIN_BOX_KM and f["path_ratio"] >= SURVEY_MIN_PATH_RATIO)


def detect(points, kinds, min_loops=None, min_legs=None):
    """Live variant of classify() for the alert engine: check only the requested geometric
    kinds ('orbit' / 'survey') so a rolling window can be judged every scan without paying
    for the maneuver metrics too. Returns {kind: short detail string}.

    A rule's knobs may TIGHTEN the detection but never relax it below the global (env-set)
    minimums -- the floor is what keeps a trainer doing a couple of laps from paging anyone,
    even if a stored rule still carries an older, looser threshold."""
    ml = max(float(min_loops), ORBIT_MIN_LOOPS) if min_loops is not None else None
    mg = max(float(min_legs), SURVEY_MIN_LEGS) if min_legs is not None else None
    out = {}
    f = _features(points)
    if f:
        if "orbit" in kinds and is_orbit(f, min_loops=ml):
            out["orbit"] = f"{f['loops']:.1f} loops over {f['box_km']} km box"
        if "survey" in kinds and is_survey(f, min_legs=mg):
            out["survey"] = f"{f['legs']} parallel legs over {f['box_km']} km box"
    return out


def classify(points):
    """Return {pattern_key: short detail string} for every pattern recognised in the track."""
    out = {}
    m = _maneuver_metrics(points)
    if _is_air_show(m):
        out["airshow"] = "maneuver"
    f = _features(points)
    if is_orbit(f):
        out["orbit"] = f"{f['loops']:.0f} loops"
    if is_survey(f):
        out["survey"] = f"{f['legs']} legs"
    return out


# pattern catalogue for the UI (key, label, short description)
CATALOG = [
    {"key": "airshow", "label": "Air show", "desc": "aerobatic maneuvering (loops/wingovers)"},
    {"key": "orbit",   "label": "Surveillance / orbit", "desc": "sustained circling over a point"},
    {"key": "survey",  "label": "Aerial survey / mapping", "desc": "parallel grid (lawnmower) legs"},
]
