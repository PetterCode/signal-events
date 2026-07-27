import json

from signal_events import db, triviality


def _make_event(conn, **fields) -> int:
    message_id = db.insert_message(
        conn, signal_timestamp=fields.pop("signal_timestamp", 1),
        sender_number=None, sender_name=None, body="text",
        raw_json=json.dumps({}),
    )
    return db.insert_event(conn, message_id=message_id, fields=fields)


def test_is_trivial_matches_wildlife_and_weather_and_routine():
    with db.get_connection() as conn:
        wildlife_id = _make_event(
            conn, signal_timestamp=1, object="Rådjur",
            activity="Betade en stund vid stängslet", raw_text="Ett rådjur sågs.",
        )
        weather_id = _make_event(
            conn, signal_timestamp=2, object="Väderobservation",
            activity="Nedsatt sikt på grund av dimma",
        )
        routine_id = _make_event(
            conn, signal_timestamp=3, object="Rutinpatrullering",
            activity="Inget avvikande att rapportera",
        )
        notable_id = _make_event(
            conn, signal_timestamp=4, object="Beväpnad person",
            activity="Siktades vid bortre parkeringen",
        )

        for event_id in (wildlife_id, weather_id, routine_id):
            assert triviality.is_trivial(db.get_event(conn, event_id))
        assert not triviality.is_trivial(db.get_event(conn, notable_id))


def test_classify_trivial_events_marks_matches_and_leaves_others():
    with db.get_connection() as conn:
        wildlife_id = _make_event(
            conn, signal_timestamp=1, object="Räv",
            activity="Sprang tvärs över vägen",
        )
        notable_id = _make_event(
            conn, signal_timestamp=2, object="Beväpnad person",
            activity="Siktades vid bortre parkeringen",
        )

        events = db.list_events(conn)
        trivial_ids = triviality.classify_trivial_events(conn, events)
        assert trivial_ids == {wildlife_id}

        assert db.get_event(conn, wildlife_id)["is_trivial"] == 1
        assert db.get_event(conn, notable_id)["is_trivial"] == 0


def test_classify_trivial_events_returns_newly_marked_ids_immediately():
    """Regression: the returned set must reflect events marked *in this
    same call*, not just ones already flagged from a previous run --
    callers filter by this set precisely because each event's own
    in-memory row is a stale, pre-classification snapshot that doesn't
    show the write this function just made."""
    with db.get_connection() as conn:
        event_id = _make_event(
            conn, signal_timestamp=1, object="Rådjur", activity="Betade vid stängslet",
        )
        events = db.list_events(conn)
        assert events[0]["is_trivial"] == 0  # stale snapshot, not yet classified

        trivial_ids = triviality.classify_trivial_events(conn, events)

        assert trivial_ids == {event_id}
        assert db.get_event(conn, event_id)["is_trivial"] == 1  # confirmed written


def test_classify_trivial_events_is_idempotent_across_repeated_calls():
    with db.get_connection() as conn:
        event_id = _make_event(
            conn, signal_timestamp=1, object="Rådjur", activity="Betade vid stängslet",
        )

        first_events = db.list_events(conn)
        first_pass = triviality.classify_trivial_events(conn, first_events)
        assert first_pass == {event_id}

        second_events = db.list_events(conn)
        second_pass = triviality.classify_trivial_events(conn, second_events)
        assert second_pass == {event_id}
        assert db.get_event(conn, event_id)["is_trivial"] == 1


def test_classify_trivial_events_never_marks_a_manually_reviewed_event():
    """A human who reviews an event and leaves "Trivial" unticked (the
    web UI sets is_trivial_reviewed=1 on every save, see
    webapp/routes.py event_detail) has made a deliberate call -- even if
    the wording happens to match the keyword heuristic, it must stay
    out of the trivial set forever after that."""
    with db.get_connection() as conn:
        event_id = _make_event(
            conn, signal_timestamp=1, object="Rådjur", activity="Betade vid stängslet",
        )
        # Simulates saving the review form with "Trivial" left unticked.
        db.update_event(
            conn, event_id, {"needs_review": False, "is_trivial": False, "is_trivial_reviewed": True},
        )

        events = db.list_events(conn)
        trivial_ids = triviality.classify_trivial_events(conn, events)

        assert trivial_ids == set()
        assert db.get_event(conn, event_id)["is_trivial"] == 0


def test_classify_trivial_events_does_not_reflag_after_a_manual_override():
    """The event was auto-flagged trivial by a previous report
    generation, then a human opened it and unticked "Trivial" -- that
    correction must stick, not get silently reverted by the next report."""
    with db.get_connection() as conn:
        event_id = _make_event(
            conn, signal_timestamp=1, object="Rådjur", activity="Betade vid stängslet",
        )
        events = db.list_events(conn)
        triviality.classify_trivial_events(conn, events)
        assert db.get_event(conn, event_id)["is_trivial"] == 1

        # Human reviews it, disagrees, and unchecks "Trivial".
        db.update_event(
            conn, event_id, {"is_trivial": False, "is_trivial_reviewed": True, "needs_review": False},
        )
        assert db.get_event(conn, event_id)["is_trivial"] == 0

        # A later report generation must not flip it back.
        events_again = db.list_events(conn)
        trivial_ids = triviality.classify_trivial_events(conn, events_again)

        assert trivial_ids == set()
        assert db.get_event(conn, event_id)["is_trivial"] == 0


def test_classify_trivial_events_still_excludes_a_manually_confirmed_trivial_event():
    """The flip side: if a human explicitly ticks "Trivial" themselves,
    that event should still count as trivial for report exclusion, even
    though the heuristic never touched it and the classifier is barred
    from writing to a reviewed event."""
    with db.get_connection() as conn:
        event_id = _make_event(
            conn, signal_timestamp=1, object="Ovanlig observation",
            activity="Något som inte matchar någon nyckelordslista",
        )
        db.update_event(
            conn, event_id, {"is_trivial": True, "is_trivial_reviewed": True, "needs_review": False},
        )

        events = db.list_events(conn)
        trivial_ids = triviality.classify_trivial_events(conn, events)

        assert trivial_ids == {event_id}
