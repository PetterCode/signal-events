"""Rule-based extraction of persons and vehicles mentioned in an event's
kännetecken (marks) text, and their persistence as first-class, linkable
records (see db.py's entities / entity_event_links tables).

Unlike analysis.py's RecurrenceGroup (recomputed fresh on every page
load, never persisted), extracted entities ARE persisted: they need a
stable identity across edits so, say, a vehicle can be recognised as
"the same Reg.nr ABC123" the next time it's reported, and so a human can
rename, annotate, or manually link one without losing that work on the
next report. To keep persistence from drifting out of sync with the
report text it came from, every extracted entity/link is tagged
source="auto" and fully re-derived (old auto links for the event
dropped, matching entities re-created/re-linked) each time
sync_event_entities runs -- call it right after every
insert_event/update_event that can touch `marks`. Manually added
entities/links (source="manual") are never touched by that resync.

Only persons and vehicles are auto-extracted -- "other objects" have no
reliable structural marker to key off in free text, so those are
catalogue entries a human adds by hand on the "Personer, fordon och
objekt" page (see webapp/routes.py's /entities routes).
"""

from __future__ import annotations

import json
import re
import sqlite3

from . import db
from .analysis import _PLATE_RE as _REG_NR_RE

# Matches the structured blocks the kännetecken composer appends (see
# event_form.html's person/vehicle panels), e.g.
# "Person 1 (A – Age: 30-40, B – Build: Muskulös)" or
# "Fordon 1 (S – Size: Kombi, R – Registration: ABC123)". Non-nested
# parens only -- good enough for the composer's own output, which never
# contains "(" or ")" inside a field value.
_ENTITY_BLOCK_RE = re.compile(r"(Person|Fordon)\s+(\d+)\s*\(([^()]*)\)")

# Splits a block's inner text into individual field parts without
# breaking on a comma that happens to appear inside a value -- only
# splits right before what looks like the start of the next field. Each
# field is either the A-H/SCRIM mnemonic style ("B – Build: Muskulös")
# or a plain "Label: value" (the composer's Namn/Alias/Nationalitet/
# Födelsedatum fields, which aren't part of either lettered convention).
_FIELD_SPLIT_RE = re.compile(r",\s*(?=(?:[A-ZÅÄÖ]\s*[–-]\s*)?[A-ZÅÄÖ][^:,]*:)")
_FIELD_RE = re.compile(r"^(?:[A-ZÅÄÖ]\s*[–-]\s*)?([^:]+):\s*(.*)$")

_ENTITY_TYPE_BY_PREFIX = {"Person": "person", "Fordon": "vehicle"}


def _normalize_plate(raw: str) -> str:
    """Same normalization analysis._extract_plate uses, so a plate found
    via either route matches the same entities.registration value."""
    return raw.upper().replace("-", "").replace(" ", "")


def _parse_block_attributes(block_text: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for part in _FIELD_SPLIT_RE.split(block_text):
        part = part.strip()
        if not part:
            continue
        match = _FIELD_RE.match(part)
        if match:
            key, value = match.group(1).strip(), match.group(2).strip()
            if value:
                attributes[key] = value
    return attributes


class ParsedEntity:
    def __init__(
        self, entity_type: str, label: str, attributes: dict[str, str],
        registration: str | None = None,
    ):
        self.entity_type = entity_type
        self.label = label
        self.attributes = attributes
        self.registration = registration


def extract_entities(marks: str | None, raw_text: str | None = None) -> list[ParsedEntity]:
    """Finds every "Person N (...)"/"Fordon N (...)" composer block in
    `marks`, plus any standalone "Reg.Nr: ..." plate mention
    (analysis._PLATE_RE) in either `marks` or `raw_text` not already
    covered by a Fordon block -- so both freshly composed reports and
    older/imported free text that predates the composer are recognised."""
    entities: list[ParsedEntity] = []
    seen_plates: set[str] = set()

    for match in _ENTITY_BLOCK_RE.finditer(marks or ""):
        prefix, number, block_text = match.group(1), match.group(2), match.group(3)
        entity_type = _ENTITY_TYPE_BY_PREFIX[prefix]
        attributes = _parse_block_attributes(block_text)
        registration = None
        if entity_type == "vehicle" and attributes.get("Registration"):
            registration = _normalize_plate(attributes["Registration"])
            seen_plates.add(registration)
        entities.append(ParsedEntity(
            entity_type=entity_type,
            label=f"{prefix} {number}",
            attributes=attributes,
            registration=registration,
        ))

    for text in (marks, raw_text):
        if not text:
            continue
        match = _REG_NR_RE.search(text)
        if not match:
            continue
        plate = _normalize_plate(match.group(1))
        if plate in seen_plates:
            continue
        seen_plates.add(plate)
        entities.append(ParsedEntity(
            entity_type="vehicle",
            label=f"Fordon {plate}",
            attributes={"Registration": match.group(1).strip()},
            registration=plate,
        ))

    return entities


def sync_event_entities(conn: sqlite3.Connection, event_id: int) -> None:
    """Re-derives event_id's auto-extracted person/vehicle entities from
    its current `marks`/`raw_text` and replaces its previous auto links
    with the new set. Safe (and idempotent) to call after every save,
    including one where nothing actually changed.

    Matching an already-known entity, rather than creating a new one
    every time, works differently depending on what identity the parser
    found:
    - A vehicle with a registration plate is matched *globally*, across
      every event -- same real plate, same entity -- and its attributes
      are merged (existing values win) rather than overwritten, so a
      fuller description picked up from one report isn't erased by a
      thinner mention of the same plate in another.
    - Anything else (a person, or a vehicle with no readable plate) has
      no reliable cross-report identity, so it's only matched against
      this *same event's* previous auto-extraction (by label, e.g.
      "Person 1") to stay stable across repeated saves of one report,
      without accidentally merging distinct people from different
      reports just because both happened to be "Person 1" in their own
      report."""
    event = db.get_event(conn, event_id)
    if event is None:
        return
    parsed = extract_entities(event["marks"], event["raw_text"])

    previously_linked = db.list_entities_for_event(conn, event_id)
    previously_linked_ids = {row["id"] for row in previously_linked}
    existing_auto_by_label = {
        row["label"]: row for row in previously_linked if row["link_source"] == "auto"
    }

    entity_ids: list[int] = []
    for item in parsed:
        entity_id = None
        merge_attributes = False

        if item.entity_type == "vehicle" and item.registration:
            existing = db.find_entity_by_registration(conn, "vehicle", item.registration)
            if existing is not None:
                entity_id = existing["id"]
                merge_attributes = True

        if entity_id is None:
            same_label = existing_auto_by_label.get(item.label)
            if same_label is not None and same_label["entity_type"] == item.entity_type:
                entity_id = same_label["id"]

        if entity_id is not None:
            if merge_attributes:
                existing_row = db.get_entity(conn, entity_id)
                existing_attrs = json.loads(existing_row["attributes"]) if existing_row["attributes"] else {}
                attributes = {**existing_attrs, **item.attributes}
            else:
                attributes = item.attributes
            db.update_entity(conn, entity_id, {"attributes": attributes})
        else:
            entity_id = db.insert_entity(
                conn, entity_type=item.entity_type, label=item.label,
                registration=item.registration, attributes=item.attributes,
                source="auto",
            )
        entity_ids.append(entity_id)

    db.replace_auto_entity_links_for_event(conn, event_id, entity_ids)
    db.prune_orphaned_auto_entities(conn, previously_linked_ids | set(entity_ids))


# --- Importing a previously saved/sent bevakningslista ----------------------
#
# Reverse of reports/generator.py's render_watchlist_markdown, for the
# "Importera bevakningslista" button on Personer, fordon och objekt --
# e.g. picking a list another unit exported and sent over back up into
# entity records. Deliberately duplicates the small section-title
# mapping rather than importing it from reports.generator: that module
# is the presentation layer (reportlab/PDF rendering) and has no reason
# to be a dependency of this one.
_WATCHLIST_SECTION_TYPES = {"Personer": "person", "Fordon": "vehicle", "Objekt": "object"}

# "{label} ({N} händelse/händelser)[ — bevakas manuellt]" -- label is
# whatever text precedes the *last* such parenthesised count, so a label
# that itself happens to contain parentheses still splits correctly.
_WATCHLIST_HEADING_RE = re.compile(
    r"^(?P<label>.+) \((?P<count>\d+) (?:händelse|händelser)\)(?P<manual> — bevakas manuellt)?$"
)

# Same "don't split inside a value" trick as _FIELD_SPLIT_RE above, for
# the one-line comma-separated "Reg.nr: X, Key: value, ..." detail line
# _watchlist_entry_details() produces.
_WATCHLIST_DETAIL_SPLIT_RE = re.compile(r",\s*(?=[A-ZÅÄÖ][^:,]*:)")
_WATCHLIST_DETAIL_KV_RE = re.compile(r"^([^:]+):\s*(.*)$")


class ImportedWatchlistEntry:
    def __init__(
        self, entity_type: str, label: str, attributes: dict[str, str],
        registration: str | None, notes: str | None, watchlist: bool,
    ):
        self.entity_type = entity_type
        self.label = label
        self.attributes = attributes
        self.registration = registration
        self.notes = notes
        self.watchlist = watchlist


def parse_watchlist_markdown(text: str) -> list[ImportedWatchlistEntry]:
    """Reads a bevakningslista markdown file back into entity records.
    Only each "### label (...)" heading and its one "- details" line are
    used; the indented "  - Händelse ..." lines underneath list events
    from the *exporting* unit's own database, which have no meaning
    here, so they're ignored entirely -- an imported record starts with
    no linked events of its own.

    Same caveat as the composer-block parser above: the details line has
    no delimiter between the last "Key: value" pair and any trailing
    freeform notes, so a comma inside that last value and real trailing
    notes are ambiguous -- both end up appended to the last key's value.
    Good enough for a round-trip through this tool's own export, not a
    general-purpose parser."""
    entries: list[ImportedWatchlistEntry] = []
    current_type: str | None = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            current_type = _WATCHLIST_SECTION_TYPES.get(stripped[3:].strip())
            continue
        if current_type is None:
            continue
        if stripped.startswith("### "):
            match = _WATCHLIST_HEADING_RE.match(stripped[4:])
            if match is None:
                continue
            entries.append(ImportedWatchlistEntry(
                entity_type=current_type, label=match.group("label").strip(),
                attributes={}, registration=None, notes=None,
                watchlist=match.group("manual") is not None,
            ))
            continue
        # A top-level "- " (no leading whitespace) is the one details
        # line for the entry just appended; anything indented (the
        # "  - Händelse ..." lines) is skipped by falling through here.
        if raw_line.startswith("- ") and entries:
            attributes: dict[str, str] = {}
            registration = None
            notes_parts: list[str] = []
            for part in _WATCHLIST_DETAIL_SPLIT_RE.split(stripped[2:]):
                part = part.strip()
                if not part:
                    continue
                kv = _WATCHLIST_DETAIL_KV_RE.match(part)
                if kv is None:
                    notes_parts.append(part)
                    continue
                key, value = kv.group(1).strip(), kv.group(2).strip()
                if key == "Reg.nr":
                    registration = _normalize_plate(value)
                else:
                    attributes[key] = value
            entries[-1].attributes = attributes
            entries[-1].registration = registration
            entries[-1].notes = ", ".join(notes_parts) or None

    return entries


def import_watchlist_entries(
    conn: sqlite3.Connection, entries: list[ImportedWatchlistEntry],
) -> tuple[int, int]:
    """Creates/updates entity records from parse_watchlist_markdown's
    output. Always sets watchlist=True regardless of whether the
    exporting unit had it manually flagged there -- the point of
    importing another unit's list is to start watching for these here
    too, and an imported record starts with zero linked events in this
    database, so without the flag it would never make it onto a future
    bevakningslista despite having just been imported for exactly that.
    Vehicles dedupe globally by plate, like sync_event_entities; persons
    and objects dedupe by an exact (type, label) match, since neither
    has a more reliable identifier. Returns (created, updated) counts."""
    created = updated = 0
    for entry in entries:
        entity_id = None
        if entry.entity_type == "vehicle" and entry.registration:
            existing = db.find_entity_by_registration(conn, "vehicle", entry.registration)
            if existing is not None:
                entity_id = existing["id"]
        if entity_id is None:
            same_label = next(
                (e for e in db.list_entities(conn, entity_type=entry.entity_type) if e["label"] == entry.label),
                None,
            )
            if same_label is not None:
                entity_id = same_label["id"]

        if entity_id is not None:
            existing_row = db.get_entity(conn, entity_id)
            existing_attrs = json.loads(existing_row["attributes"]) if existing_row["attributes"] else {}
            db.update_entity(conn, entity_id, {
                "attributes": {**existing_attrs, **entry.attributes},
                "notes": entry.notes or existing_row["notes"],
                "watchlist": True,
            })
            updated += 1
        else:
            db.insert_entity(
                conn, entity_type=entry.entity_type, label=entry.label,
                registration=entry.registration, attributes=entry.attributes,
                notes=entry.notes, source="manual", watchlist=True,
            )
            created += 1
    return created, updated
