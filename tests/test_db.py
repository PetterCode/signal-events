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

        removed_ids = db.clear_demo_events(conn)

        assert removed_ids == [demo_message_id]
        assert db.get_event(conn, demo_event_id) is None
        assert db.get_message(conn, demo_message_id) is None
        assert db.list_attachments_for_message(conn, demo_message_id) == []
        assert db.get_event(conn, real_event_id) is not None
        assert db.get_message(conn, real_message_id) is not None
        assert db.has_demo_events(conn) is False


def test_clear_demo_events_returns_empty_list_when_nothing_to_clear():
    with db.get_connection() as conn:
        assert db.clear_demo_events(conn) == []


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
