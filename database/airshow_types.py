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


# Common aircraft that are NOT air-show / aerobatic mounts but can still trip the maneuver detector
# while doing ordinary work -- flight-school steep turns & climbs, photo/EMS helicopters, etc. A
# maneuvering aircraft of one of these types is NOT flagged as "air show" (it can only qualify by
# being in the curated AIRSHOW_TYPES above). Editable; UPPERCASE ICAO type designators.
NON_AEROBATIC = {
    # Cessna singles / trainers / tourers
    "C150", "C152", "C162", "C170", "C172", "C72R", "C175", "C177", "C180", "C182", "C82R",
    "C185", "C206", "C207", "C208", "C210", "C310", "C337", "C340", "C402", "C404", "C414", "C421",
    # Piper
    "P28A", "P28B", "P28R", "P28S", "P28T", "PA28", "PA38", "PA32", "P32R", "P32T", "PA34",
    "PA44", "PA46", "P46T", "PA24", "PA23", "PA31",
    # Cirrus / Diamond / Mooney / Grumman
    "SR20", "SR22", "S22T", "DA40", "DA42", "DA62", "DV20", "DA20", "M20P", "M20T", "M20J",
    "AA1", "AA5",
    # Beechcraft
    "BE19", "BE23", "BE24", "BE33", "BE35", "BE36", "BE58", "BE76", "BE9L", "B190",
    # Socata / other tourers + utility
    "TB10", "TB20", "TB21", "TBM7", "TBM8", "TBM9", "PC12", "GA8",
    # Common helicopters (these orbit / work; not aerobatic)
    "R22", "R44", "R66", "B06", "B407", "B412", "B429", "B505", "AS50", "AS65", "A139",
    "EC20", "EC30", "EC35", "EC45", "EC55", "H125", "H135", "H145", "H500", "S76", "S92", "B47G",
}


def maneuver_plausible(icao_type):
    """True if a maneuvering aircraft of this type could plausibly be doing air-show aerobatics
    (i.e. it is not a known non-aerobatic trainer/tourer/helicopter). Unknown type -> allowed."""
    return (icao_type or "").upper() not in NON_AEROBATIC
