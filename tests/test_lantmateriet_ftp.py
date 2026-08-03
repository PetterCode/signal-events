import subprocess

import pytest
from PIL import Image

from signal_events import lantmateriet_ftp, tiles


def _fake_gdal_translate_factory():
    """Builds a fake subprocess.run replacement standing in for
    gdal_translate: instead of touching the network, it writes a real
    (small, solid-colour) PNG of the requested -outsize to the destination
    path, so the real PIL crop/slice logic downstream can be exercised
    against genuine image data rather than a mock."""

    def fake_run(args, capture_output=None, text=None, timeout=None):
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    return fake_run


def test_gdal_available_reflects_whether_gdal_translate_is_on_path(monkeypatch):
    monkeypatch.setattr(lantmateriet_ftp.shutil, "which", lambda name: "/usr/bin/gdal_translate")
    assert lantmateriet_ftp.gdal_available() is True

    monkeypatch.setattr(lantmateriet_ftp.shutil, "which", lambda name: None)
    assert lantmateriet_ftp.gdal_available() is False


def test_extract_area_to_cache_raises_when_gdal_is_not_available(monkeypatch, tmp_path):
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: False)
    with pytest.raises(lantmateriet_ftp.GdalNotAvailableError):
        lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 1, 10, 10, tmp_path)


def test_extract_area_to_cache_writes_a_tile_file_per_expected_tile(monkeypatch, tmp_path):
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", _fake_gdal_translate_factory())

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 1, 10, 11, tmp_path)

    total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 11)
    assert written == total
    assert failed == 0
    assert tiles.cached_tile_count(tmp_path) == total

    # Spot-check one actual tile file is a genuine, correctly-sized PNG,
    # not just an empty placeholder.
    x, y = tiles.latlon_to_tile(59.3, 18.0, 10)
    with Image.open(tiles.tile_path(tmp_path, 10, x, y)) as img:
        assert img.size == (256, 256)


def test_extract_area_to_cache_skips_a_zoom_level_that_is_already_fully_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    calls = []

    def fake_run(args, capture_output=None, text=None, timeout=None):
        calls.append(args)
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (1, 2, 3, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", fake_run)

    # Pre-populate every tile zoom 10 needs, so that zoom should be skipped.
    x_min, x_max, y_min, y_max = tiles._tile_bounds(59.3, 18.0, 1, 10)
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            path = tiles.tile_path(tmp_path, 10, x, y)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"already-cached")

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 1, 10, 11, tmp_path)

    assert failed == 0
    # zoom 10 was skipped entirely -- its pre-existing files must be untouched.
    for x in range(x_min, x_max + 1):
        for y in range(y_min, y_max + 1):
            assert tiles.tile_path(tmp_path, 10, x, y).read_bytes() == b"already-cached"
    # Only zoom 11's gdal_translate call should have happened.
    assert len(calls) == 1


def test_extract_area_to_cache_counts_a_failed_zoom_without_aborting_the_rest(monkeypatch, tmp_path):
    """The second zoom level (11) fails; the first (10) must still have
    been extracted -- one bad zoom shouldn't lose everything else, the
    same "don't abort the whole run over one problem" principle
    tiles.download_area already applies to individual tile failures."""
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    calls = []

    def fake_run(args, capture_output=None, text=None, timeout=None):
        calls.append(args)
        if len(calls) == 2:
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="simulated gdal failure")
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", fake_run)

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 1, 10, 11, tmp_path)

    assert failed == 1
    assert len(calls) == 2
    zoom10_total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 10)
    assert written == zoom10_total
    assert tiles.cached_tile_count(tmp_path) == zoom10_total


def test_extract_area_to_cache_treats_a_hung_gdal_call_as_a_failed_zoom(monkeypatch, tmp_path):
    """Regression: gdal_translate reads this batch's tiles over anonymous
    FTP via /vsicurl/ -- with no timeout on the subprocess call, a single
    stalled connection used to hang forever, which also meant the download
    thread (and the lock it holds, see routes.py's _tile_download_lock)
    never released, so the whole feature looked permanently stuck until
    the app was restarted. A timed-out call must be treated the same as
    any other failed zoom (see the "one bad zoom doesn't abort the rest"
    test above) instead of propagating and blocking forever."""
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    calls = []

    def fake_run(args, capture_output=None, text=None, timeout=None):
        calls.append(args)
        if len(calls) == 2:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout)
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", fake_run)

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 1, 10, 11, tmp_path)

    assert failed == 1
    assert len(calls) == 2
    zoom10_total = tiles.expected_tile_count(59.3, 18.0, 1, 10, 10)
    assert written == zoom10_total


def test_extract_zoom_level_splits_a_large_zoom_into_row_band_sub_batches(monkeypatch, tmp_path):
    """Regression: a whole zoom level used to be fetched in one
    gdal_translate call, however large -- for the biggest zoom levels that
    meant the on-disk tile count (what Inställningar's progress readout
    reads) sat completely still for as long as an hour, indistinguishable
    from a hang, and restarting mid-fetch threw away the whole zoom's
    progress. A big-enough area must now be split into multiple smaller
    row-band calls instead of a single one covering everything."""
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    monkeypatch.setattr(lantmateriet_ftp, "_TARGET_TILES_PER_BATCH", 10)
    calls = []

    def fake_run(args, capture_output=None, text=None, timeout=None):
        calls.append(args)
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", fake_run)

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 15, 12, 12, tmp_path)

    total = tiles.expected_tile_count(59.3, 18.0, 15, 12, 12)
    assert written == total
    assert failed == 0
    assert tiles.cached_tile_count(tmp_path) == total
    # A single zoom level this size must have taken more than one call.
    assert len(calls) > 1


def test_extract_zoom_level_resumes_only_the_bands_still_missing(monkeypatch, tmp_path):
    """A zoom level split across several row bands is resumable at the
    band level, not just all-or-nothing per zoom -- re-running after some
    bands already succeeded (e.g. the app was restarted mid-zoom) must
    only re-fetch the bands still missing."""
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    monkeypatch.setattr(lantmateriet_ftp, "_TARGET_TILES_PER_BATCH", 10)
    calls = []

    def fake_run(args, capture_output=None, text=None, timeout=None):
        calls.append(args)
        width, height = int(args[args.index("-outsize") + 1]), int(args[args.index("-outsize") + 2])
        Image.new("RGBA", (width, height), (10, 20, 30, 255)).save(args[-1], format="PNG")
        return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", fake_run)

    written, failed = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 15, 12, 12, tmp_path)
    assert failed == 0
    first_run_calls = len(calls)
    assert first_run_calls > 1

    # Second run against the same (now fully cached) area must make no
    # gdal_translate calls at all.
    written_again, failed_again = lantmateriet_ftp.extract_area_to_cache(59.3, 18.0, 15, 12, 12, tmp_path)
    assert written_again == 0
    assert failed_again == 0
    assert len(calls) == first_run_calls


def test_extract_area_to_cache_reports_progress(monkeypatch, tmp_path):
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    monkeypatch.setattr(lantmateriet_ftp.subprocess, "run", _fake_gdal_translate_factory())

    progress = []
    lantmateriet_ftp.extract_area_to_cache(
        59.3, 18.0, 1, 10, 12, tmp_path, on_progress=lambda done, total: progress.append((done, total))
    )

    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_tile_to_meters_matches_the_standard_web_mercator_origin():
    mx, my = lantmateriet_ftp._tile_to_meters(0, 0, 0)
    assert mx == pytest.approx(-lantmateriet_ftp._MERCATOR_ORIGIN)
    assert my == pytest.approx(lantmateriet_ftp._MERCATOR_ORIGIN)
