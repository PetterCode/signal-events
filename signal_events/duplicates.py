"""Detects reports that describe the same real-world incident reported
more than once -- e.g. the same observation entered twice, or two
reporters independently messaging about the same sighting within
minutes of each other. This is deliberately distinct from *recurrence*
(analysis.py's pattern-matching), which is the same subject -- a
vehicle, a person -- showing up again over time and is exactly what the
threat analysis is *for*. A duplicate is redundant data entry about a
single incident, not a second occurrence, so it's excluded from the
threat-level analysis entirely rather than inflating a recurrence count.

Offline heuristic, no ML: two events are duplicates if they share the
same place and object, their marks/activity/raw_text overlap far more
than analysis.py's recurrence threshold (near-identical wording, not
just a similar theme), and they were logged close together in time --
the same incident described twice, not the same subject seen again
later. Within a cluster of matches, the earliest-logged event is kept
as the canonical, non-duplicate copy.

Events flagged `is_sensor` (see db.insert_event/signal_client.py) are
never evaluated at all -- an automated sensor is *expected* to fire the
same templated message at the same place again and again, and each
trigger is a genuine, separate occurrence, not a human accidentally
filing the same report twice. Flagging that as a "duplicate" would
silently drop real trigger events from the threat analysis exactly
backwards from what this module is for.

A human can always tick/untick the "Dublett" checkbox in the review UI
(see webapp/routes.py's event_detail) -- once saved, `is_duplicate_reviewed`
is set on that event, and this module then treats it as final in either
direction, the same way triviality.py's `is_trivial_reviewed` already
works: it never re-flags an event a human explicitly cleared, and it
never un-flags one a human explicitly confirmed.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from . import analysis, db

_DUPLICATE_SIMILARITY_THRESHOLD = 0.8
_DUPLICATE_MAX_GAP = timedelta(hours=2)


def _normalized(value: str | None) -> str:
    return (value or "").strip().lower()


def _event_datetime(event: sqlite3.Row) -> datetime:
    return datetime.fromisoformat(event["created_at"])


def _description_tokens(event: sqlite3.Row) -> set[str]:
    return (
        analysis._tokenize(event["marks"])
        | analysis._tokenize(event["activity"])
        | analysis._tokenize(event["raw_text"])
    )


def is_same_incident(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """Best-effort, offline check -- see module docstring."""
    if _normalized(a["place"]) != _normalized(b["place"]):
        return False
    if _normalized(a["object"]) != _normalized(b["object"]):
        return False
    if abs(_event_datetime(a) - _event_datetime(b)) > _DUPLICATE_MAX_GAP:
        return False
    return analysis._jaccard(_description_tokens(a), _description_tokens(b)) >= (
        _DUPLICATE_SIMILARITY_THRESHOLD
    )


def classify_duplicate_events(conn: sqlite3.Connection, events: list[sqlite3.Row]) -> set[int]:
    """Marks any of `events` that describe the same incident as an
    earlier one (by created_at order) as `is_duplicate` in the database.
    Returns the ids of *every* duplicate event in `events` -- both
    already-flagged and newly-flagged -- so the caller can filter by id
    rather than by each row's own `is_duplicate` column, which is a
    stale, pre-classification snapshot for anything just marked in this
    same call (row objects don't reflect writes made after they were
    fetched; see triviality.classify_trivial_events for the same
    reasoning).

    Events with `is_duplicate_reviewed` set are never re-flagged here --
    a human already made the call via the review UI's "Dublett"
    checkbox. One a human cleared back to "not a duplicate" is still
    eligible to be matched against as a canonical original for later
    events, exactly like any other non-duplicate."""
    duplicate_ids: set[int] = set()
    canonical: list[sqlite3.Row] = []

    for event in sorted(events, key=_event_datetime):
        if event["is_sensor"]:
            continue
        if event["is_duplicate"]:
            duplicate_ids.add(event["id"])
            continue
        if event["is_duplicate_reviewed"]:
            canonical.append(event)
            continue
        match = next((c for c in canonical if is_same_incident(c, event)), None)
        if match is not None:
            db.update_event(conn, event["id"], {"is_duplicate": True})
            duplicate_ids.add(event["id"])
        else:
            canonical.append(event)
    return duplicate_ids
