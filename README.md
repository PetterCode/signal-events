# signal-events

Receives incident reports (text + photos) sent over Signal, stores them in a
local SQLite database, and generates summary reports (PDF/Markdown) — all
designed to run on a laptop with **no network connection**, except for the
one step that talks to Signal's servers.

## How it fits together

```
Signal (phone) --link--> signal-cli (this laptop) --sync (needs network)--> SQLite + attachments/
                                                                                    |
                                                                     heuristic field parser
                                                                                    |
                                                                     local Flask review UI (offline)
                                                                                    |
                                                                     PDF / Markdown report (offline)
```

Only `signal-events sync` needs network access, since it talks to Signal's
servers to fetch new messages. Everything else — reviewing events, editing
fields, and generating reports — reads and writes only the local SQLite
database and local files under `data/`, so it works fully offline.

## The 8 report fields

Each incoming message is parsed (best-effort, offline, regex/keyword based —
not an LLM) into:

1. **Time**
2. **Place**
3. **Number of objects observed**
4. **What object is observed**
5. **What activities are being made**
6. **Distinguishing marks**
7. **Who is reporting** (a 7S-labeled message's own "Sagesman" — or "Från"
   if that's all it gives — wins when present, since the report body is
   the more authoritative source and the two can genuinely differ from
   whoever's Signal account it was sent from, e.g. a duty officer relaying
   someone else's account; otherwise falls back to the Signal sender)
8. **What happens next**

Because free-text extraction is inherently unreliable, every parsed event is
flagged `needs_review` until someone confirms or corrects it in the web UI.
Only reviewed events are included in reports by default.

## Setup

### 1. Install Python dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional: if you plan to use Lantmäteriet's free FTP map source (Inställningar
→ "Kartkälla" → "Lantmäteriets FTP (GDAL)" — see "Kart-vy" under Day-to-day
usage), also install GDAL as a system package (not in `requirements.txt` —
this app calls its `gdal_translate` command-line tool, not its Python
bindings): `brew install gdal` on macOS, or your distro's `gdal-bin`
package on Linux. Not needed for the default URL-based map providers.

### 2. Install and link signal-cli (one-time, needs network)

`signal-cli` is a separate open-source project — not part of this repo —
that speaks the Signal protocol. Install it (e.g. `brew install signal-cli`
on macOS, or see https://github.com/AsamK/signal-cli for other platforms).

Link it as a secondary device to an existing Signal account on your phone:

```bash
python -m signal_events link --name "incident-laptop"
```

This prints a QR code directly in the terminal (needs the `qrcode` package,
already in `requirements.txt`); scan it from **Signal app → Settings →
Linked devices → Link new device** on the phone that will be sending
reports. Once scanned, it prints the linked phone number and the exact
`export SIGNAL_EVENTS_PHONE_NUMBER=...` line to use below. (Use `--no-qr`
to print the raw link URI as text instead, e.g. if your terminal can't
render block characters.)

Other account-management commands, if needed:

```bash
python -m signal_events accounts              # list numbers signal-cli knows locally
python -m signal_events devices --number +...  # list devices linked to an account
```

### 3. Configure

Set environment variables (or put them in a `.env` you source yourself —
this project doesn't read `.env` files automatically to avoid adding a
dependency):

```bash
export SIGNAL_EVENTS_PHONE_NUMBER="+15551234567"   # the linked account's number
# Optional overrides (defaults shown):
# export SIGNAL_EVENTS_DATA_DIR="$(pwd)/data"
# export SIGNAL_EVENTS_SIGNAL_CLI_BIN="signal-cli"
```

### 4. Initialize the database

```bash
python -m signal_events init-db
```

## Day-to-day usage

**Starting the server without a terminal.** `Starta server.command` in
the project root is a double-clickable launcher (Finder runs `.command`
files in Terminal.app) that starts `signal-events serve` for you. It
asks one question — whether to allow guests on the same WiFi/LAN to log
in (see "Letting guests..." below) — then starts the server accordingly,
on port 5001, adding `--watch` automatically if
`SIGNAL_EVENTS_PHONE_NUMBER` is already set in your environment. Needs
`chmod +x` to still be set if it's ever re-copied or re-cloned (Finder
won't run a non-executable script); otherwise it just needs a
double-click.

**Header status strip** — every page in the web UI shows a status bar
under "Signalhändelser" with, at a glance: the configured unit name, the
current date/time (a plain client-side clock, ticking in the browser's
own local time — nothing server-rendered or stale), a quick threat-level
badge (GRÖN/GUL/RÖD, always agreeing with whatever period the
Sammanställd hotbedömning page itself is currently showing — it reuses
that page's own computation rather than a separately fixed window that
could silently disagree with it), and when a report — incident report or
threat-level summary — was last successfully sent via Signal to the
report group that adjacent units' own status updates also arrive on
("Aldrig" if never). This is a quick-glance indicator only; the
authoritative assessment is always the dedicated Sammanställd
hotbedömning page, computed over whichever period you actually select
there.

If any adjacent units have sent a status report, a third row lists each
one's own latest reported threat level (GRÖN/GUL/RÖD/okänd) and when that
report was received, as a small badge, e.g. "2.Kompani: GUL · mottagen
2026-07-28 18:44". Adjacent-unit reports are free text, not a
structured field, so the level is extracted heuristically
(`analysis.parse_adjacent_level`): a line that actually starts with
"Bedömning" wins if there is one, otherwise the most severe level keyword
mentioned anywhere in the body wins (never the benefit of the doubt) —
and if nothing matches at all, the badge reads "okänd" rather than
guessing. The row itself is hidden entirely until at least one adjacent
unit has actually reported.

**Fetch new messages once** (needs network; run whenever you have
connectivity — at home, on wifi, etc.):

```bash
python -m signal_events sync
```

**Continuously watch a Signal group** (needs network; run this in its own
terminal/tmux/service for as long as you want live ingestion):

```bash
python -m signal_events watch --group "Stabsassistent test-händelser"
```

Resolves the group name to its Signal group id once (via `signal-cli
listGroups`), then long-polls for new messages and pulls in only the ones
posted to that group — direct messages and other groups are ignored.
Prints a line each time it ingests new report(s), and a "still watching"
heartbeat every 15 silent polls so you can tell it's alive; stop it with
Ctrl+C. `--group` defaults to `"Stabsassistent test-händelser"`
(`SIGNAL_EVENTS_WATCH_GROUP`); `--poll-timeout`
controls how many seconds each poll cycle waits for new messages
(default 20).

**A single bad poll cycle no longer kills the whole loop.** A transient
network blip, signal-cli briefly failing, or an unexpected ingestion
error used to propagate straight out and end watching permanently, with
nothing to show for it beyond a terminal that had gone quiet — the
single biggest reason this was hard to trust ("is it still running, or
did it silently die an hour ago?"). Now a failing cycle is retried
automatically every 5 seconds until it recovers, and the failure is
visible in three places at once: the terminal prints it the moment it
appears (and again the moment it clears, rather than waiting for the
periodic heartbeat), the header status strip at the top of every web UI
page shows **"Senast mottagning från Signal: ..."** plus a red
**"Mottagning misslyckas"** badge while an error is active, and
Systemlogg (admin-only, see below) records when watching started,
stopped, started failing, and recovered — so a multi-hour outage shows
up as two log lines (when it started, when it recovered), not hundreds
of identical ones. Only one thing still stops the loop outright: an
unresolvable Signal group name (a typo, a renamed/deleted group) at
startup, since retrying that forever would just repeat the same
failure — fix the name (`--group`/`SIGNAL_EVENTS_WATCH_GROUP`, or the
Signal group settings in Inställningar) and restart.

**Testing ingestion by messaging the incident group yourself?** Send it
from a *different* phone/account than the one signal-cli is linked or
registered to. A message from the *same* account arrives as a Signal
"sync" transcript rather than a normal incoming message, and is
intentionally never ingested (it's the same mechanism that keeps this
unit's own outgoing reports from being picked back up as if an adjacent
unit sent them) — so it'll silently do nothing, which used to be
indistinguishable from a broken receive path. It's now called out by
name in Systemlogg ("Eget testmeddelande hoppades över") so this doesn't
look like a hang.

This same command also polls a *second* group: `SIGNAL_EVENTS_REPORT_GROUP`
(default `"Stabsassistent test-rapport"`) — the same group this unit's own
generated reports are sent *to* (see "Send a report or summary to Signal"
below) doubles as the shared exchange channel where adjacent units post
their own reports too. Both groups are checked in a single signal-cli
receive call per cycle (running two concurrent signal-cli processes
against the same account can corrupt its local state, so this is
deliberately one call, not two). The sending unit is identified from the
plain-text unit name embedded in the report's own filename
(`<enhet>_<TNR>_<rapporttyp>.<ext>`) — no separate sender mapping needed,
and only genuine incoming messages are ever ingested this way, so this
unit's own sent reports are never picked back up as if they were an
adjacent unit's. Received status reports show up on the "Sammanställd
hotbedömning" page under "Status från angränsande enheter" — but only
ones with an identified unit name; since this group is shared with
report distribution, it can also carry plain chat from people not
posting a named report at all (someone just messaging in the group),
and that's stored (for completeness) but left out of this list rather
than shown as if it were a unit's status. Manage a reference list of
known adjacent unit names on the "Inställningar" page (this list is just
for your own reference — it doesn't affect how incoming reports are
matched, since that's read directly from the filename).

**Events received from adjacent units** are a separate thing from the
status reports above — a specific observation an adjacent unit passed
along (phone call, radio, in person) that's worth keeping in the same
structured log. "+ Från angränsande enhet" on Tidslinje opens the same
8-field manual-entry form as "+ Lägg till händelse", plus a required
unit-name field (autocompleted from the Inställningar roster above, but
free text — no need to have pre-added the unit). The saved event is
tagged with that unit's name (`events.source_unit`) so it's tracked
separately from this unit's own: shown on Tidslinje with a "från
`<enhet>`" badge and on Kart-vy as a small blue dot instead of this
unit's own pin, but always excluded from this unit's own threat
assessment and every generated report (`db.list_events`'s `own_only`) —
the same way the adjacent-unit status reports above never get merged
into this unit's own picture.

This same `watch`/`serve --watch` command also polls a *third* group:
`SIGNAL_EVENTS_SENSOR_GROUP` (default `"Stabsassistent test-sensorer"`)
— for automated sensor-trigger events from a sensor gateway. A sensor
event is reported in the exact same 7S format as a human incident
report, so it's parsed and stored identically (same `events` table, same
review workflow, same fields) — the only difference is which Signal
group it came in on. This makes three groups checked in the same single
signal-cli receive call per cycle described above.

**A *fourth* group, `SIGNAL_EVENTS_TAK_BRIDGE_GROUP`** (default
`"Stabsassistent test-tak"`), is the bridge toward a TAK Server (the
backend ATAK/WinTAK/iTAK clients connect to) — reusing this app's
existing Signal infrastructure rather than adding a native CoT/TLS
client. The design deliberately keeps all TAK/CoT-specific complexity
(the actual protocol handling, certificate trust, symbology) out of this
app entirely: a *separate*, not-yet-built plugin living on/next to the
TAK Server would hold its own Signal identity in this group and
translate between CoT and plain text in both directions, using whatever
plugin API that specific TAK Server offers (FreeTAKServer's and the
official TAK Server's are quite different, which is exactly why that
choice is left to whoever administers the actual server, rather than
baked in here).

- **Inbound**: a GeoChat message an ATAK operator sends, relayed by that
  future plugin as plain text into this group, is ingested exactly like
  any other incoming report — same `parser.parse_event_fields`
  heuristics — and tagged `events.is_tak_bridge` for display/provenance
  (shown as "Mottagen via TAK-brygga" on the event page). Unlike a
  sensor-group event, it's *not* exempted from `duplicates.py`: a report
  relayed from an ATAK operator is a normal one-off human observation,
  not a repeating automated trigger.
- **Outbound**: the "Skicka till TAK-brygga" button on any event's page
  sends a one-event PDF summary to this group (the same
  `_send_pdf_to_group` machinery period reports already use), for that
  same future plugin to convert into a CoT marker for connected ATAK
  clients to see.

**Rename the Signal groups** (web UI, "Inställningar" page → "Signal-
grupper") — the five group names above (bevakningsgrupp/watch,
rapportgrupp/report, återkommande-grupp/recurring, sensorgrupp/sensor,
TAK-brygga) can be edited there instead of only via the
`SIGNAL_EVENTS_WATCH_GROUP`/`_REPORT_GROUP`/`_RECURRING_GROUP`/
`_SENSOR_GROUP`/`_TAK_BRIDGE_GROUP` env vars, which now only serve as
the fallback default when nothing's been set on that page. A change
there applies immediately to web-triggered sends (report/summary/
recurring/tak-brygga) and to the incident-report/summary forms' own
display, but `signal-events watch`/`serve --watch`'s background poller
only resolves the watch/report group names once, at its own startup —
restart it (or the whole server, for `serve --watch`) to pick up a
renamed group. An explicit `--group`/
`--watch-group` CLI flag always overrides whatever's configured here.

**Review and correct parsed events** (fully offline, run anytime):

```bash
python -m signal_events serve
# open http://127.0.0.1:5000 in a browser
```

`serve` on its own never touches the network — it only reads/writes the
local database. To also pull new Signal messages continuously while the
web UI is running, add `--watch` (runs the same group-poller as
`signal-events watch`, in a background thread — including the report
group doubling as the adjacent-units exchange channel):

```bash
python -m signal_events serve --watch --watch-group "Stabsassistent test-händelser"
```

If the poller fails (no network, signal-cli not linked, an unexpected
error), it retries automatically every 5 seconds rather than stopping
itself — the web UI keeps working normally either way, and the failure
is visible on the header status strip (a red "Mottagning misslyckas"
badge, plus "Senast mottagning från Signal: ...") and in Systemlogg,
not just the server's terminal output. Only a wrong/renamed/deleted
`--watch-group` name stops the poller outright, since that can't be
fixed by retrying — restart `serve --watch` with the correct name once
it's known. `--watch-poll-timeout` mirrors `watch`'s `--poll-timeout`.

**Structured person/vehicle descriptions (Kännetecken).** On any event's
edit form, "+ Lägg till person" and "+ Lägg till fordon" open a small
panel of labeled fields — SCRIM (Size/Colour/Registration/Identifying
marks/Model) for a vehicle, A–H (Age/Build/Colour/Distinguishing
marks/Elevation/Face/Gait/Hair) for a person, matching the mnemonics
used by [7srapport.com](https://7srapport.com/) — plus, for a person,
Namn/Alias/Nationalitet/Födelsedatum when any of those happen to be
known. Filling in a few fields and clicking "Lägg till i Kännetecken"
composes them into readable free text (`"Fordon 1 (S – Size: Kombi, R –
Registration: ABC123)"`) appended to the Kännetecken field — multiple
people/vehicles in one report are numbered automatically (Person 1,
Person 2, ...). This is a convenience for writing consistent, structured
descriptions; the underlying field is still plain text a human can edit
freely afterward.

**Personer, fordon och objekt — a database of who and what keeps
showing up.** Every time an event is saved, its Kännetecken text is
scanned (`entities.py`, rule-based, offline) for these composer blocks
and for a bare `Reg.Nr: ...` mention, and each person/vehicle found is
created (or matched) as its own record on the "Personer, fordon och
objekt" nav tab and linked to that event. A vehicle is matched **across
different reports** by its normalized plate — the same real vehicle
mentioned in three separate sightings becomes one record with three
linked events, not three unrelated ones. A person found via the
structured composer block (see above) has no such reliable identifier,
so each report's own "Person 1"/"Person 2" stays a distinct record
unless a human manually links two together — but a person described in
plain prose with no composer block at all (typed up outside the app: a
file import, a historical backlog, another system) *is* matched across
reports, by the same Jaccard text-similarity heuristic the "Sammanställd
hotbedömning" page's own recurring-person clustering uses (see "RED is
reserved for..." below) — not exact identity like a plate, but the best
a rule-based parser can do for prose with no other structural marker,
and the only way a freeform-described recurring person ever gets picked
up at all. Composer text deliberately isn't given the same cross-report
matching: it's short and heavily templated (the field labels themselves
— "A – Age:", "B – Build:", ...  — are identical on every person),
which at realistic report volume pushes even unrelated people's
similarity score close enough to the threshold that real, unrelated
reports start coincidentally merging — confirmed while building the
10-day training scenario's own composer-formatted noise. Editing or
resaving an event re-syncs its automatically-found
records without touching anything added by hand. Objects (a found item,
a suspicious package, ...) have no automatic marker to key off and are
always added manually, from the same page. Every record's own page shows
every event it's linked to, everything "seen together with" it (other
records sharing at least one of the same events), an optional uploaded
photo, and free-text notes — and supports linking/unlinking an event, or
deleting the record, by hand at any time. The whole database (including
manually catalogued/watchlisted records, not just auto-extracted ones)
is cleared along with the event log by "Rensa händelselogg"/"Rensa allt"
on Inställningar — it exists to track who/what recurs *in this event
log*, not a standalone roster like the adjacent-unit list, so it
shouldn't outlive the events it was built from.

**Bevakningslista (watchlist).** The "Skicka bevakningslista" button on
Personer, fordon och objekt sends a focused PDF list to Signal: every
record linked to 2 or more events (recurring, straight from the
already-persisted database — no fresh text-similarity guessing at
list-render time, unlike the identity matching described above) plus
every record a human has flagged with its own "Bevaka" checkbox,
regardless of its own event count. Sent to its own group by default `SIGNAL_EVENTS_RECURRING_GROUP`
(`"Stabsassistent test-återkommande"`) — the same group used to be fed
from the "Sammanställd hotbedömning" page's own recurring-vehicle/person
text-clustering (see "RED is reserved for..." below), which remains a
separate, purely informational part of the automatic threat score and is
no longer what this button sends. Next to it, **"Spara som text"/"Spara
som PDF"** save the same list to disk (see "Rapportmapp" below) and as a
browser download, without needing Signal at all — for archiving, or for
handing to another unit by any channel. **"Importera bevakningslista"**
reads a list saved this way (by this unit or another) back into the
entities database: every person/vehicle/object on the imported list is
created or matched (vehicles by plate, persons/objects by exact label)
and always ends up flagged "Bevaka", since an imported record starts
with zero linked events of its own in this database and would otherwise
never resurface on a future locally-generated list despite having just
been imported specifically to be watched for.

**Rapportmapp — where generated reports are archived.** Every generated
report (hotbedömning, händelserapport, bevakningslista, in any format)
is written to a folder on disk in addition to triggering the browser's
own download, since this app runs on the user's own laptop and a fixed,
predictable archive location is more useful than relying on wherever the
browser happens to save downloads. Defaults to `data/reports/`
(`SIGNAL_EVENTS_REPORTS_DIR` env var, or override it per-installation on
Inställningar under "Enhet" — leave the field blank and save to reset to
the default).

**Kart-vy — position of events on a map.** If a report's "Ställe" field (or
its body text) contains a coordinate, the position is converted to lat/lon
automatically and offline (`signal_events/coordinates.py`) — no network
involved, and it never overrides a position a human has set. MGRS is tried
first and is the only format the app itself displays a position in
(Kartcentrum's own "MGRS: ..." line on Inställningar, via the same module's
reverse conversion), but a report can just as well use decimal degrees
(`59.3269, 18.0717`), degrees/minutes/seconds (`59°19'37"N 18°04'18"E`), or
degrees/decimal minutes (`59°19.617'N 18°04.300'E`) — whichever one a
person actually typed, tried in that order, first match wins. On an
event's own page, a click-to-place map lets anyone drop or move the pin
manually regardless of whether a coordinate was found in the text — a
manual pin always wins over an auto-extracted one, and a "Ta bort
position" button clears it. This same click-to-place map (and the same
auto-extraction, tried against the "Plats" and "Anteckningar" fields) is
also on the "Lägg till händelse manuellt" and "+ Från angränsande enhet"
forms, so a manually entered report can carry a position too, not just
Signal-ingested ones.
The "Kart-vy" nav tab shows every event that has a known position as a
marker on one map, filtered by the same Tidsperiod selection (24 tim/7
dagar/30 dagar/Alla) as Sammanställd hotbedömning and Tidslinje — defaults
to 7 dagar; clicking a marker opens that event. This unit's own events show
as ordinary pins; events received from adjacent units (see above) show as
small blue dots instead, and a "Visa/Dölj händelser från angränsande
enheter" link toggles them on or off (shown by default).

**Kartcentrum** (Inställningar) is the reference point everything else in
this section revolves around — the area that gets downloaded/cached, and
the fallback view when no event has a position yet. It defaults to
`config.DEFAULT_MAP_CENTER` — Stockholm Palace, 59°19'37"N 18°04'17"E —
until explicitly set. Once set, Inställningar also shows it converted to MGRS
(`coordinates.to_mgrs`) — handy for cross-referencing against a report
written in that format.

**Map tiles: online by default, with a local-cache option for offline
use.** Inställningar → "Kartläge" picks between the two:

- **Online** (the default) — the app fetches each tile live from the
  configured provider the moment a map needs it, and caches it to disk as
  a side effect. Maps work immediately with no setup beyond a tile
  provider, same as any ordinary online map; anything not already cached
  needs connectivity.
- **Lokal cache** — tiles are served strictly from whatever's already in
  the local cache, so viewing either map (an event's own page, or the
  "Kart-vy" tab) touches the network exactly zero times. An area outside
  the cached radius, or before any download has run, just renders as
  empty map rather than failing.

Either mode, the tile provider URL (including any API key) stays on the
server side — the browser always requests `/tiles/{z}/{x}/{y}.png` from
this app, never the provider directly, so a guest viewing the map never
sees the key. Leaflet's own JS/CSS is vendored locally under
`webapp/static/vendor/leaflet/` regardless of mode, so no CDN is ever
needed to load the page itself, only the tile images depend on the
network/cache.

**Downloading a whole area for local/offline use** works the same in
either mode, and is what "Lokal cache" mode depends on: set a center
point (latitud/longitud) for the skyddsobjekt's area on Inställningar →
"Kartcentrum", pick an **area size** — "Litet" (1×1 km), "Mellan" (10×10
km), or "Stort" (100×100 km, the default, matching this app's original
fixed 50 km radius before this became configurable) — then click "Ladda
ner kartor för området" — the one deliberate, bulk network-touching step
(besides `sync`/`watch`). Smaller sizes mean a quicker, lighter download;
"Litet" is enough for reviewing a single small compound closely, "Stort"
covers a wide surrounding area at the cost of a much larger one-time
fetch (and, if using Lantmäteriet's FTP source, a much longer one — see
below). It runs in a background thread so the web UI stays responsive;
Inställningar shows "X av Y kartrutor cachade" so you can check progress
by refreshing the page, and re-clicking the button later only fetches
whatever's still missing (e.g. after a run got interrupted, or to fill in
a changed center point or area size). Pre-filling the cache this way and
then switching to "Lokal cache" mode is the way to prepare for a
deployment that's expected to lose connectivity — in "Online" mode, the
cache still fills in tile-by-tile as maps get viewed, but only for
whatever's actually been looked at.

**Default provider: Lantmäteriet (Sweden's national land survey).**
`config.DEFAULT_TILE_URL_TEMPLATE` points at their free, open
"topowebb-ccby" WMTS layer — despite the "-ccby" in the name (left over
from a past product rename), it's actually licensed
[CC0](https://geotorget.lantmateriet.se/dokumentation/GEODOK/72/1.0_utgaende/atkomst-och-leverans/teknisk-beskrivning.html)
(no attribution required, commercial use and redistribution both fine) —
a better fit for a Swedish skyddsobjekt than a generic global provider,
both in map detail and in having terms that actually permit this app's
kind of caching.

Getting a working API key currently means going through Lantmäteriet's
Geotorget/NGP flow rather than the old (now-retired) opendata.lantmateriet.se
self-service signup:
1. Create a free private-person account at
   [geotorget.lantmateriet.se](https://geotorget.lantmateriet.se/).
2. Under "Nationella geodataplattformen" → "Bli konsument", apply and
   accept the terms.
3. In the [API-portalen](https://apimanager.lantmateriet.se/store/),
   generate a key for the **"token in URL"** REST method (not the
   OAuth2/Bearer-header one — that issues short-lived tokens this app's
   plain URL-template setting has no way to refresh on its own).

Paste the resulting key into Inställningar → "Kartleverantör"'s URL,
replacing `DIN_TOKEN` in:
```
https://api.lantmateriet.se/open/topowebb-ccby/v1/wmts/token/DIN_TOKEN/1.0.0/topowebb/default/3857/{z}/{y}/{x}.png
```
Until that's done, every tile request simply fails (server rejects the
placeholder token) and both maps show blank tiles — an honest failure
mode with nothing to debug, just a key to fill in. Other CC0 layers work
the same way, just swap `topowebb` for e.g. `topowebb_nedtonad` (a
muted-colour basemap that reads well under markers).

**For deployments outside Sweden**, or if you'd rather use an account you
already have, Inställningar → "Kartleverantör" accepts any tile URL (with
`{z}`/`{x}`/`{y}` placeholders and an API key baked into the query
string) — this is the URL both "Online" mode's per-tile fetches and the
bulk download use. Services like
[MapTiler](https://www.maptiler.com/), [Stadia Maps](https://stadiamaps.com/),
or [Thunderforest](https://www.thunderforest.com/) all have a free tier
whose terms explicitly permit caching tiles for offline use (e.g.
MapTiler's URL looks like
`https://api.maptiler.com/maps/basic-v2/{z}/{x}/{y}.png?key=YOUR_KEY`).
Besides the Inställningar area-size setting, `config.py`'s
`MAP_CACHE_AREA_SIZES`/`MAP_CACHE_MIN_ZOOM`/`MAP_CACHE_MAX_ZOOM` constants
can still be edited directly for further control (e.g. a custom radius, or
a narrower zoom range) if the three presets don't fit.

**A third option, if the WMTS API is unavailable or throttled: Lantmäteriet
also distributes the same map as one giant file over plain, anonymous
FTP** — no account, API key, or Geotorget order at all
(`ftp://download-opendata.lantmateriet.se/Topografisk_webbkarta_raster/`).
The catch: it's a single ~145 GB GeoPackage covering all of Sweden, not
split by region, so it's not something to download whole. Inställningar →
"Kartkälla" → "Lantmäteriets FTP (GDAL)" uses
[GDAL](https://gdal.org/)'s `/vsicurl/` virtual filesystem
(`lantmateriet_ftp.py`) to read just the tiles that intersect the
configured area directly from the remote file via HTTP range requests,
without downloading the rest — confirmed working (a real extracted tile
came back as genuine Stockholm street-level map imagery). Needs GDAL
installed separately (`brew install gdal` on macOS; not a Python
dependency, so it's not in `requirements.txt`) — Inställningar flags it
clearly if `gdal_translate` isn't on `PATH`.

The real tradeoff: measured throughput is **~1.85 seconds per tile even
batched one `gdal_translate` call per zoom level** (FTP's per-request
connection overhead dominates) — several hours for this app's usual 50
km/zoom 8–14 area, versus minutes for a working WMTS-style API. Because of
that, this source is bulk-download-only: unlike the URL-based source,
"Online" mode's live per-tile fetch is never used with it — Inställningar
→ "Kartor för området" always serves strictly from the local cache when
this source is selected, regardless of the Kartläge setting, and the bulk
download itself is meant to run once, unattended, in the background. Worth
it specifically if you want zero ongoing API dependency (no account to
maintain, no throttling to ever hit again) and don't mind a long one-time
wait; otherwise the WMTS API (once working) or MapTiler/Stadia/Thunderforest
are faster.

**Why not the public OpenStreetMap tile server, found by actually hitting
it:** at the default zoom range (8–14), the default "Stort" area size (50
km radius) is around 8,800 tiles — enough that OSM's public tile server
can and does cut a download
off partway with an automated abuse block (see their
[tile usage policy](https://operations.osmfoundation.org/policies/tiles/)),
even with the polite pacing and identifying User-Agent
`signal_events/tiles.py` already sends — and in "Online" mode, even
ordinary per-tile viewing can trigger the same block over time. The
tricky part: it doesn't reply with an HTTP error — it replies `200 OK`
with a genuine, normal-sized PNG (so it displays like a real tile in any
map viewer) whose pixels spell out "Access blocked", plus an `X-Blocked`
response header as the only reliable signal. `tiles.py` checks for that
header and stops the whole bulk download immediately the moment it sees
one (further requests would only be blocked the same way), and the
on-demand per-tile path treats a block the same as any other failed
fetch — falling back to a blank tile rather than caching it. If you hit
this during a bulk download, Systemlogg will show a
`map_tiles_download_blocked` entry, and Inställningar's tile count will
simply be lower than expected — re-running the download later (once
whatever triggered the block has cooled down) picks up from there, since
already-cached tiles aren't re-fetched. A cache filled by a version of
this code from *before* the block-detection fix existed may already
contain the blocked notice image saved as if it were real tiles —
Inställningar has a "Rensa blockerade kartrutor" button
(`tiles.purge_blocked_tiles`) that finds and removes exactly those (by
exact byte match against the known notice image), leaving genuine cached
tiles untouched, so you can re-download just the gaps instead of starting
over. Pointing Kartleverantör at Lantmäteriet or another provider whose
terms actually permit this use avoids the whole problem.

**Letting guests on the same WiFi/LAN use the web UI.** By default
(`--host 127.0.0.1`, the default) the web UI is reachable only from the
machine running it, with no login at all — unchanged from before this
feature existed. Starting it with `--host 0.0.0.0` (or a specific
LAN-visible address) also makes it reachable from other devices on the
same private network, at `http://<this-machine's-LAN-IP>:5000`. That
access is tiered automatically, based on where a request actually comes
from — there's nothing to configure beyond `--host`:

- **This machine itself (127.0.0.1/::1)** — full access, no login, exactly
  as before. This is "you"; if you also want to check the app from your
  own phone on the same WiFi, that counts as a guest (see below) and
  needs an account like anyone else — there's no separate admin login.
- **Another device on the same private network** — must log in with an
  account created on Inställningar → "Ytterligare användare" (name +
  password; `werkzeug.security` password hashing, not plain text). Once
  logged in, a guest can review/add/correct events, generate and send
  reports, view the threat-level summary and AI narrative — everything
  except Inställningar and Demo och övning, which stay admin-only (both
  hidden from the guest's own nav and rejected outright if requested
  directly). A "Logga ut" button sits next to "Inloggad som: &lt;name&gt;"
  in the header once logged in.
- **Anything not on a private network at all** (e.g. this laptop somehow
  has a public IP, or the port gets forwarded) — refused outright with no
  login form ever shown, regardless of credentials. Guest accounts are
  for people on your WiFi, not for exposing this over the internet.

**Systemlogg** (its own nav tab, admin-only — hidden from guests and
rejected outright if a guest requests it directly) records every login,
failed login attempt, logout, server start, and rejected non-private-
network access attempt, newest first, along with who (name and IP for
guest events) and when. It also lists which guest accounts have made a
request in the last 5 minutes and haven't since logged out ("Aktiva
användare just nu") — a login only counts as still "active" while that
holds; logging out clears it immediately rather than waiting for the
window to lapse. This is separate from the reported incident events
themselves (see Händelser) — it's an audit trail of who's touched this
installation, not operational data.

This is still a plain-HTTP local dev server with no rate-limiting or
account lockout — reasonable for a trusted home/office/field WiFi, but
don't port-forward it or put it on a network you don't trust. The
session-signing key is generated once per installation and stored
locally (not a fixed value baked into the source), so a guest session
from one install can't be forged by anyone who's just read this
open-source code.

There's no network discovery built in — a guest's device doesn't
automatically find the server, so the address still has to reach them
somehow. Inställningar → "Dela adress med gäster" shows a QR code (using
the `qrcode`/Pillow packages already bundled for the signal-cli linking
flow) that encodes this machine's actual LAN address — point a guest's
phone camera at it and it opens the login page directly, no typing an IP
address by hand. It only appears once the server is actually started
with `--host 0.0.0.0` (or another LAN-visible address); with the default
`127.0.0.1` binding, that card just explains that and shows no code,
since one would point at an address nobody outside this machine could
reach anyway.

Browse events, open one, check/correct the 8 fields, tick "Mark as
reviewed", save. Photos attached to the original message are shown inline.

Browse events, open one, check/correct the 8 fields, tick "Mark as
reviewed", save. Photos attached to the original message are shown inline.
The same form has a **"Trivial"** checkbox for routine, non-notable
reports (a deer crossing, a weather note, "nothing to report" on a
patrol) — tick it and the event is excluded both from generated reports
and from the "Sammanställd hotbedömning" threat analysis (see "RED is
reserved for..." below), the same way a duplicate already is. A
**"Hög vikt"** checkbox does the opposite kind of flagging — a badge on
the event page and in Tidslinje's "Status" column, for a report a human
wants to stand out at a glance — but purely manual: unlike Trivial/
Dublett there's no auto-classifier behind it (no reliable rule to guess
importance), and it has no effect on what's included in a generated
report. An event page also has a **"Ta bort händelse"** (delete) button — confirmed
before it takes effect — that permanently removes the event along with
its source message and any attached photos; use it for genuine mistakes
or unwanted duplicates rather than leaving them in the log.
Two other ways to get events in besides Signal sync, both in the web UI:

- **Add report** — a form to type in a single report by hand (phone call,
  in-person, radio), with optional photo upload.
- **Import from file** — upload a plain text `.txt` file with multiple
  reports at once. One report per block, blocks separated by a `---` line
  (or a blank line if none of your reports need blank lines internally).
  Each block may start with an optional `From: <name>` line to set the
  sender; otherwise a fallback name entered on the import form is used.
  Each block is parsed the same way as a Signal message (heuristically,
  flagged for review) — this is meant for backlogs, notes typed up
  elsewhere, or reports brought over from another device via USB.
- **Demo och övning** (its own nav tab, separate from plain file import) —
  a 10-day training scenario bundled with the app: buttons "Dag 1" through
  "Dag 10" each import that day's ~30 pre-written reports (mostly routine
  noise, with a recurring vehicle/person and an escalating armed/sabotage
  pattern woven in) with one click, no file needed. A handful of the
  notable events (the recurring van, the person in dark clothing, the two
  sabotage signs, the two armed sightings) come with a cartoon-style
  illustration attached, visible on that event's own page — a stand-in
  for a phone photo a guard might take, generated locally with Pillow (no
  external images/network) via `demo/generate_training_images.py`. Click
  through the days in order and watch **Sammanställd hotbedömning** build
  from GRÖN to GUL to RÖD as the pattern recurs. Each day also delivers a
  status report from two adjacent units ("2.Kompani", "3.Kompani") that
  escalate on the
  same rhythm shifted a day earlier/later, shown under "Status från
  angränsande enheter" — see `demo/README.md` for the full story and
  `demo/generate_training_days.py` to regenerate the files. While any
  demo/training-day events remain in the database, the header status
  strip on every page shows a **"DEMO-LÄGE AKTIVT"** badge, as a reminder
  that what's on screen (including the threat level) isn't real
  operational data. The same "Demo och övning" tab has a **"Rensa
  demohändelser"** button (confirmed before it takes effect) that removes
  the events/messages/attachments tagged as coming from that scenario,
  *and* the demo-seeded 2.Kompani/3.Kompani status reports the same import
  creates (both header badge and Sammanställd hotbedömning's adjacent-unit
  card used to keep showing stale demo status otherwise) — every other
  stored report, the unit name, the adjacent-unit roster, and any
  *genuinely received* adjacent-unit status report are left alone (a demo
  one is only ever distinguished by a negative `signal_timestamp`, never
  by unit name, so a real unit happening to also be called "2.Kompani" is
  never at risk), unlike the blunter "Rensa händelselogg"/"Rensa allt"
  resets on Inställningar.

  An "Inkludera sensorhändelser" checkbox on the same tab controls
  whether each day's import also brings in that day's three automated
  sensor-trigger reports — one tripwire, one motion detector, one
  camera — reported by a "Sensorgateway" account rather than a guard, in
  the same 7S format as everything else but deliberately bare-bones:
  Slag/Symbol/Sedan are all left blank and Sysselsättning is always the
  same generic "Sensor aktiverad" line (an automated gateway integration
  just says "something happened at place X," not prose about what/why)
  — the place name itself says which sensor type triggered ("Trådlarm
  vid...", "Rörelsedetektor vid...", "Kamera vid..."). The one exception
  is the camera: its capture cycles through
  car/person/deer and comes with a matching cartoon-style photo attached
  instead, same mechanism as the human-reported signal events above, so
  what it saw only ever shows up as the picture, never as text. This is
  a UI-only toggle (session-remembered, not a file), independent of the
  human-report story, which is identical either way — see
  `demo/generate_training_days.py`'s `generate_sensor_day` and
  `dag_NN_sensor.txt` for the generated files.

**Inställningar is organized into expandable sections** — "Enhet" (unit
name + adjacent-unit roster), "Signalgrupper", "Kartinställningar"
(Kartläge/Kartkälla/Kartcentrum/Kartleverantör together), "AI-
inställningar" (Ollama port), and "Lokala användare" (guest accounts +
the LAN QR code) — each collapsed by default, with "Hoppa till" jump
links at the top of the page. "Rensa allt" and "Rensa händelselogg" stay
outside any section, always visible at the top, since they're
destructive actions worth seeing without expanding anything.

**Set the unit name** (web UI, "Inställningar" page) — used together with a
freshly generated TNR (a Day-Hour-Minute date-time-group, e.g. `301842`)
and the report type in the filename of every generated report, e.g.
`Kompani1_301842_hotbedomning.pdf`. Set once via the web UI; the CLI reads
the same value from the local database, so `signal-events report`/
`summary` filenames match whatever's configured there. Defaults to
`enhet` if never set. This also names the file attached when sending a
report to Signal, so the recipient sees a meaningful filename instead of
a random one.

**Generate a report** (fully offline):

```bash
python -m signal_events report --since 7d --format pdf --output report.pdf
```

Or generate from the web UI's "Generate report" page, which downloads the
file directly. `--since` accepts `24h`, `7d`, `30d`, or `all`. `--format`
accepts `pdf`, `markdown`, or `text` (a plain-text rendering with no
Markdown syntax — the same content, for reading in a basic text viewer
or pasting into a plain email). By default only reviewed events are
included; pass `--include-unreviewed` to override.

Before rendering, report generation also runs a best-effort **trivial
filter** (`signal_events/triviality.py`): the same kind of offline
keyword heuristics used elsewhere in this app, looking for routine,
non-notable content (wildlife, weather notes, "nothing to report" on a
patrol) among the events about to be included. Anything it recognizes is
marked `is_trivial` in the database — same effect as ticking the
"Trivial" checkbox by hand — and left out of that report, as well as out
of the "Sammanställd hotbedömning" threat analysis (see below): a
routine wildlife sighting shouldn't move the needle on the threat level
any more than it does on the report text. This is decision support, not
a verdict: check `data/events.db` or the events list if you want to
confirm what got filtered. Once a human reviews an event (saves the
review form at all, whichever way the "Trivial" checkbox ends up), that
judgment is final — the filter never overrides it on a later report or
assessment, whether the event was reviewed and left non-trivial, or was
auto-flagged trivial and then manually corrected back to normal.

**Generate a consolidated threat-level summary** (fully offline):

```bash
python -m signal_events summary --since 7d --format pdf --output summary.pdf
```

Or use the web UI's "Sammanställd hotbedömning" page. This looks across
all events in the period for the same vehicle (matched by registration
number) or the same person/object (matched by description similarity)
showing up more than once, flags observations with surveillance-like
language ("photographing", "returns", "loiters", etc.), and produces a
green/yellow/red recommendation with the full reasoning listed underneath
— every score component names the group and evidence behind it. The page
remembers whichever time period you last viewed (per browser session),
so navigating to another tab and back shows the same period again
instead of resetting to the "7 dagar" default — picking a different
period always updates what's remembered.

Alongside the recurring vehicle/person groups, **"Övriga
anmärkningsvärda observationer"** also surfaces individually notable
events that don't recur — a threat of violence, an armed person, a
suspected explosive, or a sabotage sign mentioned just once — each
linked directly to its event, rather than only being tallied as a count
in the reasoning text above. An event already shown via a recurring
group isn't listed twice here.

**RED is reserved for *recurring* threats of violent action**, not
pattern volume and not a single one-off report: it only triggers on 2+
reports in the same period showing sightings of armed individuals,
discovery of explosive devices, or signs of attempted sabotage (checked
across *all* events, not just recurring vehicle/person groups, since
these are about content, not correlation). A single report in any of
those categories, or a recurring vehicle/person no matter how
suspicious-looking, caps out at YELLOW — one credible-but-unrepeated
report is worth flagging, and recurrence of an ordinary pattern is worth
attention, but neither alone is a confirmed *pattern* of violent intent.
This is rule-based decision support (no ML), not a verdict: always check
the underlying events before acting on it. `SIGNAL_EVENTS_SITE_NAME` sets
the site name shown in the report heading (defaults to "skyddsobjektet").
`--format` accepts `pdf`, `markdown`, or `text`, same as `report` above.

Before the analysis runs, it also excludes **duplicate reports**
(`signal_events/duplicates.py`) and **trivial/routine events** (the same
trivial filter described above, run live against the period's events
rather than only at report-generation time): two events with the same
place and object, near-identical wording, and logged close together in
time are treated as one incident described twice rather than a real
second occurrence — this is deliberately distinct from *recurrence* (the
same vehicle or person showing up again over time), which is exactly
what the pattern-matching above is for. Anything recognized this way is
marked `is_duplicate`/`is_trivial` in the database (shown as "dublett"/
"trivial" badges on the events list and the event page) and left out of
the summary's event count and groups; events aren't removed, so they
still show up in the events list and can be corrected or deleted by hand
if the automatic call was wrong. Automated sensor-trigger events
(`SIGNAL_EVENTS_SENSOR_GROUP`, described above) are never evaluated for
duplication at all — a sensor is *expected* to fire the same templated
message at the same place repeatedly, and each trigger is a genuine,
separate occurrence, not a person accidentally filing the same report
twice.

A false positive can always be corrected by hand: the event's own page
has a **"Dublett"** checkbox, the same "Trivial" checkbox's pattern —
tick or untick it and save, and `is_duplicate_reviewed` locks that
judgment in permanently, in whichever direction, so the automatic
classifier never overrides it on a later report generation.

**Manuell justering av hotnivå** — the "Sammanställd hotbedömning" page
has a "Manuell justering av hotnivå" card to correct or override the
automatic level when a human's judgment differs from it. Saving one
doesn't discard the automatic reasoning: it's layered on top
(`analysis.apply_threat_override`), so the badge and the "Motivering"
list still show the full rule-based assessment and its evidence,
prefixed with a note naming the manual level, the automatic level, and
any note you added. A single current override applies everywhere — the
summary page, its downloads/sends, the CLI's `summary --llm`, and the
header status strip on every page — until you click "Återgå till
automatisk bedömning" to clear it. It persists in the local database
across restarts, the same way the unit name does.

**Logg över hotbedömningar** ("Sammanställd hotbedömning" page → "Visa
logg över tidigare hotbedömningar") records every time a threat-level
summary is actually produced — downloaded (any format) or sent to
Signal, from the CLI or the web UI — with its timestamp, level, score,
period, and how it was generated, in time order (newest first). Just
viewing the summary page doesn't add an entry; only generating an
artifact does. Each entry is identified as **"TNR Enhetsnamn"** (e.g.
`262020 Kompani 1`) — the same TNR that's actually in the downloaded or
sent file's own name, generated once per request and reused for both,
so the log entry and the artifact always match.

**Events are identified by TNR, not database id** — "Händelse 221430"
instead of "Händelse #42", wherever an event is referenced (its own
page, and every report/summary listing). Uses the event's own
`event_time` directly when that's already in valid TNR (DDHHMM) format,
as it is for a 7S report's "Stund" field; otherwise derives one from
when the event was recorded. Like report-file TNRs, this isn't
guaranteed unique (no month/year in DDHHMM) — it's a display label, not
a primary key; the underlying link still uses the real database id.
The "Händelser" list is sorted by this same TNR (newest first), not by
when a report was logged into the database — so a sensor event (or any
report with an accurate Stund) lines up by when it actually happened
even if it was ingested out of order relative to other reports.

**Optional: AI narrative via a local LLM.** The summary can also include a
prose write-up generated by a locally running [Ollama](https://ollama.com)
server — no internet involved, since Ollama serves the model from disk over
localhost. The green/yellow/red level and the evidence stay exactly as
computed by the rule-based engine; the model is only asked to turn that
into readable text, and is explicitly instructed not to invent facts or
change the verdict.

```bash
ollama serve                      # if not already running
ollama pull llama3.1               # one-time download, tags as llama3.1:latest
python -m signal_events summary --since 7d --llm --format pdf --output summary.pdf
```

**Snabbsökning: plain and near-match search.** The AI-analys tab also
has a quick search box above the chat, unrelated to the LLM — a plain
SQL `LIKE` lookup across plats/objekt/aktivitet/kännetecken/rapporterad
av/nästa steg/originaltext, instant and exact, for finding a known
registration number or keyword. "Inkludera nära träffar" adds a second,
still fully offline pass using `rapidfuzz` (character-level similarity,
not the LLM) to also surface near-misses the exact search wouldn't —
a typo'd or OCR'd plate, a misspelled place name — ranked by how close
the match is, and excluding whatever the exact search already found so
it's a genuinely additional list. It's deliberately not routed through
Ollama: an LLM call here would cost 30-190+ seconds per search (see
`OLLAMA_TIMEOUT_SECONDS` below) for something that should feel
instant, and risks a hallucinated "match" for something security-
relevant like a vehicle plate.

**AI-analys tab: chat with your own data.** Separate from the CLI's
one-shot `--narrative` above, the web UI's "AI-analys" tab (its own nav
item) is a chat-bot backed by the same local Ollama model. Ask it things
like "har vi sett den här bilen förut?" or "hur har hotnivån utvecklats
den senaste månaden?" and it answers grounded in three kinds of stored
data, rebuilt fresh from the database on every turn so it's never stale:

- this unit's own saved event reports,
- this unit's own threat-level assessment history (both the current one
  and everything logged before it — see "Logg" on the summary page),
- reports received from adjacent units, current and older (not just each
  unit's latest status).

Like the narrative feature, the model never decides or overrides the
threat level — the system prompt explicitly limits it to reasoning about
data it's been given and forbids inventing facts. Conversation history is
kept in your browser session (a "Rensa konversation" button resets it).
Same config env vars as above: `SIGNAL_EVENTS_OLLAMA_URL`,
`SIGNAL_EVENTS_OLLAMA_MODEL`.

Asking a question is two requests, not one: the question is saved to
the session immediately, and a separate request (auto-submitted by the
page) actually calls the model, which — being a local 8B model with a
sizeable underlag — can take anywhere from a few seconds to a couple of
minutes. This split exists specifically so navigating to another tab
mid-answer can never lose the question itself: only the reply might
still be pending, and coming back to AI-analys resumes waiting for it
automatically. If Ollama isn't running or the model tag isn't pulled,
the question stays visible with a "Försök igen" (retry) button rather
than auto-retrying against a server that isn't there.

Every Ollama call explicitly sets `num_ctx` (`SIGNAL_EVENTS_OLLAMA_NUM_CTX`,
default 24576) — without this, Ollama silently falls back to its own much
smaller default context window and truncates the underlag before the model
ever sees it, which looks exactly like "the AI can't read all the events"
even though the data is really there. 24576 was verified against a real
~300-event/~80-adjacent-report log; if your own log grows well past that,
raise it further (each older event pushed out of the cap is still disclosed
via a "N av totalt M" note in what's sent to the model, so it's a visible
cutoff, not a silent one). A bigger context window costs real processing
time on CPU-only/modest hardware, which is also why
`SIGNAL_EVENTS_OLLAMA_TIMEOUT` (`OLLAMA_TIMEOUT_SECONDS`) defaults to 300
seconds rather than a shorter value — lower it if your hardware is fast
enough that you'd rather fail fast than wait.

Two other things fed into the chat looking like it "can't find" events,
neither of which was actually a context-window problem: the per-event
underlag was missing `reported_by`/`next_steps` entirely, so a question
like "what has Vakt Berg reported" was unanswerable no matter how much
context the model got — that's fixed, both fields are now in every event
line. And the threat-assessment log's "how many events this past summary
covered" was worded ambiguously enough to get read as the *total* event
count, producing confidently wrong made-up totals — the underlag now
states the real totals explicitly, once, up front, so the model reads a
number instead of trying to count or guess. Whether an event has a photo
attached is also stated explicitly per event and as an overall total
(`bifogat foto=ja/nej`, plus a "totalt antal ... med bifogat foto" line),
for the same reason a plain "reported_by"-shaped gap once made "what has
Vakt Berg reported" unanswerable.

That top-of-context total still wasn't enough on its own once the
underlag reached this unit's real size (~300 events): a stated fact near
the top got answered as "none" purely because it was too far from the
question by the time the model got there, even though a handful-of-events
version of the exact same context answered correctly every time. Local
models attend far more reliably to the end of a long prompt than to
something stated once, however clearly, hundreds of lines back — so the
same core totals are now also repeated verbatim in a short "Sammanfattning"
section at the very end of the underlag, right before the question. This
fixed the wrong-count failure in practice, though which *section* a fact
came from can still get mislabeled occasionally. Small local models also
still transpose the odd TNR digit or mix up two very similar routine
reports when the log is highly repetitive — that residual imprecision is
why the "kontrollera alltid mot originalhändelserna" disclaimer on the tab
isn't just boilerplate.

**Changing the Ollama port.** If `ollama serve` runs on a non-default
port, set it once on Inställningar under "Ollama-port" instead of an env
var — it takes priority over whatever port is in `SIGNAL_EVENTS_OLLAMA_URL`
(only the port is overridden; the scheme/host still come from that env
var) and applies immediately to both the AI-analys chat and the CLI's
`--llm` flag, no restart needed.

**Send a report or summary to Signal.** Both the report page and the
summary page have a "Skicka till Signal" button (needs network + a linked
signal-cli account) that generates the PDF and sends it, with a short
caption, as an attachment to a Signal group — by default
`SIGNAL_EVENTS_REPORT_GROUP` (`"Stabsassistent test-rapport"`). This is
the same group `watch` polls for adjacent units' status reports (see
above) — it's a two-way channel: this unit sends its own reports here,
and reads everyone else's from the same place. If sending fails (not
linked, group not found, no network), it's reported inline on the page
and nothing else is affected — the underlying PDF generation and
download options keep working.

The "Personer, fordon och objekt" page has its own **"Skicka
bevakningslista"** button — see "Bevakningslista (watchlist)" above —
sent to its own group, separate from both the incident-intake group
(`watch`) and the report/adjacent-status group above.

## Message format expectations

Senders write free text, e.g.:

> At 14:30 near the old bridge, 3 trucks seen parked, camo painted, no
> plates visible. Recommend continued monitoring.

The parser looks for time expressions, "at/near/in <place>" phrases,
"<number> <noun>" patterns, activity/marks/next-step trigger words, etc.
It will often miss or misattribute fields on unusual phrasing — that's
expected and why the review step exists. If your reporters can use a
consistent phrasing style (mentioning time, place, and counts explicitly),
extraction quality improves a lot.

**The Swedish military "7S rapport" template is recognized directly** and
preferred over the generic heuristics above whenever a message has labeled
lines — `Till`/`Från`/`TNR`/`Stund`/`Ställe`/`Styrka`/`Slag`/
`Sysselsättning`/`Symbol`/`Reg.Nr`/`Sagesman`/`Sedan` — since the labels
already say exactly which field is which (`parser._map_7s_fields`). This
has been checked against real reports generated by the third-party tool
[7srapport.com](https://7srapport.com/) (not affiliated with this
project), which combines Styrka/Slag/Sysselsättning into one free-text
`Händelse:` field instead of three separate lines, and writes a lone `-`
for its blank optional fields (e.g. `Sedan: -`) rather than omitting the
line — both are handled: `Händelse` falls back to the same best-effort
count/object/activity heuristics as the plain free-text path, and a lone
`-` is treated as no value at all. `Sagesman` (who actually observed and
is vouching for the report) is what becomes "Rapporterad av", preferred
over `Från` (who relayed the message) and over the Signal sender, since
those two can genuinely differ from who's actually reporting.

## Data layout

```
data/
  events.db              SQLite database (messages, attachments, events,
                          entities/entity_event_links for Personer, fordon
                          och objekt)
  attachments/<msg>/     copied image files from Signal messages
  attachments/entities/<id>/  uploaded reference photos for a person/
                          vehicle/object record
  reports/                archived copy of every generated report (see
                          "Rapportmapp" above) — override with
                          SIGNAL_EVENTS_REPORTS_DIR or on Inställningar
```

Back this directory up like any other sensitive local data store — it's
plain SQLite plus image files, no encryption is applied by this tool beyond
whatever your disk/filesystem provides.

## Running tests

```bash
pytest
```

## Notes and limitations

- The field parser is intentionally simple (regex/keyword heuristics) so it
  needs no network, no ML runtime, and no external services — it will not
  match the accuracy of an LLM-based extractor, by design. Always review
  before reporting.
- `signal-cli` must remain linked/registered; if the link is revoked from
  the phone, `sync`/`watch`/`serve --watch` will start failing and you'll
  need to re-link. You don't have to watch the terminal to notice: the
  header status strip shows a red "Mottagning misslyckas" badge (hover
  it for the actual error) and stops updating "Senast mottagning från
  Signal", and Systemlogg (admin-only) records when the failures started
  and, once you've re-linked, when they recovered.
- **Newly added group members.** `receive`/`send` pass
  `--trust-new-identities always` to signal-cli, so a message from someone
  it hasn't seen before (a just-added group member, or someone who
  reinstalled Signal) doesn't silently fail to decrypt and vanish — this
  app has no identity-trust logic of its own, so without that flag those
  messages never even reach it. The tradeoff: it also auto-trusts a
  changed identity key without asking, which is the same warning Signal
  normally shows if an account is compromised or re-registered elsewhere.
  If you'd rather keep that check, remove the flag in `signal_client.py`
  and instead run `signal-cli -u "$SIGNAL_EVENTS_PHONE_NUMBER" trust -a
  <number>` by hand whenever a new sender's messages don't come through.
- The only messages this tool sends are the explicit "Skicka till Signal"
  report/summary sends, triggered by a deliberate click in the web UI —
  it never sends anything on its own (`watch`/`sync` only read). The web
  UI binds to `127.0.0.1` by default so it isn't exposed on your network.
- Map tiles default to "Online" mode (fetched live from Inställningar's
  configured provider as needed, cached to `data/tiles/` as a side
  effect) — set a tile provider token/key there before either map (the
  "Kart-vy" view, or the map on an event's own page) will show real
  imagery. "Lokal cache" mode and the "Ladda ner kartor för området"
  bulk download remain available for a fully offline deployment. See
  "Kart-vy" under Day-to-day usage. The `mgrs`/`certifi` PyPI packages this
  depends on (see requirements.txt) are themselves offline once
  installed — `certifi` in particular is there so tile requests work out
  of the box even on a fresh python.org macOS install, which otherwise
  ships without a populated CA certificate bundle and would fail every
  HTTPS request with a certificate-verify error until you separately run
  its "Install Certificates.command".

## License

MIT — see [LICENSE](LICENSE).
