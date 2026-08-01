import io

from PIL import Image

from signal_events import demo_map


def test_generate_demo_tile_returns_a_valid_256x256_png():
    data = demo_map.generate_demo_tile(12, 2200, 1150)
    img = Image.open(io.BytesIO(data))
    assert img.format == "PNG"
    assert img.size == (256, 256)


def test_generate_demo_tile_is_deterministic():
    first = demo_map.generate_demo_tile(11, 1100, 575)
    second = demo_map.generate_demo_tile(11, 1100, 575)
    assert first == second


def test_generate_demo_tile_varies_by_coordinate():
    near_compound = demo_map.generate_demo_tile(14, 9015, 4821)
    far_away = demo_map.generate_demo_tile(14, 9015 + 500, 4821 + 500)
    assert near_compound != far_away


def test_adjacent_tiles_line_up_with_no_seam():
    """Terrain is derived from continuous world-pixel functions rather
    than per-tile randomness specifically so neighbouring tiles agree at
    their shared edge -- verify the actual rendered pixels rather than
    just trusting the math: the right edge column of one tile should
    exactly match the left edge column of the tile immediately to its
    east, one pixel row at a time."""
    zoom, x, y = 13, 4500, 2400
    left = Image.open(io.BytesIO(demo_map.generate_demo_tile(zoom, x, y)))
    right = Image.open(io.BytesIO(demo_map.generate_demo_tile(zoom, x + 1, y)))

    left_edge = [left.getpixel((255, py)) for py in range(256)]
    right_edge = [right.getpixel((0, py)) for py in range(256)]
    assert left_edge == right_edge


def test_river_and_road_appear_near_the_demo_compound():
    """Sanity check that the illustrated terrain actually has *some*
    non-meadow feature (river/road/forest) near the point the demo
    scenario's own events are positioned around, not just empty meadow
    everywhere -- regression guard for the river/road being anchored to
    an unreachable world-pixel offset (a real bug hit during development,
    where they only ever appeared near world coordinate (0, 0), nowhere
    near any real deployment's tiles)."""
    zoom = 14
    ref_x, ref_y = demo_map._world_pixel(demo_map._DEMO_CENTER_LAT, demo_map._DEMO_CENTER_LON, zoom)
    x, y = int(ref_x) // 256, int(ref_y) // 256

    img = Image.open(io.BytesIO(demo_map.generate_demo_tile(zoom, x, y)))
    colors = {img.getpixel((px, py)) for px in range(0, 256, 4) for py in range(0, 256, 4)}
    non_meadow = colors - {demo_map._MEADOW}
    assert non_meadow, "expected at least one non-meadow feature near the demo compound"
