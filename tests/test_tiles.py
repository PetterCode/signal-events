import hashlib
import io
import json
import urllib.error
from pathlib import Path

from signal_events import tiles


def test_latlon_to_tile_matches_known_slippy_map_tile():
    # Stockholm-ish, zoom 10 -- cross-checked against the standard
    # slippy-map tile formula by hand.
    assert tiles.latlon_to_tile(59.3, 18.0, 10) == (563, 301)


def test_latlon_to_tile_clamps_to_valid_tile_range_at_the_poles():
    x, y = tiles.latlon_to_tile(89.9, 179.9, 3)
    n = 2 ** 3
    assert 0 <= x < n
    assert 0 <= y < n


def test_expected_tile_count_for_a_known_area():
    # A single tile's worth of radius at zoom 10 is exactly one tile.
    assert tiles.expected_tile_count(59.3, 18.0, 1, 10, 10) == 1
    # Same area across two zoom levels adds them together.
    assert tiles.expected_tile_count(59.3, 18.0, 1, 10, 11) == 1 + tiles.expected_tile_count(
        59.3, 18.0, 1, 11, 11
    )


def test_cached_tile_count_counts_png_files_recursively(tmp_path):
    (tmp_path / "10" / "500").mkdir(parents=True)
    (tmp_path / "10" / "500" / "300.png").write_bytes(b"x")
    (tmp_path / "10" / "500" / "301.png").write_bytes(b"x")
    (tmp_path / "11" / "1000").mkdir(parents=True)
    (tmp_path / "11" / "1000" / "600.png").write_bytes(b"x")

    assert tiles.cached_tile_count(tmp_path) == 3


def test_cached_tile_count_returns_zero_for_a_missing_directory(tmp_path):
    assert tiles.cached_tile_count(tmp_path / "does-not-exist") == 0


def test_tile_path_builds_the_expected_nested_path(tmp_path):
    path = tiles.tile_path(tmp_path, 12, 2100, 1150)
    assert path == tmp_path / "12" / "2100" / "1150.png"


def test_cached_tile_count_for_area_ignores_tiles_outside_the_area(tmp_path):
    lat, lon, radius = 59.3, 18.0, 1
    x, y = tiles.latlon_to_tile(lat, lon, 10)
    tiles.tile_path(tmp_path, 10, x, y).parent.mkdir(parents=True)
    tiles.tile_path(tmp_path, 10, x, y).write_bytes(b"x")
    # A stray tile from some other, unrelated area/zoom -- e.g. left over
    # from a previous Kartcentrum or area-size setting.
    tiles.tile_path(tmp_path, 10, x + 500, y + 500).parent.mkdir(parents=True)
    tiles.tile_path(tmp_path, 10, x + 500, y + 500).write_bytes(b"x")

    assert tiles.expected_tile_count(lat, lon, radius, 10, 10) == 1
    assert tiles.cached_tile_count(tmp_path) == 2
    assert tiles.cached_tile_count_for_area(tmp_path, lat, lon, radius, 10, 10) == 1


def test_cached_tile_count_for_area_counts_partial_progress(tmp_path):
    lat, lon, radius = 59.3, 18.0, 5
    total = tiles.expected_tile_count(lat, lon, radius, 10, 10)
    x_min, x_max, y_min, y_max = tiles._tile_bounds(lat, lon, radius, 10)
    tiles.tile_path(tmp_path, 10, x_min, y_min).parent.mkdir(parents=True)
    tiles.tile_path(tmp_path, 10, x_min, y_min).write_bytes(b"x")

    assert total > 1
    assert tiles.cached_tile_count_for_area(tmp_path, lat, lon, radius, 10, 10) == 1


def test_point_in_cached_area_true_at_center_false_far_away():
    center_lat, center_lon, radius = 59.3, 18.0, 5.0
    assert tiles.point_in_cached_area(center_lat, center_lon, center_lat, center_lon, radius)
    # ~250 km north -- well outside a 5 km radius.
    assert not tiles.point_in_cached_area(61.5, center_lon, center_lat, center_lon, radius)


def test_download_area_skips_tiles_already_present_on_disk(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        calls.append((zoom, x, y))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"tile")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)

    # Pre-populate the single tile this 1 km/zoom-10 area maps to.
    x, y = tiles.latlon_to_tile(59.3, 18.0, 10)
    existing = tiles.tile_path(tmp_path, 10, x, y)
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"already-cached")

    downloaded, skipped, failed, blocked = tiles.download_area(59.3, 18.0, 1, 10, 10, tmp_path)

    assert downloaded == 0
    assert skipped == 1
    assert failed == 0
    assert blocked is False
    assert calls == []
    assert existing.read_bytes() == b"already-cached"  # untouched, not re-fetched


def test_download_area_fetches_missing_tiles_without_hitting_the_network(tmp_path, monkeypatch):
    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fetched")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)
    monkeypatch.setattr(tiles, "_REQUEST_DELAY_SECONDS", 0)

    downloaded, skipped, failed, blocked = tiles.download_area(59.3, 18.0, 1, 10, 10, tmp_path)

    assert downloaded == 1
    assert skipped == 0
    assert failed == 0
    assert blocked is False
    assert tiles.cached_tile_count(tmp_path) == 1


def test_download_area_counts_failures_without_aborting_the_rest(tmp_path, monkeypatch):
    def flaky_fetch(zoom, x, y, dest, tile_url_template=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(tiles, "_fetch_tile", flaky_fetch)
    monkeypatch.setattr(tiles, "_REQUEST_DELAY_SECONDS", 0)

    downloaded, skipped, failed, blocked = tiles.download_area(59.3, 18.0, 1, 10, 11, tmp_path)

    total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 11)
    assert failed == total
    assert downloaded == 0
    assert skipped == 0
    assert blocked is False


def test_download_area_stops_immediately_once_the_server_reports_a_block(tmp_path, monkeypatch):
    """A TileBlockedError must abort the whole run right away, not just
    count as one more failure -- every remaining request would be
    blocked the exact same way, so continuing is both pointless and
    impolite to the server. This is the regression test for the real
    incident where OSM's abuse detection kicked in mid-download and the
    old code kept "succeeding" (silently caching the blocked-notice image
    as if it were real tile data) for every tile after that."""
    calls = []

    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        calls.append((zoom, x, y))
        if len(calls) == 2:
            raise tiles.TileBlockedError("blocked")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fetched")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)
    monkeypatch.setattr(tiles, "_REQUEST_DELAY_SECONDS", 0)

    total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 12)
    assert total > 2  # otherwise this test wouldn't actually exercise an early stop

    downloaded, skipped, failed, blocked = tiles.download_area(59.3, 18.0, 1, 10, 12, tmp_path)

    assert blocked is True
    assert downloaded == 1
    assert failed == 0
    assert len(calls) == 2  # never attempted the remaining tiles


def test_fetch_tile_raises_and_does_not_write_when_the_response_is_blocked(tmp_path, monkeypatch):
    class FakeHeaders:
        def get(self, key, default=None):
            return "Access denied. See https://operations.osmfoundation.org/policies/tiles/" \
                if key == tiles._BLOCKED_HEADER else default

    class FakeResponse:
        headers = FakeHeaders()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            raise AssertionError("must not read/cache the body of a blocked response")

    monkeypatch.setattr(tiles.urllib.request, "urlopen", lambda *a, **k: FakeResponse())

    dest = tmp_path / "12" / "1" / "1.png"
    try:
        tiles._fetch_tile(12, 1, 1, dest)
        assert False, "expected TileBlockedError"
    except tiles.TileBlockedError:
        pass
    assert not dest.exists()


def test_fetch_tile_raises_tile_blocked_error_on_a_wso2_style_throttle_block(tmp_path, monkeypatch):
    """WSO2 API Manager (the platform behind Lantmäteriet's API-portalen)
    signals a throttle/abuse block with an HTTP error whose JSON body
    carries a known code -- confirmed by hitting one live: Online mode's
    ordinary browsing tripped a free-tier limit and got a 503 with body
    {"code":"700700","type":"API blocked",...} on every further request.
    Must be treated the same as OSM's block: abort immediately, don't
    write anything."""
    body = json.dumps({"code": "700700", "type": "API blocked", "description": "blocked"}).encode()

    def fake_urlopen(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            "https://example.com/tile.png", 503, "Service Unavailable", None, io.BytesIO(body)
        )

    monkeypatch.setattr(tiles.urllib.request, "urlopen", fake_urlopen)

    dest = tmp_path / "12" / "1" / "1.png"
    try:
        tiles._fetch_tile(12, 1, 1, dest)
        assert False, "expected TileBlockedError"
    except tiles.TileBlockedError:
        pass
    assert not dest.exists()


def test_fetch_tile_reraises_an_unrelated_http_error_instead_of_treating_it_as_a_block(tmp_path, monkeypatch):
    body = b"plain 500 error, not a WSO2 throttle block"

    def fake_urlopen(request, timeout=None, context=None):
        raise urllib.error.HTTPError(
            "https://example.com/tile.png", 500, "Internal Server Error", None, io.BytesIO(body)
        )

    monkeypatch.setattr(tiles.urllib.request, "urlopen", fake_urlopen)

    dest = tmp_path / "12" / "1" / "1.png"
    try:
        tiles._fetch_tile(12, 1, 1, dest)
        assert False, "expected the HTTPError to propagate"
    except tiles.TileBlockedError:
        assert False, "an unrelated HTTP error must not be treated as a block"
    except urllib.error.HTTPError:
        pass
    assert not dest.exists()


def test_is_wso2_blocked_response_matches_known_throttle_codes():
    assert tiles._is_wso2_blocked_response(b'{"code":"700700","type":"API blocked"}')
    assert tiles._is_wso2_blocked_response(b'{"code":"900800","type":"Message throttled out"}')


def test_is_wso2_blocked_response_rejects_unrelated_bodies():
    assert not tiles._is_wso2_blocked_response(b'{"code":"999999","type":"Something else"}')
    assert not tiles._is_wso2_blocked_response(b'{"type":"no code field"}')
    assert not tiles._is_wso2_blocked_response(b"not json at all")
    assert not tiles._is_wso2_blocked_response(b"[1,2,3]")


def test_purge_blocked_tiles_removes_only_files_matching_the_known_hash(tmp_path, monkeypatch):
    fake_blocked_content = b"fake-blocked-notice-image"
    monkeypatch.setattr(
        tiles, "_BLOCKED_TILE_SHA256", hashlib.sha256(fake_blocked_content).hexdigest()
    )

    blocked_tile = tiles.tile_path(tmp_path, 10, 1, 1)
    blocked_tile.parent.mkdir(parents=True)
    blocked_tile.write_bytes(fake_blocked_content)

    real_tile = tiles.tile_path(tmp_path, 10, 2, 2)
    real_tile.parent.mkdir(parents=True)
    real_tile.write_bytes(b"genuine-tile-bytes")

    removed = tiles.purge_blocked_tiles(tmp_path)

    assert removed == 1
    assert not blocked_tile.exists()
    assert real_tile.exists()


def test_purge_blocked_tiles_returns_zero_for_a_missing_directory(tmp_path):
    assert tiles.purge_blocked_tiles(tmp_path / "does-not-exist") == 0


def test_download_area_reports_progress(tmp_path, monkeypatch):
    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fetched")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)
    monkeypatch.setattr(tiles, "_REQUEST_DELAY_SECONDS", 0)

    progress = []
    tiles.download_area(59.3, 18.0, 1, 10, 11, tmp_path, on_progress=lambda done, total: progress.append((done, total)))

    total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 11)
    assert progress[-1] == (total, total)
    assert [p[0] for p in progress] == list(range(1, total + 1))


def test_fetch_tile_on_demand_serves_an_already_cached_tile_without_fetching(tmp_path, monkeypatch):
    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        raise AssertionError("must not fetch when the tile is already cached")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)

    dest = tiles.tile_path(tmp_path, 12, 1, 1)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already-cached")

    assert tiles.fetch_tile_on_demand(12, 1, 1, tmp_path) == b"already-cached"


def test_fetch_tile_on_demand_fetches_and_caches_a_missing_tile(tmp_path, monkeypatch):
    def fake_fetch(zoom, x, y, dest, tile_url_template=None):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"live-fetched")

    monkeypatch.setattr(tiles, "_fetch_tile", fake_fetch)

    data = tiles.fetch_tile_on_demand(12, 1, 1, tmp_path)

    assert data == b"live-fetched"
    assert tiles.tile_path(tmp_path, 12, 1, 1).read_bytes() == b"live-fetched"


def test_fetch_tile_on_demand_returns_none_and_caches_nothing_when_the_fetch_fails(tmp_path, monkeypatch):
    def flaky_fetch(zoom, x, y, dest, tile_url_template=None):
        raise RuntimeError("simulated network failure")

    monkeypatch.setattr(tiles, "_fetch_tile", flaky_fetch)

    assert tiles.fetch_tile_on_demand(12, 1, 1, tmp_path) is None
    assert not tiles.tile_path(tmp_path, 12, 1, 1).exists()


def test_fetch_tile_on_demand_returns_none_when_the_server_reports_a_block(tmp_path, monkeypatch):
    def blocked_fetch(zoom, x, y, dest, tile_url_template=None):
        raise tiles.TileBlockedError("blocked")

    monkeypatch.setattr(tiles, "_fetch_tile", blocked_fetch)

    assert tiles.fetch_tile_on_demand(12, 1, 1, tmp_path) is None
    assert not tiles.tile_path(tmp_path, 12, 1, 1).exists()
