"""Runtime configuration, driven by environment variables so the tool needs
no network access and no external config service to run on a laptop."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# .resolve() on all three of these: a relative SIGNAL_EVENTS_DATA_DIR (or
# _DB_PATH/_ATTACHMENTS_DIR) would otherwise be stored as-is, relative to
# whatever the process's CWD happened to be when it started -- fine for
# writing files, but attachment_file's send_file(attachment["file_path"])
# resolves relative paths against the Flask app's root_path (the
# signal_events/webapp/ package directory), not the CWD, so a relative
# DATA_DIR silently 404s/500s on every attachment despite the file
# genuinely existing on disk.
DATA_DIR = Path(os.environ.get("SIGNAL_EVENTS_DATA_DIR", PROJECT_ROOT / "data")).resolve()

DB_PATH = Path(os.environ.get("SIGNAL_EVENTS_DB_PATH", DATA_DIR / "events.db")).resolve()
ATTACHMENTS_DIR = Path(
    os.environ.get("SIGNAL_EVENTS_ATTACHMENTS_DIR", DATA_DIR / "attachments")
).resolve()

# Where every generated report (hotbedömning, händelserapport,
# bevakningslista) is persisted to disk on top of the browser's own
# download, since this app runs on the user's own laptop -- a
# db.get_reports_dir() override set on Inställningar takes priority over
# this default, same layering as the map tile URL/mode settings.
REPORTS_DIR = Path(os.environ.get("SIGNAL_EVENTS_REPORTS_DIR", DATA_DIR / "reports")).resolve()

# Number linked/registered with signal-cli, e.g. "+15551234567". Only needed
# for `sync`, which is the one command that requires network access.
PHONE_NUMBER = os.environ.get("SIGNAL_EVENTS_PHONE_NUMBER")

SIGNAL_CLI_BIN = os.environ.get("SIGNAL_EVENTS_SIGNAL_CLI_BIN", "signal-cli")

# Where signal-cli itself stores downloaded attachments before we copy them
# into ATTACHMENTS_DIR.
SIGNAL_CLI_ATTACHMENTS_DIR = Path(
    os.environ.get(
        "SIGNAL_EVENTS_SIGNAL_CLI_ATTACHMENTS_DIR",
        Path.home() / ".local" / "share" / "signal-cli" / "attachments",
    )
)

WEB_HOST = os.environ.get("SIGNAL_EVENTS_WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.environ.get("SIGNAL_EVENTS_WEB_PORT", "5000"))

# Name of the site/facility these reports are about, used in the
# consolidated summary report's heading. Purely cosmetic.
SITE_NAME = os.environ.get("SIGNAL_EVENTS_SITE_NAME", "skyddsobjektet")

# Optional local LLM narrative for the summary report, via a locally running
# Ollama server (http://localhost:11434 by default) -- no internet involved,
# same as the rest of this tool, since Ollama serves the model from disk.
OLLAMA_URL = os.environ.get("SIGNAL_EVENTS_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("SIGNAL_EVENTS_OLLAMA_MODEL", "llama3.1:latest")

# Ollama's own default context window (num_ctx) is much smaller than what
# this model actually supports -- 2048/4096 tokens on a typical install --
# and silently truncates whatever doesn't fit, with no error raised. The
# AI-analys chat's underlag (saved events + report history, see
# webapp/routes._build_ai_context) regularly runs to 10-20k+ tokens on a
# real event log, so without an explicit override here Ollama would drop
# most of it before the model ever sees it -- the model would answer from
# a truncated fragment and look like it "can't see" older data, without
# any error being raised anywhere. Verified against a real 301-event log:
# 24576 was the smallest size that let the model correctly find and quote
# the actual oldest event rather than a truncated fragment. Llama 3.1
# itself supports up to 131072, but a bigger window costs real time on
# CPU-only/modest hardware -- see OLLAMA_TIMEOUT_SECONDS below.
OLLAMA_NUM_CTX = int(os.environ.get("SIGNAL_EVENTS_OLLAMA_NUM_CTX", "24576"))

# A full, uncached underlag at the context size above can legitimately take
# several minutes to process on CPU-only/modest hardware (measured on a
# real ~300-event log while diagnosing truncation) -- 120s was too short
# and would cut off a response that was still correctly on its way,
# reporting it as a failure. Lower this back down if your hardware is
# fast enough that you'd rather fail fast than wait.
OLLAMA_TIMEOUT_SECONDS = int(os.environ.get("SIGNAL_EVENTS_OLLAMA_TIMEOUT", "300"))

# Signal group `watch`/`serve --watch` poll incoming reports *from*.
WATCH_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_WATCH_GROUP", "Stabsassistent test-händelser"
)

# Signal group generated reports/summaries are sent *to* via the "Skicka
# till Signal" buttons in the web UI (and requires network + signal-cli,
# like `sync`/`watch`). Separate from WATCH_GROUP_NAME above -- input and
# output channels are deliberately different groups.
#
# This same group doubles as the exchange channel `watch`/`serve --watch`
# poll for status reports *from* adjacent units -- since everyone posts
# their own generated reports here, it's already the shared channel where
# other units' reports show up too. The sending unit is identified from
# the plain-text unit name embedded in the report's own filename (see
# naming.parse_report_filename), not from a separate sender mapping, and
# only real incoming messages are ever ingested (see ingest_adjacent_report
# in signal_client.py), so this unit's own sent reports are never picked
# back up as if they were an adjacent unit's.
REPORT_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_REPORT_GROUP", "Stabsassistent test-rapport"
)

# Signal group the recurring/suspicious vehicles-and-people list is sent to
# via the "Skicka lista över återkommande" button -- a focused watchlist,
# separate from the full report/summary distribution above.
RECURRING_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_RECURRING_GROUP", "Stabsassistent test-återkommande"
)

# Signal group automated sensor-trigger events are ingested from -- a
# dedicated sensor gateway account posts here, reporting in the same 7S
# format as human-submitted incident reports (see parser.parse_event_fields),
# so they're parsed and stored the exact same way, just from a separate
# group than WATCH_GROUP_NAME's human intake.
SENSOR_GROUP_NAME = os.environ.get(
    "SIGNAL_EVENTS_SENSOR_GROUP", "Stabsassistent test-sensorer"
)

# Fallback (lat, lon) used wherever a map needs a center point but
# Inställningar's Kartcentrum hasn't been set yet -- Stockholm Palace
# (Kungliga slottet), Gamla Stan (59°19'37"N 18°04'17"E). Only ever a
# starting point/placeholder view; Kartcentrum's own configured value
# always wins once one is set.
DEFAULT_MAP_CENTER = (59.326944, 18.071389)

# Where downloaded map tile images are cached on disk (see tiles.py) --
# once filled, viewing the map (event page or Karta) needs no network at
# all, matching the rest of this tool. Only the deliberate "Ladda ner
# kartor" action on Inställningar touches the network.
TILE_CACHE_DIR = Path(os.environ.get("SIGNAL_EVENTS_TILE_CACHE_DIR", DATA_DIR / "tiles")).resolve()

# Tile source used when Inställningar hasn't been given a provider URL of
# its own (see db.get_map_tile_url_template). Defaults to Lantmäteriet's
# free, open "topowebb-ccby" WMTS layer (https://www.lantmateriet.se/) --
# despite the "-ccby" in its name (a leftover from a past product rename),
# it's actually licensed CC0 per Lantmäteriet's own technical description,
# so commercial use and redistribution need no attribution at all -- the
# natural fit for a Swedish skyddsobjekt, and unlike the public OSM tile
# server (which actively blocks the kind of bulk/repeated download this
# app needs -- confirmed by hitting that block during development, see
# tiles.py) its terms actually permit this use.
#
# The DIN_TOKEN placeholder is not a working credential -- getting a real
# one currently means registering a free Geotorget account
# (geotorget.lantmateriet.se), applying as an NGP consumer, then
# generating an API key via the API-portalen using the "token in URL"
# REST method documented for this service (not the OAuth2/Bearer-header
# one, which issues short-lived tokens this app's static URL-template
# setting can't refresh on its own). That key must be pasted into
# Inställningar -> Kartleverantör (never baked into source code, since
# it's a personal credential). Until that's done, tile requests will
# simply fail (same visible effect -- blank map tiles -- as the OSM block
# did, just for an honest reason: nothing to guess or debug, just a key
# to fill in). Other CC0 layers work the same way, just swap "topowebb"
# for e.g. "topowebb_nedtonad" (muted colours, a good basemap under
# markers). Non-Swedish deployments should set their own provider on
# Inställningar instead -- MapTiler, Stadia Maps, and Thunderforest all
# offer a free tier permitting tile caching for offline use.
DEFAULT_TILE_URL_TEMPLATE = (
    "https://api.lantmateriet.se/open/topowebb-ccby/v1/wmts/token/DIN_TOKEN/"
    "1.0.0/topowebb/default/3857/{z}/{y}/{x}.png"
)

# Named area-size presets selectable on Inställningar (db.get_map_cache_area_size)
# -- radius (km) around the configured center point that gets cached, i.e.
# half the total cached square's side length ("large" -> a ~100 x 100 km
# area). "small" is the default so a first "Ladda ner kartor" finishes
# quickly -- larger areas take proportionally longer and download far more
# tiles, especially via the lantmateriet_ftp source's slow bulk extraction.
MAP_CACHE_AREA_SIZES = {
    "small": 0.5,    # ~1 x 1 km
    "medium": 5.0,   # ~10 x 10 km
    "large": 50.0,   # ~100 x 100 km
}
MAP_CACHE_DEFAULT_AREA_SIZE = "small"

# Zoom range cached for that radius. 14 is detailed enough to make out
# individual buildings/roads; going to 15 would roughly quadruple the tile
# count (see MAP_CACHE_MAX_TILE_COUNT) for the "large" 50 km radius, which
# stops being a reasonable one-off bulk fetch against the public OSM tile
# server. 8 is zoomed out enough to still show the whole cached area at
# once.
MAP_CACHE_MIN_ZOOM = 8
MAP_CACHE_MAX_ZOOM = 14

# Safety cap so a bug (or a center point that somehow produces a much
# larger bounding box) can't accidentally kick off an enormous, impolite
# bulk download against the public tile server. ~8800 tiles are expected
# for the defaults above; this leaves headroom without being unbounded.
MAP_CACHE_MAX_TILE_COUNT = 15000


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ATTACHMENTS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    TILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
