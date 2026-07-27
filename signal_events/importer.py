"""Bulk import of reports from local files. Complements `sync` (from Signal)
and the manual-entry web form: this is for reports typed up elsewhere
(notes app, another device, historical backlog, another local tool) and
brought in via files, with no network involved.

`import_text`: a plain-text file, one report per block, separated by
either a line containing only dashes (`---`) or a blank line. Each block
may optionally start with a `From: <name>` / `Reported by: <name>` line
(or the Swedish equivalents `Från: <namn>` / `Rapporterad av: <namn>`) to
set the sender; the rest of the block is parsed with the same
best-effort heuristics used for Signal messages.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Optional

from . import db, parser

_DASH_SEPARATOR_RE = re.compile(r"^\s*-{3,}\s*$", re.MULTILINE)
_BLANK_LINE_SPLIT_RE = re.compile(r"\n\s*\n+")
_SENDER_LINE_RE = re.compile(
    r"^\s*(?:from|reported\s*by|från|rapporterad\s*av)\s*:\s*(.+?)\s*$", re.IGNORECASE
)


def split_report_blocks(text: str) -> list[str]:
    if _DASH_SEPARATOR_RE.search(text):
        blocks = _DASH_SEPARATOR_RE.split(text)
    else:
        blocks = _BLANK_LINE_SPLIT_RE.split(text)
    return [b.strip() for b in blocks if b.strip()]


def extract_sender_line(block: str) -> tuple[Optional[str], str]:
    lines = block.split("\n", 1)
    match = _SENDER_LINE_RE.match(lines[0])
    if not match:
        return None, block
    rest = lines[1] if len(lines) > 1 else ""
    return match.group(1).strip(), rest.strip()


def import_text(
    conn: sqlite3.Connection,
    text: str,
    filename: str,
    default_reported_by: Optional[str] = None,
) -> list[int]:
    """Parse and store each report block in `text`. Returns the new event ids."""
    event_ids: list[int] = []
    for i, block in enumerate(split_report_blocks(text)):
        sender, body = extract_sender_line(block)
        reported_by = sender or default_reported_by

        message_id = db.insert_message(
            conn,
            signal_timestamp=-time.time_ns() - i,
            sender_number=None,
            sender_name=reported_by,
            body=body,
            raw_json=json.dumps({"source": "file_import", "filename": filename}),
        )
        fields = parser.parse_event_fields(body, reported_by=reported_by)
        event_ids.append(db.insert_event(conn, message_id=message_id, fields=fields))
    return event_ids
