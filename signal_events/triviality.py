"""Detects reports describing routine, non-notable activity (wildlife,
weather, routine deliveries/patrols with nothing to report, ...) so the
incident report can leave them out by default -- the same kind of
offline keyword heuristics used elsewhere in this app, not a verdict.
Every classification is written back as `is_trivial` on the event, so
it's visible and reversible in the review UI (see webapp/routes.py
event_detail), the same way needs_review already is.

Deliberately conservative: only content that's unambiguously routine
(a deer crossing, weather notes, "nothing to report") is flagged. A
human can always tick/untick the "Trivial" checkbox in the review UI --
and once they save that form, `is_trivial_reviewed` is set on the event,
which this module always treats as final: it never writes over a human's
own judgment, whichever way it went, whether that's "reviewed and left
unticked" or "was auto-flagged trivial, then manually unticked back to
normal".
"""

from __future__ import annotations

import sqlite3

from . import db

_TRIVIAL_KEYWORDS = [
    # Wildlife
    "rådjur", "räv", "hare", "ekorre", "fasan", "grävling", "fågelflock",
    "vildsvin", "älg", "fiskare",
    "deer", "fox", "squirrel", "wildlife", "pheasant",
    # Weather
    "väderobservation", "dimma", "nederbörd", "kraftig vind", "snöfall",
    "weather", "fog", "heavy rain", "snowfall",
    # Routine / nothing to report
    "rutinpatrullering", "inget avvikande", "inget att rapportera",
    "ingen avvikelse", "inga avvikelser",
    "routine patrol", "nothing to report", "no anomalies",
    # Mundane scheduled deliveries/services with no unusual behavior
    "sopbil", "brevbärare", "postbil", "renhållningsfordon",
    "mail delivery", "garbage truck", "postal service",
]


def _event_text(event: sqlite3.Row) -> str:
    return " ".join(
        filter(None, [
            event["object"], event["activity"], event["marks"],
            event["next_steps"], event["raw_text"],
        ])
    ).lower()


def is_trivial(event: sqlite3.Row) -> bool:
    """Best-effort, offline keyword check -- see module docstring."""
    text = _event_text(event)
    return any(keyword in text for keyword in _TRIVIAL_KEYWORDS)


def classify_trivial_events(conn: sqlite3.Connection, events: list[sqlite3.Row]) -> set[int]:
    """Marks any of `events` that look routine/non-notable as
    `is_trivial` in the database. Returns the ids of *every* trivial
    event in `events` -- both already-flagged and newly-flagged -- so
    the caller can filter its report by id rather than by each row's own
    `is_trivial` column, which is a stale, pre-classification snapshot
    for anything just marked in this same call (row objects don't
    reflect writes made after they were fetched).

    Events with `is_trivial_reviewed` set are never touched here, in
    either direction -- a human already made the call via the review
    UI's "Trivial" checkbox (see webapp/routes.py event_detail, which
    sets that flag on every save), and this heuristic must not second-
    guess it, whether that means leaving a reviewed-and-unticked event
    out of future auto-flagging, or leaving a reviewed-and-ticked one
    marked trivial even if its wording later stops matching."""
    trivial_ids: set[int] = set()
    for event in events:
        if event["is_trivial"]:
            trivial_ids.add(event["id"])
        elif not event["is_trivial_reviewed"] and is_trivial(event):
            db.update_event(conn, event["id"], {"is_trivial": True})
            trivial_ids.add(event["id"])
    return trivial_ids
