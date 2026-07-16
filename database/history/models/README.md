# 3D aircraft models (optional)

Drop glTF binary models (`.glb`) into this folder and the 3D view swaps them in for the
built-in silhouettes. Lookup order per aircraft, first hit wins:

1. `<ICAO-TYPE>.glb` — exact type, e.g. `C172.glb`, `A320.glb`, `B738.glb`, `PC12.glb`
   (upper- or lower-case filenames both work)
2. a category fallback: `ga.glb` (light singles), `jet.glb` (fast military),
   `heli.glb` (helicopters), `airliner.glb` (everything else)

Anything without a match keeps the silhouette — no configuration needed, missing files
are probed once and remembered for the session.

Model conventions (the glTF standard): the front of the aircraft faces **+Z**, up is +Y.
Models are auto-scaled to the display size and auto-centred, so raw exports work as-is.
If a model has nodes named `prop*`, `rotor*` or `spinner*`, they spin.

A good source is Flightradar24's published model set:
https://github.com/Flightradar24/fr24-3d-models — check that repo's LICENSE for what
use it permits before copying models in; they are not bundled here for that reason.
