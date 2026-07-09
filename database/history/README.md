# tar1090 reports — web interface guide

The reports site is the extra web front-end this fork adds on top of tar1090: a
live map with a sortable aircraft table, a searchable **history** of every flight
that has been logged, a rules-based **alerting** system, and an admin **settings**
area. It is served same-origin by [`tar1090-history-api.py`](../tar1090-history-api.py)
(port `8090` by default) and is bundled into the all-in-one image.

Everything below is just the front-end. Nothing here changes readsb or the stock
tar1090 live map — it reads the same data readsb already produces.

---

## Navigation & sign-in

Four tabs run across the top: **Live**, **History**, **Alerts**, **Settings**.
**Live is the default page** on both desktop and mobile.

- **Alerts** and **Settings** are admin-only and are hidden for viewers.
- If OIDC login is enabled (see the [all-in-one README](../../allinone/README.md#sign-in-authentik-oidc--optional)),
  a **Logout** button sits at the top-right. There are two roles:
  - **admin** — full access, including Alerts and Settings.
  - **viewer** — Live and History only.
- With login disabled, the whole site is open and everyone is effectively an admin.

On phones each page collapses to a single column; History and the editors gain a
**List / Map** switcher so you can flip between the table and the map.

---

## Live

Real-time view of every aircraft readsb is currently tracking.

**Top toolbar**
- **Find callsign / hex / reg** — type to filter the table and map to matching aircraft.
- **🏷 Labels** — show a callsign label on every aircraft.
- **〰 Trails** — draw the selected aircraft's trail.
- **⏚ Ground** — show/hide aircraft on the ground (on by default).
- **⤢ Fit** — zoom the map to fit every aircraft.
- A live counter (`N shown · N with position · N tracked`) and the last-update time.

**Aircraft table (left)** — sortable by any column; click a header to sort.
Columns: **Flight**, **Type**, **Alt** (ft), **Spd** (kn), **Sqk** (squawk) and
**Dist** (km from your receiver). Click a row to select that aircraft; the panel
collapses with the `—` button.

**Detail card (right)** — appears when you select an aircraft (from the table or by
clicking it on the map). Shows a photo (via planespotters) plus altitude, ground
speed, track, vertical rate, squawk, last-seen, ICAO hex, signal (dBFS), operator
and position. For scheduled (airline) callsigns the **Route** row draws the flight
leg boarding-pass style — `SYD ——✈·····○ MEL` — with the aircraft at its true
along-track position (it crawls as the flight progresses), a radar ping on the
**destination** airport, and a live **"lands ≈ HH:MM"** estimate from ground speed
and remaining distance. Map labels and the 3D view gain a `→ MEL`-style destination
tag. A **Sightings on this radar** row counts how many flights of this airframe are
in your history index ("147 flights · since Jun 4, 2025" — or **First sighting** for
a brand-new catch). Close it with **✕**.

**Map** — trails are coloured by altitude using the legend at the bottom-right
(ground → 40k ft). Click anywhere empty to deselect.

> The **Dist** column and receiver-relative distances need your receiver location.
> Set `SITE_LAT` / `SITE_LON` (the API also falls back to readsb's `LAT` / `LON`).

---

## History

Search the flight index and replay trails from `globe_history`.

**Search bar**
- Quick time presets: **4h · 8h · 24h · 7d · 30d · 1y**.
- A compact search box (🔍) matches callsign / registration / type.
- **Filters ▾** expands the advanced filters: callsign, registration, type,
  operator, military, and an exact **From / To** range. They stay collapsed until
  you need them.

**Results & map**
- **Per page** (100 / 500 / 1000 / 2000) with **Prev / Next**. The map draws a trail
  for **every flight on the current page**, so the page size you pick is what you see.
- **〰 Altitude trails / ✈ Aircraft icons** toggles between altitude-coloured trails
  and directional aircraft icons.
- **Multi** — tap-to-add multi-select (handy on touch screens). **Columns ▾** shows
  or hides table columns.
- Select a **single** flight to get its detailed altitude-coloured trail plus a
  replay scrubber (play it back point by point). On mobile, selecting a flight jumps
  to the map and zooms to fit it. Airline callsigns also get a **route card** under
  the photo — the same drawn flight leg as the Live page, with the plane at wherever
  it was along the route when its recorded trail ended.
- **🔔 Alert** (admin) turns the selected flight(s) into alert rules on the Alerts tab.

---

## Alerts (admin)

Create rules that fire when matching aircraft are seen, and review what has fired.
Alerts publish to MQTT / Home Assistant (configured under Settings → Integrations)
and show up in-app.

**Rules** — each rule has:
- **Match conditions** (all must match): callsign, ICAO type, registration, operator,
  squawk, altitude band, military-only. Wildcards (`*` `?`) and comma lists are allowed.
- **Zone** (optional) — draw a circle or polygon on the map; empty = anywhere in range.
- **Time window** (optional) — restrict to certain days / hours (container local time).
- **Re-alert after** a cooldown so you aren't spammed for the same aircraft.

**Recent alerts** — a live log of everything that has fired. **Click any entry** to
open a map showing that flight's **trail** plus a **plane icon at the exact spot the
alert went off**, pointed along its direction of travel. Enable browser
**notifications** or **mute** the chime from the buttons at the top.

---

## Settings (admin)

Organised into three sub-tabs (deep-linkable, e.g. `settings.html#users`):

**Integrations** — MQTT broker → Home Assistant: broker host/port/credentials/TLS,
base topic, and HA MQTT Discovery (auto-creates a `binary_sensor` per rule, no YAML).
**Save** stores it; **Save & send test** publishes a test message.

**Historical import** — backfill the searchable flight index from readsb's
`globe_history` heatmap chunks, so traffic from before logging began (or with traces
off) becomes searchable. It runs in the background and **resumes automatically** after
a crash or restart. The live panel shows progress only while a job is running and sits
at **idle** otherwise; a **Previous imports** table lists past runs (finished time,
outcome, chunks, flights, fixes). Use **Start / resume** to continue from the last
checkpoint, or **Restart from scratch** to re-scan everything.

**Users** — everyone who has signed in is recorded here (name/email, role, login count,
first/last seen). **Block** a user to deny access: their active session is dropped
within ~20 s and any future sign-in is refused. You can't block your own account.
(Blocking only applies when OIDC login is enabled.)

---

## Configuration

The front-end itself needs no configuration. Route (origin → destination) lookups
query the same public routeset service the stock tar1090 UI uses (`adsb.im`, a cache
in front of `api.adsb.lol`); results are cached in the browser for 6 h. To turn them
off, run `localStorage.setItem('routeApi','off')` in the browser console, or set
`window.ROUTE_API_URL = ''` (or a different routeset URL) before `route.js` loads.
The serving API is configured entirely
through environment variables — see [`tar1090-history-api.py`](../tar1090-history-api.py)
and the [all-in-one README](../../allinone/README.md) for `TAR1090_DB_DSN`,
`GLOBE_HISTORY_DIR`, `SITE_LAT` / `SITE_LON`, and the optional `OIDC_*` sign-in
settings. No personal or site-specific values are baked into the code.
