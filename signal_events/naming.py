"""Builds consistent, filesystem-safe filenames for generated reports:
`<enhet>_<TNR>_<rapporttyp>.<ext>`, e.g. "Kompani1_301842_hotbedomning.pdf".

TNR here is a fresh Day-Hour-Minute date-time-group generated at the
moment the report is produced (same DDHHMM format used throughout this
tool for report time references) -- it identifies *when this report
artifact was generated*, not any single underlying event's own time.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9åäöÅÄÖ_-]+")
_FILENAME_RE = re.compile(r"^(?P<unit>.+)_(?P<tnr>\d{6})_(?P<type>.+)\.(?P<ext>[^.]+)$")
_VALID_TNR_RE = re.compile(r"^(?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])[0-5]\d$")


def sanitize_filename_part(value: str, fallback: str) -> str:
    """Collapse anything that isn't a letter/digit/underscore/hyphen into a
    single underscore, so free-text values (like a unit name) are always
    safe to use directly in a filename."""
    cleaned = _UNSAFE_CHARS_RE.sub("_", (value or "").strip()).strip("_")
    return cleaned or fallback


def generate_tnr(now: Optional[datetime] = None) -> str:
    """Day-Hour-Minute date-time-group, e.g. "301842" for day 30, 18:42."""
    now = now or datetime.now(timezone.utc)
    return now.strftime("%d%H%M")


def build_report_filename(
    unit_name: str, report_type: str, ext: str, tnr: Optional[str] = None
) -> str:
    """`<enhet>_<TNR>_<rapporttyp>.<ext>`. Falls back to "enhet" if
    `unit_name` is blank (not yet set in Inställningar)."""
    tnr = tnr or generate_tnr()
    unit_part = sanitize_filename_part(unit_name, "enhet")
    type_part = sanitize_filename_part(report_type, "rapport")
    return f"{unit_part}_{tnr}_{type_part}.{ext}"


def event_tnr(event_time: Optional[str], created_at: str) -> str:
    """A DDHHMM identifier for an event -- its own `event_time` if that's
    already in valid TNR format (as it is for the "Stund" field of a
    properly labeled 7S report), otherwise derived from when it was
    recorded. Used to identify events by a meaningful timestamp instead
    of an arbitrary database id, the same convention used for generated
    report filenames above. Not guaranteed unique (TNR has no month/year,
    same tradeoff already accepted for report filenames) -- for anything
    that needs a stable reference, the database id is still what backs
    the actual link/URL; this is only ever the displayed label."""
    candidate = (event_time or "").strip()
    if _VALID_TNR_RE.match(candidate):
        return candidate
    return generate_tnr(datetime.fromisoformat(created_at))


def parse_report_filename(filename: str) -> Optional[dict[str, str]]:
    """Reverse of build_report_filename(): recovers the unit name, TNR,
    report type, and extension from a `<enhet>_<TNR>_<rapporttyp>.<ext>`
    filename -- e.g. the attachment on an incoming adjacent-unit report,
    used to identify *which* unit sent it directly from plain text in the
    report's own name, with no separate sender configuration needed.
    Returns None if the filename doesn't match that convention."""
    match = _FILENAME_RE.match(filename or "")
    if not match:
        return None
    return {
        "unit_name": match.group("unit"),
        "tnr": match.group("tnr"),
        "report_type": match.group("type"),
        "ext": match.group("ext"),
    }
