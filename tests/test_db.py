import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from signal_events import config, db


def test_insert_and_list_event_round_trip():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn,
            signal_timestamp=1234,
            sender_number="+15550000",
            sender_name="Alice",
            body="3 trucks near the bridge at 14:30",
            raw_json=json.dumps({"ok": True}),
        )
        event_id = db.insert_event(
            conn,
            message_id=message_id,
            fields={
                "event_time": "14:30",
                "place": "the bridge",
                "count": "3",
                "object": "trucks",
                "reported_by": "Alice",
                "raw_text": "3 trucks near the bridge at 14:30",
            },
        )

    with db.get_connection() as conn:
        events = db.list_events(conn)
        assert len(events) == 1
        assert events[0]["id"] == event_id
        assert events[0]["needs_review"] == 1

        db.update_event(conn, event_id, {"needs_review": 0, "place": "Old Bridge"})

    with db.get_connection() as conn:
        reviewed = db.list_events(conn, needs_review=False)
        assert len(reviewed) == 1
        assert reviewed[0]["place"] == "Old Bridge"

        still_pending = db.list_events(conn, needs_review=True)
        assert len(still_pending) == 0


def test_message_dedup_by_signal_timestamp():
    with db.get_connection() as conn:
        assert db.message_exists(conn, 999) is False
        db.insert_message(
            conn, signal_timestamp=999, sender_number="+1", sender_name=None,
            body="hi", raw_json="{}",
        )
        assert db.message_exists(conn, 999) is True


def test_get_setting_returns_default_when_unset():
    with db.get_connection() as conn:
        assert db.get_setting(conn, "nope", default="fallback") == "fallback"
        assert db.get_setting(conn, "nope") is None


def test_set_setting_then_get_round_trip():
    with db.get_connection() as conn:
        db.set_setting(conn, "greeting", "hej")
    with db.get_connection() as conn:
        assert db.get_setting(conn, "greeting") == "hej"


def test_set_setting_overwrites_existing_value():
    with db.get_connection() as conn:
        db.set_setting(conn, "greeting", "hej")
        db.set_setting(conn, "greeting", "hallå")
    with db.get_connection() as conn:
        assert db.get_setting(conn, "greeting") == "hallå"


def test_unit_name_defaults_to_empty_string():
    with db.get_connection() as conn:
        assert db.get_unit_name(conn) == ""


def test_set_and_get_unit_name_strips_whitespace():
    with db.get_connection() as conn:
        db.set_unit_name(conn, "  Kompani 1  ")
    with db.get_connection() as conn:
        assert db.get_unit_name(conn) == "Kompani 1"


def test_group_names_default_to_config_values():
    from signal_events import config

    with db.get_connection() as conn:
        assert db.get_watch_group_name(conn) == config.WATCH_GROUP_NAME
        assert db.get_report_group_name(conn) == config.REPORT_GROUP_NAME
        assert db.get_recurring_group_name(conn) == config.RECURRING_GROUP_NAME
        assert db.get_sensor_group_name(conn) == config.SENSOR_GROUP_NAME


def test_set_and_get_group_names_override_the_config_defaults():
    with db.get_connection() as conn:
        db.set_watch_group_name(conn, "  Ny bevakningsgrupp  ")
        db.set_report_group_name(conn, "Ny rapportgrupp")
        db.set_recurring_group_name(conn, "Ny återkommande-grupp")
        db.set_sensor_group_name(conn, "Ny sensorgrupp")

        assert db.get_watch_group_name(conn) == "Ny bevakningsgrupp"
        assert db.get_report_group_name(conn) == "Ny rapportgrupp"
        assert db.get_recurring_group_name(conn) == "Ny återkommande-grupp"
        assert db.get_sensor_group_name(conn) == "Ny sensorgrupp"


def test_clearing_a_group_name_reverts_to_the_config_default():
    from signal_events import config

    with db.get_connection() as conn:
        db.set_watch_group_name(conn, "Ny bevakningsgrupp")
        db.set_watch_group_name(conn, "")
        assert db.get_watch_group_name(conn) == config.WATCH_GROUP_NAME


def _expected_default_ollama_port() -> str:
    import urllib.parse

    from signal_events import config

    return str(urllib.parse.urlsplit(config.OLLAMA_URL).port or 11434)


def test_ollama_port_defaults_to_the_port_in_config_ollama_url():
    with db.get_connection() as conn:
        assert db.get_ollama_port(conn) == _expected_default_ollama_port()


def test_set_and_get_ollama_port_overrides_the_default():
    with db.get_connection() as conn:
        db.set_ollama_port(conn, "  11500  ")
        assert db.get_ollama_port(conn) == "11500"


def test_clearing_the_ollama_port_reverts_to_the_config_default():
    with db.get_connection() as conn:
        db.set_ollama_port(conn, "11500")
        db.set_ollama_port(conn, "")
        assert db.get_ollama_port(conn) == _expected_default_ollama_port()


def test_map_center_defaults_to_the_config_default():
    with db.get_connection() as conn:
        assert db.get_map_center(conn) == config.DEFAULT_MAP_CENTER
        assert db.has_custom_map_center(conn) is False


def test_set_and_get_map_center_round_trip():
    with db.get_connection() as conn:
        db.set_map_center(conn, 59.33, 18.06)
        lat, lon = db.get_map_center(conn)
        assert lat == pytest.approx(59.33)
        assert lon == pytest.approx(18.06)
        assert db.has_custom_map_center(conn) is True


def test_clear_map_center_reverts_to_the_config_default():
    with db.get_connection() as conn:
        db.set_map_center(conn, 59.33, 18.06)
        db.clear_map_center(conn)
        assert db.get_map_center(conn) == config.DEFAULT_MAP_CENTER
        assert db.has_custom_map_center(conn) is False


def test_get_map_center_falls_back_to_the_config_default_for_an_unparsable_stored_value():
    with db.get_connection() as conn:
        db.set_setting(conn, db.MAP_CENTER_LAT_KEY, "not-a-number")
        db.set_setting(conn, db.MAP_CENTER_LON_KEY, "18.06")
        assert db.get_map_center(conn) == config.DEFAULT_MAP_CENTER
        assert db.has_custom_map_center(conn) is False


def test_map_tile_url_template_defaults_to_the_config_default():
    with db.get_connection() as conn:
        assert db.get_map_tile_url_template(conn) == config.DEFAULT_TILE_URL_TEMPLATE


def test_set_and_get_map_tile_url_template_overrides_the_default():
    with db.get_connection() as conn:
        db.set_map_tile_url_template(conn, "  https://api.maptiler.com/x/{z}/{x}/{y}.png?key=abc  ")
        assert db.get_map_tile_url_template(conn) == "https://api.maptiler.com/x/{z}/{x}/{y}.png?key=abc"


def test_clear_map_tile_url_template_reverts_to_the_config_default():
    with db.get_connection() as conn:
        db.set_map_tile_url_template(conn, "https://example.com/{z}/{x}/{y}.png")
        db.clear_map_tile_url_template(conn)
        assert db.get_map_tile_url_template(conn) == config.DEFAULT_TILE_URL_TEMPLATE


def test_map_tile_mode_defaults_to_online():
    with db.get_connection() as conn:
        assert db.get_map_tile_mode(conn) == db.MAP_TILE_MODE_ONLINE


def test_reports_dir_defaults_to_the_config_default():
    with db.get_connection() as conn:
        assert db.get_reports_dir(conn) == config.REPORTS_DIR


def test_set_and_get_reports_dir_overrides_the_default(tmp_path):
    custom = tmp_path / "custom-reports"
    with db.get_connection() as conn:
        db.set_reports_dir(conn, f"  {custom}  ")
        assert db.get_reports_dir(conn) == custom


def test_clear_reports_dir_reverts_to_the_config_default(tmp_path):
    with db.get_connection() as conn:
        db.set_reports_dir(conn, str(tmp_path / "custom-reports"))
        db.clear_reports_dir(conn)
        assert db.get_reports_dir(conn) == config.REPORTS_DIR


def test_set_and_get_map_tile_mode_round_trips():
    with db.get_connection() as conn:
        db.set_map_tile_mode(conn, db.MAP_TILE_MODE_LOCAL)
        assert db.get_map_tile_mode(conn) == db.MAP_TILE_MODE_LOCAL
        db.set_map_tile_mode(conn, db.MAP_TILE_MODE_ONLINE)
        assert db.get_map_tile_mode(conn) == db.MAP_TILE_MODE_ONLINE


def test_set_map_tile_mode_rejects_unknown_values():
    with db.get_connection() as conn:
        with pytest.raises(ValueError):
            db.set_map_tile_mode(conn, "offline")


def test_get_map_tile_mode_falls_back_to_online_for_an_unparsable_stored_value():
    with db.get_connection() as conn:
        db.set_setting(conn, db.MAP_TILE_MODE_KEY, "bogus")
        assert db.get_map_tile_mode(conn) == db.MAP_TILE_MODE_ONLINE


def test_map_tile_source_defaults_to_lantmateriet_ftp():
    with db.get_connection() as conn:
        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_LANTMATERIET_FTP


def test_set_and_get_map_tile_source_round_trips():
    with db.get_connection() as conn:
        db.set_map_tile_source(conn, db.MAP_TILE_SOURCE_LANTMATERIET_FTP)
        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_LANTMATERIET_FTP
        db.set_map_tile_source(conn, db.MAP_TILE_SOURCE_URL)
        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_URL


def test_set_map_tile_source_rejects_unknown_values():
    with db.get_connection() as conn:
        with pytest.raises(ValueError):
            db.set_map_tile_source(conn, "wmts")


def test_get_map_tile_source_falls_back_to_lantmateriet_ftp_for_an_unparsable_stored_value():
    with db.get_connection() as conn:
        db.set_setting(conn, db.MAP_TILE_SOURCE_KEY, "bogus")
        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_LANTMATERIET_FTP


def test_map_cache_area_size_defaults_to_small():
    with db.get_connection() as conn:
        assert db.get_map_cache_area_size(conn) == config.MAP_CACHE_DEFAULT_AREA_SIZE
        assert db.get_map_cache_area_size(conn) == "small"


def test_set_and_get_map_cache_area_size_round_trips():
    with db.get_connection() as conn:
        db.set_map_cache_area_size(conn, "small")
        assert db.get_map_cache_area_size(conn) == "small"
        db.set_map_cache_area_size(conn, "medium")
        assert db.get_map_cache_area_size(conn) == "medium"


def test_set_map_cache_area_size_rejects_unknown_values():
    with db.get_connection() as conn:
        with pytest.raises(ValueError):
            db.set_map_cache_area_size(conn, "huge")


def test_get_map_cache_area_size_falls_back_to_small_for_an_unparsable_stored_value():
    with db.get_connection() as conn:
        db.set_setting(conn, db.MAP_CACHE_AREA_SIZE_KEY, "bogus")
        assert db.get_map_cache_area_size(conn) == "small"


def test_get_map_cache_radius_km_resolves_the_selected_preset():
    with db.get_connection() as conn:
        db.set_map_cache_area_size(conn, "small")
        assert db.get_map_cache_radius_km(conn) == pytest.approx(0.5)
        db.set_map_cache_area_size(conn, "medium")
        assert db.get_map_cache_radius_km(conn) == pytest.approx(5.0)
        db.set_map_cache_area_size(conn, "large")
        assert db.get_map_cache_radius_km(conn) == pytest.approx(50.0)


def test_create_and_verify_user():
    with db.get_connection() as conn:
        db.create_user(conn, "Vakt Andersson", "hemligt123")

        assert db.verify_user_password(conn, "Vakt Andersson", "hemligt123") is not None
        assert db.verify_user_password(conn, "Vakt Andersson", "fel lösenord") is None
        assert db.verify_user_password(conn, "Okänd användare", "hemligt123") is None


def test_create_user_rejects_a_duplicate_name():
    with db.get_connection() as conn:
        db.create_user(conn, "Vakt Andersson", "hemligt123")
        with pytest.raises(ValueError):
            db.create_user(conn, "Vakt Andersson", "annat lösenord")


def test_list_and_delete_users():
    with db.get_connection() as conn:
        user_id = db.create_user(conn, "Vakt Andersson", "hemligt123")
        assert len(db.list_users(conn)) == 1

        db.delete_user(conn, user_id)
        assert db.list_users(conn) == []


def test_password_hash_is_never_stored_in_plain_text():
    with db.get_connection() as conn:
        db.create_user(conn, "Vakt Andersson", "hemligt123")
        row = conn.execute("SELECT password_hash FROM users WHERE name = ?", ("Vakt Andersson",)).fetchone()
    assert "hemligt123" not in row["password_hash"]


def test_get_or_create_secret_key_persists_across_calls():
    with db.get_connection() as conn:
        first = db.get_or_create_secret_key(conn)
        second = db.get_or_create_secret_key(conn)

    assert first == second
    assert len(first) >= 32


def test_touch_user_last_seen_makes_the_user_show_up_as_active():
    with db.get_connection() as conn:
        user_id = db.create_user(conn, "Vakt Andersson", "hemligt123")
        assert db.list_active_users(conn) == []

        db.touch_user_last_seen(conn, user_id)
        active = db.list_active_users(conn)

    assert len(active) == 1
    assert active[0]["name"] == "Vakt Andersson"


def test_clear_user_last_seen_removes_them_from_the_active_list():
    with db.get_connection() as conn:
        user_id = db.create_user(conn, "Vakt Andersson", "hemligt123")
        db.touch_user_last_seen(conn, user_id)
        assert len(db.list_active_users(conn)) == 1

        db.clear_user_last_seen(conn, user_id)
        assert db.list_active_users(conn) == []


def test_list_active_users_excludes_stale_last_seen_timestamps():
    with db.get_connection() as conn:
        user_id = db.create_user(conn, "Vakt Andersson", "hemligt123")
        stale = (datetime.fromisoformat(db.now_iso()) - timedelta(minutes=30)).isoformat()
        conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (stale, user_id))

        assert db.list_active_users(conn, within_minutes=5) == []


def test_log_and_list_system_events_newest_first():
    with db.get_connection() as conn:
        db.log_system_event(conn, "server_start", "host=127.0.0.1 port=5000")
        db.log_system_event(conn, "login", "Vakt Andersson (192.168.1.50)")

        entries = db.list_system_log(conn)

    assert [e["event_type"] for e in entries] == ["login", "server_start"]
    assert entries[0]["detail"] == "Vakt Andersson (192.168.1.50)"


def test_last_adjacent_send_defaults_to_none():
    with db.get_connection() as conn:
        assert db.get_last_adjacent_send(conn) is None


def test_set_last_adjacent_send_records_a_timestamp():
    with db.get_connection() as conn:
        db.set_last_adjacent_send(conn)
        recorded = db.get_last_adjacent_send(conn)

    assert recorded is not None
    from datetime import datetime  # local import: only this test needs it
    datetime.fromisoformat(recorded)  # must parse as a valid ISO timestamp


def test_receive_status_defaults_to_none():
    with db.get_connection() as conn:
        assert db.get_last_receive_attempt(conn) is None
        assert db.get_last_receive_success(conn) is None
        assert db.get_last_receive_error(conn) is None


def test_record_receive_attempt_success_sets_attempt_and_success_and_clears_error():
    with db.get_connection() as conn:
        db.record_receive_attempt(conn, error="tidigare fel")
        assert db.get_last_receive_error(conn) == "tidigare fel"

        db.record_receive_attempt(conn)

        assert db.get_last_receive_attempt(conn) is not None
        assert db.get_last_receive_success(conn) is not None
        assert db.get_last_receive_error(conn) is None


def test_record_receive_attempt_failure_sets_attempt_and_error_but_not_success():
    with db.get_connection() as conn:
        db.record_receive_attempt(conn, error="signal-cli misslyckades")

        assert db.get_last_receive_attempt(conn) is not None
        assert db.get_last_receive_success(conn) is None
        assert db.get_last_receive_error(conn) == "signal-cli misslyckades"


def test_has_demo_events_is_false_by_default():
    with db.get_connection() as conn:
        assert db.has_demo_events(conn) is False


def test_has_demo_events_is_true_after_a_training_day_import():
    with db.get_connection() as conn:
        db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({"source": "file_import", "filename": "dag_01.txt"}),
        )
        assert db.has_demo_events(conn) is True


def test_has_demo_events_is_false_for_an_unrelated_file_import():
    with db.get_connection() as conn:
        db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({"source": "file_import", "filename": "backlog.txt"}),
        )
        assert db.has_demo_events(conn) is False


def test_clear_demo_events_removes_only_demo_tagged_data():
    with db.get_connection() as conn:
        demo_message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({"source": "file_import", "filename": "dag_01.txt"}),
        )
        demo_event_id = db.insert_event(conn, message_id=demo_message_id, fields={"place": "Demo"})
        db.insert_attachment(conn, message_id=demo_message_id, file_path="/tmp/demo.jpg", content_type="image/jpeg")

        real_message_id = db.insert_message(
            conn, signal_timestamp=2, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        real_event_id = db.insert_event(conn, message_id=real_message_id, fields={"place": "Verklig"})

        removed_ids, removed_attachment_paths = db.clear_demo_events(conn)

        assert removed_ids == [demo_message_id]
        assert removed_attachment_paths == []
        assert db.get_event(conn, demo_event_id) is None
        assert db.get_message(conn, demo_message_id) is None
        assert db.list_attachments_for_message(conn, demo_message_id) == []
        assert db.get_event(conn, real_event_id) is not None
        assert db.get_message(conn, real_message_id) is not None
        assert db.has_demo_events(conn) is False


def test_clear_demo_events_returns_empty_lists_when_nothing_to_clear():
    with db.get_connection() as conn:
        assert db.clear_demo_events(conn) == ([], [])


def test_clear_demo_events_also_removes_demo_seeded_adjacent_reports():
    """Regression: demo/training-day import also seeds adjacent-unit
    status reports (2.Kompani/3.Kompani) with a negative signal_timestamp
    (routes.py's _SYNTHETIC_TIMESTAMP_OFFSET convention) -- these used to
    survive "clear demo data" entirely, leaving the header badge and
    Sammanställd hotbedömning's adjacent-unit card showing stale demo
    status even after every demo event was gone."""
    with db.get_connection() as conn:
        demo_adjacent_id = db.insert_adjacent_report(
            conn, signal_timestamp=-1234, sender_number=None,
            sender_name="2.Kompani", unit_name="2.Kompani", body="Demo status",
        )
        db.insert_adjacent_report_attachment(
            conn, adjacent_report_id=demo_adjacent_id,
            file_path="/tmp/demo_adjacent.jpg", content_type="image/jpeg",
        )
        real_adjacent_id = db.insert_adjacent_report(
            conn, signal_timestamp=1700000000000, sender_number="+15551234567",
            sender_name="3.Kompani", unit_name="3.Kompani", body="Verklig status",
        )

        removed_ids, removed_attachment_paths = db.clear_demo_events(conn)

        assert removed_ids == []
        assert removed_attachment_paths == ["/tmp/demo_adjacent.jpg"]
        assert db.list_attachments_for_adjacent_report(conn, demo_adjacent_id) == []
        assert conn.execute(
            "SELECT 1 FROM adjacent_reports WHERE id = ?", (demo_adjacent_id,)
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM adjacent_reports WHERE id = ?", (real_adjacent_id,)
        ).fetchone() is not None


def test_threat_override_defaults_to_none():
    with db.get_connection() as conn:
        assert db.get_threat_override(conn) is None


def test_set_and_get_threat_override():
    with db.get_connection() as conn:
        db.set_threat_override(conn, "red", "  Bekräftad av chefvakt.  ")
        override = db.get_threat_override(conn)

    assert override["level"] == "red"
    assert override["notes"] == "Bekräftad av chefvakt."
    assert override["set_at"] is not None


def test_clear_threat_override_reverts_to_none():
    with db.get_connection() as conn:
        db.set_threat_override(conn, "yellow", "")
        db.clear_threat_override(conn)
        assert db.get_threat_override(conn) is None


def test_threat_snapshot_defaults_to_none():
    """No summary_refresh has ever run -- callers must show a neutral
    "not yet updated" state, not a fabricated level."""
    with db.get_connection() as conn:
        assert db.get_threat_snapshot(conn) is None


def test_set_and_get_threat_snapshot():
    with db.get_connection() as conn:
        db.set_threat_snapshot(
            conn, level="red", score=7, reasons=["Återkommande beväpnad person"],
            total_events=3, period_label="all",
        )
        snapshot = db.get_threat_snapshot(conn)

    assert snapshot["level"] == "red"
    assert snapshot["score"] == 7
    assert snapshot["reasons"] == ["Återkommande beväpnad person"]
    assert snapshot["total_events"] == 3
    assert snapshot["period_label"] == "all"
    assert snapshot["updated_at"] is not None


def test_set_threat_snapshot_overwrites_the_previous_one():
    """A single current snapshot, not a history -- see summary_log for
    the actual history feature."""
    with db.get_connection() as conn:
        db.set_threat_snapshot(
            conn, level="green", score=0, reasons=[], total_events=0, period_label="7d",
        )
        db.set_threat_snapshot(
            conn, level="yellow", score=4, reasons=["Ett fordon återkommande"],
            total_events=2, period_label="30d",
        )
        snapshot = db.get_threat_snapshot(conn)

    assert snapshot["level"] == "yellow"
    assert snapshot["score"] == 4
    assert snapshot["period_label"] == "30d"


def test_add_list_delete_adjacent_unit():
    with db.get_connection() as conn:
        assert db.list_adjacent_units(conn) == []
        unit_id = db.add_adjacent_unit(conn, "Kompani 2")

    with db.get_connection() as conn:
        units = db.list_adjacent_units(conn)
        assert len(units) == 1
        assert units[0]["id"] == unit_id
        assert units[0]["name"] == "Kompani 2"

        db.delete_adjacent_unit(conn, unit_id)

    with db.get_connection() as conn:
        assert db.list_adjacent_units(conn) == []


def test_adjacent_units_listed_alphabetically():
    with db.get_connection() as conn:
        db.add_adjacent_unit(conn, "Kompani 3")
        db.add_adjacent_unit(conn, "Kompani 1")
    with db.get_connection() as conn:
        names = [u["name"] for u in db.list_adjacent_units(conn)]
        assert names == ["Kompani 1", "Kompani 3"]


def test_adjacent_report_dedup_and_insert():
    with db.get_connection() as conn:
        assert db.adjacent_report_exists(conn, 555) is False
        report_id = db.insert_adjacent_report(
            conn, signal_timestamp=555, sender_number="+46700000000",
            sender_name="Alice", unit_name="Kompani 2", body="Statusrapport",
        )
        assert db.adjacent_report_exists(conn, 555) is True

    with db.get_connection() as conn:
        reports = db.list_latest_adjacent_reports_per_unit(conn)
        assert len(reports) == 1
        assert reports[0]["id"] == report_id
        assert reports[0]["unit_name"] == "Kompani 2"
        assert reports[0]["body"] == "Statusrapport"


def test_list_latest_adjacent_reports_per_unit_keeps_only_newest_per_unit():
    with db.get_connection() as conn:
        db.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number="+1", sender_name=None,
            unit_name="Kompani 2", body="Gammal rapport",
        )
        db.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number="+1", sender_name=None,
            unit_name="Kompani 2", body="Ny rapport",
        )
        db.insert_adjacent_report(
            conn, signal_timestamp=3, sender_number="+2", sender_name=None,
            unit_name="Kompani 3", body="Annan enhet",
        )

    with db.get_connection() as conn:
        latest = db.list_latest_adjacent_reports_per_unit(conn)

    by_unit = {row["unit_name"]: row["body"] for row in latest}
    assert by_unit == {"Kompani 2": "Ny rapport", "Kompani 3": "Annan enhet"}


def test_list_adjacent_reports_returns_full_history_newest_first():
    """Unlike list_latest_adjacent_reports_per_unit this keeps every row,
    including the older ones for the same unit -- used by the AI-analys
    chat's context builder to show adjacent units' history, not just
    their current status."""
    with db.get_connection() as conn:
        db.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number="+1", sender_name=None,
            unit_name="Kompani 2", body="Gammal rapport",
        )
        db.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number="+1", sender_name=None,
            unit_name="Kompani 2", body="Ny rapport",
        )

    with db.get_connection() as conn:
        reports = db.list_adjacent_reports(conn)

    assert [r["body"] for r in reports] == ["Ny rapport", "Gammal rapport"]


def test_list_adjacent_reports_respects_limit():
    with db.get_connection() as conn:
        for i in range(3):
            db.insert_adjacent_report(
                conn, signal_timestamp=i, sender_number="+1", sender_name=None,
                unit_name="Kompani 2", body=f"Rapport {i}",
            )

    with db.get_connection() as conn:
        reports = db.list_adjacent_reports(conn, limit=1)

    assert len(reports) == 1
    assert reports[0]["body"] == "Rapport 2"


def test_adjacent_report_attachments_round_trip():
    with db.get_connection() as conn:
        report_id = db.insert_adjacent_report(
            conn, signal_timestamp=42, sender_number="+1", sender_name=None,
            unit_name="Kompani 2", body="Med bilaga",
        )
        attachment_id = db.insert_adjacent_report_attachment(
            conn, adjacent_report_id=report_id, file_path="/tmp/x.pdf",
            content_type="application/pdf",
        )

    with db.get_connection() as conn:
        attachments = db.list_attachments_for_adjacent_report(conn, report_id)
        assert len(attachments) == 1
        assert attachments[0]["id"] == attachment_id

        fetched = db.get_adjacent_attachment(conn, attachment_id)
        assert fetched["file_path"] == "/tmp/x.pdf"


def test_reset_all_empties_every_table_and_resets_autoincrement():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number="+1", sender_name="Alice",
            body="text", raw_json=json.dumps({}),
        )
        db.insert_attachment(conn, message_id=message_id, file_path="/tmp/a.jpg", content_type="image/jpeg")
        db.insert_event(conn, message_id=message_id, fields={"place": "X", "raw_text": "text"})
        db.set_unit_name(conn, "Kompani 1")
        db.add_adjacent_unit(conn, "Kompani 2")
        report_id = db.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number="+2", sender_name=None,
            unit_name="Kompani 2", body="status",
        )
        db.insert_adjacent_report_attachment(
            conn, adjacent_report_id=report_id, file_path="/tmp/b.pdf", content_type="application/pdf",
        )

        db.reset_all(conn)

        assert db.list_events(conn) == []
        assert db.get_message(conn, message_id) is None
        assert db.get_unit_name(conn) == ""
        assert db.list_adjacent_units(conn) == []
        assert db.list_latest_adjacent_reports_per_unit(conn) == []

    # Autoincrement counters reset too -- a fresh insert after a full
    # reset should start back at id 1, not continue from where it left off.
    with db.get_connection() as conn:
        new_message_id = db.insert_message(
            conn, signal_timestamp=99, sender_number=None, sender_name=None,
            body="fresh", raw_json=json.dumps({}),
        )
        assert new_message_id == 1


def test_reset_all_restores_map_settings_to_their_defaults():
    """"Rensa allt" wipes the whole settings table, and Kartleverantör
    (source + URL template), områdesstorlek, and Kartcentrum all read
    through get_setting's default fallback -- so a full reset must leave
    every one of them back at its out-of-the-box default, not stuck on
    whatever was last configured."""
    with db.get_connection() as conn:
        db.set_map_tile_source(conn, db.MAP_TILE_SOURCE_URL)
        db.set_map_tile_url_template(conn, "https://example.com/{z}/{x}/{y}.png")
        db.set_map_cache_area_size(conn, "large")
        db.set_map_center(conn, 58.0, 12.0)
        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_URL
        assert db.get_map_cache_area_size(conn) == "large"
        assert db.get_map_center(conn) == (58.0, 12.0)

        db.reset_all(conn)

        assert db.get_map_tile_source(conn) == db.MAP_TILE_SOURCE_LANTMATERIET_FTP
        assert db.get_map_tile_url_template(conn) == config.DEFAULT_TILE_URL_TEMPLATE
        assert db.get_map_cache_area_size(conn) == config.MAP_CACHE_DEFAULT_AREA_SIZE
        assert db.get_map_center(conn) == config.DEFAULT_MAP_CENTER
        assert db.has_custom_map_center(conn) is False


def test_insert_event_defaults_is_trivial_to_false():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        assert db.get_event(conn, event_id)["is_trivial"] == 0


def test_update_event_can_set_is_trivial():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        db.update_event(conn, event_id, {"is_trivial": True})
        assert db.get_event(conn, event_id)["is_trivial"] == 1

        db.update_event(conn, event_id, {"is_trivial": False})
        assert db.get_event(conn, event_id)["is_trivial"] == 0


def test_list_events_filters_by_is_trivial():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        trivial_id = db.insert_event(
            conn, message_id=message_id, fields={"place": "A", "is_trivial": True}
        )
        notable_id = db.insert_event(
            conn, message_id=message_id, fields={"place": "B", "is_trivial": False}
        )

        only_trivial = db.list_events(conn, is_trivial=True)
        only_notable = db.list_events(conn, is_trivial=False)

        assert {e["id"] for e in only_trivial} == {trivial_id}
        assert {e["id"] for e in only_notable} == {notable_id}


def test_insert_event_defaults_is_duplicate_to_false():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        assert db.get_event(conn, event_id)["is_duplicate"] == 0


def test_update_event_can_set_is_duplicate():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        db.update_event(conn, event_id, {"is_duplicate": True})
        assert db.get_event(conn, event_id)["is_duplicate"] == 1

        db.update_event(conn, event_id, {"is_duplicate": False})
        assert db.get_event(conn, event_id)["is_duplicate"] == 0


def test_insert_event_defaults_is_important_to_false():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        assert db.get_event(conn, event_id)["is_important"] == 0


def test_update_event_can_set_is_important():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        db.update_event(conn, event_id, {"is_important": True})
        assert db.get_event(conn, event_id)["is_important"] == 1

        db.update_event(conn, event_id, {"is_important": False})
        assert db.get_event(conn, event_id)["is_important"] == 0


def test_delete_event_removes_event_and_its_message_and_attachments():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        db.insert_attachment(conn, message_id=message_id, file_path="/tmp/x.jpg", content_type="image/jpeg")

        message_also_deleted = db.delete_event(conn, event_id)

        assert message_also_deleted is True
        assert db.get_event(conn, event_id) is None
        assert db.get_message(conn, message_id) is None
        assert db.list_attachments_for_message(conn, message_id) == []


def test_delete_event_on_unknown_id_is_a_no_op():
    with db.get_connection() as conn:
        assert db.delete_event(conn, 999999) is False


def test_delete_event_keeps_message_and_attachments_when_another_event_still_references_it():
    """Today one message always parses into exactly one event, but
    delete_event must not assume that -- if it ever changes, deleting
    one event must not silently drop another event's source message and
    attachments out from under it."""
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        first_id = db.insert_event(conn, message_id=message_id, fields={"place": "X"})
        second_id = db.insert_event(conn, message_id=message_id, fields={"place": "Y"})
        db.insert_attachment(conn, message_id=message_id, file_path="/tmp/x.jpg", content_type="image/jpeg")

        message_also_deleted = db.delete_event(conn, first_id)

        assert message_also_deleted is False
        assert db.get_event(conn, first_id) is None
        assert db.get_event(conn, second_id) is not None
        assert db.get_message(conn, message_id) is not None
        assert len(db.list_attachments_for_message(conn, message_id)) == 1


def test_insert_event_stores_lat_lon_when_provided():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9001, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(
            conn, message_id=message_id,
            fields={"place": "33VWE 18190 99510", "lat": 58.6355, "lon": 15.3133},
        )
        event = db.get_event(conn, event_id)
        assert event["lat"] == pytest.approx(58.6355)
        assert event["lon"] == pytest.approx(15.3133)


def test_insert_event_defaults_lat_lon_to_none():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9002, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})
        event = db.get_event(conn, event_id)
        assert event["lat"] is None
        assert event["lon"] is None


def test_update_event_can_set_and_clear_lat_lon():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9003, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "Kajen"})

        db.update_event(conn, event_id, {"lat": 58.6, "lon": 15.3})
        event = db.get_event(conn, event_id)
        assert event["lat"] == pytest.approx(58.6)
        assert event["lon"] == pytest.approx(15.3)

        db.update_event(conn, event_id, {"lat": None, "lon": None})
        event = db.get_event(conn, event_id)
        assert event["lat"] is None
        assert event["lon"] is None


def test_list_events_with_position_only_returns_events_that_have_both_coordinates():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9004, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        with_position = db.insert_event(
            conn, message_id=message_id, fields={"place": "Kajen", "lat": 58.6, "lon": 15.3}
        )
        db.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})

        rows = db.list_events_with_position(conn)
        assert [r["id"] for r in rows] == [with_position]


def test_list_events_with_position_filters_by_since():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9005, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        old_event = db.insert_event(
            conn, message_id=message_id, fields={"place": "Kajen", "lat": 58.6, "lon": 15.3}
        )
        conn.execute(
            "UPDATE events SET created_at = ? WHERE id = ?",
            ("2020-01-01T10:00:00+00:00", old_event),
        )
        new_event = db.insert_event(
            conn, message_id=message_id, fields={"place": "Norra grinden", "lat": 58.7, "lon": 15.4}
        )

        rows = db.list_events_with_position(conn, since="2025-01-01T00:00:00+00:00")
        assert [r["id"] for r in rows] == [new_event]

        rows = db.list_events_with_position(conn)
        assert {r["id"] for r in rows} == {old_event, new_event}


def test_insert_event_stores_source_unit_when_provided():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9006, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(
            conn, message_id=message_id, fields={"place": "Kajen", "source_unit": "2.Pluton"}
        )
        event = db.get_event(conn, event_id)
        assert event["source_unit"] == "2.Pluton"


def test_insert_event_defaults_source_unit_to_none():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9007, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "Kajen"})
        event = db.get_event(conn, event_id)
        assert event["source_unit"] is None


def test_update_event_can_set_source_unit():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9008, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        event_id = db.insert_event(conn, message_id=message_id, fields={"place": "Kajen"})

        db.update_event(conn, event_id, {"source_unit": "3.Kompani"})
        event = db.get_event(conn, event_id)
        assert event["source_unit"] == "3.Kompani"


def test_list_events_own_only_excludes_events_with_a_source_unit():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9009, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        own_id = db.insert_event(conn, message_id=message_id, fields={"place": "Egen"})
        db.insert_event(
            conn, message_id=message_id, fields={"place": "Angränsande", "source_unit": "2.Pluton"}
        )

        rows = db.list_events(conn, own_only=True)
        assert [r["id"] for r in rows] == [own_id]

        rows = db.list_events(conn)
        assert len(rows) == 2


def test_search_events_finds_a_registration_number_in_marks():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9011, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        match_id = db.insert_event(
            conn, message_id=message_id,
            fields={"place": "Norra grinden", "marks": "Silver Volvo, Reg.nr KRN482"},
        )
        db.insert_event(conn, message_id=message_id, fields={"place": "Södra grinden"})

        rows = db.search_events(conn, "KRN482")
        assert [r["id"] for r in rows] == [match_id]


def test_search_events_is_case_insensitive_and_matches_a_substring():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9012, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        match_id = db.insert_event(
            conn, message_id=message_id, fields={"marks": "Reg.nr KRN482"},
        )

        rows = db.search_events(conn, "krn")
        assert [r["id"] for r in rows] == [match_id]


def test_search_events_searches_place_object_activity_reported_by_next_steps_and_raw_text():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9013, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        ids = {
            "place": db.insert_event(conn, message_id=message_id, fields={"place": "Findme plats"}),
            "object": db.insert_event(conn, message_id=message_id, fields={"object": "Findme objekt"}),
            "activity": db.insert_event(conn, message_id=message_id, fields={"activity": "Findme aktivitet"}),
            "reported_by": db.insert_event(
                conn, message_id=message_id, fields={"reported_by": "Findme rapportör"}
            ),
            "next_steps": db.insert_event(
                conn, message_id=message_id, fields={"next_steps": "Findme åtgärd"}
            ),
            "raw_text": db.insert_event(conn, message_id=message_id, fields={"raw_text": "Findme text"}),
        }

        rows = db.search_events(conn, "Findme")
        assert {r["id"] for r in rows} == set(ids.values())


def test_search_events_returns_no_results_for_a_blank_query():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9014, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})

        assert db.search_events(conn, "") == []
        assert db.search_events(conn, "   ") == []


def test_search_events_includes_matches_from_adjacent_units():
    """Unlike list_events(own_only=True), a plate/keyword lookup should
    surface a hit regardless of which unit logged it -- an adjacent
    unit's sighting of the same vehicle is exactly the kind of match a
    guard is trying to find."""
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9015, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        adjacent_id = db.insert_event(
            conn, message_id=message_id,
            fields={"marks": "KRN482", "source_unit": "2.Pluton"},
        )

        rows = db.search_events(conn, "KRN482")
        assert [r["id"] for r in rows] == [adjacent_id]


def test_search_events_fuzzy_finds_a_typo_d_registration_number():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9016, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        match_id = db.insert_event(
            conn, message_id=message_id, fields={"marks": "Silver Volvo, Reg.nr KRN482"},
        )

        # One digit off -- a plain LIKE search finds nothing for this.
        assert db.search_events(conn, "KRN483") == []
        rows = db.search_events_fuzzy(conn, "KRN483")
        assert [r["id"] for r in rows] == [match_id]


def test_search_events_fuzzy_is_case_insensitive():
    """Regression: a lowercase query against a stored uppercase
    registration plate (the normal case -- users type lowercase, plates
    are logged uppercase) used to score far below the cutoff purely
    because every letter differed in case, even for a one-character
    typo that should score very high -- fuzz.partial_ratio needs
    processor=utils.default_process to normalize case first, unlike
    search_events' plain SQL LIKE which is already case-insensitive."""
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9021, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        match_id = db.insert_event(
            conn, message_id=message_id,
            fields={"marks": "Fordon 1 (S – Size: Mellanstor skåpbil, R – Registration: QAB456)"},
        )

        # "qah456" is a one-character typo of QAB456 -- lowercase query,
        # uppercase stored plate, exactly the normal real-world case.
        assert db.search_events(conn, "qah456") == []
        rows = db.search_events_fuzzy(conn, "qah456")
        assert [r["id"] for r in rows] == [match_id]


def test_search_events_fuzzy_excludes_ids_the_caller_already_has():
    """The web UI's "Inkludera nära träffar" list is meant to be
    additional to the exact search_events results, not a reshuffled
    duplicate of the same rows."""
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9017, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        exact_id = db.insert_event(conn, message_id=message_id, fields={"marks": "KRN482"})
        near_id = db.insert_event(conn, message_id=message_id, fields={"marks": "KRN483"})

        rows = db.search_events_fuzzy(conn, "KRN482", exclude_ids=[exact_id])
        assert [r["id"] for r in rows] == [near_id]


def test_search_events_fuzzy_does_not_match_unrelated_text():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9018, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})

        assert db.search_events_fuzzy(conn, "KRN482") == []


def test_search_events_fuzzy_returns_no_results_for_a_blank_query():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9019, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        db.insert_event(conn, message_id=message_id, fields={"place": "Norra grinden"})

        assert db.search_events_fuzzy(conn, "") == []
        assert db.search_events_fuzzy(conn, "   ") == []


def test_search_events_fuzzy_ranks_the_closest_match_first():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9020, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        close_id = db.insert_event(conn, message_id=message_id, fields={"marks": "KRN482"})
        farther_id = db.insert_event(conn, message_id=message_id, fields={"marks": "KRQ489"})

        rows = db.search_events_fuzzy(conn, "KRN483")
        assert [r["id"] for r in rows] == [close_id, farther_id]


def test_list_events_with_position_include_adjacent_false_excludes_adjacent_events():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=9010, sender_number=None, sender_name=None,
            body="text", raw_json=json.dumps({}),
        )
        own_id = db.insert_event(
            conn, message_id=message_id, fields={"place": "Egen", "lat": 58.6, "lon": 15.3}
        )
        db.insert_event(
            conn, message_id=message_id,
            fields={"place": "Angränsande", "lat": 58.7, "lon": 15.4, "source_unit": "2.Pluton"},
        )

        rows = db.list_events_with_position(conn, include_adjacent=False)
        assert [r["id"] for r in rows] == [own_id]

        rows = db.list_events_with_position(conn)
        assert len(rows) == 2


def test_migrate_add_lat_lon_columns_is_idempotent_on_an_old_schema(tmp_path, monkeypatch):
    """Simulates a database created before lat/lon existed to confirm
    init_db() upgrades it in place without losing existing rows -- same
    reasoning as the is_trivial migration test above."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")

    old_conn = sqlite3.connect(config.DB_PATH)
    old_conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp INTEGER NOT NULL UNIQUE,
            sender_number TEXT, sender_name TEXT, body TEXT,
            raw_json TEXT, received_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            event_time TEXT, place TEXT, count TEXT, object TEXT,
            activity TEXT, marks TEXT, reported_by TEXT, next_steps TEXT,
            raw_text TEXT, needs_review INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    old_conn.execute(
        "INSERT INTO messages (signal_timestamp, received_at) VALUES (1, 'now')"
    )
    old_conn.execute(
        "INSERT INTO events (message_id, place, needs_review, created_at, updated_at) "
        "VALUES (1, 'Pre-existing place', 0, 'now', 'now')"
    )
    old_conn.commit()
    old_conn.close()

    db.init_db()
    db.init_db()  # idempotent

    with db.get_connection() as conn:
        events = db.list_events(conn)
        assert len(events) == 1
        assert events[0]["place"] == "Pre-existing place"
        assert events[0]["lat"] is None
        assert events[0]["lon"] is None


def test_migrate_add_is_tak_bridge_column_is_idempotent_on_an_old_schema(tmp_path, monkeypatch):
    """Simulates a database created before is_tak_bridge existed to
    confirm init_db() upgrades it in place without losing existing rows
    -- same reasoning as the lat/lon migration test above."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")

    old_conn = sqlite3.connect(config.DB_PATH)
    old_conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp INTEGER NOT NULL UNIQUE,
            sender_number TEXT, sender_name TEXT, body TEXT,
            raw_json TEXT, received_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            event_time TEXT, place TEXT, count TEXT, object TEXT,
            activity TEXT, marks TEXT, reported_by TEXT, next_steps TEXT,
            raw_text TEXT, needs_review INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    old_conn.execute(
        "INSERT INTO messages (signal_timestamp, received_at) VALUES (1, 'now')"
    )
    old_conn.execute(
        "INSERT INTO events (message_id, place, needs_review, created_at, updated_at) "
        "VALUES (1, 'Pre-existing place', 0, 'now', 'now')"
    )
    old_conn.commit()
    old_conn.close()

    db.init_db()
    db.init_db()  # idempotent

    with db.get_connection() as conn:
        events = db.list_events(conn)
        assert len(events) == 1
        assert events[0]["place"] == "Pre-existing place"
        assert events[0]["is_tak_bridge"] == 0


def test_get_set_tak_bridge_group_name_round_trips_and_falls_back_to_config():
    with db.get_connection() as conn:
        assert db.get_tak_bridge_group_name(conn) == config.TAK_BRIDGE_GROUP_NAME
        db.set_tak_bridge_group_name(conn, "  TAK-brygga test  ")
        assert db.get_tak_bridge_group_name(conn) == "TAK-brygga test"


def test_migrate_add_is_trivial_column_is_idempotent_on_an_old_schema(tmp_path, monkeypatch):
    """Simulates a database created before is_trivial existed (a bare
    ALTER TABLE-less events table) to confirm init_db() upgrades it in
    place without losing existing rows."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")

    old_conn = sqlite3.connect(config.DB_PATH)
    old_conn.executescript("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_timestamp INTEGER NOT NULL UNIQUE,
            sender_number TEXT, sender_name TEXT, body TEXT,
            raw_json TEXT, received_at TEXT NOT NULL
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            event_time TEXT, place TEXT, count TEXT, object TEXT,
            activity TEXT, marks TEXT, reported_by TEXT, next_steps TEXT,
            raw_text TEXT, needs_review INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
    """)
    old_conn.execute(
        "INSERT INTO messages (signal_timestamp, received_at) VALUES (1, 'now')"
    )
    old_conn.execute(
        "INSERT INTO events (message_id, place, needs_review, created_at, updated_at) "
        "VALUES (1, 'Pre-existing place', 0, 'now', 'now')"
    )
    old_conn.commit()
    old_conn.close()

    db.init_db()
    db.init_db()  # calling it twice must not raise (idempotent migration)

    with db.get_connection() as conn:
        events = db.list_events(conn)
        assert len(events) == 1
        assert events[0]["place"] == "Pre-existing place"
        assert events[0]["is_trivial"] == 0
        assert events[0]["is_important"] == 0


def test_reset_events_only_clears_event_log_not_settings_or_adjacent():
    with db.get_connection() as conn:
        message_id = db.insert_message(
            conn, signal_timestamp=1, sender_number="+1", sender_name="Alice",
            body="text", raw_json=json.dumps({}),
        )
        db.insert_attachment(conn, message_id=message_id, file_path="/tmp/a.jpg", content_type="image/jpeg")
        db.insert_event(conn, message_id=message_id, fields={"place": "X", "raw_text": "text"})
        db.set_unit_name(conn, "Kompani 1")
        db.add_adjacent_unit(conn, "Kompani 2")
        db.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number="+2", sender_name=None,
            unit_name="Kompani 2", body="status",
        )

        db.reset_events(conn)

        assert db.list_events(conn) == []
        assert db.get_message(conn, message_id) is None
        # Untouched by the scoped reset:
        assert db.get_unit_name(conn) == "Kompani 1"
        assert [u["name"] for u in db.list_adjacent_units(conn)] == ["Kompani 2"]
        assert len(db.list_latest_adjacent_reports_per_unit(conn)) == 1

    # Autoincrement for the event-log tables resets too.
    with db.get_connection() as conn:
        new_message_id = db.insert_message(
            conn, signal_timestamp=99, sender_number=None, sender_name=None,
            body="fresh", raw_json=json.dumps({}),
        )
        assert new_message_id == 1


def test_insert_and_list_summary_log_entries_newest_first():
    with db.get_connection() as conn:
        first_id = db.insert_summary_log_entry(
            conn, tnr="221430", unit_name="Kompani 1", period_label="7d",
            total_events=3, level="green", score=0, source="download", format="pdf",
        )
        second_id = db.insert_summary_log_entry(
            conn, tnr="221500", unit_name="Kompani 1", period_label="7d",
            total_events=5, level="red", score=12, source="send", format="pdf",
        )

        entries = db.list_summary_log(conn)

    assert [e["id"] for e in entries] == [second_id, first_id]
    assert entries[0]["level"] == "red"
    assert entries[0]["source"] == "send"
    assert entries[0]["tnr"] == "221500"
    assert entries[0]["unit_name"] == "Kompani 1"
    assert entries[1]["level"] == "green"


def test_summary_log_format_can_be_none():
    with db.get_connection() as conn:
        db.insert_summary_log_entry(
            conn, tnr="221430", unit_name="Kompani 1", period_label="all",
            total_events=0, level="green", score=0, source="cli", format=None,
        )
        entries = db.list_summary_log(conn)

    assert entries[0]["format"] is None


def test_list_latest_adjacent_reports_per_unit_excludes_unidentified_reports():
    """A report with no identified unit_name (e.g. plain chat in the
    shared group, not a named status report) must not show up here,
    even though it's still stored in adjacent_reports for completeness."""
    with db.get_connection() as conn:
        db.insert_adjacent_report(
            conn, signal_timestamp=1, sender_number="+1", sender_name="Olle",
            unit_name=None, body="",
        )
        db.insert_adjacent_report(
            conn, signal_timestamp=2, sender_number="+2", sender_name=None,
            unit_name="Kompani 2", body="Statusrapport",
        )

        reports = db.list_latest_adjacent_reports_per_unit(conn)

    assert len(reports) == 1
    assert reports[0]["unit_name"] == "Kompani 2"


def test_migrate_summary_log_identity_columns_backfills_pre_existing_rows(tmp_path, monkeypatch):
    """Simulates a summary_log table created before tnr/unit_name existed
    (a real scenario: this table shipped once already without them) --
    confirms init_db() backfills a derived TNR (from that row's own
    created_at) and the currently configured unit name, without losing
    the row or crashing on the NOT NULL-shaped columns used going forward."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")

    old_conn = sqlite3.connect(config.DB_PATH)
    old_conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE summary_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            period_label TEXT NOT NULL,
            total_events INTEGER NOT NULL,
            level TEXT NOT NULL,
            score INTEGER NOT NULL,
            source TEXT NOT NULL,
            format TEXT
        );
    """)
    old_conn.execute(
        "INSERT INTO settings (key, value) VALUES ('unit_name', 'Kompani 1')"
    )
    old_conn.execute(
        "INSERT INTO summary_log "
        "(created_at, period_label, total_events, level, score, source, format) "
        "VALUES ('2026-07-22T14:30:00+00:00', '7d', 3, 'red', 12, 'download', 'pdf')"
    )
    old_conn.commit()
    old_conn.close()

    db.init_db()
    db.init_db()  # calling it twice must not raise (idempotent migration)

    with db.get_connection() as conn:
        entries = db.list_summary_log(conn)

    assert len(entries) == 1
    assert entries[0]["tnr"] == "221430"
    assert entries[0]["unit_name"] == "Kompani 1"
    assert entries[0]["level"] == "red"


def test_migrate_summary_log_identity_columns_uses_enhet_fallback_with_no_unit_name(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "old.db")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")

    old_conn = sqlite3.connect(config.DB_PATH)
    old_conn.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE summary_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            period_label TEXT NOT NULL,
            total_events INTEGER NOT NULL,
            level TEXT NOT NULL,
            score INTEGER NOT NULL,
            source TEXT NOT NULL,
            format TEXT
        );
    """)
    old_conn.execute(
        "INSERT INTO summary_log "
        "(created_at, period_label, total_events, level, score, source, format) "
        "VALUES ('2026-07-22T14:30:00+00:00', 'all', 0, 'green', 0, 'cli', NULL)"
    )
    old_conn.commit()
    old_conn.close()

    db.init_db()

    with db.get_connection() as conn:
        entries = db.list_summary_log(conn)

    assert entries[0]["unit_name"] == "enhet"
