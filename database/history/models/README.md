# 3D aircraft models (optional)

The easy way: **Settings → 3D models** — drag `.glb` files onto the upload area and
they're stored under the persistent data volume (`HISTORY_MODELS_DIR`, default
`<globe_history>/models`) and used immediately on the next page load.

Files placed in this folder work too (bundled fallbacks; uploads take precedence on a
name clash). Lookup order per aircraft, first hit wins:

1. `<ICAO-TYPE>.glb` — exact type, e.g. `C172.glb`, `A320.glb`, `B738.glb`, `PC12.glb`
   (upper- or lower-case filenames both work)
2. a category fallback: `ga.glb` (light singles), `jet.glb` (fast military),
   `heli.glb` (helicopters), `airliner.glb` (everything else)

Anything without a match keeps the silhouette — no configuration needed, missing files
are probed once and remembered for the session.

Model conventions (the glTF standard): the front of the aircraft faces **+Z**, up is +Y.
Models are auto-scaled to the display size and auto-centred, so raw exports work as-is.
If a model has nodes named `prop*`, `rotor*` or `spinner*`, they spin.

A good source is Flightradar24's published model set (GPLv2):
https://github.com/Flightradar24/fr24-3d-models — already named by ICAO type. Those
files are old-format **glTF 1.0** binaries; the Settings uploader converts them to
glTF 2.0 automatically (`gltf1to2.py`), and any 1.0 file dropped into this folder by
hand is converted the first time it is served.
