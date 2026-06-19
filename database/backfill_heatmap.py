#!/usr/bin/env python3
"""backfill_heatmap: build the searchable flight index from readsb's heatmap .ttf chunks.

Unlike backfill_globe_history (which needs the per-aircraft trace_full_*.json files that
only exist with READSB_ENABLE_TRACES), readsb ALWAYS writes
  globe_history/<YYYY>/<MM>/<DD>/heatmap/NN.bin.ttf
-- 30-minute binary chunks of every position it saw. This importer walks those chunks,
reconstructs each aircraft's legs by time gaps, and populates the aircraft + flights tables
the history search uses, so traffic from before the logger ran (or with traces disabled)
becomes searchable. The trails themselves are still read on demand from these same .ttf
files by the history API -- nothing is duplicated here, only the lightweight index.

LIMITATION -- no callsign: heatmap chunks store only hex + position + altitude (+ groundspeed
and a source flag), NOT the callsign/squawk. So imported flights have callsign = NULL; you
search them by registration / type / operator / military / time / altitude instead. Those
come from the aircraft table -- pass AIRCRAFT_CSV (the wiedehopf aircraft.csv.gz, same file
the logger uses) to fill them in for aircraft your live logger never saw. To get callsigns
for old data you need the trace_full_*.json files + backfill_globe_history.py instead.

Heatmap record format (mirrors html/script.js and tar1090-history-api.py): little-endian
int32 quads [hex(+flags), lat*1e6, lon*1e6, gs<<16|alt]; each ~15 s time "slice" is preceded
by a HEAT_MAGIC marker record. Bit 24 of the hex word marks a non-ICAO (TIS-B/ADS-R "~")
address -- skipped here so the registry index stays to real ICAO aircraft.

Usage:  backfill_heatmap.py [GLOBE_HISTORY_DIR]      (default /var/globe_history or $GLOBE_HISTORY_DIR)
Env:
  TAR1090_DB_DSN   DB connection (same as the logger; default "dbname=tar1090")
  AIRCRAFT_CSV     optional aircraft.csv(.gz) for registration/type/operator/military
  GAP_MINUTES      split an aircraft's track into a new flight after a gap this long (default 15)
  MIN_POINTS       ignore legs with fewer fixes than this (default 2)

Re-running is safe: a leg is inserted only when no existing flight for that hex overlaps it,
and (icao_hex, start_time) is UNIQUE -> ON CONFLICT DO NOTHING.
"""

import glob
import gzip
import logging
import os
import struct
import sys
from datetime import datetime, timezone

import psycopg

log = logging.getLogger("backfill_heatmap")

DB_DSN       = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
AIRCRAFT_CSV = os.environ.get("AIRCRAFT_CSV", "").strip()
GAP_SECONDS  = float(os.environ.get("GAP_MINUTES", "15")) * 60
MIN_POINTS   = int(os.environ.get("MIN_POINTS", "2"))

HEAT_MAGIC   = 0xe7f7c9d
NONICAO_BIT  = 0x1000000          # bit 24: TIS-B/ADS-R "~" address
CHUNK_SECS   = 1800               # 30-min heatmap chunks
BATCH        = 5000

AIRCRAFT_UPSERT = """
INSERT INTO aircraft (icao_hex, registration, icao_type, type_desc, operator,
                      military, interesting, pia, ladd, year, first_seen, last_seen)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (icao_hex) DO UPDATE SET
  registration = COALESCE(aircraft.registration, EXCLUDED.registration),
  icao_type    = COALESCE(aircraft.icao_type,    EXCLUDED.icao_type),
  type_desc    = COALESCE(aircraft.type_desc,    EXCLUDED.type_desc),
  operator     = COALESCE(aircraft.operator,     EXCLUDED.operator),
  military     = aircraft.military OR EXCLUDED.military,
  first_seen   = LEAST(aircraft.first_seen,      EXCLUDED.first_seen),
  last_seen    = GREATEST(aircraft.last_seen,    EXCLUDED.last_seen)
"""

# Insert a flight only if no existing flight for this hex overlaps its window -- avoids
# shadow-duplicating the callsign-bearing flights the live logger / trace backfill recorded.
FLIGHT_INSERT = """
INSERT INTO flights (icao_hex, callsign, squawk, start_time, end_time, msg_count, max_alt)
SELECT %s, NULL, NULL, %s, %s, %s, %s
WHERE NOT EXISTS (
  SELECT 1 FROM flights f
  WHERE f.icao_hex = %s AND f.start_time <= %s AND f.end_time >= %s
)
ON CONFLICT (icao_hex, start_time) DO NOTHING
"""


# --- aircraft metadata (optional tar1090-db CSV) ----------------------------
def parse_dbflags(s):
    """tar1090-db encodes dbFlags as a positional bit string: idx 0=military, 1=interesting,
    2=pia, 3=ladd (see html/planeObject.js). e.g. '10' = military. Tolerate a plain int too."""
    s = (s or "").strip()
    if not s:
        return 0
    if set(s) <= {"0", "1"}:
        f = 0
        for i, bit in enumerate((1, 2, 4, 8)):
            if i < len(s) and s[i] == "1":
                f |= bit
        return f
    try:
        return int(s)
    except ValueError:
        return 0


def load_csv_metadata(path):
    """{icao_hex: (registration, icao_type, type_desc, operator, dbFlags)} from the real
    wiedehopf aircraft.csv(.gz): headerless, ';'-separated, columns
    icao;registration;icaotype;dbflags;description;year;operator."""
    if not path:
        return {}
    if not os.path.exists(path):
        log.warning("AIRCRAFT_CSV %s not found; importing hexes without registry metadata", path)
        return {}
    opener = gzip.open if path.endswith(".gz") else open
    out, _i = {}, sys.intern
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace", newline="") as fh:
            for line in fh:
                p = line.rstrip("\n").split(";")
                if len(p) < 3:
                    continue
                key = p[0].strip().lower().lstrip("~")
                if len(key) != 6 or any(c not in "0123456789abcdef" for c in key):
                    continue                       # header row or junk -> skip
                reg  = (p[1].strip() if len(p) > 1 else "") or None
                typ  = (p[2].strip() if len(p) > 2 else "") or None
                desc = (p[4].strip() if len(p) > 4 else "") or None
                op   = (p[6].strip() if len(p) > 6 else "") or None
                out[key] = (reg, _i(typ) if typ else None, _i(desc) if desc else None,
                            _i(op) if op else None, parse_dbflags(p[3] if len(p) > 3 else ""))
    except OSError as e:
        log.warning("failed to read AIRCRAFT_CSV %s: %s", path, e)
        return {}
    log.info("loaded registry metadata for %d aircraft from %s", len(out), path)
    return out


# --- heatmap chunk parsing (mirrors tar1090-history-api._parse_heat_chunk) ---
def heat_alt(f3):
    a = f3 & 0xFFFF
    if a & 0x8000:
        a -= 0x10000
    if a == -123:        # on ground
        return 0
    if a == -124:        # unknown
        return None
    return a * 25        # units of 25 ft


def chunk_files(root):
    """Every heatmap chunk under root as (cbase_epoch, path), in chronological order."""
    out = []
    for path in glob.glob(os.path.join(root, "[0-9]" * 4, "[0-9]" * 2, "[0-9]" * 2,
                                       "heatmap", "*.bin.ttf*")):
        parts = path.split(os.sep)
        try:
            y, m, d = int(parts[-5]), int(parts[-4]), int(parts[-3])
            idx = int(os.path.basename(path).split(".")[0])
        except (ValueError, IndexError):
            continue
        if not 0 <= idx <= 47:
            continue
        day = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        out.append((day + idx * CHUNK_SECS, path))
    out.sort()
    return out


def parse_chunk(path):
    """(ints, n, slice_positions, ival) for a heatmap chunk, or None if unreadable/empty."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    except (OSError, ValueError) as e:
        log.warning("read failed %s: %s", path, e)
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


# --- importer ---------------------------------------------------------------
class Importer:
    def __init__(self, conn, meta):
        self.conn = conn
        self.meta = meta
        self.legs = {}            # hex -> [start_ts, last_ts, max_alt_or_None, count]
        self.flush_aircraft = {}  # hex -> aircraft row tuple, pending upsert
        self.aircraft_done = set()
        self.flight_buf = []      # parametrised rows for FLIGHT_INSERT
        self.n_flights = 0
        self.n_points = 0

    def aircraft_row(self, hexid, ts):
        m = self.meta.get(hexid)
        reg, typ, desc, op, flags = m if m else (None, None, None, None, 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return (hexid, reg, typ, desc, op,
                bool(flags & 1), bool(flags & 2), bool(flags & 4), bool(flags & 8),
                None, dt, dt)

    def emit_leg(self, hexid, leg):
        start, last, max_alt, count = leg
        if count < MIN_POINTS:
            return
        if hexid not in self.aircraft_done:
            self.flush_aircraft[hexid] = self.aircraft_row(hexid, start)
        self.flight_buf.append((
            hexid,
            datetime.fromtimestamp(start, tz=timezone.utc),
            datetime.fromtimestamp(last, tz=timezone.utc),
            count, max_alt,
            hexid,
            datetime.fromtimestamp(last, tz=timezone.utc),
            datetime.fromtimestamp(start, tz=timezone.utc),
        ))
        if len(self.flight_buf) >= BATCH:
            self.write_batch()

    def write_batch(self):
        if self.flush_aircraft:
            with self.conn.cursor() as cur:
                cur.executemany(AIRCRAFT_UPSERT, list(self.flush_aircraft.values()))
            self.aircraft_done.update(self.flush_aircraft)
            self.flush_aircraft.clear()
        if self.flight_buf:
            with self.conn.cursor() as cur:
                cur.executemany(FLIGHT_INSERT, self.flight_buf)
            self.n_flights += len(self.flight_buf)
            self.flight_buf.clear()
        self.conn.commit()

    def add(self, hexid, t, alt):
        self.n_points += 1
        leg = self.legs.get(hexid)
        if leg is None:
            self.legs[hexid] = [t, t, alt, 1]
        elif t - leg[1] > GAP_SECONDS:
            self.emit_leg(hexid, leg)
            self.legs[hexid] = [t, t, alt, 1]
        else:
            leg[1] = t
            if alt is not None and (leg[2] is None or alt > leg[2]):
                leg[2] = alt
            leg[3] += 1

    def expire(self, before):
        """Close legs that cannot continue (last fix older than the gap before `before`)."""
        if not self.legs:
            return
        dead = [h for h, lg in self.legs.items() if lg[1] < before - GAP_SECONDS]
        for h in dead:
            self.emit_leg(h, self.legs.pop(h))

    def run(self, root):
        chunks = chunk_files(root)
        if not chunks:
            log.error("no heatmap chunks found under %s "
                      "(expected <Y>/<M>/<D>/heatmap/NN.bin.ttf)", root)
            return
        log.info("scanning %d heatmap chunks under %s", len(chunks), root)
        for ci, (cbase, path) in enumerate(chunks):
            self.expire(cbase)
            parsed = parse_chunk(path)
            if not parsed:
                continue
            a, n, sl, ival = parsed
            for si, spos in enumerate(sl):
                t = cbase + si * ival
                j = spos + 4
                while j < n and a[j] != HEAT_MAGIC:
                    w = a[j]
                    if not (w & NONICAO_BIT):                 # skip "~" non-ICAO addresses
                        self.add(format(w & 0xFFFFFF, "06x"), t, heat_alt(a[j + 3]))
                    j += 4
            if (ci + 1) % 500 == 0:
                log.info("  %d/%d chunks · %d flights · %d fixes",
                         ci + 1, len(chunks), self.n_flights + len(self.flight_buf), self.n_points)
        for h, lg in list(self.legs.items()):                 # flush everything still open
            self.emit_leg(h, lg)
        self.legs.clear()
        self.write_batch()
        log.info("done: imported %d flight legs from %d fixes", self.n_flights, self.n_points)


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    root = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GLOBE_HISTORY_DIR", "/var/globe_history")
    meta = load_csv_metadata(AIRCRAFT_CSV)
    with psycopg.connect(DB_DSN) as conn:
        Importer(conn, meta).run(root)


if __name__ == "__main__":
    main()
