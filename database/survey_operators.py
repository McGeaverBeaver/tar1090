"""Curated "who flies surveillance / aerial photography" knowledge base.

When an orbit (surveillance circling) or survey (mapping "lawnmower") pattern fires, this module
answers the follow-up questions: who is this operator, who do they likely work for, and -- if the
imagery they collect ever becomes public -- where you might find it. Like airshow_types.py it is
deliberately a plain, hand-maintained list (not a runtime guess) so it is easy to audit and
extend: add an entry to PROFILES (specific companies) or KEYWORD_CATEGORIES (operator-name
keywords like "police" / "pipeline") and both the lookup API and the Alerts page pick it up.

Everything here is heuristic context, not an identification: profiles match on the operator name
recorded in the aircraft database (plus optional callsign/registration patterns), and the texts
are worded accordingly ("typically", "often"). Shared by tar1090-history-api.py (/api/lookup).
"""

import fnmatch
import urllib.parse

# --- reusable "where the imagery ends up" notes ------------------------------
_IMG_US_PUBLIC = ("Imagery flown for US federal programs is usually public: NAIP and USGS aerial "
                  "imagery can be downloaded from USGS EarthExplorer (earthexplorer.usgs.gov); "
                  "county orthophotos usually appear on the commissioning county's GIS site.")
_IMG_CA_PUBLIC = ("Government-commissioned photography in Canada often lands in NRCan's National "
                  "Air Photo Library and provincial open-data portals (e.g. Ontario's LIO "
                  "imagery); commercial capture stays with the client.")
_IMG_COMMERCIAL = "Commercial imagery -- sold to subscribers/clients, no general free public access."
_IMG_GEOPHYS = ("Geophysical survey data (magnetics/EM/gravity, not photos) -- surveys flown for "
                "NRCan or provincial geological surveys are often published (GEOSCAN, provincial "
                "geophysics databases); mineral-exploration surveys stay private.")

# --- specific companies (matched on operator name, optionally callsign/registration) ---------
# match values are case-insensitive substrings unless they contain * or ? (then fnmatch).
PROFILES = [
    # imagery-program operators (recurring city capture)
    {"name": "Nearmap", "match": {"operator": ["nearmap"]},
     "category": "Aerial imagery program",
     "works_for": "Flies its own recurring capture program over cities; imagery is sold by "
                  "subscription to government, insurance and construction customers.",
     "imagery": "Commercial -- browsable with a Nearmap subscription at nearmap.com.",
     "url": "https://www.nearmap.com"},
    {"name": "EagleView / Pictometry", "match": {"operator": ["eagleview", "pictometry"]},
     "category": "Aerial imagery program (oblique)",
     "works_for": "Recurring ortho + oblique capture sold to insurers, assessors and local "
                  "government (property measurement / assessment).",
     "imagery": _IMG_COMMERCIAL, "url": "https://www.eagleview.com"},
    {"name": "Vexcel Imaging", "match": {"operator": ["vexcel"]},
     "category": "Aerial imagery program",
     "works_for": "Flies the Vexcel Data Program; the imagery is licensed widely and some of it "
                  "surfaces publicly as Bing Maps aerial imagery.",
     "imagery": "Partly public via Bing Maps aerial view (bing.com/maps); full-resolution access "
                "is commercial.", "url": "https://vexcel-imaging.com"},
    {"name": "Hexagon (HxGN Content Program)", "match": {"operator": ["hexagon", "leica geosystems"]},
     "category": "Aerial imagery program",
     "works_for": "Flies the HxGN Content Program -- recurring high-resolution capture over North "
                  "America and Europe, sold to GIS/government customers.",
     "imagery": _IMG_COMMERCIAL, "url": "https://hxgncontent.com"},
    {"name": "Apple Maps imagery collection", "match": {"operator": ["apple"]},
     "category": "Aerial imagery program",
     "works_for": "Imagery collection flown on Apple's behalf (contract operators) for Apple Maps "
                  "/ Look Around.",
     "imagery": "Ends up in Apple Maps; Apple publishes where it is currently collecting.",
     "url": "https://maps.apple.com/imagecollection"},
    {"name": "Google aerial imagery", "match": {"operator": ["google"]},
     "category": "Aerial imagery program",
     "works_for": "Aerial capture flown for Google's mapping products (often by contractors).",
     "imagery": "Ends up as Google Maps / Google Earth 3D and aerial imagery; no public flight "
                "schedule.", "url": "https://www.google.com/earth/"},

    # survey / mapping contractors (fly for whoever commissions the job)
    {"name": "Woolpert", "match": {"operator": ["woolpert"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "US geospatial contractor -- flies imagery and lidar for USGS, NOAA, USDA and "
                  "state/county GIS programs as well as private clients.",
     "imagery": _IMG_US_PUBLIC, "url": "https://woolpert.com"},
    {"name": "Sanborn Map Company", "match": {"operator": ["sanborn"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "Long-running US mapping contractor -- federal (incl. NAIP), state and county "
                  "imagery/lidar programs.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.sanborn.com"},
    {"name": "Surdex", "match": {"operator": ["surdex"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "US aerial imagery contractor; a regular flyer of USDA NAIP state blocks and "
                  "county orthophoto programs.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.surdex.com"},
    {"name": "NV5 Geospatial (Quantum Spatial)", "match": {"operator": ["nv5", "quantum spatial"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "Large US geospatial contractor -- lidar and imagery for USGS 3DEP, federal and "
                  "state agencies.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.nv5.com"},
    {"name": "Kucera International", "match": {"operator": ["kucera"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "Ohio-based aerial mapping firm -- county/state orthophoto and lidar programs.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.kucerainternational.com"},
    {"name": "Dewberry", "match": {"operator": ["dewberry"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "US engineering/geospatial firm -- FEMA floodplain, USGS and coastal mapping.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.dewberry.com"},
    {"name": "Keystone Aerial Surveys", "match": {"operator": ["keystone aerial"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "One of the larger US survey-flying operators; flies camera/lidar missions "
                  "under contract for mapping firms and government programs.",
     "imagery": _IMG_US_PUBLIC, "url": "https://www.kasurveys.com"},
    {"name": "Fugro", "match": {"operator": ["fugro"]},
     "category": "Aerial survey / geo-data contractor",
     "works_for": "Global geo-data company -- aerial mapping, coastal lidar and geophysics for "
                  "government and energy clients.",
     "imagery": "Client-owned; government-commissioned coastal/lidar work is often published by "
                "the commissioning agency.", "url": "https://www.fugro.com"},

    # Canadian survey / mapping operators
    {"name": "Leading Edge Geomatics", "match": {"operator": ["leading edge geomatics"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "New Brunswick-based imagery/lidar operator flying large-area capture programs "
                  "across Canada and the US.",
     "imagery": _IMG_CA_PUBLIC, "url": "https://www.leadingedgegeomatics.com"},
    {"name": "Airborne Sensing", "match": {"operator": ["airborne sensing"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "Toronto-based aerial photography/lidar operator flying municipal, provincial "
                  "and federal mapping contracts.",
     "imagery": _IMG_CA_PUBLIC, "url": "https://www.airbornesensing.com"},
    {"name": "Kisik Aerial Survey", "match": {"operator": ["kisik"]},
     "category": "Aerial survey / mapping contractor",
     "works_for": "BC-based aerial survey operator flying imagery/lidar contracts in western "
                  "Canada.",
     "imagery": _IMG_CA_PUBLIC, "url": "https://kisik.ca"},

    # airborne geophysics (very tight, low, long grids -- data, not photos)
    {"name": "Sander Geophysics", "match": {"operator": ["sander geophysics"]},
     "category": "Airborne geophysical survey",
     "works_for": "Ottawa-based airborne geophysics operator -- magnetics/gravity/EM grids for "
                  "geological surveys (NRCan, provinces) and mineral/oil exploration clients.",
     "imagery": _IMG_GEOPHYS, "url": "https://www.sgl.com"},
    {"name": "Terraquest", "match": {"operator": ["terraquest"]},
     "category": "Airborne geophysical survey",
     "works_for": "Toronto-based airborne geophysics operator -- magnetics/radiometrics grids for "
                  "exploration and government geoscience programs.",
     "imagery": _IMG_GEOPHYS, "url": "https://terraquest.ca"},
    {"name": "Xcalibur Multiphysics", "match": {"operator": ["xcalibur"]},
     "category": "Airborne geophysical survey",
     "works_for": "Global airborne geophysics group (absorbed CGG's airborne unit) -- exploration "
                  "and government geoscience surveys.",
     "imagery": _IMG_GEOPHYS, "url": "https://xcaliburmp.com"},

    # government / quasi-government
    {"name": "NOAA", "match": {"operator": ["noaa", "national oceanic"]},
     "category": "Government survey",
     "works_for": "US government -- coastal mapping, snow/water surveys and post-storm damage "
                  "imagery.",
     "imagery": "Largely public: post-event imagery at storms.ngs.noaa.gov, coastal imagery via "
                "NOAA Digital Coast (coast.noaa.gov).", "url": "https://www.omao.noaa.gov"},
    {"name": "Civil Air Patrol", "match": {"operator": ["civil air patrol"]},
     "category": "Government / auxiliary survey",
     "works_for": "USAF auxiliary -- aerial damage-assessment photography for FEMA and state "
                  "emergency agencies, plus search and rescue.",
     "imagery": "Disaster imagery is often released publicly through FEMA/state portals after "
                "events.", "url": "https://www.gocivilairpatrol.com"},
]

# --- operator-name keyword fallbacks (category-level guesses) ---------------------------------
KEYWORD_CATEGORIES = [
    {"keywords": ["police", "sheriff", "constabulary", "gendarmerie", "rcmp", "highway patrol",
                  "public safety", "surete", "sûreté"],
     "category": "Law enforcement aerial support",
     "works_for": "The police service named as the operator -- orbiting is typical of overwatch, "
                  "pursuit support or search tasking.",
     "imagery": "Not public."},
    {"keywords": ["news", "television", "broadcast", "media"],
     "category": "Electronic newsgathering",
     "works_for": "The broadcaster named as the operator -- orbiting a scene for live coverage.",
     "imagery": "Aired footage -- check the broadcaster's site and social channels."},
    {"keywords": ["pipeline", "hydro", "transmission", "powerline", "power line", "utility",
                  "utilities"],
     "category": "Pipeline / powerline patrol",
     "works_for": "Right-of-way inspection along the operator's (or a client's) pipeline or "
                  "transmission corridors -- long low runs or slow orbits over infrastructure.",
     "imagery": "Inspection records -- not public."},
    {"keywords": ["survey", "geomatics", "mapping", "photogrammetry", "lidar", "aerial photo",
                  "aerial imaging", "geospatial"],
     "category": "Aerial survey / mapping contractor",
     "works_for": "Whoever commissioned the mapping job -- typically municipal/provincial/state "
                  "GIS programs, engineering firms or developers.",
     "imagery": "Depends on the client: government-commissioned imagery often ends up on the "
                "agency's open-data portal; private capture stays with the client."},
    {"keywords": ["conservation", "natural resources", "wildlife", "forestry", "environment"],
     "category": "Resource / wildlife survey",
     "works_for": "The resource agency named as the operator -- wildlife counts, forestry and "
                  "fire mapping fly grid or orbit patterns too.",
     "imagery": "Sometimes published in agency reports or open-data portals."},
]

# --- operators that are almost certainly NOT surveillance ------------------------------------
# Flight schools / training operators fly loops, back-and-forth drills and practice-area
# wandering all day, which is exactly the geometry the orbit/survey detectors look for. The
# alert engine skips pattern (orbit/survey) alerts for operators whose name matches one of
# these, unless the rule opts back in -- plain metadata/zone rules are unaffected.
TRAINING_KEYWORDS = [
    "flight school", "flying school", "flight training", "pilot training", "aviation training",
    "air training", "flight academy", "flying academy", "aviation academy", "flight college",
    "flight instruction", "flight center", "flight centre", "flying club", "aero club",
    "aeroclub", "college", "university", "polytechnic", "cegep", "institute of technology",
]


def is_training_operator(operator):
    """True when the operator name reads like a flight school / training organisation
    (e.g. 'Seneca College Of Applied Arts And Technology', 'Brampton Flying Club')."""
    op = (operator or "").lower()
    return bool(op) and any(k in op for k in TRAINING_KEYWORDS)


# aircraft types commonly used as camera/lidar survey platforms (heuristic hint only)
SURVEY_PLATFORM_TYPES = {
    "C402", "C404", "C310", "C421", "C414", "PA31", "PA34", "P68", "DA42", "DA62",
    "C208", "C206", "C182", "C172", "B190", "AC90", "AC95", "BN2P", "BE58", "PC12",
}
# common working helicopters (police / news / utility patrol platforms)
WORK_HELI_TYPES = {
    "R22", "R44", "R66", "B06", "B407", "B412", "B429", "B505", "AS50", "AS55", "AS65",
    "EC20", "EC30", "EC35", "EC45", "H125", "H135", "H145", "H500", "MD52", "MD60", "A109", "A139",
}


def _tok_match(patterns_, value):
    """Any-token match: substring, or fnmatch when the token has wildcards."""
    if value is None:
        return False
    v = str(value).lower().strip()
    if not v:
        return False
    for tok in (patterns_ or []):
        t = str(tok).lower().strip()
        if not t:
            continue
        if ("*" in t or "?" in t):
            if fnmatch.fnmatch(v, t):
                return True
        elif t in v:
            return True
    return False


def match_profile(operator=None, callsign=None, registration=None):
    """Best matching PROFILES entry (specific company), else a KEYWORD_CATEGORIES guess from the
    operator name, else None. Returns a plain dict ready to serialise."""
    for pr in PROFILES:
        m = pr["match"]
        if (_tok_match(m.get("operator"), operator)
                or _tok_match(m.get("callsign"), callsign)
                or _tok_match(m.get("registration"), registration)):
            return {"name": pr["name"], "category": pr["category"], "works_for": pr["works_for"],
                    "imagery": pr["imagery"], "url": pr.get("url"), "matched": "profile"}
    op = (operator or "").lower()
    if op:
        for kc in KEYWORD_CATEGORIES:
            if any(k in op for k in kc["keywords"]):
                return {"name": operator, "category": kc["category"],
                        "works_for": kc["works_for"], "imagery": kc["imagery"],
                        "url": None, "matched": "keyword"}
    return None


def heuristic(icao_type=None, pattern_kinds=None):
    """Category-level guess from airframe type + which pattern fired, for aircraft whose operator
    is unknown or matched nothing. Returns {category, text} or None."""
    ty = (icao_type or "").upper()
    kinds = set(pattern_kinds or [])
    if "survey" in kinds:
        base = ("The parallel-line \"lawnmower\" track is characteristic of aerial photography / "
                "lidar mapping or airborne geophysics.")
        if ty in SURVEY_PLATFORM_TYPES:
            base += (f" A {ty} is a common survey platform, which strengthens that read.")
        return {"category": "Likely aerial survey / photography",
                "text": base + " Mapping flights are usually flown under contract; if the client "
                               "is a government agency the imagery often becomes public on that "
                               "agency's open-data or air-photo portal."}
    if "orbit" in kinds:
        if ty in WORK_HELI_TYPES:
            return {"category": "Likely police / news / utility helicopter",
                    "text": f"A {ty} orbiting a point is most often police aerial support, a news "
                            "helicopter over a scene, or powerline/site inspection work."}
        return {"category": "Likely surveillance / observation orbit",
                "text": "Sustained same-direction circling over one spot is typical of police or "
                        "news aircraft, site photography, traffic watch, pipeline/powerline "
                        "inspection, or a training hold."}
    return None


def public_imagery_sources(registration=None):
    """Where publicly-released aerial imagery for this aircraft's registry country tends to be
    published (generic pointers, independent of operator)."""
    reg = (registration or "").upper().strip()
    out = []
    if reg.startswith("N"):
        out += [{"label": "USGS EarthExplorer (NAIP + federal aerial imagery)",
                 "url": "https://earthexplorer.usgs.gov"},
                {"label": "NOAA post-storm emergency imagery",
                 "url": "https://storms.ngs.noaa.gov"}]
    if reg.startswith(("C-", "CF-", "C-F", "C-G", "C-I")):
        out += [{"label": "NRCan National Air Photo Library",
                 "url": "https://natural-resources.canada.ca/maps-tools-and-publications/"
                        "satellite-imagery-elevation-data-and-air-photos/air-photos"},
                {"label": "Ontario GeoHub imagery (LIO)",
                 "url": "https://geohub.lio.gov.on.ca"}]
    out.append({"label": "OpenAerialMap (openly licensed aerial imagery)",
                "url": "https://openaerialmap.org"})
    return out


def external_links(hexid=None, registration=None, callsign=None, operator=None):
    """Per-aircraft lookup links: live trackers, photo sites, civil registries, operator search."""
    links = []
    hx = (hexid or "").strip().lower().lstrip("~")
    reg = (registration or "").strip().upper()
    if hx:
        links.append({"label": "ADS-B Exchange (track history)",
                      "url": "https://globe.adsbexchange.com/?icao=" + urllib.parse.quote(hx)})
        links.append({"label": "Planespotters photos",
                      "url": "https://www.planespotters.net/hex/" + urllib.parse.quote(hx.upper())})
    if reg:
        links.append({"label": "JetPhotos " + reg,
                      "url": "https://www.jetphotos.com/registration/" + urllib.parse.quote(reg)})
        links.append({"label": "Flightradar24 " + reg,
                      "url": "https://www.flightradar24.com/data/aircraft/"
                             + urllib.parse.quote(reg.lower())})
        if reg.startswith("N"):
            links.append({"label": "FAA registry " + reg,
                          "url": "https://registry.faa.gov/aircraftinquiry/Search/NNumberResult"
                                 "?nNumberTxt=" + urllib.parse.quote(reg)})
        elif reg.startswith("C-") or reg.startswith("CF-"):
            links.append({"label": "Transport Canada registry",
                          "url": "https://wwwapps.tc.gc.ca/saf-sec-sur/2/ccarcs-riacc/RchSimp.aspx"})
        elif reg.startswith("G-"):
            links.append({"label": "UK CAA G-INFO registry",
                          "url": "https://siteapps.caa.co.uk/g-info/"})
    op = (operator or "").strip()
    if op:
        links.append({"label": f"Search the web for “{op}”",
                      "url": "https://www.google.com/search?q="
                             + urllib.parse.quote_plus(f'"{op}" aerial survey OR photography')})
    return links
