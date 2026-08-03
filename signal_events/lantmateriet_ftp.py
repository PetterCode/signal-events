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


# A zoom level's tiles are fetched in row-band sub-batches of roughly this
# many tiles each, rather than one gdal_translate call for the entire zoom.
# At the ~1.85 s/tile this module's docstring measures for a real batched
# fetch, that keeps a single call to a few minutes. Fetching a whole large
# zoom level (the biggest can be several thousand tiles) in one call meant
# the on-disk tile count -- which is also what Inställningar's "X av Y
# kartrutor cachade" progress readout reads -- sat completely still for as
# long as an hour at a stretch, indistinguishable from an actual hang to
# someone watching it; and restarting during that stretch (as a "why isn't
# this doing anything" reaction) threw away the whole zoom's progress
# instead of just the one band in flight. Banding also tightens the
# subprocess timeout below to match, so a truly dead connection is caught
# much sooner than waiting out a whole zoom's worst-case duration.
_TARGET_TILES_PER_BATCH = 250


def _extract_row_band(
    zoom: int, x_min: int, x_max: int, y_min: int, y_max: int, cache_dir: Path, geopackage_path: str,
) -> int:
    """Extracts one row-band of a zoom level in a single gdal_translate
    call, then slices the result into the individual z/x/y files
    tiles.tile_path expects. Returns how many tiles were written."""
    ulx, uly = _tile_to_meters(x_min, y_min, zoom)
    lrx, lry = _tile_to_meters(x_max + 1, y_max + 1, zoom)
    width = (x_max - x_min + 1) * _TILE_SIZE
    height = (y_max - y_min + 1) * _TILE_SIZE
    tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)

    tmp_path = cache_dir / f"_lantmateriet_ftp_tmp_zoom{zoom}_y{y_min}.png"
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
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
            raise RuntimeError(f"gdal_translate timed out for zoom {zoom}, rows {y_min}-{y_max}") from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"gdal_translate failed for zoom {zoom}, rows {y_min}-{y_max}: {result.stderr.strip()}"
            )

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


def _extract_zoom_level(
    center_lat: float, center_lon: float, radius_km: float, zoom: int, cache_dir: Path,
    geopackage_path: str,
) -> tuple[int, int]:
    """Extracts one zoom level's tiles in row-band sub-batches (see
    _TARGET_TILES_PER_BATCH) rather than one call for the whole zoom, so
    progress lands on disk continuously instead of appearing frozen for a
    long stretch and then jumping all at once. A band already fully cached
    (e.g. from an earlier run that got interrupted partway through this
    same zoom) is skipped -- finer-grained resumability than before, where
    a zoom was all-or-nothing since it was written in one piece. A band
    that fails (network hiccup, timeout) is skipped rather than aborting
    the rest of the zoom, the same "don't lose everything over one
    problem" principle already applied at the whole-run and per-tile
    layers. Returns (written, failed_bands)."""
    x_min, x_max, y_min, y_max = tiles._tile_bounds(center_lat, center_lon, radius_km, zoom)
    width = x_max - x_min + 1
    rows_per_batch = max(1, _TARGET_TILES_PER_BATCH // width)

    written = 0
    failed_bands = 0
    for band_y_min in range(y_min, y_max + 1, rows_per_batch):
        band_y_max = min(band_y_min + rows_per_batch - 1, y_max)
        if all(
            tiles.tile_path(cache_dir, zoom, x, y).exists()
            for x in range(x_min, x_max + 1) for y in range(band_y_min, band_y_max + 1)
        ):
            continue
        try:
            written += _extract_row_band(zoom, x_min, x_max, band_y_min, band_y_max, cache_dir, geopackage_path)
        except Exception:
            failed_bands += 1
    return written, failed_bands


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
    FTP-hosted GeoPackage, one zoom level at a time (each in turn split
    into row-band sub-batches, see _extract_zoom_level). Returns
    (tiles_written, failed_bands) -- idempotent/resumable at the
    row-band granularity: re-running only re-fetches whatever's still
    missing, whether that's a whole zoom or a handful of bands within one.
    A failure doesn't stop the rest of the run -- unlike tiles.download_area's
    abort-on-block behaviour, there's no equivalent "you've been blocked"
    signal here, since this is a plain file server, not a rate-limited API."""
    if not gdal_available():
        raise GdalNotAvailableError(
            "gdal_translate not found on PATH -- install GDAL (e.g. `brew install gdal`) first."
        )

    written = 0
    failed = 0
    zooms = list(range(min_zoom, max_zoom + 1))
    for done, zoom in enumerate(zooms, start=1):
        zoom_written, zoom_failed = _extract_zoom_level(
            center_lat, center_lon, radius_km, zoom, cache_dir, geopackage_path
        )
        written += zoom_written
        failed += zoom_failed
        if on_progress is not None:
            on_progress(done, len(zooms))
    return written, failed
