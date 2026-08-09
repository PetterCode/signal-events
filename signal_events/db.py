"""SQLite storage layer. Everything here is local-file based so the tool
works with no network connection once messages have been synced."""

from __future__ import annotations

import json
import secrets
import sqlite3
import urllib.parse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from werkzeug.security import check_password_hash, generate_password_hash

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_timestamp INTEGER NOT NULL UNIQUE,
    sender_number TEXT,
    sender_name TEXT,
    body TEXT,
    raw_json TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    file_path TEXT NOT NULL,
    content_type TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    event_time TEXT,
    place TEXT,
    count TEXT,
    object TEXT,
    activity TEXT,
    marks TEXT,
    reported_by TEXT,
    next_steps TEXT,
    raw_text TEXT,
    needs_review INTEGER NOT NULL DEFAULT 1,
    is_trivial INTEGER NOT NULL DEFAULT 0,
    is_trivial_reviewed INTEGER NOT NULL DEFAULT 0,
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    is_duplicate_reviewed INTEGER NOT NULL DEFAULT 0,
    is_sensor INTEGER NOT NULL DEFAULT 0,
    source_unit TEXT, -- NULL = this unit's own event; otherwise the
                       -- angränsande enhet (adjacent_units.name) it was
                       -- received from, see insert_event/list_events
    lat REAL,
    lon REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjacent_units (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjacent_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_timestamp INTEGER NOT NULL UNIQUE,
    sender_number TEXT,
    sender_name TEXT,
    unit_name TEXT,
    body TEXT,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adjacent_report_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    adjacent_report_id INTEGER NOT NULL REFERENCES adjacent_reports(id),
    file_path TEXT NOT NULL,
    content_type TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS system_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_log_created_at ON system_log(created_at);

CREATE TABLE IF NOT EXISTS summary_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    tnr TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    period_label TEXT NOT NULL,
    total_events INTEGER NOT NULL,
    level TEXT NOT NULL,
    score INTEGER NOT NULL,
    source TEXT NOT NULL,
    format TEXT
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL, -- 'person' | 'vehicle' | 'object'
    label TEXT NOT NULL,
    registration TEXT, -- normalized vehicle plate (see entities.py
                        -- _normalize_plate) used to recognise the same
                        -- vehicle recurring across separate reports;
                        -- NULL for person/object entities and vehicles
                        -- with no readable plate
    attributes TEXT, -- JSON object of free-form key/value details, e.g.
                      -- {"Age": "30-40", "Registration": "ABC 123"}
    notes TEXT,
    photo_path TEXT, -- absolute path to one uploaded reference photo, see
                      -- webapp/routes.py's entity_photo/entity_photo_file;
                      -- manual only, never set by entities.py's parser
    watchlist INTEGER NOT NULL DEFAULT 0, -- human-set "include in
                      -- bevakningslista" flag (see
                      -- webapp/routes.py's entities_list.html checkbox) --
                      -- included in the sent watchlist report regardless
                      -- of event_count, same as list_watchlist_entities
    source TEXT NOT NULL DEFAULT 'manual', -- 'auto' | 'manual' -- see
                                            -- entities.sync_event_entities
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity_event_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entities(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    source TEXT NOT NULL DEFAULT 'manual', -- 'auto' | 'manual'
    created_at TEXT NOT NULL,
    UNIQUE(entity_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_message_id ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_adjacent_reports_received_at ON adjacent_reports(received_at);
CREATE INDEX IF NOT EXISTS idx_summary_log_created_at ON summary_log(created_at);
CREATE INDEX IF NOT EXISTS idx_entities_registration ON entities(registration);
CREATE INDEX IF NOT EXISTS idx_entities_entity_type ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entity_event_links_entity_id ON entity_event_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_event_links_event_id ON entity_event_links(event_id);
"""

# Key used in the `settings` table for the reporting unit's name -- edited
# via the web UI's Inställningar page, unlike everything in config.py
# which is env-var only. Used in generated report filenames.
UNIT_NAME_KEY = "unit_name"

# Key for a user-chosen folder every generated report (hotbedömning,
# händelserapport, bevakningslista) is written to on top of the browser's
# own download -- see webapp/routes.py's _write_report_to_reports_dir.
# Falls back to config.REPORTS_DIR when unset.
REPORTS_DIR_KEY = "reports_dir"

# Key used in the `settings` table for when a report (incident report or
# threat-level summary) was last successfully sent via Signal to the
# report group -- the same group adjacent units' own status reports come
# in on (see list_latest_adjacent_reports_per_unit). Shown on the header
# status strip on every page.
LAST_ADJACENT_SEND_KEY = "last_adjacent_send_at"

# Keys used in the `settings` table for a human-set override of the
# current threat level (see Sammanställd hotbedömning's "Manuell
# justering av hotnivå" card) -- layered on top of the automatic
# assessment for display/export (see analysis.apply_threat_override)
# rather than replacing it, so the automatic reasoning is always still
# visible alongside the human's correction. A single current override,
# not one per period: the header status strip and every page only ever
# show one "current" threat level.
THREAT_OVERRIDE_LEVEL_KEY = "threat_override_level"
THREAT_OVERRIDE_NOTES_KEY = "threat_override_notes"
THREAT_OVERRIDE_AT_KEY = "threat_override_at"

# Keys used in the `settings` table for the four Signal group names --
# edited via the web UI's Inställningar page, same as the unit name,
# taking priority over the SIGNAL_EVENTS_WATCH_GROUP/REPORT_GROUP/
# RECURRING_GROUP/SENSOR_GROUP env vars in config.py when set. Changes
# apply immediately to web-triggered sends (report/summary/recurring),
# but `signal-events watch`/`serve --watch`'s background poller only
# reads these at startup, so it needs restarting to pick up a change.
WATCH_GROUP_NAME_KEY = "watch_group_name"
REPORT_GROUP_NAME_KEY = "report_group_name"
RECURRING_GROUP_NAME_KEY = "recurring_group_name"
SENSOR_GROUP_NAME_KEY = "sensor_group_name"

# Key used in the `settings` table for the local Ollama server's port --
# edited via the web UI's Inställningar page, taking priority over
# whatever port is in the SIGNAL_EVENTS_OLLAMA_URL env var default (see
# config.OLLAMA_URL) when set. Only the port is stored/edited here (as a
# string, since it's only ever spliced into a URL) -- the scheme and host
# still come from config.OLLAMA_URL, same as every other Ollama setting.
OLLAMA_PORT_KEY = "ollama_port"

# Keys for the map center point set on Inställningar (see tiles.py) --
# stored as two separate string settings since `settings` is a flat
# key/value table; both are always set/cleared together.
MAP_CENTER_LAT_KEY = "map_center_lat"
MAP_CENTER_LON_KEY = "map_center_lon"

# Key for a user-supplied tile URL template (with {z}/{x}/{y} placeholders,
# and any API key baked directly into the URL's query string) -- lets
# Inställningar point tile downloads at a provider that actually permits
# bulk/offline caching instead of the public OpenStreetMap tile server,
# which blocks exactly that kind of use (see tiles.py). Falls back to
# config.DEFAULT_TILE_URL_TEMPLATE when unset.
MAP_TILE_URL_KEY = "map_tile_url_template"

# Key for the map tile mode set on Inställningar: "online" (the browser
# view fetches each tile live from the configured provider on demand,
# caching it as a side effect -- works immediately, needs connectivity,
# the way both maps worked before the local-cache feature existed) or
# "local" (tiles are served strictly from whatever's already in the local
# cache -- see tiles.py -- so viewing the map never touches the network,
# but anything outside a completed download renders blank). Falls back to
# "online" when unset, since that's the immediately-usable default; the
# bulk pre-download remains available either way, e.g. to pre-warm the
# cache before a deployment expects to lose connectivity.
MAP_TILE_MODE_KEY = "map_tile_mode"
MAP_TILE_MODE_ONLINE = "online"
MAP_TILE_MODE_LOCAL = "local"

# Key for which *source* the bulk "Ladda ner kartor för området" download
# (and, for "url", the "Online" mode on-demand fetch too) reads tiles from:
# "lantmateriet_ftp" (default) -- tiles extracted via GDAL from
# Lantmäteriet's free FTP-hosted GeoPackage (see lantmateriet_ftp.py),
# which needs no API key/account at all, so the map works out of the box.
# It's only practical as a slow, one-time bulk operation (measured
# ~1.85s/tile even batched), not for live per-tile fetching -- so this
# source always serves strictly from the local cache regardless of the
# Online/Lokal cache mode setting above (see routes.py's map_tile). Needs
# GDAL (gdal_translate) installed; the download route explains that and
# how to switch back if it isn't. The alternative, "url", fetches
# per-tile over HTTP/HTTPS from the Inställningar-configured
# tile_url_template the way every ordinary provider (MapTiler,
# Lantmäteriet's own WMTS, ...) normally works, but needs an API key.
MAP_TILE_SOURCE_KEY = "map_tile_source"
MAP_TILE_SOURCE_URL = "url"
MAP_TILE_SOURCE_LANTMATERIET_FTP = "lantmateriet_ftp"

# Key for the cached-area size preset (config.MAP_CACHE_AREA_SIZES) -- how
# large a square around the Kartcentrum point gets downloaded/cached:
# "small"/"medium"/"large" (see config.py for the actual km radius each
# maps to). Falls back to config.MAP_CACHE_DEFAULT_AREA_SIZE when unset.
MAP_CACHE_AREA_SIZE_KEY = "map_cache_area_size"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate_add_column(conn, "events", "is_trivial", "INTEGER NOT NULL DEFAULT 0")
        _migrate_add_column(conn, "events", "is_trivial_reviewed", "INTEGER NOT NULL DEFAULT 0")
        _migrate_add_column(conn, "events", "is_duplicate", "INTEGER NOT NULL DEFAULT 0")
        _migrate_add_column(conn, "events", "is_duplicate_reviewed", "INTEGER NOT NULL DEFAULT 0")
        _migrate_add_column(conn, "events", "is_sensor", "INTEGER NOT NULL DEFAULT 0")
        _migrate_add_column(conn, "events", "lat", "REAL")
        _migrate_add_column(conn, "events", "lon", "REAL")
        _migrate_add_column(conn, "events", "source_unit", "TEXT")
        _migrate_add_column(conn, "users", "last_seen", "TEXT")
        _migrate_add_column(conn, "entities", "photo_path", "TEXT")
        _migrate_add_column(conn, "entities", "watchlist", "INTEGER NOT NULL DEFAULT 0")
        _migrate_summary_log_identity_columns(conn)


def _migrate_add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """`CREATE TABLE IF NOT EXISTS` in SCHEMA above only applies to brand
    new databases -- an already-initialized table (from before `column`
    existed) needs this one-time ALTER TABLE instead. Safe to call on
    every init_db(): checks first, so it's a no-op once applied."""
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migrate_summary_log_identity_columns(conn: sqlite3.Connection) -> None:
    """summary_log's tnr/unit_name columns can't take a single static
    ALTER TABLE ... DEFAULT (each existing row needs its own tnr, derived
    from that row's own created_at) -- so this adds them nullable first,
    backfills every row, then leaves them nullable (SQLite can't add a
    NOT NULL column without a constant default to an already-populated
    table). New rows always provide both explicitly via
    insert_summary_log_entry, so this only matters for rows logged
    before this migration existed."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(summary_log)")}
    if "tnr" not in columns:
        conn.execute("ALTER TABLE summary_log ADD COLUMN tnr TEXT")
    if "unit_name" not in columns:
        conn.execute("ALTER TABLE summary_log ADD COLUMN unit_name TEXT")
    if "tnr" in columns and "unit_name" in columns:
        return  # both already existed -- nothing left to backfill

    fallback_unit_name = get_unit_name(conn) or "enhet"
    for row in conn.execute(
        "SELECT id, created_at FROM summary_log WHERE tnr IS NULL OR unit_name IS NULL"
    ):
        tnr = datetime.fromisoformat(row["created_at"]).strftime("%d%H%M")
        conn.execute(
            "UPDATE summary_log SET tnr = COALESCE(tnr, ?), "
            "unit_name = COALESCE(unit_name, ?) WHERE id = ?",
            (tnr, fallback_unit_name, row["id"]),
        )


def message_exists(conn: sqlite3.Connection, signal_timestamp: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM messages WHERE signal_timestamp = ?", (signal_timestamp,)
    ).fetchone()
    return row is not None


def insert_message(
    conn: sqlite3.Connection,
    signal_timestamp: int,
    sender_number: Optional[str],
    sender_name: Optional[str],
    body: Optional[str],
    raw_json: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO messages
           (signal_timestamp, sender_number, sender_name, body, raw_json, received_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (signal_timestamp, sender_number, sender_name, body, raw_json, now_iso()),
    )
    return cur.lastrowid


def insert_attachment(
    conn: sqlite3.Connection,
    message_id: int,
    file_path: str,
    content_type: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO attachments (message_id, file_path, content_type) VALUES (?, ?, ?)",
        (message_id, file_path, content_type),
    )
    return cur.lastrowid


def insert_event(conn: sqlite3.Connection, message_id: int, fields: dict[str, Any]) -> int:
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO events
           (message_id, event_time, place, count, object, activity, marks,
            reported_by, next_steps, raw_text, needs_review, is_trivial,
            is_duplicate, is_sensor, source_unit, lat, lon, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            message_id,
            fields.get("event_time"),
            fields.get("place"),
            fields.get("count"),
            fields.get("object"),
            fields.get("activity"),
            fields.get("marks"),
            fields.get("reported_by"),
            fields.get("next_steps"),
            fields.get("raw_text"),
            1 if fields.get("needs_review", True) else 0,
            1 if fields.get("is_trivial", False) else 0,
            1 if fields.get("is_duplicate", False) else 0,
            1 if fields.get("is_sensor", False) else 0,
            fields.get("source_unit"),
            fields.get("lat"),
            fields.get("lon"),
            ts,
            ts,
        ),
    )
    return cur.lastrowid


def update_event(conn: sqlite3.Connection, event_id: int, fields: dict[str, Any]) -> None:
    columns = [
        "event_time", "place", "count", "object", "activity", "marks",
        "reported_by", "next_steps", "needs_review", "is_trivial",
        "is_trivial_reviewed", "is_duplicate", "is_duplicate_reviewed",
        "source_unit", "lat", "lon",
    ]
    updates = {k: v for k, v in fields.items() if k in columns}
    if not updates:
        return
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE events SET {set_clause} WHERE id = ?",
        (*updates.values(), event_id),
    )


def list_events(
    conn: sqlite3.Connection,
    since: Optional[str] = None,
    needs_review: Optional[bool] = None,
    is_trivial: Optional[bool] = None,
    own_only: bool = False,
) -> list[sqlite3.Row]:
    """`own_only=True` excludes events with a source_unit set -- i.e.
    ones logged as received from an angränsande enhet rather than this
    unit's own reporters (see insert_event). Used at every choke point
    that feeds this unit's own threat analysis or generated reports
    (_compute_summary, report()/report_send(), the CLI equivalents) so
    another unit's sightings never silently inflate this unit's own
    picture; left False (the default) everywhere display-only, like
    Tidslinje, so both are still visible there, just labeled."""
    query = "SELECT * FROM events WHERE 1=1"
    params: list[Any] = []
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since)
    if needs_review is not None:
        query += " AND needs_review = ?"
        params.append(1 if needs_review else 0)
    if is_trivial is not None:
        query += " AND is_trivial = ?"
        params.append(1 if is_trivial else 0)
    if own_only:
        query += " AND source_unit IS NULL"
    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()


def get_event(conn: sqlite3.Connection, event_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def search_events(conn: sqlite3.Connection, query: str, limit: int = 50) -> list[sqlite3.Row]:
    """Fast plain-text search across every free-text field an event has --
    plats, objekt, aktivitet, kännetecken (where a vehicle registration
    number typically ends up), rapporterad av, nästa steg, and the
    original message text -- for the AI-analys tab's search box. A
    deliberately simple SQL LIKE lookup, not a semantic/LLM search:
    instant, and finds an exact plate/keyword match without waiting on
    Ollama or risking a paraphrased miss. Matches events from every unit
    (this one's own and adjacent units') and regardless of trivial/
    duplicate/review status, since the point is "has this ever been
    logged anywhere", not threat analysis. Returns [] for a blank query
    rather than every event in the database."""
    query = query.strip()
    if not query:
        return []
    needle = f"%{query}%"
    return conn.execute(
        """
        SELECT * FROM events
        WHERE place LIKE ? OR object LIKE ? OR activity LIKE ? OR marks LIKE ?
           OR reported_by LIKE ? OR next_steps LIKE ? OR raw_text LIKE ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (needle, needle, needle, needle, needle, needle, needle, limit),
    ).fetchall()


def list_events_with_position(
    conn: sqlite3.Connection, since: Optional[str] = None, include_adjacent: bool = True
) -> list[sqlite3.Row]:
    """Events with a known lat/lon -- either auto-extracted from an MGRS
    grid reference in the report, or set by a human via the map pin-drop
    on the event page. `since` is the same ISO-timestamp cutoff list_events
    takes, so the Karta overview can offer the identical Tidsperiod
    selection (24 tim/7 dagar/30 dagar/Alla) as Sammanställd hotbedömning
    and Tidslinje. `include_adjacent=False` drops events received from an
    angränsande enhet (see insert_event's source_unit) -- the Karta
    "Dölj händelser från angränsande enheter" toggle. Everything else
    here behaves the same as any other event (duplicates/trivial
    exclusion is the caller's job, same as list_events)."""
    query = "SELECT * FROM events WHERE lat IS NOT NULL AND lon IS NOT NULL"
    params: list[Any] = []
    if since is not None:
        query += " AND created_at >= ?"
        params.append(since)
    if not include_adjacent:
        query += " AND source_unit IS NULL"
    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()


def get_message(conn: sqlite3.Connection, message_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()


def list_attachments_for_message(
    conn: sqlite3.Connection, message_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE message_id = ?", (message_id,)
    ).fetchall()


def get_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def list_message_ids_with_attachments(conn: sqlite3.Connection) -> set[int]:
    """Every message id that has at least one attachment -- a single
    query, used instead of calling list_attachments_for_message once per
    event (e.g. in webapp/routes._build_ai_context, which needs to know
    per-event whether a photo exists across the whole event log)."""
    rows = conn.execute("SELECT DISTINCT message_id FROM attachments").fetchall()
    return {row["message_id"] for row in rows}


def delete_event(conn: sqlite3.Connection, event_id: int) -> bool:
    """Deletes one event (a manual "Ta bort händelse" action). Also
    deletes its underlying message and attachment rows if no other event
    still references that message -- which is always the case today (one
    message parses into one event), but this avoids ever orphaning
    another event's message if that ever changes. Returns True if the
    message/attachments were also deleted, so the caller (which fetches
    the attachment file paths beforehand) knows whether it should also
    remove the attachment files from disk.

    Also drops this event's entity_event_links rows first (FK constraint,
    PRAGMA foreign_keys = ON) and prunes any auto-extracted entity that
    links leaves with no remaining link anywhere -- a manually-added
    entity, or one still linked to another event, survives regardless."""
    event = get_event(conn, event_id)
    if event is None:
        return False
    message_id = event["message_id"]
    linked_entity_ids = [
        row["entity_id"] for row in conn.execute(
            "SELECT entity_id FROM entity_event_links WHERE event_id = ?", (event_id,)
        )
    ]
    conn.execute("DELETE FROM entity_event_links WHERE event_id = ?", (event_id,))
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    prune_orphaned_auto_entities(conn, linked_entity_ids)
    remaining = conn.execute(
        "SELECT 1 FROM events WHERE message_id = ?", (message_id,)
    ).fetchone()
    if remaining is not None:
        return False
    conn.execute("DELETE FROM attachments WHERE message_id = ?", (message_id,))
    conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
    return True


# Every table, children before parents so the FK constraints (PRAGMA
# foreign_keys = ON in get_connection) don't reject the deletes.
_ALL_TABLES = [
    "entity_event_links", "attachments", "events", "entities", "messages",
    "adjacent_report_attachments", "adjacent_reports", "adjacent_units",
    "settings", "users", "system_log",
]

# Just the event log -- events, their source messages, and attachments.
# Leaves settings (unit name), the adjacent-unit roster, and received
# adjacent-unit status reports untouched. entity_event_links is cleared
# alongside it (a link to a deleted event is meaningless either way), and
# entities.py-extracted ("auto") entities go with it since they only
# existed to represent something in the now-gone event log; manually
# catalogued entities (source='manual') are reference data like the
# adjacent-unit roster and survive, same as everything else this leaves
# untouched.
_EVENT_LOG_TABLES = ["entity_event_links", "attachments", "events", "messages"]


def _reset_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    for table in tables:
        conn.execute(f"DELETE FROM {table}")

    has_sequence_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
    ).fetchone()
    if has_sequence_table:
        conn.execute(
            f"DELETE FROM sqlite_sequence WHERE name IN "
            f"({', '.join('?' for _ in tables)})",
            tables,
        )


def reset_events(conn: sqlite3.Connection) -> None:
    """Partial reset: wipes only the event log (events, their source
    messages, and attachments), plus every entity_event_links row and
    auto-extracted entity -- both are meaningless once the events they
    came from are gone. Manually catalogued entities (source='manual')
    are reference data, like the adjacent-unit roster, and survive this,
    same as everything else it leaves untouched. Used by the "Rensa
    händelselogg" button on Inställningar. The caller is also responsible
    for clearing the non-adjacent parts of config.ATTACHMENTS_DIR on
    disk, since files there aren't tracked by SQLite itself."""
    _reset_tables(conn, _EVENT_LOG_TABLES)
    conn.execute("DELETE FROM entities WHERE source = 'auto'")


def reset_all(conn: sqlite3.Connection) -> None:
    """Full reset: wipes every table back to empty -- events, messages,
    attachments, received adjacent-unit reports, the adjacent-unit roster,
    and settings (including the unit name). Used by the "Rensa allt"
    button on Inställningar. The caller is also responsible for clearing
    config.ATTACHMENTS_DIR on disk, since files there aren't tracked by
    SQLite itself."""
    _reset_tables(conn, _ALL_TABLES)


def count_messages_by_import_filename(conn: sqlite3.Connection, filename: str) -> int:
    """How many stored messages came from a given file-import filename (see
    importer.import_text's raw_json tagging) -- used to show an "already
    imported" hint next to the training-day buttons on the import page,
    without maintaining a separate tracking table."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM messages WHERE raw_json LIKE ?",
        (f'%"filename": "{filename}"%',),
    ).fetchone()
    return row["n"] if row else 0


def has_demo_events(conn: sqlite3.Connection) -> bool:
    """Whether any stored message came from the bundled demo/training-day
    scenario (see webapp/routes.py's TRAINING_DAYS_DIR, filenames
    "dag_01.txt".."dag_10.txt") -- used to show a "Demo-läge" indicator
    in the header while any are present, since the threat level and
    other on-screen numbers would otherwise look like real operational
    data."""
    row = conn.execute(
        """SELECT 1 FROM messages
           WHERE raw_json LIKE '%"filename": "dag_%.txt"%' LIMIT 1"""
    ).fetchone()
    return row is not None


def clear_demo_events(conn: sqlite3.Connection) -> tuple[list[int], list[str]]:
    """Removes the events, messages, and attachments that came from the
    bundled demo/training-day import (see has_demo_events), *and* the
    demo-seeded adjacent-unit status reports (2.Kompani/3.Kompani) that
    same import creates -- otherwise the header badge and Sammanställd
    hotbedömning's "Status från angränsande enheter" card kept showing
    stale demo data even after "clear demo data" removed everything else.
    A *genuinely received* adjacent report (signal_client.py, always a
    real positive Signal timestamp) is never touched -- only rows with a
    negative signal_timestamp are, which is exactly and only how this
    import (and demo/seed_demo.py) mark a report as synthetic rather than
    Signal-received (see _SYNTHETIC_TIMESTAMP_OFFSET in routes.py). The
    real adjacent-unit roster (adjacent_units) and unit name are never
    touched either way.

    Returns (removed_message_ids, removed_adjacent_attachment_paths) so
    the caller can also delete the corresponding files on disk
    (ATTACHMENTS_DIR/<message_id>/... and the adjacent-report attachment
    paths), neither of which is tracked by SQLite itself."""
    rows = conn.execute(
        """SELECT id FROM messages
           WHERE raw_json LIKE '%"filename": "dag_%.txt"%'"""
    ).fetchall()
    message_ids = [row["id"] for row in rows]
    if message_ids:
        placeholders = ",".join("?" for _ in message_ids)
        event_rows = conn.execute(
            f"SELECT id FROM events WHERE message_id IN ({placeholders})", message_ids
        ).fetchall()
        event_ids = [row["id"] for row in event_rows]
        if event_ids:
            event_placeholders = ",".join("?" for _ in event_ids)
            linked_entity_rows = conn.execute(
                f"SELECT entity_id FROM entity_event_links WHERE event_id IN ({event_placeholders})",
                event_ids,
            ).fetchall()
            linked_entity_ids = [row["entity_id"] for row in linked_entity_rows]
            conn.execute(
                f"DELETE FROM entity_event_links WHERE event_id IN ({event_placeholders})", event_ids
            )
            prune_orphaned_auto_entities(conn, linked_entity_ids)
        conn.execute(f"DELETE FROM attachments WHERE message_id IN ({placeholders})", message_ids)
        conn.execute(f"DELETE FROM events WHERE message_id IN ({placeholders})", message_ids)
        conn.execute(f"DELETE FROM messages WHERE id IN ({placeholders})", message_ids)

    adjacent_rows = conn.execute(
        "SELECT id FROM adjacent_reports WHERE signal_timestamp < 0"
    ).fetchall()
    adjacent_ids = [row["id"] for row in adjacent_rows]
    attachment_paths: list[str] = []
    if adjacent_ids:
        placeholders = ",".join("?" for _ in adjacent_ids)
        attachment_rows = conn.execute(
            f"SELECT file_path FROM adjacent_report_attachments WHERE adjacent_report_id IN ({placeholders})",
            adjacent_ids,
        ).fetchall()
        attachment_paths = [row["file_path"] for row in attachment_rows]
        conn.execute(
            f"DELETE FROM adjacent_report_attachments WHERE adjacent_report_id IN ({placeholders})",
            adjacent_ids,
        )
        conn.execute(f"DELETE FROM adjacent_reports WHERE id IN ({placeholders})", adjacent_ids)

    return message_ids, attachment_paths


def get_setting(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_unit_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, UNIT_NAME_KEY, "") or ""


def set_unit_name(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, UNIT_NAME_KEY, value.strip())


def get_reports_dir(conn: sqlite3.Connection) -> Path:
    value = get_setting(conn, REPORTS_DIR_KEY)
    return Path(value).expanduser() if value else config.REPORTS_DIR


def set_reports_dir(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, REPORTS_DIR_KEY, value.strip())


def clear_reports_dir(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (REPORTS_DIR_KEY,))


def get_watch_group_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, WATCH_GROUP_NAME_KEY) or config.WATCH_GROUP_NAME


def set_watch_group_name(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, WATCH_GROUP_NAME_KEY, value.strip())


def get_report_group_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, REPORT_GROUP_NAME_KEY) or config.REPORT_GROUP_NAME


def set_report_group_name(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, REPORT_GROUP_NAME_KEY, value.strip())


def get_recurring_group_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, RECURRING_GROUP_NAME_KEY) or config.RECURRING_GROUP_NAME


def set_recurring_group_name(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, RECURRING_GROUP_NAME_KEY, value.strip())


def get_ollama_port(conn: sqlite3.Connection) -> str:
    """The port to reach the local Ollama server on, as a string -- the
    stored override if one's been set on Inställningar, otherwise
    whatever port is in config.OLLAMA_URL (defaulting to Ollama's own
    standard 11434 if that URL somehow has none)."""
    default_port = urllib.parse.urlsplit(config.OLLAMA_URL).port or 11434
    return get_setting(conn, OLLAMA_PORT_KEY) or str(default_port)


def set_ollama_port(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, OLLAMA_PORT_KEY, value.strip())


def get_sensor_group_name(conn: sqlite3.Connection) -> str:
    return get_setting(conn, SENSOR_GROUP_NAME_KEY) or config.SENSOR_GROUP_NAME


def set_sensor_group_name(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, SENSOR_GROUP_NAME_KEY, value.strip())


def get_map_center(conn: sqlite3.Connection) -> tuple[float, float]:
    """The (lat, lon) center point tile downloads and both maps in the web
    UI are built around -- falls back to config.DEFAULT_MAP_CENTER when
    nothing has been explicitly saved on Inställningar (or the stored
    value is unparsable), the same "always returns something usable"
    convention as get_map_tile_url_template. Never returns None, so every
    map/tile-cache feature has a real center to work with from the very
    first run, without requiring a save first. Use has_custom_map_center
    to tell whether the returned point is this fallback or an explicit
    override."""
    lat = get_setting(conn, MAP_CENTER_LAT_KEY)
    lon = get_setting(conn, MAP_CENTER_LON_KEY)
    if lat is None or lon is None:
        return config.DEFAULT_MAP_CENTER
    try:
        return float(lat), float(lon)
    except ValueError:
        return config.DEFAULT_MAP_CENTER


def has_custom_map_center(conn: sqlite3.Connection) -> bool:
    """True once a center has been explicitly saved on Inställningar --
    i.e. get_map_center is returning that value rather than falling back
    to config.DEFAULT_MAP_CENTER. Gates whether "Rensa kartcentrum" has
    anything to actually clear."""
    lat = get_setting(conn, MAP_CENTER_LAT_KEY)
    lon = get_setting(conn, MAP_CENTER_LON_KEY)
    if lat is None or lon is None:
        return False
    try:
        float(lat)
        float(lon)
    except ValueError:
        return False
    return True


def set_map_center(conn: sqlite3.Connection, lat: float, lon: float) -> None:
    set_setting(conn, MAP_CENTER_LAT_KEY, str(lat))
    set_setting(conn, MAP_CENTER_LON_KEY, str(lon))


def clear_map_center(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM settings WHERE key IN (?, ?)",
        (MAP_CENTER_LAT_KEY, MAP_CENTER_LON_KEY),
    )


def get_map_tile_url_template(conn: sqlite3.Connection) -> str:
    return get_setting(conn, MAP_TILE_URL_KEY) or config.DEFAULT_TILE_URL_TEMPLATE


def set_map_tile_url_template(conn: sqlite3.Connection, value: str) -> None:
    set_setting(conn, MAP_TILE_URL_KEY, value.strip())


def clear_map_tile_url_template(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (MAP_TILE_URL_KEY,))


def get_map_tile_mode(conn: sqlite3.Connection) -> str:
    value = get_setting(conn, MAP_TILE_MODE_KEY)
    if value not in (MAP_TILE_MODE_ONLINE, MAP_TILE_MODE_LOCAL):
        return MAP_TILE_MODE_ONLINE
    return value


def set_map_tile_mode(conn: sqlite3.Connection, mode: str) -> None:
    if mode not in (MAP_TILE_MODE_ONLINE, MAP_TILE_MODE_LOCAL):
        raise ValueError(f"invalid map tile mode: {mode!r}")
    set_setting(conn, MAP_TILE_MODE_KEY, mode)


def get_map_tile_source(conn: sqlite3.Connection) -> str:
    value = get_setting(conn, MAP_TILE_SOURCE_KEY)
    if value not in (MAP_TILE_SOURCE_URL, MAP_TILE_SOURCE_LANTMATERIET_FTP):
        return MAP_TILE_SOURCE_LANTMATERIET_FTP
    return value


def set_map_tile_source(conn: sqlite3.Connection, source: str) -> None:
    if source not in (MAP_TILE_SOURCE_URL, MAP_TILE_SOURCE_LANTMATERIET_FTP):
        raise ValueError(f"invalid map tile source: {source!r}")
    set_setting(conn, MAP_TILE_SOURCE_KEY, source)


def get_map_cache_area_size(conn: sqlite3.Connection) -> str:
    value = get_setting(conn, MAP_CACHE_AREA_SIZE_KEY)
    if value not in config.MAP_CACHE_AREA_SIZES:
        return config.MAP_CACHE_DEFAULT_AREA_SIZE
    return value


def set_map_cache_area_size(conn: sqlite3.Connection, size: str) -> None:
    if size not in config.MAP_CACHE_AREA_SIZES:
        raise ValueError(f"invalid map cache area size: {size!r}")
    set_setting(conn, MAP_CACHE_AREA_SIZE_KEY, size)


def get_map_cache_radius_km(conn: sqlite3.Connection) -> float:
    """The actual km radius (see config.MAP_CACHE_AREA_SIZES) for whichever
    area-size preset is currently selected -- the one value everywhere else
    (tile-count math, downloads, Karta) actually needs, so callers don't
    have to know both the setting and the size table themselves."""
    return config.MAP_CACHE_AREA_SIZES[get_map_cache_area_size(conn)]


def get_last_adjacent_send(conn: sqlite3.Connection) -> Optional[str]:
    return get_setting(conn, LAST_ADJACENT_SEND_KEY)


def set_last_adjacent_send(conn: sqlite3.Connection) -> None:
    set_setting(conn, LAST_ADJACENT_SEND_KEY, now_iso())


def get_threat_override(conn: sqlite3.Connection) -> Optional[dict]:
    level = get_setting(conn, THREAT_OVERRIDE_LEVEL_KEY)
    if not level:
        return None
    return {
        "level": level,
        "notes": get_setting(conn, THREAT_OVERRIDE_NOTES_KEY, "") or "",
        "set_at": get_setting(conn, THREAT_OVERRIDE_AT_KEY),
    }


def set_threat_override(conn: sqlite3.Connection, level: str, notes: str) -> None:
    set_setting(conn, THREAT_OVERRIDE_LEVEL_KEY, level)
    set_setting(conn, THREAT_OVERRIDE_NOTES_KEY, notes.strip())
    set_setting(conn, THREAT_OVERRIDE_AT_KEY, now_iso())


def clear_threat_override(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM settings WHERE key IN (?, ?, ?)",
        (THREAT_OVERRIDE_LEVEL_KEY, THREAT_OVERRIDE_NOTES_KEY, THREAT_OVERRIDE_AT_KEY),
    )


def list_adjacent_units(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Reference roster of known adjacent unit names, maintained on the
    Inställningar page. Not used to *identify* incoming reports -- that's
    read directly from the plain-text unit name embedded in each report's
    own filename (see naming.parse_report_filename) -- this list is for
    display/reference (e.g. showing which known units haven't reported)."""
    return conn.execute("SELECT * FROM adjacent_units ORDER BY name").fetchall()


def add_adjacent_unit(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO adjacent_units (name, created_at) VALUES (?, ?)",
        (name.strip(), now_iso()),
    )
    return cur.lastrowid


def delete_adjacent_unit(conn: sqlite3.Connection, unit_id: int) -> None:
    conn.execute("DELETE FROM adjacent_units WHERE id = ?", (unit_id,))


def adjacent_report_exists(conn: sqlite3.Connection, signal_timestamp: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM adjacent_reports WHERE signal_timestamp = ?", (signal_timestamp,)
    ).fetchone()
    return row is not None


def insert_adjacent_report(
    conn: sqlite3.Connection,
    signal_timestamp: int,
    sender_number: Optional[str],
    sender_name: Optional[str],
    unit_name: Optional[str],
    body: Optional[str],
) -> int:
    cur = conn.execute(
        """INSERT INTO adjacent_reports
           (signal_timestamp, sender_number, sender_name, unit_name, body, received_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (signal_timestamp, sender_number, sender_name, unit_name, body, now_iso()),
    )
    return cur.lastrowid


def insert_adjacent_report_attachment(
    conn: sqlite3.Connection,
    adjacent_report_id: int,
    file_path: str,
    content_type: Optional[str],
) -> int:
    cur = conn.execute(
        "INSERT INTO adjacent_report_attachments (adjacent_report_id, file_path, content_type) "
        "VALUES (?, ?, ?)",
        (adjacent_report_id, file_path, content_type),
    )
    return cur.lastrowid


def list_attachments_for_adjacent_report(
    conn: sqlite3.Connection, adjacent_report_id: int
) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM adjacent_report_attachments WHERE adjacent_report_id = ?",
        (adjacent_report_id,),
    ).fetchall()


def get_adjacent_attachment(conn: sqlite3.Connection, attachment_id: int) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM adjacent_report_attachments WHERE id = ?", (attachment_id,)
    ).fetchone()


def list_latest_adjacent_reports_per_unit(
    conn: sqlite3.Connection, since: Optional[str] = None
) -> list[sqlite3.Row]:
    """Most recent report per distinct *identified* unit (the plain-text
    unit name parsed from the report's own filename), newest first -- for
    showing each adjacent unit's current status rather than its whole
    history. Reports with no identified unit_name are excluded here --
    they're still stored (see ingest_adjacent_report), but this shared
    group can also carry plain chat from people not posting a named
    report at all, and that shouldn't show up on the summary page as if
    it were a unit's status."""
    query = "SELECT * FROM adjacent_reports WHERE unit_name IS NOT NULL AND unit_name != ''"
    params: list[Any] = []
    if since is not None:
        query += " AND received_at >= ?"
        params.append(since)
    query += " ORDER BY signal_timestamp DESC"

    seen: set[str] = set()
    latest: list[sqlite3.Row] = []
    for row in conn.execute(query, params).fetchall():
        key = row["unit_name"]
        if key in seen:
            continue
        seen.add(key)
        latest.append(row)
    return latest


def list_adjacent_reports(
    conn: sqlite3.Connection, since: Optional[str] = None, limit: Optional[int] = None
) -> list[sqlite3.Row]:
    """Full history of reports received from adjacent units, newest
    first -- unlike list_latest_adjacent_reports_per_unit this is not
    collapsed to one row per unit, and includes rows with no identified
    unit_name (plain chat in the shared group). Used to give the AI-chat
    tab (see webapp/routes.py's _build_ai_context) both current and
    older adjacent-unit reports, not just each unit's latest."""
    query = "SELECT * FROM adjacent_reports WHERE 1=1"
    params: list[Any] = []
    if since is not None:
        query += " AND received_at >= ?"
        params.append(since)
    query += " ORDER BY signal_timestamp DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return conn.execute(query, params).fetchall()


def insert_summary_log_entry(
    conn: sqlite3.Connection,
    tnr: str,
    unit_name: str,
    period_label: str,
    total_events: int,
    level: str,
    score: int,
    source: str,
    format: Optional[str] = None,
) -> int:
    """Records one threat-level summary being generated or sent, for the
    "Logg" page's time-ordered history -- `tnr`/`unit_name` identify the
    entry the same way a generated report's own filename does ("TNR
    Unit-name"), so the log entry matches whatever was actually
    downloaded/sent. `source` is "download", "send", or "cli"; `format`
    is the export format used ("pdf"/"markdown"/"text"), None for the
    CLI-less cases where it doesn't apply."""
    cur = conn.execute(
        """INSERT INTO summary_log
           (created_at, tnr, unit_name, period_label, total_events, level, score, source, format)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (now_iso(), tnr, unit_name, period_label, total_events, level, score, source, format),
    )
    return cur.lastrowid


def list_summary_log(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """All logged threat-level summary generations, most recent first.
    Ties on created_at (two entries logged within the same microsecond)
    are broken by id, so ordering stays deterministic either way."""
    return conn.execute(
        "SELECT * FROM summary_log ORDER BY created_at DESC, id DESC"
    ).fetchall()


# Key used in the `settings` table for the Flask session-signing secret --
# generated once (secrets.token_hex) and persisted, rather than a static
# value baked into source code. Every installation gets its own random
# key this way, so nobody who's simply read this open-source repo can
# forge a session cookie for someone else's running instance -- this
# matters now that the web UI can be reached by guest accounts over the
# local network (see webapp/routes.py's access-tier check), not just
# trusted localhost use.
SECRET_KEY_SETTING = "flask_secret_key"


def get_or_create_secret_key(conn: sqlite3.Connection) -> str:
    existing = get_setting(conn, SECRET_KEY_SETTING)
    if existing:
        return existing
    key = secrets.token_hex(32)
    set_setting(conn, SECRET_KEY_SETTING, key)
    return key


def create_user(conn: sqlite3.Connection, name: str, password: str) -> int:
    """Creates an additional/guest account (see webapp/routes.py's
    access-tier check) -- name and password, chosen by the admin on
    Inställningar, no email or other identity needed for a small local
    guest list. Raises ValueError if the name is already taken (checked
    explicitly rather than relying on the UNIQUE constraint's
    IntegrityError, so callers don't need to know sqlite3 specifics)."""
    name = name.strip()
    if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
        raise ValueError(f"Det finns redan en användare med namnet '{name}'.")
    cur = conn.execute(
        "INSERT INTO users (name, password_hash, created_at) VALUES (?, ?, ?)",
        (name, generate_password_hash(password), now_iso()),
    )
    return cur.lastrowid


def list_users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, created_at FROM users ORDER BY name").fetchall()


def delete_user(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def verify_user_password(conn: sqlite3.Connection, name: str, password: str) -> Optional[sqlite3.Row]:
    """Returns the matching user row if `name`/`password` are a valid
    login, else None -- covers both "no such user" and "wrong password"
    without distinguishing them to the caller, so a login form never
    reveals which one it was."""
    row = conn.execute("SELECT * FROM users WHERE name = ?", (name.strip(),)).fetchone()
    if row is None or not check_password_hash(row["password_hash"], password):
        return None
    return row


def touch_user_last_seen(conn: sqlite3.Connection, user_id: int) -> None:
    """Marks a guest account as active right now -- called on every
    authenticated request from that account (see webapp/routes.py's
    access-control hook), so Systemlogg's "Aktiva användare" list reflects
    genuine recent activity, not just "has ever logged in"."""
    conn.execute("UPDATE users SET last_seen = ? WHERE id = ?", (now_iso(), user_id))


def clear_user_last_seen(conn: sqlite3.Connection, user_id: int) -> None:
    """Called on logout so a guest who explicitly signs out drops off the
    "Aktiva användare" list immediately, rather than lingering there until
    their last activity ages out of the window."""
    conn.execute("UPDATE users SET last_seen = NULL WHERE id = ?", (user_id,))


def list_active_users(conn: sqlite3.Connection, within_minutes: int = 5) -> list[sqlite3.Row]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=within_minutes)).isoformat()
    return conn.execute(
        "SELECT * FROM users WHERE last_seen IS NOT NULL AND last_seen >= ? ORDER BY last_seen DESC",
        (cutoff,),
    ).fetchall()


def log_system_event(conn: sqlite3.Connection, event_type: str, detail: str = "") -> None:
    """Records a system-level event (login/logout/server start/denied
    access/...) for the admin-only Systemlogg page -- see
    webapp/routes.py's system_log route. An audit trail of who/what
    touched this installation and when, separate from the incident
    events reported over Signal."""
    conn.execute(
        "INSERT INTO system_log (created_at, event_type, detail) VALUES (?, ?, ?)",
        (now_iso(), event_type, detail),
    )


def list_system_log(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Most recent system events first, capped at `limit` -- this is an
    operational audit trail, not something meant to grow unbounded in the
    UI (the underlying table itself is never pruned, only the page's own
    display is capped)."""
    return conn.execute(
        "SELECT * FROM system_log ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
    ).fetchall()


# --- Entities (persons, vehicles, other objects) --------------------------
#
# See entities.py for the rule-based extraction that populates these
# ('auto'-sourced rows) and signal_events/webapp/routes.py's /entities
# routes for manual catalogue entries ('manual'-sourced). "Seen together"
# is deliberately not its own table -- it's fully determined by which
# entities link to the same event, so list_entities_seen_with derives it
# on the fly instead of keeping a second, driftable copy of that fact.


def insert_entity(
    conn: sqlite3.Connection,
    entity_type: str,
    label: str,
    registration: Optional[str] = None,
    attributes: Optional[dict] = None,
    notes: Optional[str] = None,
    source: str = "manual",
    watchlist: bool = False,
) -> int:
    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO entities
           (entity_type, label, registration, attributes, notes, source, watchlist, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entity_type, label, registration,
            json.dumps(attributes) if attributes else None,
            notes, source, 1 if watchlist else 0, ts, ts,
        ),
    )
    return cur.lastrowid


def update_entity(conn: sqlite3.Connection, entity_id: int, fields: dict[str, Any]) -> None:
    columns = [
        "entity_type", "label", "registration", "attributes", "notes", "photo_path", "watchlist",
    ]
    updates = {k: v for k, v in fields.items() if k in columns}
    if not updates:
        return
    if "attributes" in updates and isinstance(updates["attributes"], dict):
        updates["attributes"] = json.dumps(updates["attributes"]) if updates["attributes"] else None
    if "watchlist" in updates:
        updates["watchlist"] = 1 if updates["watchlist"] else 0
    updates["updated_at"] = now_iso()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE entities SET {set_clause} WHERE id = ?",
        (*updates.values(), entity_id),
    )


def get_entity(conn: sqlite3.Connection, entity_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM entities WHERE id = ?", (entity_id,)).fetchone()


def find_entity_by_registration(
    conn: sqlite3.Connection, entity_type: str, registration: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM entities WHERE entity_type = ? AND registration = ?",
        (entity_type, registration),
    ).fetchone()


def list_entities(
    conn: sqlite3.Connection,
    entity_type: Optional[str] = None,
    query: Optional[str] = None,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM entities WHERE 1=1"
    params: list[Any] = []
    if entity_type is not None:
        sql += " AND entity_type = ?"
        params.append(entity_type)
    if query:
        sql += " AND (label LIKE ? OR registration LIKE ? OR notes LIKE ? OR attributes LIKE ?)"
        like = f"%{query}%"
        params.extend([like, like, like, like])
    sql += " ORDER BY updated_at DESC"
    return conn.execute(sql, params).fetchall()


def delete_entity(conn: sqlite3.Connection, entity_id: int) -> None:
    conn.execute("DELETE FROM entity_event_links WHERE entity_id = ?", (entity_id,))
    conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))


def link_entity_to_event(
    conn: sqlite3.Connection, entity_id: int, event_id: int, source: str = "manual"
) -> None:
    """A human manually linking an entity (source='manual') always wins
    over -- and upgrades -- a pre-existing 'auto' link for the same pair,
    since a deliberate confirmation should never be silently reverted to
    'auto' (and therefore be eligible for auto-resync's cleanup) later.
    The reverse never happens: an 'auto' insert leaves an existing row
    (of either source) alone rather than downgrading it."""
    if source == "manual":
        conn.execute(
            """INSERT INTO entity_event_links (entity_id, event_id, source, created_at)
               VALUES (?, ?, 'manual', ?)
               ON CONFLICT(entity_id, event_id) DO UPDATE SET source = 'manual'""",
            (entity_id, event_id, now_iso()),
        )
    else:
        conn.execute(
            """INSERT INTO entity_event_links (entity_id, event_id, source, created_at)
               VALUES (?, ?, 'auto', ?)
               ON CONFLICT(entity_id, event_id) DO NOTHING""",
            (entity_id, event_id, now_iso()),
        )


def unlink_entity_from_event(conn: sqlite3.Connection, entity_id: int, event_id: int) -> None:
    conn.execute(
        "DELETE FROM entity_event_links WHERE entity_id = ? AND event_id = ?",
        (entity_id, event_id),
    )


def replace_auto_entity_links_for_event(
    conn: sqlite3.Connection, event_id: int, entity_ids: list[int]
) -> None:
    """Used by entities.sync_event_entities to make this event's 'auto'
    links match `entity_ids` exactly: drops any existing 'auto' link not
    in that list, then links (or leaves alone, see link_entity_to_event)
    every id in it. Manual links for this event are never touched, so a
    human's deliberate link survives even if that entity later stops
    being mentioned in the report text."""
    keep = set(entity_ids)
    existing_auto = {
        row["entity_id"] for row in conn.execute(
            "SELECT entity_id FROM entity_event_links WHERE event_id = ? AND source = 'auto'",
            (event_id,),
        )
    }
    for entity_id in existing_auto - keep:
        conn.execute(
            "DELETE FROM entity_event_links WHERE entity_id = ? AND event_id = ? AND source = 'auto'",
            (entity_id, event_id),
        )
    for entity_id in entity_ids:
        link_entity_to_event(conn, entity_id, event_id, source="auto")


def prune_orphaned_auto_entities(conn: sqlite3.Connection, entity_ids: Iterable[int]) -> None:
    """Deletes any of `entity_ids` that are source='auto' and no longer
    linked to any event -- called after an event is deleted or resynced,
    so an auto-extracted person/vehicle that only ever existed because of
    that one mention doesn't linger as a dangling catalogue entry.
    Manually added entities (and any auto entity still linked elsewhere,
    e.g. a vehicle matched by plate to another report) are left alone."""
    for entity_id in entity_ids:
        row = conn.execute(
            "SELECT source FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None or row["source"] != "auto":
            continue
        remaining = conn.execute(
            "SELECT 1 FROM entity_event_links WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if remaining is None:
            conn.execute("DELETE FROM entities WHERE id = ?", (entity_id,))


def list_events_for_entity(conn: sqlite3.Connection, entity_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT events.*, entity_event_links.source AS link_source
           FROM events
           JOIN entity_event_links ON entity_event_links.event_id = events.id
           WHERE entity_event_links.entity_id = ?
           ORDER BY events.created_at DESC""",
        (entity_id,),
    ).fetchall()


def list_entities_for_event(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT entities.*, entity_event_links.source AS link_source
           FROM entities
           JOIN entity_event_links ON entity_event_links.entity_id = entities.id
           WHERE entity_event_links.event_id = ?
           ORDER BY entities.label""",
        (event_id,),
    ).fetchall()


def list_entities_seen_with(conn: sqlite3.Connection, entity_id: int) -> list[sqlite3.Row]:
    """Other entities linked to at least one of the same events as
    entity_id -- derived from entity_event_links rather than a persisted
    "seen together" table (see module note above)."""
    return conn.execute(
        """SELECT DISTINCT other.*
           FROM entity_event_links AS mine
           JOIN entity_event_links AS others
             ON others.event_id = mine.event_id AND others.entity_id != mine.entity_id
           JOIN entities AS other ON other.id = others.entity_id
           WHERE mine.entity_id = ?
           ORDER BY other.label""",
        (entity_id,),
    ).fetchall()


# Recurring, for the watchlist report below, means "linked to at least
# this many distinct events" -- a plain database fact, unlike
# analysis.py's RecurrenceGroup (regex/Jaccard text clustering over raw
# event rows, used only for the separate hotbedömning threat score).
WATCHLIST_MIN_EVENTS = 2


def list_watchlist_entities(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Entities to include in the "Skicka bevakningslista" report (see
    webapp/routes.py's entities_send_watchlist): every entity linked to
    WATCHLIST_MIN_EVENTS+ events (recurring, by database fact alone) plus
    every entity a human has manually flagged via the "Bevaka"
    checkbox (entities.watchlist), regardless of its own event count.
    Each row carries an extra `event_count` column."""
    return conn.execute(
        """SELECT entities.*, COUNT(entity_event_links.id) AS event_count
           FROM entities
           LEFT JOIN entity_event_links ON entity_event_links.entity_id = entities.id
           GROUP BY entities.id
           HAVING event_count >= ? OR entities.watchlist = 1
           ORDER BY entities.entity_type, event_count DESC, entities.label""",
        (WATCHLIST_MIN_EVENTS,),
    ).fetchall()
