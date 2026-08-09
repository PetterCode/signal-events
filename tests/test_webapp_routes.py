"""Mostly unit tests for pure-Python helpers in webapp/routes.py that
don't need a Flask test client (this project tests routes via direct
helper calls and live smoke testing rather than a test client -- see
other test_* files for the same pattern). The exceptions are session
persistence across requests and a couple of end-to-end route behaviors
that can't be verified any other way."""

import hashlib
import io
import json
import threading
from pathlib import Path

import pytest

from signal_events import config, db as db_module, demo_map, lantmateriet_ftp, tiles
from signal_events.webapp import create_app
from signal_events.webapp import routes as routes_module
from signal_events.webapp.routes import _build_ai_context, _clear_event_attachment_files


def test_pages_include_a_viewport_meta_tag_for_mobile_rendering():
    """Regression: base.html had no <meta name="viewport"> at all, so a
    phone browser (the realistic way a guard actually uses this app in the
    field) fell back to a desktop-width virtual viewport and rendered
    every page zoomed out and hard to read/tap, instead of laying out at
    the device's actual width."""
    client = create_app().test_client()
    resp = client.get("/events")

    assert resp.status_code == 200
    assert b'name="viewport" content="width=device-width, initial-scale=1"' in resp.data


def test_clear_event_attachment_files_leaves_adjacent_subdir_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)

    event_dir = tmp_path / "42"
    event_dir.mkdir()
    (event_dir / "photo.jpg").write_bytes(b"fake")

    adjacent_dir = tmp_path / "adjacent" / "7"
    adjacent_dir.mkdir(parents=True)
    (adjacent_dir / "status.pdf").write_bytes(b"fake")

    entities_dir = tmp_path / "entities" / "3"
    entities_dir.mkdir(parents=True)
    (entities_dir / "reference.jpg").write_bytes(b"fake")

    _clear_event_attachment_files()

    assert not event_dir.exists()
    assert (adjacent_dir / "status.pdf").exists()
    # Entity photos are removed alongside event attachments -- consistent
    # with reset_events() now also clearing the entities rows they belong
    # to (see db.py), not left dangling on disk with no DB row pointing
    # at them.
    assert not entities_dir.exists()


def test_clear_event_attachment_files_tolerates_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "does-not-exist")
    _clear_event_attachment_files()  # must not raise


def test_event_detail_post_toggles_the_important_marker_and_shows_it_on_the_list():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "Norra grinden", "marks": "Notering"})
    with db_module.get_connection() as conn:
        event_id = db_module.list_events(conn)[0]["id"]

    resp = client.post(f"/events/{event_id}", data={"mark_important": "1"}, follow_redirects=True)
    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_event(conn, event_id)["is_important"] == 1
    assert b'class="badge badge-important"' in resp.data

    resp = client.get("/events", query_string={"since": "all"})
    assert b'<span class="badge badge-important">h\xc3\xb6g vikt</span>' in resp.data

    resp = client.post(f"/events/{event_id}", data={}, follow_redirects=True)
    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_event(conn, event_id)["is_important"] == 0


def test_summary_page_remembers_last_viewed_period_across_navigation():
    """Clicking the plain "Sammanställd hotbedömning" nav link (no query
    params -- see base.html) after having viewed a different period must
    show that same period again, not reset to the "7d" default -- the
    route falls back to the session-remembered value only when the query
    param is absent entirely."""
    client = create_app().test_client()

    first = client.get("/summary?since=30d")
    assert b"period: 30d" in first.data

    back_via_plain_nav_link = client.get("/summary")
    assert b"period: 30d" in back_via_plain_nav_link.data

    # An explicit period always overrides and becomes the new "remembered" one.
    client.get("/summary?since=24h")
    again_via_plain_nav_link = client.get("/summary")
    assert b"period: 24h" in again_via_plain_nav_link.data


def test_summary_page_defaults_to_7d_with_no_prior_session():
    client = create_app().test_client()
    resp = client.get("/summary")
    assert b"period: 7d" in resp.data


def test_summary_download_filename_tnr_matches_the_logged_entry():
    """The TNR in the downloaded file's name and the TNR recorded in the
    threat-assessment log for that same generation must be identical --
    both come from a single naming.generate_tnr() call per request, not
    two separate calls that could drift apart."""
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        db_module.set_unit_name(conn, "Kompani 1")

    client = create_app().test_client()
    resp = client.post("/summary/save-pdf", data={"since": "all"})

    assert resp.status_code == 200
    disposition = resp.headers.get("Content-Disposition", "")
    assert "Kompani_1_" in disposition
    assert "_hotbedomning.pdf" in disposition

    filename_tnr = disposition.split("Kompani_1_")[1].split("_hotbedomning")[0]

    with db_module.get_connection() as conn:
        entries = db_module.list_summary_log(conn)

    assert len(entries) == 1
    assert entries[0]["tnr"] == filename_tnr
    assert entries[0]["unit_name"] == "Kompani 1"
    assert entries[0]["source"] == "download"
    assert entries[0]["format"] == "pdf"


def test_summary_narrative_route_shows_the_generated_text_in_an_editable_textarea():
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"response": "Läget är lugnt vid skyddsobjektet under perioden."}
    ).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        resp = client.post("/summary/narrative", data={"since": "7d"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b'name="narrative_text"' in resp.data
    assert "Läget är lugnt vid skyddsobjektet under perioden.".encode() in resp.data


def test_summary_narrative_route_flashes_an_error_and_shows_no_draft_on_llm_failure():
    from unittest.mock import patch

    import urllib.error

    client = create_app().test_client()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
        resp = client.post("/summary/narrative", data={"since": "7d"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b'name="narrative_text"' not in resp.data


def test_summary_save_text_uses_the_submitted_narrative_text_not_a_fresh_generation():
    """save-text must use exactly what's in the reviewed/edited textarea --
    it never calls the LLM again, so a human's edits are what actually
    gets saved, not the original AI draft."""
    client = create_app().test_client()
    resp = client.post(
        "/summary/save-text",
        data={"since": "7d", "narrative_text": "Redigerad lägestext av vakthavande."},
    )

    assert resp.status_code == 200
    assert "Redigerad lägestext av vakthavande.".encode() in resp.data


def test_summary_save_pdf_logs_the_generation_like_the_old_download_did():
    client = create_app().test_client()
    resp = client.post(
        "/summary/save-pdf",
        data={"since": "7d", "narrative_text": "Redigerad lägestext."},
    )

    assert resp.status_code == 200
    assert "_hotbedomning.pdf" in resp.headers.get("Content-Disposition", "")

    with db_module.get_connection() as conn:
        entries = db_module.list_summary_log(conn)
    assert len(entries) == 1
    assert entries[0]["source"] == "download"
    assert entries[0]["format"] == "pdf"


def test_summary_send_uses_the_submitted_narrative_text():
    from unittest.mock import patch

    from signal_events import signal_client

    client = create_app().test_client()
    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post(
            "/summary/send",
            data={"since": "7d", "narrative_text": "Lägestext för Signal-utskick."},
        )

    assert resp.status_code == 302
    mock_send.assert_called_once()


def test_header_status_strip_shows_unit_name_threat_level_and_last_adjacent_send():
    """The status strip under "Signalhändelser" (see base.html and
    routes.inject_header_status) appears on every page via an
    app_context_processor -- checked here through a plain page rather
    than /summary itself, since it must not depend on which page is
    currently open."""
    with db_module.get_connection() as conn:
        db_module.set_unit_name(conn, "Kompani 1")

    client = create_app().test_client()
    resp = client.get("/events")

    assert b"Kompani 1" in resp.data
    assert b"badge-level-green" in resp.data  # no events yet -- default is green
    assert b"Aldrig" in resp.data  # no report sent to adjacent units yet

    with db_module.get_connection() as conn:
        db_module.set_last_adjacent_send(conn)

    resp_after_send = client.get("/events")
    assert b"Aldrig" not in resp_after_send.data


def test_header_hides_adjacent_status_row_when_no_reports_received():
    client = create_app().test_client()
    resp = client.get("/events")
    assert b'class="header-status header-adjacent"' not in resp.data


def test_header_shows_adjacent_unit_threat_status_parsed_from_latest_report():
    with db_module.get_connection() as conn:
        db_module.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number=None, sender_name="2.Kompani",
            unit_name="2.Kompani", body="Läget lugnt.\nBedömning: GRÖN.",
        )
        db_module.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number=None, sender_name="3.Kompani",
            unit_name="3.Kompani", body="Beväpnad person siktad igen.\nBedömning: RÖD.",
        )

    client = create_app().test_client()
    resp = client.get("/events")

    assert b'class="header-status header-adjacent"' in resp.data
    assert "2.Kompani".encode() in resp.data
    assert "3.Kompani".encode() in resp.data
    assert b"badge-level-green" in resp.data
    assert b"badge-level-red" in resp.data
    assert "mottagen".encode() in resp.data


def test_header_shows_unknown_badge_when_adjacent_report_has_no_parseable_level():
    with db_module.get_connection() as conn:
        db_module.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number=None, sender_name="2.Kompani",
            unit_name="2.Kompani", body="Inget särskilt att rapportera.",
        )

    client = create_app().test_client()
    resp = client.get("/events")

    assert b"badge-level-unknown" in resp.data
    assert "okänd".encode() in resp.data


def test_header_threat_level_matches_whatever_period_the_summary_page_last_viewed():
    """Regression: the header's threat badge must always agree with the
    Sammanställd hotbedömning page's own assessment for the period that
    page last showed -- it must not use some independently fixed window
    that could silently disagree with it (see inject_header_status)."""
    with db_module.get_connection() as conn:
        for i in (1, 2):
            message_id = db_module.insert_message(
                conn, signal_timestamp=i, sender_number=None, sender_name=None,
                body="text", raw_json=json.dumps({}),
            )
            event_id = db_module.insert_event(
                conn, message_id=message_id,
                fields={
                    "place": f"Grind {i}", "object": "Civil",
                    "marks": "man beväpnad med gevär", "needs_review": False,
                },
            )
            conn.execute(
                "UPDATE events SET created_at = ? WHERE id = ?",
                ("2020-01-01T10:00:00+00:00", event_id),
            )

    client = create_app().test_client()

    # Viewing the summary page over "all" picks up the old recurring
    # armed sighting -> RED, and remembers "all" in the session.
    resp_all = client.get("/summary?since=all")
    assert "RÖD".encode() in resp_all.data
    assert b"badge-level-red" in resp_all.data  # the header, same response

    # Any other page must show the same RED the summary page just showed.
    resp_events = client.get("/events")
    assert b"badge-level-red" in resp_events.data

    # Switching the summary page back to the 7-day default excludes
    # those old events -> GREEN, and the header must follow along.
    client.get("/summary?since=7d")
    resp_events_again = client.get("/events")
    assert b"badge-level-green" in resp_events_again.data


def test_delete_event_route_removes_the_event_and_redirects_to_the_list():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(conn, message_id=message_id, fields={"place": "X"})

    client = create_app().test_client()
    resp = client.post(f"/events/{event_id}/delete")

    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/events")
    with db_module.get_connection() as conn:
        assert db_module.get_event(conn, event_id) is None
        assert db_module.get_message(conn, message_id) is None


def test_delete_event_route_on_unknown_id_returns_404():
    client = create_app().test_client()
    resp = client.post("/events/999999/delete")
    assert resp.status_code == 404


def test_event_detail_post_saves_a_manually_dropped_pin():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(conn, message_id=message_id, fields={"place": "X"})

    client = create_app().test_client()
    client.post(f"/events/{event_id}", data={"place": "X", "lat": "58.6", "lon": "15.3"})

    with db_module.get_connection() as conn:
        event = db_module.get_event(conn, event_id)
    assert event["lat"] == pytest.approx(58.6)
    assert event["lon"] == pytest.approx(15.3)


def test_event_detail_post_without_lat_lon_fields_leaves_existing_position_untouched():
    """Regression guard: an ordinary field edit (no map click this
    submission) must not wipe out a position that was set earlier --
    _position_form_values() returning {} is what protects this."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 58.6, "lon": 15.3}
        )

    client = create_app().test_client()
    client.post(f"/events/{event_id}", data={"place": "X"})

    with db_module.get_connection() as conn:
        event = db_module.get_event(conn, event_id)
    assert event["lat"] == pytest.approx(58.6)
    assert event["lon"] == pytest.approx(15.3)


def test_event_detail_shows_a_hint_when_the_position_is_outside_the_cached_area():
    """Regression: an event positioned far from Kartcentrum used to just
    render an all-gray map with no explanation -- the hint tells the user
    why, instead of looking like a broken map."""
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.326944, 18.071667)
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        # ~250 km from Kartcentrum -- well outside even the "large" preset.
        event_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 61.5, "lon": 18.07}
        )

    client = create_app().test_client()
    resp = client.get(f"/events/{event_id}")

    assert resp.status_code == 200
    assert "utanför det nedladdade kartområdet".encode() in resp.data


def test_event_detail_has_no_hint_when_the_position_is_inside_the_cached_area():
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.326944, 18.071667)
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 59.33, "lon": 18.07}
        )

    client = create_app().test_client()
    resp = client.get(f"/events/{event_id}")

    assert resp.status_code == 200
    assert "utanför det nedladdade kartområdet".encode() not in resp.data


def test_map_view_passes_min_zoom_so_fitbounds_cant_collapse_to_a_blank_map():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert f"var minZoom = {config.MAP_CACHE_MIN_ZOOM};".encode() in resp.data


def test_map_view_clamped_zoom_centers_on_kartcentrum_not_the_bounds_centroid():
    """Regression: when a far-outlier marker forces the min-zoom clamp,
    centering on the bounds' own centroid can itself land on ground with
    no cached tiles (dragged there by the outlier) -- Kartcentrum is the
    one point guaranteed to have coverage, so the clamped view must use
    it instead of bounds.getCenter()."""
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"map.setView(hasCenter ? [centerLat, centerLon] : bounds.getCenter(), minZoom);" in resp.data


def test_map_view_flags_which_markers_are_inside_the_cached_area():
    """Regression: even at a zoom that clears the min-zoom clamp,
    fitBounds still centers on the bounds' own centroid -- one marker far
    outside the cached square drags that centroid onto uncached ground
    too, showing an all-gray map despite "working" zoom math. The fix is
    to keep out-of-cache markers off the fit calculation entirely (they
    still show on the map, just don't steer the initial view) -- this
    checks the per-marker in_cache flag the JS relies on to do that."""
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.5321, 17.3004)
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"place": "Nara", "lat": 59.5335, "lon": 17.301}
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"place": "Langt bort", "lat": 61.8, "lon": 17.3}
        )

    client = create_app().test_client()
    resp = client.get("/karta?since=all")

    assert resp.status_code == 200
    assert b'"in_cache": true' in resp.data
    assert b'"in_cache": false' in resp.data
    assert b"!hasCenter || m.in_cache" in resp.data


def test_map_view_marks_kartcentrum_with_its_own_marker_when_a_center_is_set():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"Kartcentrum (Inst" in resp.data
    assert b"L.circleMarker" in resp.data


def test_map_view_marks_the_config_default_as_kartcentrum_when_no_center_is_set():
    """get_map_center falls back to config.DEFAULT_MAP_CENTER when nothing's
    been explicitly saved, so Kart-vy always has a real Kartcentrum to draw
    -- unlike before, hasCenter is never false."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 59.33, "lon": 18.06}
        )
    client = create_app().test_client()

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"var hasCenter = true;" in resp.data
    assert b"L.circleMarker" in resp.data


def test_map_view_shows_no_tile_cache_status_in_online_mode_with_url_source():
    """The default (online mode, URL-based provider) fetches tiles live
    rather than depending on the local cache, so "X av Y kartrutor
    cachade" would be misleading/irrelevant here -- unlike Inställningar,
    where it's always shown since that's about managing the cache
    itself regardless of whether it's actually being relied on."""
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.33, 18.06)
        db_module.set_map_tile_source(conn, db_module.MAP_TILE_SOURCE_URL)

    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"kartrutor cachade" not in resp.data


def test_map_view_shows_tile_cache_status_in_local_mode():
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.33, 18.06)
        db_module.set_map_tile_mode(conn, db_module.MAP_TILE_MODE_LOCAL)
        db_module.set_map_cache_area_size(conn, "medium")

    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"kartrutor cachade" in resp.data
    assert b"Mellan (10 x 10 km)" in resp.data


def test_map_view_shows_tile_cache_status_for_lantmateriet_ftp_source_even_in_online_mode():
    """Lantmäteriet's FTP source is always cache-only regardless of
    Kartläge (see map_tile()'s docstring), so the cache status is
    relevant here too, not just in "Lokal cache" mode."""
    with db_module.get_connection() as conn:
        db_module.set_map_center(conn, 59.33, 18.06)
        db_module.set_map_tile_source(conn, db_module.MAP_TILE_SOURCE_LANTMATERIET_FTP)

    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"kartrutor cachade" in resp.data


def test_map_view_shows_tile_cache_status_in_local_mode_even_without_an_explicit_center():
    """get_map_center's config.DEFAULT_MAP_CENTER fallback means there's
    always a real point to compute an expected/cached tile count for, even
    before Kartcentrum's ever been touched on Inställningar."""
    with db_module.get_connection() as conn:
        db_module.set_map_tile_mode(conn, db_module.MAP_TILE_MODE_LOCAL)

    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"kartrutor cachade" in resp.data


def test_new_event_get_passes_map_context_to_the_template():
    client = create_app().test_client()
    resp = client.get("/events/new")

    assert resp.status_code == 200
    assert b'id="new-event-map"' in resp.data


def test_new_event_form_has_a_fill_current_time_button():
    client = create_app().test_client()
    resp = client.get("/events/new")

    assert resp.status_code == 200
    assert b'id="event-time-now-btn"' in resp.data


def test_event_detail_has_a_fill_current_time_button():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(conn, message_id=message_id, fields={"place": "X"})

    client = create_app().test_client()
    resp = client.get(f"/events/{event_id}")

    assert resp.status_code == 200
    assert b'id="event-time-now-btn"' in resp.data


def test_now_button_fills_in_day_hour_minute_not_just_clock_time():
    """Regression: the "Nu" button used to fill in only "HH:MM", which
    never matches naming._VALID_TNR_RE -- so a manually entered event's
    displayed TNR silently fell back to its created_at timestamp instead
    of the time it actually happened. It must fill in the same
    day-hour-minute date-time-group naming.generate_tnr uses everywhere
    else, on every form that has the button."""
    client = create_app().test_client()
    for path in ("/events/new", "/events/new-adjacent"):
        resp = client.get(path)
        assert resp.status_code == 200
        body = resp.data.decode("utf-8")
        assert "now.getDate()" in body
        assert 'value = dd + hh + mm;' in body
        assert 'value = hh + ":" + mm;' not in body


def test_new_event_post_saves_a_manually_dropped_pin():
    """Regression: event_form.html previously had no map/position UI at
    all, and new_event() never looked at lat/lon form fields -- manually
    entered events silently never got a position no matter what."""
    client = create_app().test_client()
    resp = client.post(
        "/events/new",
        data={"place": "Östra grinden", "lat": "58.6", "lon": "15.3"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["lat"] == pytest.approx(58.6)
    assert events[0]["lon"] == pytest.approx(15.3)


def test_new_event_post_without_a_pin_auto_extracts_an_mgrs_reference_from_place():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "Östra grinden 33VVN1234567890"})

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["lat"] is not None
    assert events[0]["lon"] is not None


def test_new_event_post_without_a_pin_auto_extracts_decimal_degrees_from_place():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "Östra grinden 59.3269, 18.0717"})

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["lat"] == pytest.approx(59.3269)
    assert events[0]["lon"] == pytest.approx(18.0717)


def test_new_event_post_manual_pin_wins_over_an_mgrs_reference_in_the_text():
    client = create_app().test_client()
    client.post(
        "/events/new",
        data={"place": "Östra grinden 33VVN1234567890", "lat": "58.6", "lon": "15.3"},
    )

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["lat"] == pytest.approx(58.6)
    assert events[0]["lon"] == pytest.approx(15.3)


def test_new_event_post_without_any_position_leaves_lat_lon_null():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "Huvudentrén"})

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["lat"] is None
    assert events[0]["lon"] is None


def test_new_event_post_with_an_overlong_photo_filename_still_saves():
    """Regression: secure_filename() strips path separators/special
    characters but not length -- an uploaded filename longer than the
    filesystem's own limit (255 bytes on macOS/most Linux) used to crash
    the whole request with OSError("File name too long") instead of just
    saving under a (still unique enough) shorter name."""
    client = create_app().test_client()
    photo = (io.BytesIO(b"fake-image-bytes"), "f" * 500 + ".jpg")

    resp = client.post(
        "/events/new",
        data={"place": "Foto med extremt långt filnamn", "photos": [photo]},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        assert len(events) == 1
        attachments = db_module.list_attachments_for_message(conn, events[0]["message_id"])
    assert len(attachments) == 1
    assert len(Path(attachments[0]["file_path"]).name) <= 120


def test_new_adjacent_event_get_shows_the_source_unit_field_and_known_units():
    with db_module.get_connection() as conn:
        db_module.add_adjacent_unit(conn, "2.Pluton")

    client = create_app().test_client()
    resp = client.get("/events/new-adjacent")

    assert resp.status_code == 200
    assert b'name="source_unit"' in resp.data
    assert b"2.Pluton" in resp.data


def test_new_adjacent_event_post_tags_the_event_with_the_source_unit():
    client = create_app().test_client()
    resp = client.post(
        "/events/new-adjacent",
        data={"place": "Norra grinden", "source_unit": "2.Pluton"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert len(events) == 1
    assert events[0]["source_unit"] == "2.Pluton"


def test_new_adjacent_event_post_without_a_source_unit_is_rejected():
    client = create_app().test_client()
    resp = client.post(
        "/events/new-adjacent", data={"place": "Norra grinden"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "Ange vilken angränsande enhet".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.list_events(conn) == []


def test_events_list_shows_a_badge_for_adjacent_sourced_events():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "source_unit": "2.Pluton"}
        )

    client = create_app().test_client()
    resp = client.get("/events")

    assert resp.status_code == 200
    assert b"fr\xc3\xa5n 2.Pluton" in resp.data


def test_event_detail_shows_a_badge_for_an_adjacent_sourced_event():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "source_unit": "2.Pluton"}
        )

    client = create_app().test_client()
    resp = client.get(f"/events/{event_id}")

    assert resp.status_code == 200
    assert "Från angränsande enhet: 2.Pluton".encode() in resp.data


def test_report_route_excludes_events_received_from_adjacent_units():
    """Regression guard for the own_only choke point: a generated report
    is this unit's own account, so an adjacent-sourced event must never
    show up in it even though it's a perfectly normal, reviewed event."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Ovan enhets egen", "needs_review": False},
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Fran angransande", "needs_review": False, "source_unit": "2.Pluton"},
        )

    client = create_app().test_client()
    resp = client.post("/report", data={"since": "all", "format": "text"})

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert "Ovan enhets egen" in body
    assert "Fran angransande" not in body


def test_summary_excludes_events_received_from_adjacent_units():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Egen", "needs_review": False, "object": "person"},
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Angransande", "needs_review": False, "object": "person",
                "source_unit": "2.Pluton",
            },
        )

    summary = routes_module._compute_summary("all", include_unreviewed=False)
    assert summary.total_events == 1


def test_map_view_default_includes_adjacent_sourced_events():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "X", "lat": 58.6, "lon": 15.3, "source_unit": "2.Pluton"},
        )

    client = create_app().test_client()
    resp = client.get("/karta?since=all")

    assert resp.status_code == 200
    assert b'"source_unit": "2.Pluton"' in resp.data
    assert "Dölj händelser från angränsande enheter".encode() in resp.data


def test_map_view_adjacent_0_excludes_adjacent_sourced_markers():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        own_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "Egen", "lat": 58.6, "lon": 15.3}
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Angransande", "lat": 58.7, "lon": 15.4, "source_unit": "2.Pluton"},
        )

    client = create_app().test_client()
    resp = client.get("/karta?since=all&adjacent=0")

    assert resp.status_code == 200
    assert f'"id": {own_id}'.encode() in resp.data
    assert b"2.Pluton" not in resp.data
    assert "Visa händelser från angränsande enheter".encode() in resp.data


def test_map_view_default_shows_all_events_toggle_link():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert "Dölj alla händelser".encode() in resp.data
    assert b"var showEvents = true;" in resp.data


def test_map_view_events_0_hides_markers_but_keeps_the_accurate_count():
    """Regression: hiding all events is a display preference, not a data
    filter -- the "N händelser" hint must still reflect the real count
    (and JS gets showEvents=false to suppress rendering the markers),
    not silently report zero as if none existed."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 58.6, "lon": 15.3}
        )

    client = create_app().test_client()
    resp = client.get("/karta?since=all&events=0")

    assert resp.status_code == 200
    assert b"1 h\xc3\xa4ndelse med k\xc3\xa4nd position" in resp.data
    assert b"var showEvents = false;" in resp.data
    assert "Visa alla händelser".encode() in resp.data
    assert "Dolda".encode() in resp.data


def test_map_view_renders_a_crosshair_and_position_box():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"map-crosshair" in resp.data
    assert b"map-position-box" in resp.data
    assert b"/karta/mgrs?lat=" in resp.data


def test_map_view_persists_pan_zoom_across_visits_via_local_storage():
    """Regression: Kart-vy must remember the last pan/zoom when the user
    navigates to another tab and back -- since each visit is a fresh
    page load (no SPA state), that has to be localStorage, restored in
    place of the usual fit-to-markers/Kartcentrum default, and kept
    current on every "moveend"."""
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"signal-events:karta:view" in resp.data
    assert b"localStorage.getItem(VIEW_STORAGE_KEY)" in resp.data
    assert b"localStorage.setItem(VIEW_STORAGE_KEY" in resp.data
    assert b'map.setView([savedView.lat, savedView.lng], savedView.zoom);' in resp.data
    assert b'map.on("moveend", saveCurrentView);' in resp.data


def test_map_center_mgrs_route_converts_lat_lon_to_mgrs():
    client = create_app().test_client()
    resp = client.get("/karta/mgrs?lat=59.326944&lon=18.071667")

    assert resp.status_code == 200
    assert resp.get_json()["mgrs"].startswith("34VCL")


def test_map_center_mgrs_route_requires_both_coordinates():
    client = create_app().test_client()
    resp = client.get("/karta/mgrs?lat=59.33")

    assert resp.status_code == 400
    assert resp.get_json()["mgrs"] is None


def test_event_detail_post_clear_position_button_removes_the_pin():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(
            conn, message_id=message_id, fields={"place": "X", "lat": 58.6, "lon": 15.3}
        )

    client = create_app().test_client()
    client.post(
        f"/events/{event_id}",
        data={"place": "X", "lat": "58.6", "lon": "15.3", "clear_position": "1"},
    )

    with db_module.get_connection() as conn:
        event = db_module.get_event(conn, event_id)
    assert event["lat"] is None
    assert event["lon"] is None


def test_map_view_lists_only_events_that_have_a_position():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Kajen", "object": "Personbil", "lat": 58.6, "lon": 15.3},
        )
        db_module.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})

    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert b"Kajen" in resp.data
    assert b"1 h\xc3\xa4ndelse med k\xc3\xa4nd position" in resp.data


def test_map_view_shows_empty_state_when_no_events_have_a_position():
    client = create_app().test_client()
    resp = client.get("/karta")

    assert resp.status_code == 200
    assert "Inga händelser med känd position under vald tidsperiod".encode() in resp.data


def test_map_view_period_selection_filters_markers_like_the_summary_page():
    """Kart-vy offers the same Tidsperiod selection (24 tim/7 dagar/30
    dagar/Alla) as Sammanställd hotbedömning and Tidslinje, filtering
    which positioned events show as markers the same way list_events
    already filters those other views -- not just decorative links."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        old_event = db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Gammal position", "lat": 58.6, "lon": 15.3},
        )
        conn.execute(
            "UPDATE events SET created_at = ? WHERE id = ?",
            ("2020-01-01T10:00:00+00:00", old_event),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Ny position", "lat": 58.7, "lon": 15.4},
        )

    client = create_app().test_client()

    default_resp = client.get("/karta")
    assert b"Ny position" in default_resp.data
    assert b"Gammal position" not in default_resp.data
    assert b"1 h\xc3\xa4ndelse med k\xc3\xa4nd position" in default_resp.data

    all_resp = client.get("/karta?since=all")
    assert b"Ny position" in all_resp.data
    assert b"Gammal position" in all_resp.data
    assert b"2 h\xc3\xa4ndelser med k\xc3\xa4nd position" in all_resp.data

    day_resp = client.get("/karta?since=24h")
    assert b"Gammal position" not in day_resp.data


def test_summary_excludes_a_duplicate_report_of_the_same_incident():
    """Two events describing the same incident (same place/object, near-
    identical wording, close together in time) must count as one event
    in the threat analysis, not two -- see duplicates.py."""
    with db_module.get_connection() as conn:
        for i in (1, 2):
            message_id = db_module.insert_message(
                conn, signal_timestamp=i, sender_number=None, sender_name=None,
                body="text", raw_json=json.dumps({}),
            )
            db_module.insert_event(
                conn, message_id=message_id,
                fields={
                    "place": "Norra grinden", "object": "Personbil",
                    "marks": "Silver Volvo, Reg.nr KRN482",
                    "activity": "Stannade vid grinden",
                    "needs_review": False,
                },
            )

    client = create_app().test_client()
    resp = client.get("/summary?since=all")

    assert b"Rapporter i underlaget: 1" in resp.data

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert sum(1 for e in events if e["is_duplicate"]) == 1


def test_summary_excludes_a_trivial_routine_event_from_the_threat_assessment():
    """A routine, non-notable event (see triviality.py) must not count
    towards the threat analysis's underlag any more than it does towards
    a generated report's own content -- excluded here the same way
    duplicates already are, not just left out of the rendered report
    text."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Skogsbrynet", "object": "Rådjur",
                "activity": "Passerade genom området", "needs_review": False,
            },
        )
        message_id2 = db_module.insert_message(
            conn, signal_timestamp=2, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id2,
            fields={
                "place": "Huvudentrén", "object": "Civil",
                "activity": "Fotograferade stängslet", "needs_review": False,
            },
        )

    client = create_app().test_client()
    resp = client.get("/summary?since=all")

    assert b"Rapporter i underlaget: 1" in resp.data

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
    assert sum(1 for e in events if e["is_trivial"]) == 1


def test_summary_override_route_saves_and_reflects_in_the_header():
    client = create_app().test_client()

    resp = client.post(
        "/summary/override",
        data={"since": "7d", "include_unreviewed": "0", "level": "red", "notes": "Bekräftad av chefvakt"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "RÖD".encode() in resp.data
    assert b"badge-level-red" in resp.data  # header status strip, same page
    assert "Bekräftad av chefvakt".encode() in resp.data

    # Any other page must show the same manually-set RÖD in its header.
    resp_events = client.get("/events")
    assert b"badge-level-red" in resp_events.data


def test_summary_override_clear_route_reverts_to_automatic():
    client = create_app().test_client()
    client.post("/summary/override", data={"since": "7d", "include_unreviewed": "0", "level": "red", "notes": ""})

    resp = client.post("/summary/override/clear", data={"since": "7d", "include_unreviewed": "0"}, follow_redirects=True)

    assert resp.status_code == 200
    assert b"badge-level-green" in resp.data  # no events -- automatic default is green
    with db_module.get_connection() as conn:
        assert db_module.get_threat_override(conn) is None


def test_summary_override_rejects_an_invalid_level():
    client = create_app().test_client()
    resp = client.post(
        "/summary/override",
        data={"since": "7d", "include_unreviewed": "0", "level": "purple", "notes": ""},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_threat_override(conn) is None


def test_build_ai_context_includes_reported_by_and_next_steps():
    """A prior real-world question ("vad har Vakt Berg rapporterat")
    failed because reported_by was never sent to the model at all --
    the context only had place/count/object/activity/marks."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number="+1", sender_name="Vakt Berg",
            body="test", raw_json="{}",
        )
        db_module.insert_event(conn, message_id=message_id, fields={
            "place": "Norra grinden", "reported_by": "Vakt Berg",
            "next_steps": "Fortsatt bevakning nästa skift",
        })

    ctx = _build_ai_context()
    assert "rapporterad av=Vakt Berg" in ctx
    assert "åtgärd/uppföljning=Fortsatt bevakning nästa skift" in ctx


def test_build_ai_context_flags_which_events_have_a_photo_attached():
    """A prior real-world question ("hur många rapporter innehåller
    foton") failed because whether an event has an attachment was never
    sent to the model at all -- there was no way for it to know, correct
    or otherwise."""
    with db_module.get_connection() as conn:
        with_photo_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number="+1", sender_name=None,
            body="test", raw_json="{}",
        )
        db_module.insert_event(conn, message_id=with_photo_id, fields={"place": "Med foto"})
        db_module.insert_attachment(
            conn, message_id=with_photo_id, file_path="/tmp/x.png", content_type="image/png",
        )

        without_photo_id = db_module.insert_message(
            conn, signal_timestamp=2, sender_number="+1", sender_name=None,
            body="test", raw_json="{}",
        )
        db_module.insert_event(conn, message_id=without_photo_id, fields={"place": "Utan foto"})

    ctx = _build_ai_context()
    assert "Totalt antal av dessa händelser som har ett bifogat foto: 1" in ctx
    lines_by_place = {
        line.split("plats=")[1].split(",")[0]: line
        for line in ctx.splitlines() if line.startswith("- TNR")
    }
    assert "bifogat foto=ja" in lines_by_place["Med foto"]
    assert "bifogat foto=nej" in lines_by_place["Utan foto"]


def test_build_ai_context_states_explicit_totals_unambiguously():
    """A prior real-world question ("hur många händelser finns det")
    got a wildly wrong, made-up answer because the model had to count
    rows itself, and also confused summary_log's per-entry "antal
    händelser" (how many events one past assessment covered) with the
    real total. Explicit, distinctly-worded totals fix both: the model
    can read one number instead of counting or guessing."""
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number="+1", sender_name=None,
            body="test", raw_json="{}",
        )
        db_module.insert_event(conn, message_id=message_id, fields={"place": "X"})
        db_module.insert_summary_log_entry(
            conn, tnr="010101", unit_name="Enhet", period_label="7d",
            total_events=1, level="green", score=0, source="send", format="pdf",
        )
        db_module.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number="+2", sender_name=None,
            unit_name="Kompani 2", body="Läget lugnt",
        )

    ctx = _build_ai_context()
    assert "Totalt antal sparade händelser i händelseloggen just nu: 1" in ctx
    assert "Totalt antal loggade hotbedömningar (Logg-sidan): 1" in ctx
    assert "Totalt antal mottagna rapporter från angränsande enheter: 1" in ctx
    # the old ambiguous wording that collided with "händelser" must be gone
    assert "antal händelser=" not in ctx
    assert "byggd på 1 rapporter" in ctx


def test_summary_ai_tab_renders_empty_chat_with_no_history():
    client = create_app().test_client()
    resp = client.get("/summary/ai")

    assert resp.status_code == 200
    assert "AI-analys".encode() in resp.data
    assert "Inget skrivet ännu".encode() in resp.data


def test_summary_ai_search_finds_a_registration_number_without_calling_the_llm():
    from unittest.mock import patch

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Norra grinden", "marks": "Silver Volvo, Reg.nr KRN482"},
        )

    client = create_app().test_client()
    with patch("urllib.request.urlopen") as mock_urlopen:
        resp = client.get("/summary/ai?q=KRN482")

    mock_urlopen.assert_not_called()
    assert resp.status_code == 200
    assert b"KRN482" in resp.data
    assert "Norra grinden".encode() in resp.data


def test_summary_ai_search_shows_no_hits_message_for_an_unmatched_query():
    client = create_app().test_client()
    resp = client.get("/summary/ai?q=NOSUCHPLATE")

    assert resp.status_code == 200
    assert "Inga träffar".encode() in resp.data


def test_summary_ai_search_box_is_empty_with_no_query():
    client = create_app().test_client()
    resp = client.get("/summary/ai")

    assert resp.status_code == 200
    assert b"Inga tr\xc3\xa4ffar" not in resp.data


def test_summary_ai_chat_saves_the_question_immediately_without_calling_ollama():
    """The question must land in the session on its own, fast request --
    see summary_ai's docstring for why: asking and answering used to be
    one slow request, so a user who navigated away mid-wait would lose
    the question too, since its Set-Cookie never reached the browser."""
    from unittest.mock import patch

    client = create_app().test_client()
    with patch("urllib.request.urlopen") as mock_urlopen:
        resp = client.post(
            "/summary/ai/chat", data={"message": "Har vi sett den här bilen förut?"},
            follow_redirects=True,
        )

    mock_urlopen.assert_not_called()
    assert resp.status_code == 200
    assert "Har vi sett den här bilen förut?".encode() in resp.data
    assert "AI-analys tänker".encode() in resp.data


def test_summary_ai_page_auto_submits_the_pending_reply_request():
    client = create_app().test_client()
    client.post("/summary/ai/chat", data={"message": "Fråga"})

    resp = client.get("/summary/ai")

    assert b'action="/summary/ai/respond"' in resp.data
    assert b"ai-respond-form" in resp.data


def test_summary_ai_respond_generates_the_reply_for_the_pending_question():
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(
        {"message": {"role": "assistant", "content": "Inga tidigare observationer av det fordonet."}}
    ).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    client.post("/summary/ai/chat", data={"message": "Har vi sett den här bilen förut?"})
    with patch("urllib.request.urlopen", return_value=mock_resp):
        resp = client.post("/summary/ai/respond", follow_redirects=True)

    assert resp.status_code == 200
    assert "Har vi sett den här bilen förut?".encode() in resp.data
    assert "Inga tidigare observationer av det fordonet.".encode() in resp.data
    assert "AI-analys tänker".encode() not in resp.data


def test_summary_ai_respond_calls_the_configured_ollama_port():
    from unittest.mock import MagicMock, patch

    with db_module.get_connection() as conn:
        db_module.set_ollama_port(conn, "11500")

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"message": {"content": "Svar."}}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    client.post("/summary/ai/chat", data={"message": "Fråga"})
    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        client.post("/summary/ai/respond")

    request = mock_urlopen.call_args[0][0]
    assert request.full_url == "http://localhost:11500/api/chat"


def test_summary_ai_respond_is_a_no_op_when_nothing_is_pending():
    from unittest.mock import patch

    client = create_app().test_client()
    with patch("urllib.request.urlopen") as mock_urlopen:
        resp = client.post("/summary/ai/respond", follow_redirects=True)

    mock_urlopen.assert_not_called()
    assert resp.status_code == 200


def test_summary_ai_chat_keeps_conversation_across_requests_via_session():
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"message": {"content": "Svar."}}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        client.post("/summary/ai/chat", data={"message": "Fråga ett"})
        client.post("/summary/ai/respond")
        client.post("/summary/ai/chat", data={"message": "Fråga två"})
        client.post("/summary/ai/respond")

    resp = client.get("/summary/ai")
    assert "Fråga ett".encode() in resp.data
    assert "Fråga två".encode() in resp.data
    assert resp.data.count(b"Svar.") == 2


def test_summary_ai_respond_keeps_the_question_and_offers_a_retry_on_llm_failure():
    """Unlike the old single-request design, a failed generation no
    longer drops the question -- it stays visible with a retry button,
    since it's already safely saved regardless of what the LLM call
    does (see summary_ai's docstring)."""
    from unittest.mock import patch

    import urllib.error

    client = create_app().test_client()
    client.post("/summary/ai/chat", data={"message": "Fungerar detta?"})
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
        resp = client.post("/summary/ai/respond", follow_redirects=True)

    assert resp.status_code == 200
    assert "Kunde inte nå Ollama".encode() in resp.data
    assert "Fungerar detta?".encode() in resp.data
    assert "Försök igen".encode() in resp.data
    # a failed attempt must not auto-retry itself -- only a manual retry
    assert b'id="ai-respond-form"' not in resp.data


def test_summary_ai_clear_empties_the_conversation():
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"message": {"content": "Svar."}}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        client.post("/summary/ai/chat", data={"message": "Fråga"})
        client.post("/summary/ai/respond")

    client.post("/summary/ai/clear")
    resp = client.get("/summary/ai")

    assert "Inget skrivet ännu".encode() in resp.data
    assert "Fråga".encode() not in resp.data


def test_demo_import_tab_lists_training_days_and_import_page_no_longer_does():
    """The training-day buttons moved off the plain file-import page onto
    their own "Demo och övning" tab (see routes.demo_import)."""
    client = create_app().test_client()

    demo_resp = client.get("/events/import/demo")
    assert demo_resp.status_code == 200
    assert b"Dag 1" in demo_resp.data
    assert b"Dag 10" in demo_resp.data

    import_resp = client.get("/events/import")
    assert import_resp.status_code == 200
    assert b"Dag 1" not in import_resp.data


def test_header_shows_demo_mode_badge_only_after_a_training_day_import():
    client = create_app().test_client()

    before = client.get("/events")
    assert "DEMO-LÄGE AKTIVT".encode() not in before.data

    resp = client.post("/events/import/training/1")
    assert resp.status_code == 302

    after = client.get("/events")
    assert "DEMO-LÄGE AKTIVT".encode() in after.data


def test_import_training_day_attaches_cartoon_images_to_signal_events():
    """Day 6 has two deliberate "signal" events with an image in
    demo/training_days/event_images.json (the person in dark clothing,
    the cut fence) -- verifies they actually land as real attachments
    (DB row + file on disk), not just that the import itself succeeds."""
    client = create_app().test_client()
    resp = client.post("/events/import/training/6", follow_redirects=True)

    assert resp.status_code == 200
    assert "bifogad bild".encode() in resp.data

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        attachments = []
        for event in events:
            attachments.extend(db_module.list_attachments_for_message(conn, event["message_id"]))

    assert len(attachments) == 2
    for attachment in attachments:
        assert attachment["content_type"] == "image/png"
        assert Path(attachment["file_path"]).is_file()


def test_demo_clear_removes_training_day_image_files_from_disk():
    client = create_app().test_client()
    client.post("/events/import/training/6")

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        attachment_paths = [
            Path(a["file_path"])
            for event in events
            for a in db_module.list_attachments_for_message(conn, event["message_id"])
        ]
    assert attachment_paths and all(p.is_file() for p in attachment_paths)

    client.post("/events/import/demo/clear")

    assert all(not p.exists() for p in attachment_paths)


def test_import_training_day_excludes_sensor_events_by_default():
    client = create_app().test_client()
    resp = client.post("/events/import/training/6", follow_redirects=True)

    assert resp.status_code == 200
    assert "automatiska sensorhändelser".encode() not in resp.data
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        assert all(e["reported_by"] != "Sensorgateway" for e in events)


def test_sensor_toggle_route_persists_and_is_reflected_on_the_demo_page():
    client = create_app().test_client()
    resp = client.post(
        "/events/import/demo/sensor-toggle", data={"include_sensors": "1"}, follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b'name="include_sensors" value="1" style="width:auto" checked' in resp.data

    client.post("/events/import/demo/sensor-toggle", data={}, follow_redirects=True)
    resp_off = client.get("/events/import/demo")
    assert b'name="include_sensors" value="1" style="width:auto" checked' not in resp_off.data


def test_import_training_day_includes_sensor_events_when_toggle_is_on():
    client = create_app().test_client()
    client.post("/events/import/demo/sensor-toggle", data={"include_sensors": "1"})

    resp = client.post("/events/import/training/6", follow_redirects=True)

    assert resp.status_code == 200
    assert "automatiska sensorhändelser".encode() in resp.data
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        sensor_events = [e for e in events if e["reported_by"] == "Sensorgateway"]
        assert len(sensor_events) == 3
        # Slag/Symbol/Sedan are blank and Sysselsättning is always the
        # same generic line -- place is what identifies the sensor type
        places = [e["place"] for e in sensor_events]
        assert any("Trådlarm" in p for p in places)
        assert any("Rörelsedetektor" in p for p in places)
        assert all(e["object"] is None for e in sensor_events)
        assert all(e["activity"] == "Sensor aktiverad" for e in sensor_events)
        assert all(e["is_sensor"] == 1 for e in sensor_events)

        # the camera capture must still get its cartoon image attached,
        # exactly like the human-reported signal events already do
        camera_event = next(e for e in sensor_events if "Kamera" in e["place"])
        attachments = db_module.list_attachments_for_message(conn, camera_event["message_id"])
        assert len(attachments) == 1


def test_importing_two_sensor_days_back_to_back_never_flags_them_as_duplicates():
    """Regression: importing multiple days' sensor events within the
    same real-world session (all logged with a created_at only seconds
    apart) used to risk a false-positive duplicate match whenever two
    days cycled to the same sensor place -- is_sensor now exempts them
    from duplicate detection entirely (see duplicates.py)."""
    client = create_app().test_client()
    client.post("/events/import/demo/sensor-toggle", data={"include_sensors": "1"})

    client.post("/events/import/training/1")
    client.post("/events/import/training/5")  # cycles back to day 1's places (period 4)
    client.get("/events?since=all")  # triggers _compute_summary -> classify_duplicate_events

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        sensor_events = [e for e in events if e["is_sensor"]]
        assert len(sensor_events) == 6
        assert all(e["is_duplicate"] == 0 for e in sensor_events)


def test_demo_clear_route_removes_demo_events_and_clears_the_header_badge():
    client = create_app().test_client()
    client.post("/events/import/training/1")

    with_demo = client.get("/events")
    assert "DEMO-LÄGE AKTIVT".encode() in with_demo.data

    resp = client.post("/events/import/demo/clear", follow_redirects=True)
    assert resp.status_code == 200
    # The demo tab's own explanatory copy mentions "DEMO-LÄGE AKTIVT" in
    # plain text, so check for the actual badge markup here, not the bare
    # phrase (which would false-positive against that copy).
    assert b'class="badge badge-demo"' not in resp.data

    after = client.get("/events")
    assert "DEMO-LÄGE AKTIVT".encode() not in after.data
    with db_module.get_connection() as conn:
        assert db_module.has_demo_events(conn) is False


def test_demo_clear_route_also_removes_demo_seeded_adjacent_reports():
    """Regression: importing a training day also seeds adjacent-unit
    status reports (2.Kompani/3.Kompani, see adjacent_status.json) -- these
    used to survive "clear demo data" entirely, leaving stale demo status
    visible in the header badge / Sammanställd hotbedömning's adjacent-unit
    card even after every demo event was gone."""
    client = create_app().test_client()
    client.post("/events/import/training/1")

    with db_module.get_connection() as conn:
        before = db_module.list_latest_adjacent_reports_per_unit(conn)
    assert before, "expected demo import to have seeded adjacent-unit reports"

    client.post("/events/import/demo/clear")

    with db_module.get_connection() as conn:
        after = db_module.list_latest_adjacent_reports_per_unit(conn)
    assert after == []


def test_demo_clear_route_leaves_a_genuinely_received_adjacent_report_untouched():
    with db_module.get_connection() as conn:
        real_id = db_module.insert_adjacent_report(
            conn, signal_timestamp=1700000000000, sender_number="+15551234567",
            sender_name="3.Kompani", unit_name="3.Kompani", body="Verklig status",
        )

    client = create_app().test_client()
    client.post("/events/import/training/1")
    client.post("/events/import/demo/clear")

    with db_module.get_connection() as conn:
        reports = db_module.list_adjacent_reports(conn)
    assert any(r["id"] == real_id for r in reports)


def test_demo_clear_route_leaves_non_demo_events_untouched():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db_module.insert_event(conn, message_id=message_id, fields={"place": "Verklig"})

    client = create_app().test_client()
    client.post("/events/import/training/1")
    client.post("/events/import/demo/clear")

    with db_module.get_connection() as conn:
        assert db_module.get_event(conn, event_id) is not None


def test_save_groups_route_persists_all_four_group_names():
    client = create_app().test_client()
    resp = client.post(
        "/settings/groups",
        data={
            "watch_group": "Ny bevakningsgrupp",
            "report_group": "Ny rapportgrupp",
            "recurring_group": "Ny återkommande-grupp",
            "sensor_group": "Ny sensorgrupp",
        },
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_watch_group_name(conn) == "Ny bevakningsgrupp"
        assert db_module.get_report_group_name(conn) == "Ny rapportgrupp"
        assert db_module.get_recurring_group_name(conn) == "Ny återkommande-grupp"
        assert db_module.get_sensor_group_name(conn) == "Ny sensorgrupp"


def test_save_ollama_port_route_persists_a_valid_port():
    client = create_app().test_client()
    resp = client.post("/settings/ollama", data={"ollama_port": "11500"}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_ollama_port(conn) == "11500"


def test_save_ollama_port_route_rejects_a_non_numeric_or_out_of_range_port():
    client = create_app().test_client()

    for bad_port in ("not-a-port", "0", "99999"):
        resp = client.post("/settings/ollama", data={"ollama_port": bad_port}, follow_redirects=True)
        assert resp.status_code == 200
        assert "Ogiltig port".encode() in resp.data

    with db_module.get_connection() as conn:
        assert db_module.get_ollama_port(conn) != "0"


def test_reset_event_log_route_also_clears_manually_catalogued_entities():
    """"Rensa händelselogg" must take the whole Personer/fordon/objekt
    database with it, including a manually added (not auto-extracted)
    record -- it exists to track who/what recurs in this event log, so
    it shouldn't outlive the log itself."""
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "object", "label": "Katalogiserad post"})
    client.post("/events/new", data={"place": "X", "marks": "Person 1 (A – Age: 30-40)"})

    resp = client.post("/database/reset-events", follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.list_entities(conn) == []


def test_reset_database_route_restores_map_settings_to_their_defaults():
    """"Rensa allt" must leave Kartleverantör, områdesstorlek, and
    Kartcentrum back at their out-of-the-box defaults, not stuck on
    whatever this unit had configured before the reset."""
    client = create_app().test_client()
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    client.post("/settings/map-cache-area-size", data={"area_size": "large"})
    client.post("/settings/map-center", data={"lat": "58.0", "lon": "12.0"})

    resp = client.post("/database/reset", follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_source(conn) == db_module.MAP_TILE_SOURCE_LANTMATERIET_FTP
        assert db_module.get_map_cache_area_size(conn) == config.MAP_CACHE_DEFAULT_AREA_SIZE
        assert db_module.get_map_center(conn) == config.DEFAULT_MAP_CENTER
        assert db_module.has_custom_map_center(conn) is False

    settings_resp = client.get("/settings")
    assert b'value="lantmateriet_ftp" checked' in settings_resp.data
    assert b'value="small" checked' in settings_resp.data
    assert "Inget kartcentrum sparat än".encode() in settings_resp.data


def test_stop_server_route_logs_and_schedules_a_process_exit(monkeypatch):
    """The route must respond with the confirmation page (and log the
    action) before the process actually exits -- the exit itself happens
    from a background thread a moment later, not inline in the handler,
    so the response has time to reach the browser. os._exit() is swapped
    for a fake here since actually calling it would kill the test run."""
    exit_calls = []
    monkeypatch.setattr(routes_module, "_exit_process", lambda: exit_calls.append(True))
    monkeypatch.setattr(routes_module.time, "sleep", lambda seconds: None)

    threads_before = set(threading.enumerate())
    client = create_app().test_client()
    resp = client.post("/settings/stop-server")

    assert resp.status_code == 200
    assert "Servern stoppas".encode() in resp.data

    with db_module.get_connection() as conn:
        log = db_module.list_system_log(conn, limit=5)
    assert any(entry["event_type"] == "server_stop" for entry in log)

    new_threads = set(threading.enumerate()) - threads_before
    for thread in new_threads:
        thread.join(timeout=2)
    assert exit_calls == [True]


def test_save_map_center_route_persists_a_valid_center():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-center", data={"lat": "59.33", "lon": "18.06"}, follow_redirects=True
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        lat, lon = db_module.get_map_center(conn)
    assert lat == pytest.approx(59.33)
    assert lon == pytest.approx(18.06)


def test_save_map_center_route_accepts_an_mgrs_reference():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-center", data={"mgrs": "34VCL3339280121"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "Kartcentrum sparat".encode() in resp.data
    with db_module.get_connection() as conn:
        lat, lon = db_module.get_map_center(conn)
    assert lat == pytest.approx(59.326944, abs=1e-3)
    assert lon == pytest.approx(18.071667, abs=1e-3)


def test_save_map_center_route_rejects_an_invalid_mgrs_reference():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-center", data={"mgrs": "not a grid reference"}, follow_redirects=True
    )

    assert resp.status_code == 200
    assert "Ogiltig MGRS-referens".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.has_custom_map_center(conn) is False


def test_save_map_center_route_mgrs_field_takes_precedence_over_lat_lon():
    """Regression: a Kartcentrum form now offers both an MGRS field and
    separate lat/lon fields (see the "make it possible to set Kartcentrum
    either in MGRS or lat/long" feature) -- if a user fills in MGRS but a
    stale lat/lon value is still sitting in the form (e.g. left over from
    the page's own pre-fill), MGRS must be the one that's saved."""
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-center",
        data={"mgrs": "34VCL3339280121", "lat": "0", "lon": "0"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        lat, lon = db_module.get_map_center(conn)
    assert lat == pytest.approx(59.326944, abs=1e-3)
    assert lon == pytest.approx(18.071667, abs=1e-3)


def test_settings_page_shows_rensa_handelselogg_directly_below_rensa_allt():
    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    reset_all_pos = body.index("Rensa allt</button>")
    reset_events_pos = body.index("Rensa händelselogg</button>")
    enhet_pos = body.index('id="settings-enhet"')
    assert reset_all_pos < reset_events_pos < enhet_pos


def test_settings_page_groups_settings_into_named_expandable_sections():
    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    for section_id, heading in [
        ("settings-enhet", "Enhet"),
        ("settings-signalgrupper", "Signalgrupper"),
        ("settings-karta", "Kartinställningar"),
        ("settings-ai", "AI-inställningar"),
    ]:
        assert f'<details id="{section_id}">' in body
        assert heading in body

    # Lokala användare is the one group open by default -- the guest
    # QR code inside it needs to be visible without an extra click, since
    # it's typically glanced at live while onboarding a device, not
    # configured once and forgotten like the others.
    assert '<details id="settings-anvandare" open>' in body
    assert "Lokala användare" in body

    # Each group is an actual <details>/<summary> disclosure, not just a
    # div with a matching id -- collapsed by default (except Lokala
    # användare, see above) so the page stays scannable.
    assert body.count("<details") == 5
    assert body.count(' open>') == 1
    assert "<summary>" in body


def test_settings_page_enhet_group_contains_unit_name_and_adjacent_units():
    with db_module.get_connection() as conn:
        db_module.add_adjacent_unit(conn, "2.Pluton")

    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    enhet_start = body.index('id="settings-enhet"')
    enhet_end = body.index("</details>", enhet_start)
    enhet_section = body[enhet_start:enhet_end]
    assert 'id="unit_name"' in enhet_section
    assert "2.Pluton" in enhet_section


def test_settings_page_karta_group_contains_all_map_settings():
    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    karta_start = body.index('id="settings-karta"')
    karta_end = body.index("</details>", karta_start)
    karta_section = body[karta_start:karta_end]
    for needle in ('name="tile_mode"', 'name="tile_source"', 'name="area_size"', 'id="map_lat"', 'id="tile_url_template"'):
        assert needle in karta_section


def test_settings_page_anvandare_group_contains_guest_users_and_qr_code():
    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    anvandare_start = body.index('id="settings-anvandare"')
    anvandare_end = body.index("</details>", anvandare_start)
    anvandare_section = body[anvandare_start:anvandare_end]
    assert 'id="new_user_name"' in anvandare_section
    assert "Dela adress med gäster" in anvandare_section


def test_settings_page_shows_kartcentrum_as_mgrs_once_a_center_is_set():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.326944", "lon": "18.071667"})

    resp = client.get("/settings")

    assert resp.status_code == 200
    assert 'id="map_mgrs"'.encode() in resp.data
    assert 'value="34VCL3339280121"'.encode() in resp.data


def test_settings_page_shows_the_config_default_center_when_none_is_set():
    client = create_app().test_client()
    resp = client.get("/settings")

    assert resp.status_code == 200
    assert 'id="map_mgrs"'.encode() in resp.data
    assert "Inget kartcentrum sparat än".encode() in resp.data
    assert b"Rensa kartcentrum" not in resp.data


def test_settings_page_tile_count_ignores_stray_tiles_from_a_previous_center():
    from signal_events import tiles as tiles_module

    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.326944", "lon": "18.071667"})
    with db_module.get_connection() as conn:
        radius_km = db_module.get_map_cache_radius_km(conn)
    expected = tiles_module.expected_tile_count(
        59.326944, 18.071667, radius_km, config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM
    )
    # A leftover tile far from the configured center -- e.g. from before
    # Kartcentrum was moved -- should not inflate the "cached" count past
    # what's actually covering the current area.
    stray_path = tiles_module.tile_path(config.TILE_CACHE_DIR, 10, 0, 0)
    stray_path.parent.mkdir(parents=True)
    stray_path.write_bytes(b"x")

    resp = client.get("/settings")

    assert resp.status_code == 200
    assert f"0 av {expected} kartrutor cachade".encode() in resp.data


def test_save_map_center_route_rejects_invalid_input():
    client = create_app().test_client()

    for bad_lat, bad_lon in [("not-a-number", "18.06"), ("999", "18.06"), ("59.33", "999")]:
        resp = client.post(
            "/settings/map-center", data={"lat": bad_lat, "lon": bad_lon}, follow_redirects=True
        )
        assert resp.status_code == 200
        assert "Ogiltig position".encode() in resp.data

    with db_module.get_connection() as conn:
        assert db_module.has_custom_map_center(conn) is False
        assert db_module.get_map_center(conn) == config.DEFAULT_MAP_CENTER


def test_save_map_center_route_rejects_latitudes_too_close_to_the_poles():
    """Regression: ±90 is a mathematically valid latitude but Web
    Mercator (which tiles.py's tile math assumes) is undefined right at
    the poles -- saving one used to crash every later page that computes
    a tile count for the configured center (Inställningar, Kart-vy) with
    a ValueError from math.log, not just reject the save. Anything past
    ±85.0511 (the same practical limit Leaflet/OSM/Google Maps clamp to)
    must be rejected up front instead."""
    client = create_app().test_client()

    for bad_lat in ["90", "-90", "89", "-89", "85.1", "-85.1"]:
        resp = client.post(
            "/settings/map-center", data={"lat": bad_lat, "lon": "18.06"}, follow_redirects=True
        )
        assert resp.status_code == 200
        assert "Ogiltig position".encode() in resp.data

    with db_module.get_connection() as conn:
        assert db_module.has_custom_map_center(conn) is False

    # A center right at the edge of the usable range must still work,
    # and the settings page (which computes a tile count for it) must
    # not crash when rendered afterwards.
    resp = client.post(
        "/settings/map-center", data={"lat": "85.0", "lon": "18.06"}, follow_redirects=True
    )
    assert resp.status_code == 200
    assert "Kartcentrum sparat".encode() in resp.data


def test_clear_map_center_route_removes_the_stored_center():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.post("/settings/map-center/clear", follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.has_custom_map_center(conn) is False
        assert db_module.get_map_center(conn) == config.DEFAULT_MAP_CENTER


def test_save_map_tile_url_route_persists_a_valid_template():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-tile-url",
        data={"tile_url_template": "https://api.maptiler.com/x/{z}/{x}/{y}.png?key=abc"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_url_template(conn) == "https://api.maptiler.com/x/{z}/{x}/{y}.png?key=abc"


def test_save_map_tile_url_route_rejects_a_url_missing_placeholders():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-tile-url",
        data={"tile_url_template": "https://example.com/tiles.png"},
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Ogiltig kart-URL".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_url_template(conn) == config.DEFAULT_TILE_URL_TEMPLATE


def test_save_map_tile_url_route_with_empty_value_reverts_to_the_default():
    client = create_app().test_client()
    client.post(
        "/settings/map-tile-url", data={"tile_url_template": "https://example.com/{z}/{x}/{y}.png"}
    )

    resp = client.post("/settings/map-tile-url", data={"tile_url_template": ""}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_url_template(conn) == config.DEFAULT_TILE_URL_TEMPLATE


def test_download_map_tiles_route_refuses_the_default_center_without_confirmation(monkeypatch):
    """Without an explicit Kartcentrum, a download would silently cache
    tiles around config.DEFAULT_MAP_CENTER (Stockholm Palace) -- almost
    never the real skyddsobjekt -- so it must be refused unless the
    "confirm_default_center" checkbox was ticked."""
    monkeypatch.setattr(
        tiles, "download_area",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not download without confirmation")),
    )

    client = create_app().test_client()
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert "Kartcentrum är inte angivet".encode() in resp.data


def test_download_map_tiles_route_uses_the_config_default_center_when_confirmed(monkeypatch):
    """get_map_center's fallback means a download can proceed at
    config.DEFAULT_MAP_CENTER once the operator has explicitly confirmed
    that's what they want (see the refusal test above for the
    unconfirmed case)."""
    calls = []

    def fake_download_area(center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir, tile_url_template=None):
        calls.append((center_lat, center_lon))
        return (0, 0, 0, False)

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(tiles, "download_area", fake_download_area)
    monkeypatch.setattr(routes_module.threading, "Thread", ImmediateThread)

    client = create_app().test_client()
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    resp = client.post(
        "/settings/map-center/download", data={"confirm_default_center": "1"}, follow_redirects=True,
    )

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(config.DEFAULT_MAP_CENTER[0])
    assert calls[0][1] == pytest.approx(config.DEFAULT_MAP_CENTER[1])


def test_download_map_tiles_route_rejects_when_the_area_exceeds_the_tile_count_cap(monkeypatch):
    monkeypatch.setattr(config, "MAP_CACHE_MAX_TILE_COUNT", 0)
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert "överstiger".encode() in resp.data


def test_download_map_tiles_route_refuses_a_second_concurrent_download():
    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})

    assert routes_module._tile_download_lock.acquire(blocking=False)
    try:
        resp = client.post("/settings/map-center/download", follow_redirects=True)
        assert resp.status_code == 200
        assert "En nedladdning pågår redan".encode() in resp.data
    finally:
        routes_module._tile_download_lock.release()


def test_download_map_tiles_route_runs_the_download_and_logs_completion(monkeypatch):
    """Runs the background thread synchronously (via a fake Thread) so the
    test is deterministic -- verifies the route wires tiles.download_area
    correctly and logs a system event on completion, without ever hitting
    the real network."""
    calls = []

    def fake_download_area(center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir, tile_url_template=None):
        calls.append((center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir, tile_url_template))
        return (3, 1, 0, False)

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(tiles, "download_area", fake_download_area)
    monkeypatch.setattr(routes_module.threading, "Thread", ImmediateThread)

    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})
    client.post("/settings/map-tile-source", data={"tile_source": "url"})

    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(59.33)
    assert calls[0][1] == pytest.approx(18.06)
    with db_module.get_connection() as conn:
        entries = db_module.list_system_log(conn)
    event_types = [e["event_type"] for e in entries]
    assert "map_tiles_download_started" in event_types
    assert "map_tiles_download_finished" in event_types
    assert not routes_module._tile_download_lock.locked()


def test_download_map_tiles_route_uses_lantmateriet_ftp_when_that_source_is_configured(monkeypatch):
    calls = []

    def fake_extract(center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir):
        calls.append((center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir))
        return (42, 0)

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: True)
    monkeypatch.setattr(lantmateriet_ftp, "extract_area_to_cache", fake_extract)
    monkeypatch.setattr(
        tiles, "download_area",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not use the URL downloader for this source")),
    )
    monkeypatch.setattr(routes_module.threading, "Thread", ImmediateThread)

    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})
    client.post("/settings/map-tile-source", data={"tile_source": "lantmateriet_ftp"})

    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert len(calls) == 1
    assert calls[0][0] == pytest.approx(59.33)
    with db_module.get_connection() as conn:
        entries = db_module.list_system_log(conn)
    event_types = [e["event_type"] for e in entries]
    assert "map_tiles_download_finished" in event_types
    finished = next(e for e in entries if e["event_type"] == "map_tiles_download_finished")
    assert "lantmateriet_ftp" in finished["detail"]
    assert not routes_module._tile_download_lock.locked()


def test_download_map_tiles_route_rejects_lantmateriet_ftp_when_gdal_is_missing(monkeypatch):
    monkeypatch.setattr(lantmateriet_ftp, "gdal_available", lambda: False)

    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})
    client.post("/settings/map-tile-source", data={"tile_source": "lantmateriet_ftp"})

    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert "GDAL".encode() in resp.data
    assert not routes_module._tile_download_lock.locked()


def test_save_map_tile_source_route_persists_a_valid_source():
    client = create_app().test_client()
    resp = client.post(
        "/settings/map-tile-source", data={"tile_source": "lantmateriet_ftp"}, follow_redirects=True
    )

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_source(conn) == db_module.MAP_TILE_SOURCE_LANTMATERIET_FTP


def test_save_map_tile_source_route_rejects_an_unknown_source():
    client = create_app().test_client()
    resp = client.post("/settings/map-tile-source", data={"tile_source": "wmts"}, follow_redirects=True)

    assert resp.status_code == 200
    assert "Ogiltig kartkälla".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_source(conn) == db_module.MAP_TILE_SOURCE_LANTMATERIET_FTP


def test_save_map_cache_area_size_route_persists_a_valid_size():
    client = create_app().test_client()
    resp = client.post("/settings/map-cache-area-size", data={"area_size": "small"}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_cache_area_size(conn) == "small"


def test_save_map_cache_area_size_route_rejects_an_unknown_size():
    client = create_app().test_client()
    resp = client.post("/settings/map-cache-area-size", data={"area_size": "huge"}, follow_redirects=True)

    assert resp.status_code == 200
    assert "Ogiltig områdesstorlek".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.get_map_cache_area_size(conn) == "small"


def test_download_map_tiles_route_uses_the_configured_area_size(monkeypatch):
    """Regression: the download must use whichever radius the selected
    area-size preset resolves to, not a fixed constant -- verified by
    switching to "small" (0.5 km) and checking that's what actually gets
    passed through to tiles.download_area."""
    calls = []

    def fake_download_area(center_lat, center_lon, radius_km, min_zoom, max_zoom, cache_dir, tile_url_template=None):
        calls.append(radius_km)
        return (0, 0, 0, False)

    class ImmediateThread:
        def __init__(self, target=None, args=(), daemon=None):
            self._target = target
            self._args = args

        def start(self):
            self._target(*self._args)

    monkeypatch.setattr(tiles, "download_area", fake_download_area)
    monkeypatch.setattr(routes_module.threading, "Thread", ImmediateThread)

    client = create_app().test_client()
    client.post("/settings/map-center", data={"lat": "59.33", "lon": "18.06"})
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    client.post("/settings/map-cache-area-size", data={"area_size": "small"})

    resp = client.post("/settings/map-center/download", follow_redirects=True)

    assert resp.status_code == 200
    assert calls == [pytest.approx(0.5)]


def test_map_tile_route_forces_cache_only_when_source_is_lantmateriet_ftp_even_in_online_mode(monkeypatch):
    """Online mode normally fetches a missing tile live -- but that's far
    too slow through this source (see lantmateriet_ftp.py), so it must
    never even attempt it here, regardless of the configured mode."""
    monkeypatch.setattr(
        tiles, "fetch_tile_on_demand",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch live through this source")),
    )
    client = create_app().test_client()
    with db_module.get_connection() as conn:
        db_module.set_map_tile_source(conn, db_module.MAP_TILE_SOURCE_LANTMATERIET_FTP)
        assert db_module.get_map_tile_mode(conn) == db_module.MAP_TILE_MODE_ONLINE

    resp = client.get("/tiles/8/999999/999999.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_map_tile_route_serves_a_cached_tile_file():
    """Default map tile mode is "online", but a cache hit is served
    straight from disk either way -- fetch_tile_on_demand only reaches
    out to the network for a miss."""
    tile_bytes = b"\x89PNG\r\n\x1a\nfake-cached-tile"
    path = tiles.tile_path(config.TILE_CACHE_DIR, 8, 100, 200)
    path.parent.mkdir(parents=True)
    path.write_bytes(tile_bytes)

    client = create_app().test_client()
    resp = client.get("/tiles/8/100/200.png")

    assert resp.status_code == 200
    assert resp.data == tile_bytes


def test_map_tile_route_in_online_mode_fetches_a_missing_tile_live(monkeypatch):
    calls = []

    def fake_fetch_on_demand(zoom, x, y, cache_dir, tile_url_template):
        calls.append((zoom, x, y, tile_url_template))
        return b"\x89PNG\r\n\x1a\nlive-fetched-tile"

    monkeypatch.setattr(tiles, "fetch_tile_on_demand", fake_fetch_on_demand)

    client = create_app().test_client()
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    resp = client.get("/tiles/8/999999/999999.png")

    assert resp.status_code == 200
    assert resp.data == b"\x89PNG\r\n\x1a\nlive-fetched-tile"
    assert calls == [(8, 999999, 999999, config.DEFAULT_TILE_URL_TEMPLATE)]


def test_map_tile_route_in_online_mode_falls_back_to_blank_when_the_live_fetch_fails(monkeypatch):
    monkeypatch.setattr(tiles, "fetch_tile_on_demand", lambda *a, **k: None)

    client = create_app().test_client()
    client.post("/settings/map-tile-source", data={"tile_source": "url"})
    resp = client.get("/tiles/8/999999/999999.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_map_tile_route_in_local_mode_never_fetches_live_and_serves_blank_when_not_cached(monkeypatch):
    def fake_fetch_on_demand(*args, **kwargs):
        raise AssertionError("local mode must never fetch tiles live")

    monkeypatch.setattr(tiles, "fetch_tile_on_demand", fake_fetch_on_demand)

    client = create_app().test_client()
    with db_module.get_connection() as conn:
        db_module.set_map_tile_mode(conn, db_module.MAP_TILE_MODE_LOCAL)

    resp = client.get("/tiles/8/999999/999999.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_map_tile_route_in_local_mode_still_serves_a_cached_tile_file():
    tile_bytes = b"\x89PNG\r\n\x1a\nfake-cached-tile"
    path = tiles.tile_path(config.TILE_CACHE_DIR, 8, 100, 200)
    path.parent.mkdir(parents=True)
    path.write_bytes(tile_bytes)

    client = create_app().test_client()
    with db_module.get_connection() as conn:
        db_module.set_map_tile_mode(conn, db_module.MAP_TILE_MODE_LOCAL)

    resp = client.get("/tiles/8/100/200.png")

    assert resp.status_code == 200
    assert resp.data == tile_bytes


def test_map_tile_route_serves_the_cartoon_demo_map_once_demo_events_exist(monkeypatch):
    """Demo/training event positions are fictional (see
    demo/generate_training_days.py) -- once any exist, every tile request
    should get the procedural cartoon dummy map (demo_map.py) instead of
    reaching out to whatever real provider is configured, regardless of
    the Inställningar tile mode, so trying the demo never needs a real
    tile provider token/network at all."""
    calls = []
    monkeypatch.setattr(demo_map, "generate_demo_tile", lambda z, x, y: calls.append((z, x, y)) or b"cartoon-bytes")
    monkeypatch.setattr(
        tiles, "fetch_tile_on_demand",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch a real tile in demo mode")),
    )

    client = create_app().test_client()
    client.post("/events/import/training/1")

    resp = client.get("/tiles/8/100/200.png")

    assert resp.status_code == 200
    assert resp.data == b"cartoon-bytes"
    assert calls == [(8, 100, 200)]


def test_map_tile_route_uses_the_real_provider_when_no_demo_events_exist(monkeypatch):
    monkeypatch.setattr(
        demo_map, "generate_demo_tile",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not use the cartoon map without demo events")),
    )

    client = create_app().test_client()
    resp = client.get("/tiles/8/999999/999999.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"


def test_save_map_tile_mode_route_persists_a_valid_mode():
    client = create_app().test_client()
    resp = client.post("/settings/map-tile-mode", data={"tile_mode": "local"}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_mode(conn) == db_module.MAP_TILE_MODE_LOCAL


def test_save_map_tile_mode_route_rejects_an_unknown_mode():
    client = create_app().test_client()
    resp = client.post("/settings/map-tile-mode", data={"tile_mode": "offline"}, follow_redirects=True)

    assert resp.status_code == 200
    assert "Ogiltigt kartläge".encode() in resp.data
    with db_module.get_connection() as conn:
        assert db_module.get_map_tile_mode(conn) == db_module.MAP_TILE_MODE_ONLINE


def test_purge_blocked_map_tiles_route_removes_only_blocked_tiles(monkeypatch):
    monkeypatch.setattr(tiles, "_BLOCKED_TILE_SHA256", hashlib.sha256(b"blocked").hexdigest())

    blocked_path = tiles.tile_path(config.TILE_CACHE_DIR, 10, 1, 1)
    blocked_path.parent.mkdir(parents=True)
    blocked_path.write_bytes(b"blocked")

    real_path = tiles.tile_path(config.TILE_CACHE_DIR, 10, 2, 2)
    real_path.parent.mkdir(parents=True)
    real_path.write_bytes(b"real-tile")

    client = create_app().test_client()
    resp = client.post("/settings/map-center/purge-blocked", follow_redirects=True)

    assert resp.status_code == 200
    assert "1 blockerade".encode() in resp.data
    assert not blocked_path.exists()
    assert real_path.exists()
    with db_module.get_connection() as conn:
        event_types = [e["event_type"] for e in db_module.list_system_log(conn)]
    assert "map_tiles_purge_blocked" in event_types


def test_report_send_uses_the_configured_report_group_name():
    from unittest.mock import patch

    from signal_events import signal_client

    with db_module.get_connection() as conn:
        db_module.set_report_group_name(conn, "Anpassad rapportgrupp")

    client = create_app().test_client()
    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/report/send", data={"since": "7d"})

    assert resp.status_code == 302
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "Anpassad rapportgrupp"


def test_summary_send_uses_the_configured_report_group_name():
    from unittest.mock import patch

    from signal_events import signal_client

    with db_module.get_connection() as conn:
        db_module.set_report_group_name(conn, "Anpassad rapportgrupp")

    client = create_app().test_client()
    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/summary/send", data={"since": "7d"})

    assert resp.status_code == 302
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "Anpassad rapportgrupp"


def test_entities_send_watchlist_uses_the_configured_recurring_group_name():
    from unittest.mock import patch

    from signal_events import signal_client

    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Bevakad person"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]
        db_module.set_recurring_group_name(conn, "Anpassad återkommande-grupp")
        db_module.update_entity(conn, entity_id, {"watchlist": True})

    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/entities/send-watchlist")

    assert resp.status_code == 302
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "Anpassad återkommande-grupp"


def test_entities_send_watchlist_with_nothing_to_send_flashes_an_error():
    from unittest.mock import patch

    from signal_events import signal_client

    client = create_app().test_client()
    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/entities/send-watchlist", follow_redirects=True)

    assert resp.status_code == 200
    mock_send.assert_not_called()
    assert "Inga poster på bevakningslistan".encode() in resp.data


def test_entities_send_watchlist_includes_entities_recurring_by_event_count_alone():
    from unittest.mock import patch

    from signal_events import signal_client

    client = create_app().test_client()
    client.post("/events/new", data={"place": "X", "marks": "Fordon 1 (R – Registration: ABC123)"})
    client.post("/events/new", data={"place": "Y", "marks": "Fordon 1 (R – Registration: ABC123)"})

    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/entities/send-watchlist")

    assert resp.status_code == 302
    mock_send.assert_called_once()


def test_set_entity_watchlist_route_toggles_the_flag_and_redirects_to_the_same_filters():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    resp = client.post(
        f"/entities/{entity_id}/watchlist", data={"watchlist": "1", "type": "person", "q": "kän"},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/entities?")
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["watchlist"] == 1

    resp = client.post(f"/entities/{entity_id}/watchlist", data={})
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["watchlist"] == 0


def test_save_reports_dir_route_persists_a_custom_folder(tmp_path):
    client = create_app().test_client()
    custom = tmp_path / "arkiv"
    resp = client.post("/settings/reports-dir", data={"reports_dir": str(custom)}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_reports_dir(conn) == custom
    assert custom.exists()  # created eagerly so the setting page can show it as usable


def test_save_reports_dir_route_with_empty_value_reverts_to_the_default(tmp_path):
    client = create_app().test_client()
    client.post("/settings/reports-dir", data={"reports_dir": str(tmp_path / "arkiv")})

    resp = client.post("/settings/reports-dir", data={"reports_dir": ""}, follow_redirects=True)

    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_reports_dir(conn) == config.REPORTS_DIR


def test_settings_page_enhet_group_shows_the_configured_reports_dir():
    resp = create_app().test_client().get("/settings")
    body = resp.data.decode("utf-8")
    enhet_section = body[body.index('id="settings-enhet"'):body.index("</details>", body.index('id="settings-enhet"'))]
    assert 'name="reports_dir"' in enhet_section
    assert str(config.REPORTS_DIR) in enhet_section


def test_summary_save_pdf_also_writes_a_copy_to_the_configured_reports_dir(tmp_path):
    """Task #175's dual save+download behavior: every generated report is
    persisted under the Rapportmapp on top of the normal browser
    download -- this app runs on the user's own laptop, so keeping a
    predictable local archive is meaningful in a way it wouldn't be for
    a hosted service."""
    custom = tmp_path / "arkiv"
    client = create_app().test_client()
    client.post("/settings/reports-dir", data={"reports_dir": str(custom)})

    resp = client.post(
        "/summary/save-pdf", data={"since": "7d", "narrative_text": "Lägestext."},
    )
    assert resp.status_code == 200

    saved_files = list(custom.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == resp.data


def test_entities_save_watchlist_writes_to_reports_dir_and_streams_the_same_bytes(tmp_path):
    custom = tmp_path / "arkiv"
    client = create_app().test_client()
    client.post("/settings/reports-dir", data={"reports_dir": str(custom)})
    client.post("/entities/new", data={"entity_type": "person", "label": "Bevakad person"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]
        db_module.update_entity(conn, entity_id, {"watchlist": True})

    resp = client.post("/entities/save-watchlist", data={"format": "text"})

    assert resp.status_code == 200
    assert "_bevakningslista.md" in resp.headers.get("Content-Disposition", "")
    assert b"Bevakad person" in resp.data
    saved_files = list(custom.iterdir())
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == resp.data


def test_entities_save_watchlist_pdf_format_streams_valid_pdf_bytes():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Bevakad person"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]
        db_module.update_entity(conn, entity_id, {"watchlist": True})

    resp = client.post("/entities/save-watchlist", data={"format": "pdf"})

    assert resp.status_code == 200
    assert "_bevakningslista.pdf" in resp.headers.get("Content-Disposition", "")
    assert resp.data.startswith(b"%PDF")


def test_entities_save_watchlist_with_nothing_to_save_flashes_an_error():
    client = create_app().test_client()
    resp = client.post("/entities/save-watchlist", data={"format": "text"}, follow_redirects=True)

    assert resp.status_code == 200
    assert "Inga poster på bevakningslistan att spara".encode() in resp.data


def test_entities_import_watchlist_creates_entities_and_flashes_a_summary():
    client = create_app().test_client()
    markdown = (
        "## Fordon\n"
        "### Fordon 1 (2 händelser)\n"
        "- Reg.nr: ABC123, Colour: Svart\n"
        "## Personer\n"
        "## Objekt\n"
    )
    upload = (io.BytesIO(markdown.encode("utf-8")), "bevakningslista.md")

    resp = client.post(
        "/entities/import-watchlist", data={"file": upload},
        content_type="multipart/form-data", follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Importerade 1 poster".encode() in resp.data
    with db_module.get_connection() as conn:
        rows = db_module.list_entities(conn, entity_type="vehicle")
    assert len(rows) == 1
    assert rows[0]["registration"] == "ABC123"
    assert rows[0]["watchlist"] == 1


def test_entities_import_watchlist_with_no_file_flashes_an_error():
    client = create_app().test_client()
    resp = client.post(
        "/entities/import-watchlist", data={}, content_type="multipart/form-data", follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Välj en bevakningslista-fil".encode() in resp.data


def test_entities_import_watchlist_with_unparsable_content_flashes_an_error():
    client = create_app().test_client()
    upload = (io.BytesIO(b"Not a bevakningslista at all."), "notes.txt")

    resp = client.post(
        "/entities/import-watchlist", data={"file": upload},
        content_type="multipart/form-data", follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Inga poster hittades i filen".encode() in resp.data


def test_entity_detail_post_saves_the_watchlist_checkbox():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    client.post(f"/entities/{entity_id}", data={"label": "Okänd", "watchlist": "1"})
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["watchlist"] == 1

    client.post(f"/entities/{entity_id}", data={"label": "Okänd"})
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["watchlist"] == 0


def test_settings_shows_no_qr_code_when_still_bound_to_localhost():
    """BIND_HOST is only set by cli.cmd_serve when the server actually
    starts -- a plain create_app() (as in every test here) leaves it
    unset, matching "not actually reachable from other devices"."""
    client = create_app().test_client()
    resp = client.get("/settings")

    assert "Servern är just nu bara nåbar".encode() in resp.data
    assert b"lan-qrcode.png" not in resp.data


def test_settings_shows_qr_code_when_bound_to_all_interfaces():
    app = create_app()
    app.config["BIND_HOST"] = "0.0.0.0"
    client = app.test_client()
    resp = client.get("/settings")

    assert b"lan-qrcode.png" in resp.data
    assert "Servern är just nu bara nåbar".encode() not in resp.data


def test_lan_qrcode_route_returns_a_png_when_reachable():
    app = create_app()
    app.config["BIND_HOST"] = "0.0.0.0"
    client = app.test_client()
    resp = client.get("/settings/lan-qrcode.png")

    assert resp.status_code == 200
    assert resp.mimetype == "image/png"
    assert resp.data[:8] == b"\x89PNG\r\n\x1a\n"


def test_lan_qrcode_route_404s_when_not_reachable():
    client = create_app().test_client()
    resp = client.get("/settings/lan-qrcode.png")
    assert resp.status_code == 404


def test_events_list_shows_a_tnr_column_before_the_tid_column():
    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db_module.insert_event(
            conn, message_id=message_id, fields={"event_time": "270600", "place": "A"}
        )

    client = create_app().test_client()
    resp = client.get("/events")

    assert resp.status_code == 200
    body = resp.data.decode("utf-8")
    assert body.index("<th>TNR</th>") < body.index("<th>Tid</th>")
    assert ">270600<" in body


def test_events_list_orders_by_created_at_not_by_event_time():
    """TNR identifies when the app received a report (created_at), not
    when the observation was made (event_time) -- so the list, which is
    labeled by TNR, must also order by created_at, newest first, even
    when that disagrees with the events' own event_time."""
    with db_module.get_connection() as conn:
        message_id_a = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_a = db_module.insert_event(
            conn, message_id=message_id_a, fields={"event_time": "270600", "place": "Norra grinden"}
        )
        message_id_b = db_module.insert_message(
            conn, signal_timestamp=2, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_b = db_module.insert_event(
            conn, message_id=message_id_b, fields={"event_time": "270900", "place": "Södra vägen"}
        )
        # A's created_at is later than B's -- the opposite of their
        # event_time order -- so this only passes if the list actually
        # sorts by created_at.
        conn.execute(
            "UPDATE events SET created_at = '2026-01-02T10:00:00+00:00' WHERE id = ?", (event_a,)
        )
        conn.execute(
            "UPDATE events SET created_at = '2026-01-01T10:00:00+00:00' WHERE id = ?", (event_b,)
        )

    client = create_app().test_client()
    resp = client.get("/events?since=all")
    text = resp.data.decode()

    # A's created_at is newer, so it must appear first even though its
    # event_time ("270600") is earlier than B's ("270900").
    assert text.index("Norra grinden") < text.index("Södra vägen")


# --- Entities (persons, vehicles, other objects) ----------------------------

def test_new_event_post_with_a_person_and_vehicle_composer_block_auto_links_entities():
    client = create_app().test_client()
    marks = (
        "Person 1 (A – Age: 30-40, B – Build: Muskulös); "
        "Fordon 1 (R – Registration: ABC123)"
    )
    resp = client.post(
        "/events/new", data={"place": "Norra grinden", "marks": marks},
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        assert len(events) == 1
        linked = db_module.list_entities_for_event(conn, events[0]["id"])
    types = sorted(row["entity_type"] for row in linked)
    assert types == ["person", "vehicle"]


def test_event_detail_page_shows_linked_entities():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X", "marks": "Person 1 (A – Age: 20)"})

    with db_module.get_connection() as conn:
        event_id = db_module.list_events(conn)[0]["id"]

    resp = client.get(f"/events/{event_id}")
    assert resp.status_code == 200
    assert "Person 1".encode() in resp.data


def test_list_entities_route_shows_created_entities_and_supports_type_filter():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X", "marks": "Fordon 1 (R – Registration: ABC123)"})

    resp = client.get("/entities")
    assert resp.status_code == 200
    assert "ABC123".encode() in resp.data

    resp_person_only = client.get("/entities?type=person")
    assert "ABC123".encode() not in resp_person_only.data


def test_new_entity_route_creates_a_manual_entity_and_can_link_it_to_an_event():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X"})
    with db_module.get_connection() as conn:
        event_id = db_module.list_events(conn)[0]["id"]

    resp = client.post(
        "/entities/new",
        data={
            "entity_type": "object", "label": "Kikare funnen vid stängslet",
            "link_event_id": str(event_id),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with db_module.get_connection() as conn:
        entities_list = db_module.list_entities(conn, entity_type="object")
        assert len(entities_list) == 1
        entity_id = entities_list[0]["id"]
        linked_events = db_module.list_events_for_entity(conn, entity_id)
    assert len(linked_events) == 1
    assert linked_events[0]["id"] == event_id


def test_new_entity_route_requires_a_label():
    client = create_app().test_client()
    resp = client.post("/entities/new", data={"entity_type": "person", "label": ""})
    assert resp.status_code == 302
    with db_module.get_connection() as conn:
        assert db_module.list_entities(conn) == []


def test_new_entity_route_stores_person_identity_fields_not_registration():
    client = create_app().test_client()
    client.post(
        "/entities/new",
        data={
            "entity_type": "person", "label": "Okänd man",
            "name": "Kalle Karlsson", "alias": "Kalles", "nationality": "Svensk",
            "date_of_birth": "1990-01-01",
            "registration": "ABC123",  # must be ignored for a person
        },
    )
    with db_module.get_connection() as conn:
        entity = db_module.list_entities(conn, entity_type="person")[0]
    assert entity["registration"] is None
    attrs = json.loads(entity["attributes"])
    assert attrs == {
        "Namn": "Kalle Karlsson", "Alias": "Kalles", "Nationalitet": "Svensk",
        "Födelsedatum": "1990-01-01",
    }


def test_new_entity_route_stores_vehicle_registration_not_identity_fields():
    client = create_app().test_client()
    client.post(
        "/entities/new",
        data={
            "entity_type": "vehicle", "label": "Vit skåpbil", "registration": "ABC 123",
            "name": "Kalle Karlsson",  # must be ignored for a vehicle
        },
    )
    with db_module.get_connection() as conn:
        entity = db_module.list_entities(conn, entity_type="vehicle")[0]
    assert entity["registration"] == "ABC123"
    assert entity["attributes"] is None


def test_entity_detail_post_updates_person_identity_fields_and_preserves_other_attributes():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X", "marks": "Person 1 (A – Age: 30-40)"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn, entity_type="person")[0]["id"]

    client.post(f"/entities/{entity_id}", data={
        "label": "Person 1", "name": "Kalle Karlsson", "alias": "Kalles",
    })

    with db_module.get_connection() as conn:
        entity = db_module.get_entity(conn, entity_id)
    attrs = json.loads(entity["attributes"])
    assert attrs["Namn"] == "Kalle Karlsson"
    assert attrs["Alias"] == "Kalles"
    assert attrs["Age"] == "30-40"  # untouched by the identity-field edit
    assert entity["registration"] is None


def test_entity_detail_post_clears_a_person_identity_field_left_blank():
    client = create_app().test_client()
    client.post("/entities/new", data={
        "entity_type": "person", "label": "Okänd", "alias": "Räven",
    })
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    client.post(f"/entities/{entity_id}", data={"label": "Okänd", "alias": ""})

    with db_module.get_connection() as conn:
        entity = db_module.get_entity(conn, entity_id)
    attrs = json.loads(entity["attributes"]) if entity["attributes"] else {}
    assert "Alias" not in attrs


def test_entity_detail_post_does_not_touch_registration_for_a_person():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    client.post(f"/entities/{entity_id}", data={
        "label": "Okänd", "registration": "ABC123",  # no registration field shown/sent for a person
    })

    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["registration"] is None


def test_entity_detail_post_still_updates_vehicle_registration():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "vehicle", "label": "Bil"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    client.post(f"/entities/{entity_id}", data={"label": "Bil", "registration": "XYZ 789"})

    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["registration"] == "XYZ789"


def test_entity_detail_route_shows_attributes_and_linked_events():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X", "marks": "Person 1 (A – Age: 30-40)"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    resp = client.get(f"/entities/{entity_id}")
    assert resp.status_code == 200
    assert "30-40".encode() in resp.data


def test_entity_detail_post_edits_label_and_notes():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "object", "label": "Först"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    client.post(f"/entities/{entity_id}", data={"label": "Sedan", "notes": "Ny anteckning"})

    with db_module.get_connection() as conn:
        entity = db_module.get_entity(conn, entity_id)
    assert entity["label"] == "Sedan"
    assert entity["notes"] == "Ny anteckning"


def test_link_entity_to_event_route_links_from_the_event_side():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X"})
    client.post("/entities/new", data={"entity_type": "object", "label": "Kikare"})
    with db_module.get_connection() as conn:
        event_id = db_module.list_events(conn)[0]["id"]
        entity_id = db_module.list_entities(conn)[0]["id"]

    resp = client.post(
        f"/events/{event_id}/link-entity", data={"entity_id": str(entity_id)},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        linked = db_module.list_entities_for_event(conn, event_id)
    assert len(linked) == 1
    assert linked[0]["link_source"] == "manual"


def test_unlink_entity_route_removes_the_link_without_deleting_the_entity():
    client = create_app().test_client()
    client.post("/events/new", data={"place": "X"})
    client.post("/entities/new", data={"entity_type": "object", "label": "Kikare"})
    with db_module.get_connection() as conn:
        event_id = db_module.list_events(conn)[0]["id"]
        entity_id = db_module.list_entities(conn)[0]["id"]
    client.post(f"/events/{event_id}/link-entity", data={"entity_id": str(entity_id)})

    resp = client.post(f"/entities/{entity_id}/unlink/{event_id}", follow_redirects=True)
    assert resp.status_code == 200

    with db_module.get_connection() as conn:
        assert db_module.list_entities_for_event(conn, event_id) == []
        assert db_module.get_entity(conn, entity_id) is not None


def test_delete_entity_route_removes_the_entity():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "object", "label": "Kikare"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    resp = client.post(f"/entities/{entity_id}/delete", follow_redirects=True)
    assert resp.status_code == 200
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id) is None


def test_entity_detail_shows_entities_seen_together_via_a_shared_event():
    client = create_app().test_client()
    marks = "Person 1 (A – Age: 30-40); Fordon 1 (R – Registration: ABC123)"
    client.post("/events/new", data={"place": "X", "marks": marks})

    with db_module.get_connection() as conn:
        person = next(e for e in db_module.list_entities(conn) if e["entity_type"] == "person")

    resp = client.get(f"/entities/{person['id']}")
    assert resp.status_code == 200
    assert "ABC123".encode() in resp.data


def test_new_entity_route_accepts_a_photo_upload():
    client = create_app().test_client()
    photo = (io.BytesIO(b"fake-image-bytes"), "person.jpg")

    resp = client.post(
        "/entities/new",
        data={"entity_type": "person", "label": "Okänd man", "photo": photo},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200

    with db_module.get_connection() as conn:
        entity = db_module.list_entities(conn)[0]
    assert entity["photo_path"] is not None
    assert Path(entity["photo_path"]).exists()


def test_entity_photo_route_uploads_and_serves_a_photo():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd man"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    photo = (io.BytesIO(b"fake-image-bytes"), "person.jpg")
    resp = client.post(
        f"/entities/{entity_id}/photo", data={"photo": photo},
        content_type="multipart/form-data", follow_redirects=True,
    )
    assert resp.status_code == 200

    file_resp = client.get(f"/entities/{entity_id}/photo/file")
    assert file_resp.status_code == 200
    assert file_resp.data == b"fake-image-bytes"


def test_entity_photo_route_replaces_the_previous_photo_file():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd man"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    first_photo = (io.BytesIO(b"first-bytes"), "first.jpg")
    client.post(
        f"/entities/{entity_id}/photo", data={"photo": first_photo},
        content_type="multipart/form-data",
    )
    with db_module.get_connection() as conn:
        first_path = Path(db_module.get_entity(conn, entity_id)["photo_path"])
    assert first_path.exists()

    second_photo = (io.BytesIO(b"second-bytes"), "second.jpg")
    client.post(
        f"/entities/{entity_id}/photo", data={"photo": second_photo},
        content_type="multipart/form-data",
    )

    assert not first_path.exists()
    file_resp = client.get(f"/entities/{entity_id}/photo/file")
    assert file_resp.data == b"second-bytes"


def test_delete_entity_photo_route_removes_the_file_and_clears_the_path():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd man"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    photo = (io.BytesIO(b"fake-image-bytes"), "person.jpg")
    client.post(
        f"/entities/{entity_id}/photo", data={"photo": photo},
        content_type="multipart/form-data",
    )
    with db_module.get_connection() as conn:
        photo_path = Path(db_module.get_entity(conn, entity_id)["photo_path"])
    assert photo_path.exists()

    resp = client.post(f"/entities/{entity_id}/photo/delete", follow_redirects=True)
    assert resp.status_code == 200
    assert not photo_path.exists()
    with db_module.get_connection() as conn:
        assert db_module.get_entity(conn, entity_id)["photo_path"] is None


def test_delete_entity_route_also_removes_the_photo_file_from_disk():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd man"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    photo = (io.BytesIO(b"fake-image-bytes"), "person.jpg")
    client.post(
        f"/entities/{entity_id}/photo", data={"photo": photo},
        content_type="multipart/form-data",
    )
    with db_module.get_connection() as conn:
        photo_path = Path(db_module.get_entity(conn, entity_id)["photo_path"])

    client.post(f"/entities/{entity_id}/delete", follow_redirects=True)

    assert not photo_path.exists()


def test_entity_photo_file_route_404s_when_there_is_no_photo():
    client = create_app().test_client()
    client.post("/entities/new", data={"entity_type": "person", "label": "Okänd man"})
    with db_module.get_connection() as conn:
        entity_id = db_module.list_entities(conn)[0]["id"]

    resp = client.get(f"/entities/{entity_id}/photo/file")
    assert resp.status_code == 404


def test_new_event_post_person_panel_identity_fields_are_stored_as_attributes():
    client = create_app().test_client()
    marks = (
        "Person 1 (Namn: Kalle Karlsson, Alias: Kalles, Nationalitet: Svensk, "
        "Födelsedatum: 1990-01-01)"
    )
    client.post("/events/new", data={"place": "X", "marks": marks})

    with db_module.get_connection() as conn:
        entity = db_module.list_entities(conn, entity_type="person")[0]
    attrs = json.loads(entity["attributes"])
    assert attrs["Namn"] == "Kalle Karlsson"
    assert attrs["Alias"] == "Kalles"
    assert attrs["Nationalitet"] == "Svensk"
    assert attrs["Födelsedatum"] == "1990-01-01"
