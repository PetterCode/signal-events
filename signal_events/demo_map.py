"""Generates a cartoon-style dummy basemap tile for Demo och övning.

Demo/training events (see demo/generate_training_days.py) carry made-up
positions that were never meant to depict a real place -- showing them on
top of a real provider's imagery (Lantmäteriet, MapTiler, ...) would imply
otherwise, and would need a real tile provider configured/reachable just to
try the demo. Instead, whenever demo events are present
(db.has_demo_events), routes.py's map_tile route serves tiles from here
instead of reaching out to whatever's configured on Inställningar --
entirely procedural, no network, no external assets, obviously
illustrative rather than a real map.

Terrain (forest/meadow patches, a river, a road, scattered trees) is
computed from continuous functions of "world pixel" coordinates -- the
same convention tiles.py uses (tile x/y/zoom against a single global pixel
grid) -- rather than anything randomized per-tile, so neighbouring tiles
always line up with no visible seams: a given world position resolves to
the same terrain no matter which 256x256 tile it's sliced from.
"""

from __future__ import annotations

import math
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw

_TILE_SIZE = 256

# Flat, saturated "cartoon" palette -- deliberately not attempting to look
# like real satellite/topographic shading, so it never reads as an actual
# map of anywhere.
_MEADOW = (168, 214, 110)
_FOREST = (66, 133, 82)
_FOREST_DARK = (48, 105, 63)
_WATER = (94, 170, 220)
_WATER_OUTLINE = (35, 90, 130)
_ROAD = (223, 201, 150)
_ROAD_OUTLINE = (110, 90, 60)
_TREE_TRUNK = (94, 64, 42)
_OUTLINE = (40, 40, 40)

_TREE_CELL = 28

# The demo scenario's fictional compound sits around here (see
# demo/generate_training_days.py's PLACE_COORDS) -- the river/road below
# are anchored relative to this point (converted to world-pixel
# coordinates per zoom, same Web Mercator math as tiles.latlon_to_tile)
# rather than to an absolute world-pixel offset, so they actually show up
# in the area the demo's own event pins and default map view land in,
# regardless of zoom level or which real-world longitude that happens to
# correspond to. Everywhere else on the "globe" is just plain
# meadow/forest patchwork -- fine, since nothing in the demo ever points
# a map there.
_DEMO_CENTER_LAT = 59.2971
_DEMO_CENTER_LON = 18.0973


def _world_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    wx = (lon + 180.0) / 360.0 * n * _TILE_SIZE
    wy = (
        (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n * _TILE_SIZE
    )
    return wx, wy


def _is_forest(wx: float, wy: float) -> bool:
    """Low-frequency blotchy patchwork of forest vs. open meadow."""
    value = (
        math.sin(wx / 260.0) + math.cos(wy / 210.0)
        + 0.6 * math.sin((wx + wy) / 140.0)
    )
    return value > 0.15


def _river_distance(local_x: float, local_y: float) -> float:
    """Distance from a gently winding river running roughly north-south
    through the demo compound's area. Takes coordinates already relative
    to the compound's reference point (see _world_pixel) -- its centreline
    is a function of local_x alone, so it's continuous however the world
    is cut into tiles."""
    center = 500 + 220 * math.sin(local_x / 300.0) + 70 * math.sin(local_x / 90.0)
    return abs(local_y - center)


def _road_distance(local_x: float, local_y: float) -> float:
    """A separate winding road, roughly east-west through the same area."""
    center = -300 + 180 * math.sin(local_y / 260.0) + 60 * math.sin(local_y / 80.0)
    return abs(local_x - center)


def _tree_at(wx: int, wy: int) -> bool:
    """Deterministic pseudo-random tree placement on a coarse world grid,
    so a tree near a tile edge is drawn identically regardless of which
    tile it's rendered from."""
    cx, cy = wx // _TREE_CELL, wy // _TREE_CELL
    h = (cx * 374761393 + cy * 668265263) & 0xFFFFFFFF
    h = (h ^ (h >> 13)) * 1274126177 & 0xFFFFFFFF
    return (h % 100) < 45


@lru_cache(maxsize=2048)
def generate_demo_tile(zoom: int, x: int, y: int) -> bytes:
    """Renders one 256x256 cartoon tile as PNG bytes. Cached in-memory
    (pure function of z/x/y) since the per-pixel terrain computation isn't
    free and the same handful of tiles get requested repeatedly as a demo
    map is panned/zoomed."""
    img = Image.new("RGB", (_TILE_SIZE, _TILE_SIZE), _MEADOW)
    origin_x, origin_y = x * _TILE_SIZE, y * _TILE_SIZE
    ref_x, ref_y = _world_pixel(_DEMO_CENTER_LAT, _DEMO_CENTER_LON, zoom)

    for py in range(_TILE_SIZE):
        wy = origin_y + py
        local_y = wy - ref_y
        for px in range(_TILE_SIZE):
            wx = origin_x + px
            local_x = wx - ref_x
            river_d = _river_distance(local_x, local_y)
            road_d = _road_distance(local_x, local_y)
            if river_d < 26:
                color = _WATER
            elif river_d < 30:
                color = _WATER_OUTLINE
            elif road_d < 10:
                color = _ROAD
            elif road_d < 13:
                color = _ROAD_OUTLINE
            elif _is_forest(wx, wy):
                color = _FOREST
            else:
                continue
            img.putpixel((px, py), color)

    draw = ImageDraw.Draw(img)
    cell = _TREE_CELL
    x0, y0 = origin_x // cell - 1, origin_y // cell - 1
    x1, y1 = (origin_x + _TILE_SIZE) // cell + 1, (origin_y + _TILE_SIZE) // cell + 1
    for cy in range(y0, y1 + 1):
        for cx in range(x0, x1 + 1):
            wx, wy = cx * cell + cell // 2, cy * cell + cell // 2
            if not _tree_at(wx, wy):
                continue
            if not _is_forest(wx, wy):
                continue
            local_x, local_y = wx - ref_x, wy - ref_y
            if _river_distance(local_x, local_y) < 34 or _road_distance(local_x, local_y) < 16:
                continue
            px, py = wx - origin_x, wy - origin_y
            if -8 <= px <= _TILE_SIZE + 8 and -8 <= py <= _TILE_SIZE + 8:
                draw.ellipse([px - 6, py - 10, px + 6, py + 2], fill=_FOREST_DARK, outline=_OUTLINE)
                draw.rectangle([px - 1, py, px + 1, py + 6], fill=_TREE_TRUNK)

    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
