"""Mostly unit tests for pure-Python helpers in webapp/routes.py that
don't need a Flask test client (this project tests routes via direct
helper calls and live smoke testing rather than a test client -- see
other test_* files for the same pattern). The exceptions are session
persistence across requests and a couple of end-to-end route behaviors
that can't be verified any other way."""

import json
from pathlib import Path

from signal_events import config, db as db_module
from signal_events.webapp import create_app
from signal_events.webapp.routes import _build_ai_context, _clear_event_attachment_files


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
