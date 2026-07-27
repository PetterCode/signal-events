"""Mostly unit tests for pure-Python helpers in webapp/routes.py that
don't need a Flask test client (this project tests routes via direct
helper calls and live smoke testing rather than a test client -- see
other test_* files for the same pattern). The exceptions are session
persistence across requests and a couple of end-to-end route behaviors
that can't be verified any other way."""

import json

from signal_events import config, db as db_module
from signal_events.webapp import create_app
from signal_events.webapp.routes import _clear_event_attachment_files


def test_clear_event_attachment_files_leaves_adjacent_subdir_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path)

    event_dir = tmp_path / "42"
    event_dir.mkdir()
    (event_dir / "photo.jpg").write_bytes(b"fake")

    adjacent_dir = tmp_path / "adjacent" / "7"
    adjacent_dir.mkdir(parents=True)
    (adjacent_dir / "status.pdf").write_bytes(b"fake")

    _clear_event_attachment_files()

    assert not event_dir.exists()
    assert (adjacent_dir / "status.pdf").exists()


def test_clear_event_attachment_files_tolerates_missing_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "does-not-exist")
    _clear_event_attachment_files()  # must not raise


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
    resp = client.get("/summary?since=all&download=pdf")

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


def test_summary_ai_tab_renders_the_generate_form_without_a_narrative_yet():
    client = create_app().test_client()
    resp = client.get("/summary/ai")

    assert resp.status_code == 200
    assert "Generera AI-sammanfattning".encode() in resp.data


def test_summary_narrative_post_renders_on_the_ai_tab_not_the_summary_page():
    from unittest.mock import MagicMock, patch

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"response": "En lugn period utan avvikelser."}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = False

    client = create_app().test_client()
    with patch("urllib.request.urlopen", return_value=mock_resp):
        resp = client.post("/summary/narrative", data={"since": "7d", "include_unreviewed": "0"})

    assert resp.status_code == 200
    assert "En lugn period utan avvikelser.".encode() in resp.data
    assert "AI-sammanfattning".encode() in resp.data


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


def test_summary_send_recurring_uses_the_configured_recurring_group_name():
    from unittest.mock import patch

    from signal_events import signal_client

    with db_module.get_connection() as conn:
        db_module.set_recurring_group_name(conn, "Anpassad återkommande-grupp")

    client = create_app().test_client()
    with patch.object(signal_client, "send_to_group_by_name") as mock_send:
        resp = client.post("/summary/send-recurring", data={"since": "7d"})

    assert resp.status_code == 302
    mock_send.assert_called_once()
    assert mock_send.call_args.args[0] == "Anpassad återkommande-grupp"


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


def test_events_list_orders_by_tnr_not_by_created_at():
    """Sensor-reported events (and any event with an accurate Stund) can
    arrive out of ingestion order relative to created_at -- the list must
    still show them by TNR (when they actually happened), newest first,
    not by when they were logged into the database."""
    with db_module.get_connection() as conn:
        message_id_a = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_a = db_module.insert_event(
            conn, message_id=message_id_a, fields={"event_time": "270600", "place": "A"}
        )
        message_id_b = db_module.insert_message(
            conn, signal_timestamp=2, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_b = db_module.insert_event(
            conn, message_id=message_id_b, fields={"event_time": "270900", "place": "B"}
        )
        # A's created_at is later than B's -- the opposite of their TNR
        # order -- so this only passes if the list actually sorts by TNR.
        conn.execute(
            "UPDATE events SET created_at = '2026-01-02T10:00:00+00:00' WHERE id = ?", (event_a,)
        )
        conn.execute(
            "UPDATE events SET created_at = '2026-01-01T10:00:00+00:00' WHERE id = ?", (event_b,)
        )

    client = create_app().test_client()
    resp = client.get("/events?since=all")
    text = resp.data.decode()

    assert text.index("270900") < text.index("270600")
