"""Downloads and caches raster map tile images for the area around the map
center point configured on Inställningar, so viewing the map in the web UI
(an event's own page, or Karta) needs no network at all once the cache is
filled -- only this one deliberate action does, same as `sync`/`watch`
being the only network-touching pieces of the rest of the app. See
config.py for the cached radius/zoom range and db.py for where the center
point and the tile provider URL are stored.

The tile *source* is a plain URL template with {z}/{x}/{y} placeholders,
configured on Inställningar (db.get_map_tile_url_template) rather than
hardcoded -- the public OpenStreetMap tile server (the fallback default,
config.DEFAULT_TILE_URL_TEMPLATE) actively blocks the kind of bulk
download this app needs (confirmed by hitting that block during
development, see _BLOCKED_HEADER below), so real use is expected to point
this at a provider whose terms actually permit caching for offline use
(e.g. MapTiler, Stadia Maps, Thunderforest -- paste their tile URL,
including your API key, directly into Inställningar).

Tile math here uses the standard slippy-map (Web Mercator) tile scheme --
https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames -- and the
bounding box around the center point is a simple equirectangular
approximation, not a true geodesic circle; more than accurate enough for
picking which 256x256 tiles to fetch."""

from __future__ import annotations

import hashlib
import json
import math
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import certifi

# Identifies this app to the tile server, and paces requests -- both
# expected of any bulk consumer of the public OSM tile server. See
# https://operations.osmfoundation.org/policies/tiles/
_USER_AGENT = "signal-events/1.0 (local offline incident-reporting tool)"
_REQUEST_DELAY_SECONDS = 0.2

# Only used when no tile_url_template is passed in explicitly (direct
# calls, tests) -- routes.py always passes the Inställningar-configured
# one (db.get_map_tile_url_template), which itself falls back to
# config.DEFAULT_TILE_URL_TEMPLATE when nothing's been set.
_DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"

# This check is specifically about the public OSM tile server's own abuse
# detection, confirmed by hitting it during development: once it decides a
# client is bulk-downloading (which a ~50 km/zoom 8-14 area unavoidably
# looks like -- around 8800 sequential requests), it does NOT reply with
# an HTTP error status. It replies 200 OK, with a genuine ~7 KB PNG image
# (rendered to look like a normal tile, so it's visible in whatever map
# viewer requested it) whose pixels spell out "403 Access blocked" -- but
# the one reliable, documented signal is the `X-Blocked` response header
# pointing at the policy page. Without checking for it, every request
# after the block kicks in "succeeds" and gets cached as if it were real
# map imagery, silently poisoning the entire cache. Harmless to check for
# against any other provider -- it just won't ever be present. See
# "Recognizing a block" in https://operations.osmfoundation.org/policies/tiles/
_BLOCKED_HEADER = "X-Blocked"
# Belt-and-braces fallback in case the header is ever dropped but the same
# notice image keeps being served: the exact SHA-256 of that image, so an
# already-cached poisoned tile can be found and purged after the fact (see
# purge_blocked_tiles) even without the header being available anymore.
_BLOCKED_TILE_SHA256 = "b02c44252dac5a5e820ecef1e9bf9200e9407c042df668a466a1aa81a9ecca7a"

# WSO2 API Manager (the platform behind Lantmäteriet's API-portalen, and
# potentially other providers built on it) signals a throttle/abuse block
# with an HTTP error response whose JSON body carries one of a small set
# of documented error codes -- confirmed by hitting one live during
# development: "Online" mode's ordinary map browsing (Leaflet requesting
# many tiles at once) tripped a free-tier limit, and every further tile
# request got a 503 with body
# {"code":"700700","type":"API blocked","description":"..."}
# instead of real tile data. Like OSM's block, every remaining tile in a
# bulk download would fail the exact same way, so this should abort the
# whole run immediately rather than grinding through the rest for
# nothing. 900800 ("Message throttled out") is WSO2's standard
# rate-limit-exceeded code, included on the same reasoning even though it
# hasn't been hit directly here.
_WSO2_BLOCKED_CODES = {"700700", "900800"}


def _is_wso2_blocked_response(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (ValueError, TypeError):
        return False
    return isinstance(payload, dict) and str(payload.get("code")) in _WSO2_BLOCKED_CODES


class TileBlockedError(RuntimeError):
    """Raised when the tile server's response indicates this client has
    been blocked for bulk/abusive use -- see _BLOCKED_HEADER above.
    Callers should stop the whole download rather than retrying, since
    every subsequent request will be blocked the same way."""

# Built from certifi's bundled CA set rather than relying on the system/
# framework Python's own trust store -- notably, a python.org macOS install
# ships with an *empty* cert.pem until the user manually runs its "Install
# Certificates.command", which would otherwise make every tile request fail
# with a certificate-verify error on a plain fresh install. certifi sidesteps
# that entirely.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def latlon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, x)), max(0, min(n - 1, y))


def _tile_bounds(center_lat: float, center_lon: float, radius_km: float, zoom: int) -> tuple[int, int, int, int]:
    """(x_min, x_max, y_min, y_max) of the tiles covering a square around
    the center point, radius_km on every side."""
    lat_delta = radius_km / 111.0
    lon_delta = radius_km / (111.0 * math.cos(math.radians(center_lat)) or 1.0)
    x1, y1 = latlon_to_tile(center_lat - lat_delta, center_lon - lon_delta, zoom)
    x2, y2 = latlon_to_tile(center_lat + lat_delta, center_lon + lon_delta, zoom)
    return min(x1, x2), max(x1, x2), min(y1, y2), max(y1, y2)


def point_in_cached_area(
    lat: float, lon: float, center_lat: float, center_lon: float, radius_km: float
) -> bool:
    """Whether (lat, lon) falls inside the square area expected_tile_count
    and download_area operate on -- an approximate equirectangular check,
    fine at this radius/latitude range, not for precise geodesy."""
    lat_delta_km = abs(lat - center_lat) * 111.0
    lon_delta_km = abs(lon - center_lon) * 111.0 * (math.cos(math.radians(center_lat)) or 1.0)
    return lat_delta_km <= radius_km and lon_delta_km <= radius_km


def expected_tile_count(
    center_lat: float, center_lon: float, radius_km: float, min_zoom: int, max_zoom: int
) -> int:
    total = 0
    for zoom in range(min_zoom, max_zoom + 1):
        x_min, x_max, y_min, y_max = _tile_bounds(center_lat, center_lon, radius_km, zoom)
        total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total


def cached_tile_count(cache_dir: Path) -> int:
    if not cache_dir.exists():
        return 0
    return sum(1 for _ in cache_dir.rglob("*.png"))


def cached_tile_count_for_area(
    cache_dir: Path, center_lat: float, center_lon: float, radius_km: float,
    min_zoom: int, max_zoom: int,
) -> int:
    """Like cached_tile_count, but counts only tiles inside the currently
    configured center/radius/zoom range instead of every file under
    cache_dir -- leftover tiles from a previous Kartcentrum or area-size
    setting stay on disk (nothing purges them) and would otherwise inflate
    the count past expected_tile_count's total for the same area."""
    count = 0
    for zoom in range(min_zoom, max_zoom + 1):
        x_min, x_max, y_min, y_max = _tile_bounds(center_lat, center_lon, radius_km, zoom)
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                if tile_path(cache_dir, zoom, x, y).exists():
                    count += 1
    return count


def tile_path(cache_dir: Path, zoom: int, x: int, y: int) -> Path:
    return cache_dir / str(zoom) / str(x) / f"{y}.png"


def download_area(
    center_lat: float,
    center_lon: float,
    radius_km: float,
    min_zoom: int,
    max_zoom: int,
    cache_dir: Path,
    tile_url_template: str = _DEFAULT_TILE_URL,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int, int, bool]:
    """Downloads every tile covering the area into cache_dir, skipping
    ones already cached -- safe to re-run to fill in gaps after a
    previous run failed partway, or to refresh only the tiles missing so
    far. Returns (downloaded, skipped, failed, blocked).

    `tile_url_template` is a URL with {z}/{x}/{y} placeholders -- pass the
    Inställningar-configured one (db.get_map_tile_url_template) to use
    whatever provider the user has set up, rather than the public OSM
    tile server this defaults to.

    A single tile failing for an ordinary reason (network hiccup, a 404
    for a genuinely tile-less area, ...) doesn't abort the rest of the
    area -- but a TileBlockedError does: once the server has decided to
    block this client, every remaining request would be blocked too, so
    continuing would just be pointless additional load against someone
    else's infrastructure. `blocked` is True whenever that happened, so
    callers can tell "ran out of tiles" apart from "got cut off partway"
    (this check only ever fires against the public OSM tile server --
    see _BLOCKED_HEADER)."""
    downloaded = 0
    skipped = 0
    failed = 0
    total = expected_tile_count(center_lat, center_lon, radius_km, min_zoom, max_zoom)
    done = 0
    for zoom in range(min_zoom, max_zoom + 1):
        x_min, x_max, y_min, y_max = _tile_bounds(center_lat, center_lon, radius_km, zoom)
        for x in range(x_min, x_max + 1):
            for y in range(y_min, y_max + 1):
                done += 1
                dest = tile_path(cache_dir, zoom, x, y)
                if dest.exists():
                    skipped += 1
                else:
                    try:
                        _fetch_tile(zoom, x, y, dest, tile_url_template)
                        downloaded += 1
                        time.sleep(_REQUEST_DELAY_SECONDS)
                    except TileBlockedError:
                        return downloaded, skipped, failed, True
                    except Exception:
                        failed += 1
                if on_progress is not None:
                    on_progress(done, total)
    return downloaded, skipped, failed, False


def _fetch_tile(zoom: int, x: int, y: int, dest: Path, tile_url_template: str = _DEFAULT_TILE_URL) -> None:
    url = tile_url_template.format(z=zoom, x=x, y=y)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=10, context=_SSL_CONTEXT) as response:
            if response.headers.get(_BLOCKED_HEADER) is not None:
                raise TileBlockedError(response.headers.get(_BLOCKED_HEADER))
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        if _is_wso2_blocked_response(body):
            raise TileBlockedError(body.decode("utf-8", errors="replace")) from exc
        raise
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def fetch_tile_on_demand(
    zoom: int, x: int, y: int, cache_dir: Path, tile_url_template: str = _DEFAULT_TILE_URL,
) -> Optional[bytes]:
    """Serves a single tile for "online" map mode: returns it from
    cache_dir if already downloaded, otherwise fetches just this one tile
    live from tile_url_template, caches it for next time, and returns it.
    Returns None if the tile can't be had at all (network error, or the
    provider blocking this client -- see TileBlockedError) so the caller
    can fall back to a blank placeholder instead of a broken image.

    This is deliberately per-tile rather than routing through
    download_area: a map view only ever needs the handful of tiles
    currently on screen, not the whole cached area, and caching each one
    as it's fetched means panning back over the same spot later is
    free -- the same tiles feed the "local" cache too, so pointing a
    deployment at real connectivity for a while and then switching to
    local mode still benefits from whatever got viewed online."""
    dest = tile_path(cache_dir, zoom, x, y)
    if dest.exists():
        return dest.read_bytes()
    try:
        _fetch_tile(zoom, x, y, dest, tile_url_template)
    except Exception:
        return None
    return dest.read_bytes()


def purge_blocked_tiles(cache_dir: Path) -> int:
    """Removes any already-cached tile that's actually the "you've been
    blocked" notice image (see _BLOCKED_TILE_SHA256) rather than real map
    imagery -- a one-time cleanup for a cache that was poisoned by a run
    that predates the _BLOCKED_HEADER check above. Safe to run any time;
    returns the number of files removed."""
    if not cache_dir.exists():
        return 0
    removed = 0
    for path in cache_dir.rglob("*.png"):
        if hashlib.sha256(path.read_bytes()).hexdigest() == _BLOCKED_TILE_SHA256:
            path.unlink()
            removed += 1
    return removed
