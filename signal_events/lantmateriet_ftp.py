"""Bulk tile extraction from Lantmäteriet's free, open FTP file distribution
(as opposed to their throttled WMTS API -- see tiles.py/db.get_map_tile_url_template
for that path). Confirmed by hand:

- ftp://download-opendata.lantmateriet.se/Topografisk_webbkarta_raster/ hosts
  a single GeoPackage per style/projection covering all of Sweden (the
  "Farg_05m_mercator" one used here is the coloured Web Mercator variant),
  requiring no account, API key, or Geotorget order at all -- just a plain
  anonymous FTP file.
- That file is enormous (~145 GB) -- not something to download whole -- but
  it's a real GeoPackage (OGC tile-pyramid raster, same 256x256 Web Mercator
  z/x/y scheme this app already uses everywhere else), and the FTP server
  honours byte-range requests. GDAL's /vsicurl/ virtual filesystem can open
  it remotely and read only the tiles that intersect a given area, via
  gdal_translate -projwin, without ever pulling the whole file.
- Measured throughput doing this: ~1.85 s/tile when batched one
  gdal_translate call per zoom level (vs. ~19 s/tile calling it once per
  tile -- FTP's per-request connection overhead dominates, so always batch).
  For this app's usual 50 km/zoom 8-14 area (~10,000 tiles) that's several
  hours -- genuinely slower than either provider's WMTS API when it isn't
  throttled, but with zero ongoing API dependency once it's done. Meant to
  be run once, unattended, in the background -- see
  routes.py's download_map_tiles.

Because per-tile latency here is much too slow for live "Online" mode
browsing, this is bulk-download-only: routes.py always serves this source's
tiles from the local cache, the same as "Lokal cache" mode, regardless of
the Inställningar tile mode setting.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import tiles

_TILE_SIZE = 256
_MERCATOR_ORIGIN = 20037508.342789244

# The coloured, Web Mercator variant -- see this module's docstring. Only
# one file exists per style/projection (not split by region), so there's
# nothing to configure here; if Lantmäteriet ever renames/moves it, this is
# the one constant that needs updating.
DEFAULT_GEOPACKAGE_PATH = (
    "/vsicurl/ftp://download-opendata.lantmateriet.se/"
    "Topografisk_webbkarta_raster/Farg_05m_mercator/1159000_7377433.gpkg"
)


class GdalNotAvailableError(RuntimeError):
    """Raised when gdal_translate isn't on PATH -- this extraction path
    needs the GDAL command-line tools installed separately (e.g. `brew
    install gdal`), unlike the rest of this app which is pure Python."""


def gdal_available() -> bool:
    return shutil.which("gdal_translate") is not None


def _tile_to_meters(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Top-left corner of tile (x, y) at `zoom`, in EPSG:3857 metres --
    the inverse of tiles.latlon_to_tile's Web Mercator math."""
    tile_size_m = 2 * _MERCATOR_ORIGIN / (2 ** zoom)
    return x * tile_size_m - _MERCATOR_ORIGIN, _MERCATOR_ORIGIN - y * tile_size_m


def _extract_zoom_level(
    center_lat: float, center_lon: float, radius_km: float, zoom: int, cache_dir: Path,
    geopackage_path: str,
) -> int:
    """Extracts every tile for one zoom level in a single gdal_translate
    call (batching -- not one call per tile -- is what makes this remotely
    practical, see the module docstring), then slices the result into the
    individual z/x/y files tiles.tile_path expects. Returns how many tiles
    were written (0 if the whole zoom was already cached and skipped)."""
    x_min, x_max, y_min, y_max = tiles._tile_bounds(center_lat, center_lon, radius_km, zoom)
    if all(
        tiles.tile_path(cache_dir, zoom, x, y).exists()
        for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)
    ):
        return 0

    ulx, uly = _tile_to_meters(x_min, y_min, zoom)
    lrx, lry = _tile_to_meters(x_max + 1, y_max + 1, zoom)
    width = (x_max - x_min + 1) * _TILE_SIZE
    height = (y_max - y_min + 1) * _TILE_SIZE

    tmp_path = cache_dir / f"_lantmateriet_ftp_tmp_zoom{zoom}.png"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)
    try:
        try:
            # /vsicurl/ reads this batch's byte ranges from Lantmäteriet's
            # anonymous FTP server over the network -- with no timeout, a
            # single stalled connection hangs this call (and the download
            # thread holding _tile_download_lock, see routes.py) forever,
            # which is exactly what made a "stuck" download need a full
            # app restart to recover from. 5s/tile (~2.7x the ~1.85s/tile
            # this module's docstring documents for a real batched fetch)
            # leaves headroom for a genuinely slow-but-working transfer
            # while still bounding a truly dead connection to a finite wait.
            result = subprocess.run(
                [
                    "gdal_translate", "-q",
                    "-projwin", str(ulx), str(uly), str(lrx), str(lry),
                    "-outsize", str(width), str(height),
                    "-of", "PNG",
                    geopackage_path, str(tmp_path),
                ],
                capture_output=True, text=True, timeout=max(60, tile_count * 5),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"gdal_translate timed out for zoom {zoom}") from exc
        if result.returncode != 0:
            raise RuntimeError(f"gdal_translate failed for zoom {zoom}: {result.stderr.strip()}")

        written = 0
        with Image.open(tmp_path) as img:
            img.load()
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    col, row = x - x_min, y - y_min
                    box = (col * _TILE_SIZE, row * _TILE_SIZE, (col + 1) * _TILE_SIZE, (row + 1) * _TILE_SIZE)
                    dest = tiles.tile_path(cache_dir, zoom, x, y)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    img.crop(box).save(dest, format="PNG")
                    written += 1
        return written
    finally:
        tmp_path.unlink(missing_ok=True)


def extract_area_to_cache(
    center_lat: float,
    center_lon: float,
    radius_km: float,
    min_zoom: int,
    max_zoom: int,
    cache_dir: Path,
    geopackage_path: str = DEFAULT_GEOPACKAGE_PATH,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> tuple[int, int]:
    """Fills the local tile cache for the given area from Lantmäteriet's
    FTP-hosted GeoPackage, one zoom level at a time. Returns
    (tiles_written, zoom_levels_failed). A zoom level already fully cached
    is skipped entirely (idempotent/resumable, same spirit as
    tiles.download_area, just at zoom-level rather than per-tile
    granularity -- there's no way to resume a partially-fetched zoom level
    without re-running the whole thing, since each zoom is one big
    gdal_translate call). A single zoom level failing (e.g. a transient FTP
    hiccup) doesn't stop the rest -- unlike tiles.download_area's
    abort-on-block behaviour, there's no equivalent "you've been blocked"
    signal here, since this is a plain file server, not a rate-limited API."""
    if not gdal_available():
        raise GdalNotAvailableError(
            "gdal_translate not found on PATH -- install GDAL (e.g. `brew install gdal`) first."
        )

    written = 0
    failed_zooms = 0
    zooms = list(range(min_zoom, max_zoom + 1))
    for done, zoom in enumerate(zooms, start=1):
        try:
            written += _extract_zoom_level(center_lat, center_lon, radius_km, zoom, cache_dir, geopackage_path)
        except Exception:
            failed_zooms += 1
        if on_progress is not None:
            on_progress(done, len(zooms))
    return written, failed_zooms
