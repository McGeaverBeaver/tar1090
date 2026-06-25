"""Shared aerobatic-maneuvering detector.

Used by the alert engine (live) and the air-show backfill (history) so both judge a track the
same way. The goal is to tell genuine air-show / aerobatic flying apart from ordinary circuits and
flight-school "up and down" work, which otherwise trips a naive altitude-reversal counter.

A track only counts as aerobatic maneuvering when, in its busiest ~2-minute window, it:
  * stays inside a small box (a display stays put; circuits / cross-country roam), and
  * pulls steep vertical rates (loops/wingovers climb & dive far harder than a trainer), and
  * reverses vertical direction several times (sustained oscillation, not one climb + descent),
  * over a meaningful total altitude span.

metrics() takes (t_sec, alt_ft, lat, lon) points and returns the measurements; is_air_show()
applies the thresholds. Thresholds are deliberately strict and overridable via env.
"""

import math
import os


def _envf(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


DEADBAND_FT   = _envf("AIRSHOW_DEADBAND_FT", 200)    # min altitude change to count a reversal
WINDOW_SEC    = _envf("AIRSHOW_WINDOW_SEC", 120)     # reversal-density / box window
MIN_REVERSALS = _envf("AIRSHOW_MIN_REVERSALS", 3)    # reversals required within the densest window
MIN_PEAK_FPM  = _envf("AIRSHOW_MIN_PEAK_FPM", 2000)  # steepest climb/descent required
MAX_BOX_KM    = _envf("AIRSHOW_MAX_BOX_KM", 12.0)    # must stay inside this box (excludes roaming)
MIN_SPAN_FT   = _envf("AIRSHOW_MIN_SPAN_FT", 700)    # min total altitude swing


def _haversine_km(a, b):
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def metrics(points):
    """points: iterable of (t_sec, alt_ft, lat, lon). alt None entries are dropped.
    Returns {span, peak_fpm, rev_window, box_km, n}."""
    pts = [(float(t), float(al), la, lo) for (t, al, la, lo) in points if al is not None]
    if len(pts) < 6:
        return {"span": 0, "peak_fpm": 0, "rev_window": 0, "box_km": 0.0, "n": len(pts)}
    pts.sort(key=lambda p: p[0])
    alts = [p[1] for p in pts]
    span = max(alts) - min(alts)

    # steepest sustained vertical rate (fpm) between consecutive fixes (ignore big time gaps)
    peak = 0.0
    for i in range(1, len(pts)):
        dt = pts[i][0] - pts[i - 1][0]
        if dt <= 0 or dt > 60:
            continue
        fpm = abs(pts[i][1] - pts[i - 1][1]) / (dt / 60.0)
        if fpm > peak:
            peak = fpm

    # times at which vertical direction reversed (with an amplitude deadband)
    rev_times = []
    direction, ref = 0, alts[0]
    for i in range(1, len(pts)):
        a = pts[i][1]
        if a - ref > DEADBAND_FT:
            d = 1
        elif ref - a > DEADBAND_FT:
            d = -1
        else:
            continue
        if direction and d != direction:
            rev_times.append(pts[i][0])
        direction, ref = d, a

    # densest WINDOW_SEC of reversals, and remember its time bounds for the box measurement
    rev_window, best = 0, None
    j = 0
    for i in range(len(rev_times)):
        while rev_times[i] - rev_times[j] > WINDOW_SEC:
            j += 1
        if i - j + 1 > rev_window:
            rev_window = i - j + 1
            best = (rev_times[j], rev_times[i])

    # bounding box of the maneuvering window only (so a ferry to/from the show doesn't inflate it)
    if best is not None:
        win = [p for p in pts if best[0] - WINDOW_SEC <= p[0] <= best[1] + WINDOW_SEC]
    else:
        win = pts
    lats = [p[2] for p in win]
    lons = [p[3] for p in win]
    box_km = _haversine_km((min(lats), min(lons)), (max(lats), max(lons)))

    return {"span": round(span), "peak_fpm": round(peak), "rev_window": rev_window,
            "box_km": round(box_km, 1), "n": len(pts)}


def is_air_show(m, min_span=None, min_reversals=None):
    """True if the measurements look like aerobatic / air-show maneuvering.
    min_span / min_reversals let a caller (e.g. an alert rule) tighten the two main knobs."""
    return (m["span"] >= (MIN_SPAN_FT if min_span is None else min_span)
            and m["rev_window"] >= (MIN_REVERSALS if min_reversals is None else min_reversals)
            and m["peak_fpm"] >= MIN_PEAK_FPM
            and 0 < m["box_km"] <= MAX_BOX_KM)
