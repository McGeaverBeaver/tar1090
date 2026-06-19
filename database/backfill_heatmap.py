#!/usr/bin/env python3
"""CLI for the heatmap importer -- builds the searchable flight index from readsb's
globe_history heatmap .ttf chunks (see tar1090_heatmap_import for the details and the
callsign-is-NULL limitation). The same importer also runs from the history app's Settings
tab ("Historical import") with progress + resume; this CLI is for one-off / scripted runs.

Usage:  backfill_heatmap.py [GLOBE_HISTORY_DIR]   (default /var/globe_history)
Env:    TAR1090_DB_DSN  (DB connection, default "dbname=tar1090")
        AIRCRAFT_CSV    (optional aircraft.csv(.gz) for registration/type/operator/military)
        GAP_MINUTES, MIN_POINTS  (see tar1090_heatmap_import)
Re-running is safe (a leg is inserted only when no existing flight for that hex overlaps it).
"""

import logging
import os
import sys

import psycopg

import tar1090_heatmap_import as hm


def main():
    logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    root = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GLOBE_HISTORY_DIR", "/var/globe_history")
    dsn = os.environ.get("TAR1090_DB_DSN", "dbname=tar1090")
    meta = hm.load_csv_metadata(os.environ.get("AIRCRAFT_CSV", "").strip())

    last = [0]
    def progress(s):
        if s["chunks_done"] - last[0] >= 500 or s["chunks_done"] == s["chunks_total"]:
            last[0] = s["chunks_done"]
            hm.log.info("  %d/%d chunks · %d flights · %d fixes",
                        s["chunks_done"], s["chunks_total"], s["flights"], s["fixes"])

    with psycopg.connect(dsn) as conn:
        imp = hm.Importer(conn, meta, on_progress=progress)
        result = imp.run(root)
        hm.log.info("import %s: %d flight legs from %d fixes", result, imp.n_flights, imp.n_points)


if __name__ == "__main__":
    main()
