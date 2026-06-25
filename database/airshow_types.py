"""Curated, editable "air-show aircraft" database.

A grouped list of ICAO type designators commonly seen at air shows: purpose-built
aerobatic mounts, historic warbirds, military fast-jet demo types, and the turboprop
trainers display teams fly. It is deliberately a plain, hand-maintained list (not a
guess at runtime) so it is easy to audit and extend -- add a designator to the right
category and both the alert engine and the web UI pick it up.

Designators are matched case-insensitively, per token, as exact or wildcard matches
(the same way the alert engine matches the ICAO type field), so e.g. "F18" also covers
the broadcast variants and "PTS*" would cover every Pitts. Keep them UPPERCASE.

This is shared by tar1090-alerter.py (matching) and tar1090-history-api.py (which serves
it to the Live + Alerts pages), so there is one source of truth.
"""

AIRSHOW_TYPES = {
    "aerobatic": {
        "label": "Aerobatic",
        "desc": "Purpose-built aerobatic & competition aircraft",
        "types": ["E300", "EA30", "PTS1", "PTS2", "PTSS", "SU26", "SU29", "SU31",
                  "YK52", "YK55", "YK50", "YK18", "CP10", "MX2", "MXS", "XA42",
                  "GP10", "ONE", "DR10"],
    },
    "warbird": {
        "label": "Warbirds",
        "desc": "Historic / vintage military aircraft",
        "types": ["P51", "SPIT", "HURI", "P40", "P38", "P47", "P63", "CORS", "F4U",
                  "T6", "AT6", "SNJ", "HRVD", "T28", "B17", "B25", "B24", "B29",
                  "LANC", "DC3", "C47", "YK3", "YK9", "BF09", "ME09", "P2", "SB2C"],
    },
    "jet_demo": {
        "label": "Jet / military demo",
        "desc": "Fast-jet display & demo-team aircraft",
        "types": ["F16", "F18", "F15", "F22", "F35", "A10", "EUFI", "RAFL", "HAWK",
                  "L39", "M339", "HUNT", "F4", "A4", "MG29", "GNAT", "JPRO", "VAMP",
                  "L29", "MB33"],
    },
    "display_team": {
        "label": "Display teams / trainers",
        "desc": "Turboprop & jet trainers flown by display teams",
        "types": ["PC7", "PC9", "PC21", "TUCA", "T34", "T6", "AT3", "PROV"],
    },
}


def types_for(categories):
    """Union of ICAO type tokens for the given category keys (deduped, order-stable)."""
    out, seen = [], set()
    for key in (categories or []):
        grp = AIRSHOW_TYPES.get(key)
        if not grp:
            continue
        for t in grp["types"]:
            tu = t.upper()
            if tu not in seen:
                seen.add(tu)
                out.append(tu)
    return out


def all_types():
    return types_for(list(AIRSHOW_TYPES.keys()))
