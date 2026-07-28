import json

from signal_events import db, duplicates


def _make_event(conn, **fields) -> int:
    message_id = db.insert_message(
        conn, signal_timestamp=fields.pop("signal_timestamp", 1),
        sender_number=None, sender_name=None, body="text",
        raw_json=json.dumps({}),
    )
    return db.insert_event(conn, message_id=message_id, fields=fields)


def _set_created_at(conn, event_id: int, iso: str) -> None:
    conn.execute("UPDATE events SET created_at = ? WHERE id = ?", (iso, event_id))


def test_is_same_incident_matches_same_place_object_and_near_identical_wording():
    with db.get_connection() as conn:
        a_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        b_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        a, b = db.get_event(conn, a_id), db.get_event(conn, b_id)
        assert duplicates.is_same_incident(a, b)


def test_is_same_incident_rejects_different_place():
    with db.get_connection() as conn:
        a_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        b_id = _make_event(
            conn, signal_timestamp=2, place="Södra vägen", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        a, b = db.get_event(conn, a_id), db.get_event(conn, b_id)
        assert not duplicates.is_same_incident(a, b)


def test_is_same_incident_rejects_dissimilar_description():
    with db.get_connection() as conn:
        a_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Civil",
            marks="man i grön jacka", activity="Gick mot skogsbrynet",
        )
        b_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Civil",
            marks="kvinna i röd klänning", activity="Stod still och rökte",
        )
        a, b = db.get_event(conn, a_id), db.get_event(conn, b_id)
        assert not duplicates.is_same_incident(a, b)


def test_is_same_incident_rejects_matching_description_far_apart_in_time():
    """The same vehicle turning up again two days later is *recurrence*,
    not a duplicate report of the same incident -- the time-proximity
    check keeps these two concepts separate."""
    with db.get_connection() as conn:
        a_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        b_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        _set_created_at(conn, a_id, "2026-01-01T10:00:00+00:00")
        _set_created_at(conn, b_id, "2026-01-03T10:00:00+00:00")
        a, b = db.get_event(conn, a_id), db.get_event(conn, b_id)
        assert not duplicates.is_same_incident(a, b)


def test_classify_duplicate_events_marks_the_later_report_and_keeps_the_first():
    with db.get_connection() as conn:
        first_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        second_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        _set_created_at(conn, first_id, "2026-01-01T10:00:00+00:00")
        _set_created_at(conn, second_id, "2026-01-01T10:15:00+00:00")

        events = db.list_events(conn)
        duplicate_ids = duplicates.classify_duplicate_events(conn, events)

        assert duplicate_ids == {second_id}
        assert db.get_event(conn, first_id)["is_duplicate"] == 0
        assert db.get_event(conn, second_id)["is_duplicate"] == 1


def test_classify_duplicate_events_returns_newly_marked_ids_immediately():
    """Same stale-row reasoning as triviality's regression test: a row
    fetched before this call doesn't reflect a write this same call just
    made, so the returned set -- not each row's own is_duplicate -- is
    what callers must filter by."""
    with db.get_connection() as conn:
        first_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        second_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        events = db.list_events(conn)
        assert all(e["is_duplicate"] == 0 for e in events)  # stale, not yet classified

        duplicate_ids = duplicates.classify_duplicate_events(conn, events)

        assert duplicate_ids == {second_id}
        assert db.get_event(conn, second_id)["is_duplicate"] == 1


def test_classify_duplicate_events_is_idempotent_across_repeated_calls():
    with db.get_connection() as conn:
        first_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        second_id = _make_event(
            conn, signal_timestamp=2, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )

        first_pass = duplicates.classify_duplicate_events(conn, db.list_events(conn))
        assert first_pass == {second_id}

        second_pass = duplicates.classify_duplicate_events(conn, db.list_events(conn))
        assert second_pass == {second_id}
        assert db.get_event(conn, first_id)["is_duplicate"] == 0


def test_classify_duplicate_events_never_flags_sensor_events():
    """Two sensor-trigger events (is_sensor=True) at the same place with
    identical templated wording, logged close together -- exactly what
    demo/generate_training_days.py's sensor scenario produces across
    different in-story days when a trainee imports them back-to-back in
    real wall-clock time. Without the is_sensor exemption these would
    look identical enough to get wrongly flagged as duplicate data entry,
    when each is actually a genuine, separate sensor trigger."""
    with db.get_connection() as conn:
        first_id = _make_event(
            conn, signal_timestamp=1, place="Trådlarm vid Östra grinden", object=None,
            activity="Sensor aktiverad", reported_by="Sensorgateway", is_sensor=True,
        )
        second_id = _make_event(
            conn, signal_timestamp=2, place="Trådlarm vid Östra grinden", object=None,
            activity="Sensor aktiverad", reported_by="Sensorgateway", is_sensor=True,
        )
        _set_created_at(conn, first_id, "2026-01-01T10:00:00+00:00")
        _set_created_at(conn, second_id, "2026-01-01T10:05:00+00:00")

        duplicate_ids = duplicates.classify_duplicate_events(conn, db.list_events(conn))

        assert duplicate_ids == set()
        assert db.get_event(conn, first_id)["is_duplicate"] == 0
        assert db.get_event(conn, second_id)["is_duplicate"] == 0


def test_classify_duplicate_events_leaves_distinct_incidents_untouched():
    with db.get_connection() as conn:
        vehicle_id = _make_event(
            conn, signal_timestamp=1, place="Norra grinden", object="Personbil",
            marks="Silver Volvo, Reg.nr KRN482", activity="Stannade vid grinden",
        )
        person_id = _make_event(
            conn, signal_timestamp=2, place="Skogsbrynet", object="Civil",
            marks="man i grön jacka och keps",
        )

        duplicate_ids = duplicates.classify_duplicate_events(conn, db.list_events(conn))

        assert duplicate_ids == set()
        assert db.get_event(conn, vehicle_id)["is_duplicate"] == 0
        assert db.get_event(conn, person_id)["is_duplicate"] == 0
