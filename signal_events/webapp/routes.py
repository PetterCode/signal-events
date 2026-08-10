from __future__ import annotations

import io
import ipaddress
import json
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, send_file,
    session, url_for,
)
from werkzeug.utils import secure_filename

from .. import (
    analysis, config, coordinates, db, demo_map, duplicates, entities, importer, lantmateriet_ftp,
    llm, naming, signal_client, tiles, triviality,
)
from ..reports import generator

bp = Blueprint("events", __name__)

# Guards against starting a second tile-download thread while one is
# already running -- a plain in-process flag is enough since this app is a
# single local process, not a multi-worker deployment. Reset by a process
# restart, which is fine: any in-flight download thread would be gone too.
_tile_download_lock = threading.Lock()

# --- Access control -------------------------------------------------------
#
# This app has no concept of "the admin" as a stored account: whoever can
# reach it from the machine it's running on (127.0.0.1/::1) always has
# full access, exactly like before this feature existed -- unchanged for
# the one person actually running signal-events. Additional/guest
# accounts (name + password, created on Inställningar -- see create_user)
# exist only for OTHER devices on the same private WiFi/LAN, and are
# deliberately restricted: no Inställningar, no Demo och övning. A
# request that isn't even coming from a private network address at all
# (i.e. not "the same local network" the admin is on) is refused outright,
# before any login is attempted -- guest accounts are for people on your
# WiFi, not for exposing this over the internet.
_ADMIN_ONLY_ENDPOINTS = {
    "events.settings",
    "events.save_groups",
    "events.save_ollama_port",
    "events.add_adjacent_unit",
    "events.delete_adjacent_unit",
    "events.reset_event_log",
    "events.reset_database",
    "events.create_user",
    "events.delete_user",
    "events.lan_qrcode",
    "events.demo_import",
    "events.demo_clear",
    "events.demo_sensor_toggle",
    "events.import_training_day",
    "events.system_log",
    "events.save_map_center",
    "events.clear_map_center",
    "events.save_map_tile_url",
    "events.save_reports_dir",
    "events.save_map_tile_mode",
    "events.save_map_tile_source",
    "events.save_map_cache_area_size",
    "events.download_map_tiles",
    "events.purge_blocked_map_tiles",
    "events.stop_server",
}


def _access_tier() -> str:
    """"admin": the local machine itself, no login needed (unchanged
    original behavior). "guest": another device on the same private
    network -- must log in as one of the accounts created on
    Inställningar. "blocked": not on a private network at all."""
    try:
        ip = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return "blocked"
    if not ip.is_private:
        return "blocked"
    return "admin" if ip.is_loopback else "guest"


def _safe_next_url(candidate: str | None) -> str | None:
    """Only ever follow a same-site relative path for post-login
    redirects -- an absolute or protocol-relative URL in `next` could
    otherwise send a user somewhere attacker-controlled."""
    if candidate and candidate.startswith("/") and not candidate.startswith("//"):
        return candidate
    return None


@bp.before_request
def _enforce_access_control():
    tier = _access_tier()
    if tier == "blocked":
        with db.get_connection() as conn:
            db.log_system_event(conn, "blocked_access", request.remote_addr or "okänd adress")
        abort(403)
    if tier == "admin":
        return
    # tier == "guest": the login/logout routes themselves must stay
    # reachable, otherwise a guest could never actually log in.
    if request.endpoint in ("events.login", "events.logout"):
        return
    if "guest_user_id" not in session:
        return redirect(url_for("events.login", next=request.path))
    with db.get_connection() as conn:
        db.touch_user_last_seen(conn, session["guest_user_id"])
    if request.endpoint in _ADMIN_ONLY_ENDPOINTS:
        abort(403)

# Bundled demo/training scenario: `demo/training_days/dag_01.txt`..
# `dag_10.txt`, 30 reports each, escalating from trivial noise to a
# confirmed recurring severe indicator -- imported one day at a time via
# the buttons on the import page, so a class or a new operator can watch
# the threat level build up rather than seeing a finished example dropped
# in all at once. `adjacent_status.json` carries the same day's status
# reports from the two adjacent units (see demo/generate_training_days.py),
# which escalate on the same rhythm but one day earlier/later.
TRAINING_DAYS_DIR = config.PROJECT_ROOT / "demo" / "training_days"
TRAINING_DAYS_COUNT = 10
ADJACENT_STATUS_PATH = TRAINING_DAYS_DIR / "adjacent_status.json"

# Cartoon-style stand-ins for a phone photo, one per notable "signal"
# event type (the recurring van, the person in dark clothing, the two
# sabotage signs, the two armed sightings) -- see
# demo/generate_training_images.py. event_images.json maps each day to
# the TNRs of that day's events that should get one, generated alongside
# the day's own report text (demo/generate_training_days.py) so the two
# files can never drift out of sync with each other.
TRAINING_IMAGES_DIR = TRAINING_DAYS_DIR / "images"
EVENT_IMAGES_PATH = TRAINING_DAYS_DIR / "event_images.json"

# Offset for synthetic (non-Signal) timestamps: large enough that
# `time.time_ns() - _SYNTHETIC_TIMESTAMP_OFFSET` stays negative (so it's
# never mistaken for a real Signal timestamp) for centuries, while still
# increasing monotonically with wall-clock time -- unlike `-time.time_ns()`
# used elsewhere for synthetic *event* timestamps, which deliberately
# flips sign and therefore ordering. That inversion doesn't matter for
# events (list_events orders by its own created_at column, not
# signal_timestamp) but it would silently break
# list_latest_adjacent_reports_per_unit's "most recent report per unit"
# query, which does rely on signal_timestamp ordering.
_SYNTHETIC_TIMESTAMP_OFFSET = 10**19

_SINCE_PRESETS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}


def _since_iso(preset: str) -> str | None:
    delta = _SINCE_PRESETS.get(preset)
    if delta is None:
        return None
    return (datetime.now(timezone.utc) - delta).isoformat()


def _unit_name() -> str:
    with db.get_connection() as conn:
        return db.get_unit_name(conn)


def _report_group_name() -> str:
    with db.get_connection() as conn:
        return db.get_report_group_name(conn)


def _recurring_group_name() -> str:
    with db.get_connection() as conn:
        return db.get_recurring_group_name(conn)


def _lan_ip() -> str | None:
    """Best-effort local-network IP -- the address another device on the
    same WiFi/LAN would use to reach this machine, not 127.0.0.1. Opens a
    UDP socket "connected" to a public address and reads back which local
    address the OS routed it through; UDP connect() never actually sends
    a packet, so this needs no real connectivity and contacts nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _lan_url() -> str | None:
    """The URL to share with a guest (see Inställningar's QR code), or
    None if the server isn't actually reachable from other devices --
    either because it's still bound to 127.0.0.1 (the default; see
    cli.cmd_serve's BIND_HOST) or because the local IP couldn't be
    determined at all."""
    if current_app.config.get("BIND_HOST") in (None, "127.0.0.1", "localhost"):
        return None
    ip = _lan_ip()
    if ip is None:
        return None
    port = request.host.split(":")[-1] if ":" in request.host else "80"
    return f"http://{ip}:{port}"


_SINCE_LABELS = {"24h": "24 tim", "7d": "7 dagar", "30d": "30 dagar", "all": "alla"}


def _format_dt(iso: str | None) -> str:
    if not iso:
        return "Aldrig"
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")


@bp.app_context_processor
def inject_header_status() -> dict:
    """Populates the status strip shown under "Signalhändelser" on every
    page: unit name, the threat level, when a report (incident report or
    threat-level summary) was last sent to the Signal group that adjacent
    units' own status reports also come in on, and when Signal was last
    successfully *received* from (see db.record_receive_attempt) -- the
    only way to tell from the web UI that the background watch poller
    (see cli.py's _run_watch_loop/signal_client.watch_multi) is actually
    alive and succeeding, since the web server itself stays up
    regardless of whether that separate thread has died or has started
    failing every cycle. The threat level here must always agree with
    the Sammanställd hotbedömning page, so it reuses that page's own
    computation (_compute_summary, which excludes duplicates) over the
    exact same session-remembered period/filter that page is currently
    showing -- not some independently fixed window that could silently
    disagree with it. Skipped for the login page itself -- it doesn't
    extend base.html (so none of this would be shown anyway), and this
    avoids running a duplicate-classification DB write for every
    anonymous hit the login page gets from the network."""
    if request.endpoint in ("events.login", "events.logout"):
        return {}
    preset = session.get("summary_since", "7d")
    include_unreviewed = session.get("summary_include_unreviewed", False)
    with db.get_connection() as conn:
        unit_name = db.get_unit_name(conn)
        last_adjacent_send_at = db.get_last_adjacent_send(conn)
        last_receive_success_at = db.get_last_receive_success(conn)
        receive_error = db.get_last_receive_error(conn)
        demo_mode = db.has_demo_events(conn)
        adjacent_reports = db.list_latest_adjacent_reports_per_unit(conn)
    threat = _compute_summary(preset, include_unreviewed).threat
    adjacent_statuses = [
        {
            "unit_name": report["unit_name"],
            "level": analysis.parse_adjacent_level(report["body"]),
            "received_at": _format_dt(report["received_at"]),
        }
        for report in adjacent_reports
    ]
    return {
        "header_unit_name": unit_name,
        "header_threat": threat,
        "header_threat_period_label": _SINCE_LABELS.get(preset, preset),
        "header_last_adjacent_send": _format_dt(last_adjacent_send_at),
        "header_last_receive_success": _format_dt(last_receive_success_at),
        "header_receive_error": receive_error,
        "header_adjacent_statuses": adjacent_statuses,
        "header_demo_mode": demo_mode,
        "header_is_admin": _access_tier() == "admin",
        "header_guest_name": session.get("guest_user_name"),
    }


def _field_form_values() -> dict:
    return {
        "event_time": request.form.get("event_time", "").strip() or None,
        "place": request.form.get("place", "").strip() or None,
        "count": request.form.get("count", "").strip() or None,
        "object": request.form.get("object", "").strip() or None,
        "activity": request.form.get("activity", "").strip() or None,
        "marks": request.form.get("marks", "").strip() or None,
        "reported_by": request.form.get("reported_by", "").strip() or None,
        "next_steps": request.form.get("next_steps", "").strip() or None,
    }


def _position_form_values() -> dict:
    """Manual pin-drop on the event page always wins over whatever
    coordinates.py auto-extracted from an MGRS grid reference -- returns {}
    (no-op, existing value untouched) when the map wasn't touched this
    submission, {"lat": None, "lon": None} when the "Ta bort position"
    button was used, or the clicked lat/lon otherwise."""
    if request.form.get("clear_position"):
        return {"lat": None, "lon": None}
    lat_raw = request.form.get("lat", "").strip()
    lon_raw = request.form.get("lon", "").strip()
    if not lat_raw or not lon_raw:
        return {}
    try:
        return {"lat": float(lat_raw), "lon": float(lon_raw)}
    except ValueError:
        return {}


def _save_uploaded_photos(conn, message_id: int) -> None:
    for file in request.files.getlist("photos"):
        if not file or not file.filename:
            continue
        if file.mimetype and not file.mimetype.startswith("image/"):
            continue
        dest_dir = config.ATTACHMENTS_DIR / str(message_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        # secure_filename strips path separators/special chars but not
        # length -- an uploaded filename longer than the filesystem's own
        # limit (255 bytes on macOS/most Linux) makes file.save() raise
        # OSError("File name too long") instead of just saving under a
        # shorter name, so the stem is capped well under that regardless
        # of what the client sent.
        safe_name = secure_filename(file.filename)
        stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
        dest = dest_dir / (stem[:100] + suffix[:20])
        file.save(dest)
        db.insert_attachment(
            conn, message_id=message_id, file_path=str(dest), content_type=file.mimetype
        )


def _save_entity_photo(conn, entity_id: int, file) -> None:
    """Saves (or replaces) the one reference photo an entity can have --
    a no-op if `file` is missing/empty, same as _save_uploaded_photos.
    Unlike event attachments (one message can carry many, kept forever as
    a list), an entity has at most one photo, so a new upload deletes the
    previous file from disk rather than accumulating them."""
    if not file or not file.filename:
        return
    if file.mimetype and not file.mimetype.startswith("image/"):
        return
    existing = db.get_entity(conn, entity_id)
    if existing and existing["photo_path"]:
        Path(existing["photo_path"]).unlink(missing_ok=True)
    dest_dir = config.ATTACHMENTS_DIR / "entities" / str(entity_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_name = secure_filename(file.filename)
    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
    dest = dest_dir / (stem[:100] + suffix[:20])
    file.save(dest)
    db.update_entity(conn, entity_id, {"photo_path": str(dest)})


@bp.route("/")
def index():
    return redirect(url_for("events.list_events"))


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        with db.get_connection() as conn:
            user = db.verify_user_password(conn, name, password)
            if user is None:
                db.log_system_event(conn, "login_failed", f"{name} ({request.remote_addr})")
            else:
                db.log_system_event(conn, "login", f"{user['name']} ({request.remote_addr})")
                db.touch_user_last_seen(conn, user["id"])
        if user is None:
            return render_template(
                "login.html", error="Fel namn eller lösenord.", next=request.form.get("next", ""),
            )
        session["guest_user_id"] = user["id"]
        session["guest_user_name"] = user["name"]
        next_url = _safe_next_url(request.form.get("next")) or url_for("events.list_events")
        return redirect(next_url)
    return render_template("login.html", next=request.args.get("next", ""))


@bp.route("/logout", methods=["POST"])
def logout():
    user_id = session.get("guest_user_id")
    user_name = session.get("guest_user_name")
    if user_id is not None:
        with db.get_connection() as conn:
            db.clear_user_last_seen(conn, user_id)
            db.log_system_event(conn, "logout", f"{user_name} ({request.remote_addr})")
    session.pop("guest_user_id", None)
    session.pop("guest_user_name", None)
    return redirect(url_for("events.login"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    with db.get_connection() as conn:
        if request.method == "POST":
            unit_name = request.form.get("unit_name", "")
            db.set_unit_name(conn, unit_name)
            flash("Enhetsnamn sparat.")
            return redirect(url_for("events.settings"))
        unit_name = db.get_unit_name(conn)
        reports_dir = db.get_reports_dir(conn)
        adjacent_units = db.list_adjacent_units(conn)
        watch_group = db.get_watch_group_name(conn)
        report_group = db.get_report_group_name(conn)
        recurring_group = db.get_recurring_group_name(conn)
        sensor_group = db.get_sensor_group_name(conn)
        ollama_port = db.get_ollama_port(conn)
        users = db.list_users(conn)
        map_center = db.get_map_center(conn)
        map_center_is_custom = db.has_custom_map_center(conn)
        map_tile_url_template = db.get_map_tile_url_template(conn)
        map_tile_mode = db.get_map_tile_mode(conn)
        map_tile_source = db.get_map_tile_source(conn)
        map_cache_area_size = db.get_map_cache_area_size(conn)
        map_cache_radius_km = db.get_map_cache_radius_km(conn)

    example_filename = naming.build_report_filename(unit_name, "hotbedomning", "pdf")
    # map_center always has a real value now (config.DEFAULT_MAP_CENTER
    # until something's explicitly saved), so the cache-area figures
    # below are always computable, not just once a center's been set.
    map_center_mgrs = coordinates.to_mgrs(*map_center)
    map_expected_tiles = tiles.expected_tile_count(
        map_center[0], map_center[1], map_cache_radius_km,
        config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM,
    )
    map_cached_tiles = tiles.cached_tile_count_for_area(
        config.TILE_CACHE_DIR, map_center[0], map_center[1], map_cache_radius_km,
        config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM,
    )
    return render_template(
        "settings.html", unit_name=unit_name, example_filename=example_filename,
        reports_dir=str(reports_dir), reports_dir_is_default=reports_dir == config.REPORTS_DIR,
        adjacent_units=adjacent_units, watch_group=watch_group,
        report_group=report_group, recurring_group=recurring_group,
        sensor_group=sensor_group, ollama_port=ollama_port, users=users,
        lan_url=_lan_url(),
        map_center=map_center, map_center_mgrs=map_center_mgrs,
        map_center_is_custom=map_center_is_custom,
        map_cache_radius_km=map_cache_radius_km,
        map_cache_area_size=map_cache_area_size,
        map_cache_min_zoom=config.MAP_CACHE_MIN_ZOOM,
        map_cache_max_zoom=config.MAP_CACHE_MAX_ZOOM,
        map_cached_tiles=map_cached_tiles,
        map_expected_tiles=map_expected_tiles,
        map_tile_url_template=map_tile_url_template,
        map_tile_url_is_default=map_tile_url_template == config.DEFAULT_TILE_URL_TEMPLATE,
        map_tile_mode=map_tile_mode,
        map_tile_source=map_tile_source,
        map_gdal_available=lantmateriet_ftp.gdal_available(),
    )


@bp.route("/settings/lan-qrcode.png")
def lan_qrcode():
    url = _lan_url()
    if url is None:
        abort(404)
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make()
    img = qr.make_image()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@bp.route("/system-log")
def system_log():
    with db.get_connection() as conn:
        entries = db.list_system_log(conn)
        active_users = db.list_active_users(conn)
    return render_template("system_log.html", entries=entries, active_users=active_users)


@bp.route("/settings/groups", methods=["POST"])
def save_groups():
    with db.get_connection() as conn:
        db.set_watch_group_name(conn, request.form.get("watch_group", ""))
        db.set_report_group_name(conn, request.form.get("report_group", ""))
        db.set_recurring_group_name(conn, request.form.get("recurring_group", ""))
        db.set_sensor_group_name(conn, request.form.get("sensor_group", ""))
    flash("Signal-grupper sparade.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/ollama", methods=["POST"])
def save_ollama_port():
    port = request.form.get("ollama_port", "").strip()
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        flash("Ogiltig port -- ange ett portnummer mellan 1 och 65535.", "error")
    else:
        with db.get_connection() as conn:
            db.set_ollama_port(conn, port)
        flash(f"Ollama-port sparad ({port}). Gäller direkt för AI-analys.")
    return redirect(url_for("events.settings"))


def _valid_map_center_latlon(lat: float, lon: float) -> bool:
    # ±85.0511... (not ±90) is the actual usable range: it's where Web
    # Mercator itself -- the projection tiles.py's tile math assumes --
    # stops being defined (the pole is an asymptote, not a point on the
    # map), the same limit Leaflet/OSM/Google Maps all clamp to. A center
    # inside ±90 but outside this would crash every tile-count/tile-cache
    # computation on this page.
    return -85.0511 <= lat <= 85.0511 and -180 <= lon <= 180


@bp.route("/settings/map-center", methods=["POST"])
def save_map_center():
    mgrs_raw = request.form.get("mgrs", "").strip()
    # MGRS wins if both are filled in -- same precedence extract_position()
    # uses when parsing a report, and it saves re-typing the lat/lon
    # fields just because a grid reference was already at hand.
    if mgrs_raw:
        latlon = coordinates.extract_mgrs_latlon(mgrs_raw)
        if latlon is None or not _valid_map_center_latlon(*latlon):
            flash("Ogiltig MGRS-referens -- t.ex. 33VVN1234567890.", "error")
            return redirect(url_for("events.settings"))
        lat, lon = latlon
        with db.get_connection() as conn:
            db.set_map_center(conn, lat, lon)
        flash(f"Kartcentrum sparat ({lat:.5f}, {lon:.5f}).")
        return redirect(url_for("events.settings"))

    lat_raw = request.form.get("lat", "").strip()
    lon_raw = request.form.get("lon", "").strip()
    try:
        lat, lon = float(lat_raw), float(lon_raw)
        if not _valid_map_center_latlon(lat, lon):
            raise ValueError
    except ValueError:
        flash(
            "Ogiltig position -- ange en MGRS-referens, eller latitud "
            "(-85.05 till 85.05) och longitud (-180 till 180), t.ex. "
            "59.3300, 18.0600.", "error",
        )
    else:
        with db.get_connection() as conn:
            db.set_map_center(conn, lat, lon)
        flash(f"Kartcentrum sparat ({lat:.5f}, {lon:.5f}).")
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-center/clear", methods=["POST"])
def clear_map_center():
    with db.get_connection() as conn:
        db.clear_map_center(conn)
    flash("Kartcentrum borttaget.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-tile-url", methods=["POST"])
def save_map_tile_url():
    url_template = request.form.get("tile_url_template", "").strip()
    if not url_template:
        with db.get_connection() as conn:
            db.clear_map_tile_url_template(conn)
        flash(f"Kartleverantör återställd till standard ({config.DEFAULT_TILE_URL_TEMPLATE}).")
    elif "{z}" not in url_template or "{x}" not in url_template or "{y}" not in url_template:
        flash(
            "Ogiltig kart-URL -- måste innehålla platshållarna {z}, {x} och {y}, "
            "t.ex. https://api.maptiler.com/maps/basic-v2/{z}/{x}/{y}.png?key=DIN_NYCKEL",
            "error",
        )
    else:
        with db.get_connection() as conn:
            db.set_map_tile_url_template(conn, url_template)
        flash("Kartleverantör sparad.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/reports-dir", methods=["POST"])
def save_reports_dir():
    reports_dir = request.form.get("reports_dir", "").strip()
    if not reports_dir:
        with db.get_connection() as conn:
            db.clear_reports_dir(conn)
        flash(f"Rapportmapp återställd till standard ({config.REPORTS_DIR}).")
        return redirect(url_for("events.settings"))

    path = Path(reports_dir).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        flash(f"Kunde inte skapa/använda mappen {path}: {exc}", "error")
        return redirect(url_for("events.settings"))

    with db.get_connection() as conn:
        db.set_reports_dir(conn, str(path))
    flash(f"Rapportmapp sparad ({path}).")
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-tile-mode", methods=["POST"])
def save_map_tile_mode():
    mode = request.form.get("tile_mode", "").strip()
    try:
        with db.get_connection() as conn:
            db.set_map_tile_mode(conn, mode)
    except ValueError:
        flash("Ogiltigt kartläge.", "error")
    else:
        if mode == db.MAP_TILE_MODE_ONLINE:
            flash("Kartläge satt till online -- kartrutor hämtas live vid behov.")
        else:
            flash("Kartläge satt till lokal cache -- endast nedladdade kartrutor visas.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-tile-source", methods=["POST"])
def save_map_tile_source():
    source = request.form.get("tile_source", "").strip()
    try:
        with db.get_connection() as conn:
            db.set_map_tile_source(conn, source)
    except ValueError:
        flash("Ogiltig kartkälla.", "error")
    else:
        if source == db.MAP_TILE_SOURCE_LANTMATERIET_FTP:
            flash(
                "Kartkälla satt till Lantmäteriets FTP -- endast nedladdade "
                "kartrutor visas oavsett kartläge, eftersom denna källa är för "
                "långsam för att hämta rutor live.",
            )
        else:
            flash("Kartkälla satt till URL-baserad kartleverantör.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-cache-area-size", methods=["POST"])
def save_map_cache_area_size():
    size = request.form.get("area_size", "").strip()
    try:
        with db.get_connection() as conn:
            db.set_map_cache_area_size(conn, size)
    except ValueError:
        flash("Ogiltig områdesstorlek.", "error")
    else:
        radius_km = config.MAP_CACHE_AREA_SIZES[size]
        flash(f"Områdesstorlek satt till {size} (radie {radius_km:g} km, dvs {radius_km * 2:g} x {radius_km * 2:g} km).")
    return redirect(url_for("events.settings"))


def _download_tiles_in_background(
    center_lat: float, center_lon: float, tile_url_template: str, source: str,
    radius_km: float,
) -> None:
    try:
        if source == db.MAP_TILE_SOURCE_LANTMATERIET_FTP:
            written, failed_bands = lantmateriet_ftp.extract_area_to_cache(
                center_lat, center_lon, radius_km,
                config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM, config.TILE_CACHE_DIR,
            )
            with db.get_connection() as conn:
                detail = f"källa=lantmateriet_ftp nya_rutor={written}"
                if failed_bands:
                    detail += f" misslyckade_delar={failed_bands}"
                db.log_system_event(conn, "map_tiles_download_finished", detail)
            return

        downloaded, skipped, failed, blocked = tiles.download_area(
            center_lat, center_lon, radius_km,
            config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM, config.TILE_CACHE_DIR,
            tile_url_template=tile_url_template,
        )
        with db.get_connection() as conn:
            if blocked:
                db.log_system_event(
                    conn, "map_tiles_download_blocked",
                    f"nedladdade={downloaded} redan_cachade={skipped} misslyckade={failed} "
                    "-- OpenStreetMap blockerade fler förfrågningar, se README.",
                )
            else:
                db.log_system_event(
                    conn, "map_tiles_download_finished",
                    f"nedladdade={downloaded} redan_cachade={skipped} misslyckade={failed}",
                )
    except Exception as exc:
        with db.get_connection() as conn:
            db.log_system_event(conn, "map_tiles_download_failed", str(exc))
    finally:
        _tile_download_lock.release()


@bp.route("/settings/map-center/download", methods=["POST"])
def download_map_tiles():
    """Kicks off a background thread that fills the local tile cache
    (tiles.py) for the Inställningar-configured area size (small/medium/
    large -- see db.get_map_cache_radius_km) around the configured center
    point -- the one deliberate, network-touching step that lets both maps
    in the web UI work fully offline afterward. Guarded so a second click
    while one is already running doesn't start a redundant, overlapping
    fetch (download_area is idempotent/resumable on its own, but there's no
    reason to run two at once).

    Uses whichever source is configured on Inställningar -- the normal
    per-tile URL fetch (tiles.download_area), or Lantmäteriet's free FTP
    GeoPackage via GDAL (lantmateriet_ftp.extract_area_to_cache), which
    needs no API key but is drastically slower (measured ~1.85s/tile even
    batched -- several hours for this app's usual area, vs. minutes for a
    working URL-based provider)."""
    with db.get_connection() as conn:
        # Falls back to config.DEFAULT_MAP_CENTER when nothing's been
        # explicitly saved -- a download can be kicked off (deliberately
        # or by mistake) before Kartcentrum's ever been touched, so this
        # always has a real point to work with rather than needing its
        # own separate "no center set yet" error path.
        center = db.get_map_center(conn)
        center_is_custom = db.has_custom_map_center(conn)
        tile_url_template = db.get_map_tile_url_template(conn)
        source = db.get_map_tile_source(conn)
        radius_km = db.get_map_cache_radius_km(conn)

    # No Kartcentrum saved yet -- downloading would silently cache tiles
    # around config.DEFAULT_MAP_CENTER (Stockholm Palace), which is
    # almost never the actual skyddsobjekt. Require an explicit
    # confirmation checkbox in that case rather than just proceeding, so
    # a click before Kartcentrum's ever been touched can't waste a bulk
    # download (or, worse, quietly leave the cache pointed at the wrong
    # place) without the operator noticing.
    if not center_is_custom and request.form.get("confirm_default_center") != "1":
        flash(
            "Kartcentrum är inte angivet -- bocka i rutan om du vill ladda "
            "ner kartor runt standardpunkten (Kungliga slottet, Stockholm) "
            "ändå, eller ange skyddsobjektets egen position ovan först.",
            "error",
        )
        return redirect(url_for("events.settings"))

    if source == db.MAP_TILE_SOURCE_LANTMATERIET_FTP and not lantmateriet_ftp.gdal_available():
        flash(
            "GDAL (gdal_translate) är inte installerat -- krävs för Lantmäteriets "
            "FTP-källa. Installera t.ex. med \"brew install gdal\", eller växla "
            "tillbaka till URL-baserad kartleverantör.", "error",
        )
        return redirect(url_for("events.settings"))

    expected = tiles.expected_tile_count(
        center[0], center[1], radius_km,
        config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM,
    )
    if expected > config.MAP_CACHE_MAX_TILE_COUNT:
        flash(
            f"Området skulle kräva {expected} kartrutor, vilket överstiger "
            f"gränsen ({config.MAP_CACHE_MAX_TILE_COUNT}). Välj en mindre "
            "områdesstorlek.", "error",
        )
        return redirect(url_for("events.settings"))

    if not _tile_download_lock.acquire(blocking=False):
        flash("En nedladdning pågår redan -- vänta tills den är klar.", "error")
        return redirect(url_for("events.settings"))

    with db.get_connection() as conn:
        db.log_system_event(
            conn, "map_tiles_download_started",
            f"källa={source} centrum=({center[0]:.5f}, {center[1]:.5f}) "
            f"radie={radius_km}km "
            f"zoom={config.MAP_CACHE_MIN_ZOOM}-{config.MAP_CACHE_MAX_ZOOM} rutor={expected}",
        )
    threading.Thread(
        target=_download_tiles_in_background,
        args=(center[0], center[1], tile_url_template, source, radius_km), daemon=True,
    ).start()
    if source == db.MAP_TILE_SOURCE_LANTMATERIET_FTP:
        flash(
            f"Nedladdning av {expected} kartrutor via Lantmäteriets FTP startad i "
            "bakgrunden -- detta är betydligt långsammare än en URL-baserad "
            "leverantör (kan ta flera timmar för hela området). Uppdatera sidan "
            "för att se hur många som laddats ner.",
        )
    else:
        flash(
            f"Nedladdning av {expected} kartrutor startad i bakgrunden -- det kan ta "
            "en stund. Uppdatera sidan för att se hur många som laddats ner.",
        )
    return redirect(url_for("events.settings"))


@bp.route("/settings/map-center/purge-blocked", methods=["POST"])
def purge_blocked_map_tiles():
    """Removes any cached tile that's actually OpenStreetMap's "you've
    been blocked" notice image rather than real map imagery (see
    tiles.py's _BLOCKED_HEADER/_BLOCKED_TILE_SHA256) -- a one-time cleanup
    for a cache poisoned by a download that predates the fix that stops
    and flags this instead of silently caching it."""
    removed = tiles.purge_blocked_tiles(config.TILE_CACHE_DIR)
    with db.get_connection() as conn:
        db.log_system_event(conn, "map_tiles_purge_blocked", f"borttagna={removed}")
    if removed:
        flash(
            f"{removed} blockerade kartrutor togs bort ur cachen. Kör "
            "nedladdningen igen senare för att fylla i dem på nytt."
        )
    else:
        flash("Inga blockerade kartrutor hittades i cachen.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/users", methods=["POST"])
def create_user():
    name = request.form.get("name", "").strip()
    password = request.form.get("password", "")
    if not name or not password:
        flash("Ange både namn och lösenord.", "error")
    else:
        with db.get_connection() as conn:
            try:
                db.create_user(conn, name, password)
            except ValueError as exc:
                flash(str(exc), "error")
            else:
                flash(f"Användaren '{name}' skapad.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id: int):
    with db.get_connection() as conn:
        db.delete_user(conn, user_id)
    flash("Användaren borttagen.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/adjacent-units", methods=["POST"])
def add_adjacent_unit():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Ange ett namn för den angränsande enheten.", "error")
    else:
        with db.get_connection() as conn:
            db.add_adjacent_unit(conn, name)
        flash(f"Angränsande enhet '{name}' tillagd.")
    return redirect(url_for("events.settings"))


@bp.route("/settings/adjacent-units/<int:unit_id>/delete", methods=["POST"])
def delete_adjacent_unit(unit_id: int):
    with db.get_connection() as conn:
        db.delete_adjacent_unit(conn, unit_id)
    flash("Angränsande enhet borttagen.")
    return redirect(url_for("events.settings"))


def _blank_tile_png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (256, 256), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


_BLANK_TILE_PNG = _blank_tile_png()


@bp.route("/tiles/<int:zoom>/<int:x>/<int:y>.png")
def map_tile(zoom: int, x: int, y: int):
    """Serves one map tile to the browser, behaviour depending on the
    Inställningar-configured map tile mode (db.get_map_tile_mode):

    - Demo mode (db.has_demo_events): serves a procedurally-generated
      cartoon-style dummy tile (see demo_map.py) instead of any real
      provider, regardless of the configured mode -- demo/training event
      positions are fictional, so showing them over real imagery would be
      misleading, and this way trying the demo needs no tile provider
      token or network at all.
    - "online" (the default otherwise): fetches the tile live from the
      configured provider if it isn't already cached, caching it for next
      time -- the map works immediately, the way it did before local tile
      caching existed, and needs connectivity for whatever isn't cached
      yet.
    - "local": serves strictly from the local cache (tiles.py), same as
      before this mode setting existed -- never touches the network
      itself, so an area outside a completed download just renders blank
      rather than Leaflet's broken-image icon.

    If the configured tile *source* (db.get_map_tile_source) is
    Lantmäteriet's FTP GeoPackage rather than a URL, "online" mode is
    ignored and tiles are always served cache-only -- per-tile fetching
    through that source is far too slow for live browsing (see
    lantmateriet_ftp.py), so it's bulk-download-only.

    Either way, keeping the tile provider URL server-side (rather than
    pointing Leaflet at it directly) means any API key baked into it
    never reaches the browser."""
    with db.get_connection() as conn:
        demo_active = db.has_demo_events(conn)
        mode = db.get_map_tile_mode(conn)
        source = db.get_map_tile_source(conn)
        tile_url_template = db.get_map_tile_url_template(conn)
    if demo_active:
        return send_file(io.BytesIO(demo_map.generate_demo_tile(zoom, x, y)), mimetype="image/png")
    if mode == db.MAP_TILE_MODE_ONLINE and source == db.MAP_TILE_SOURCE_URL:
        data = tiles.fetch_tile_on_demand(zoom, x, y, config.TILE_CACHE_DIR, tile_url_template)
        if data is not None:
            return send_file(io.BytesIO(data), mimetype="image/png")
        return send_file(io.BytesIO(_BLANK_TILE_PNG), mimetype="image/png")
    path = tiles.tile_path(config.TILE_CACHE_DIR, zoom, x, y)
    if path.exists():
        return send_file(path, mimetype="image/png")
    return send_file(io.BytesIO(_BLANK_TILE_PNG), mimetype="image/png")


@bp.route("/karta")
def map_view():
    preset = request.args.get("since", "7d")
    # "1" (default) shows events received from an angränsande enhet
    # alongside this unit's own; "0" is the Kart-vy "Dölj händelser från
    # angränsande enheter" toggle.
    show_adjacent = request.args.get("adjacent", "1") != "0"
    # "1" (default) shows every event marker; "0" is "Dölj alla
    # händelser" -- a plain decluttering view of Kartcentrum/crosshair
    # against the base map. Filtered client-side (not at the query
    # below) so the "N händelser" count/hint stays accurate even while
    # markers themselves are hidden.
    show_events = request.args.get("events", "1") != "0"
    with db.get_connection() as conn:
        events = db.list_events_with_position(
            conn, since=_since_iso(preset), include_adjacent=show_adjacent
        )
        map_center = db.get_map_center(conn)
        map_cache_radius_km = db.get_map_cache_radius_km(conn)
        map_cache_area_size = db.get_map_cache_area_size(conn)
        map_tile_mode = db.get_map_tile_mode(conn)
        map_tile_source = db.get_map_tile_source(conn)
    # Tiles are served strictly from the local cache -- rather than
    # fetched live -- whenever "Lokal cache" mode is on, or regardless of
    # mode when the Lantmäteriet FTP source is selected (per-tile fetching
    # through it is too slow, see map_tile()) -- exactly the cases where
    # "how much of the area is actually cached" is worth showing.
    using_local_cache = (
        map_tile_mode == db.MAP_TILE_MODE_LOCAL or map_tile_source != db.MAP_TILE_SOURCE_URL
    )
    map_cached_tiles = None
    map_expected_tiles = None
    if using_local_cache:
        map_expected_tiles = tiles.expected_tile_count(
            map_center[0], map_center[1], map_cache_radius_km,
            config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM,
        )
        map_cached_tiles = tiles.cached_tile_count_for_area(
            config.TILE_CACHE_DIR, map_center[0], map_center[1], map_cache_radius_km,
            config.MAP_CACHE_MIN_ZOOM, config.MAP_CACHE_MAX_ZOOM,
        )
    markers = [
        {
            "id": e["id"],
            "lat": e["lat"],
            "lon": e["lon"],
            "tnr": naming.event_tnr(e["created_at"]),
            "place": e["place"],
            "object": e["object"],
            "activity": e["activity"],
            "source_unit": e["source_unit"],
            "url": url_for("events.event_detail", event_id=e["id"]),
            "in_cache": tiles.point_in_cached_area(
                e["lat"], e["lon"], map_center[0], map_center[1], map_cache_radius_km
            ),
        }
        for e in events
    ]
    return render_template(
        "karta.html", markers=markers, map_has_center=True,
        map_fallback_lat=map_center[0], map_fallback_lon=map_center[1],
        map_min_zoom=config.MAP_CACHE_MIN_ZOOM,
        map_max_zoom=config.MAP_CACHE_MAX_ZOOM, since=preset,
        show_adjacent=show_adjacent, show_events=show_events,
        using_local_cache=using_local_cache, map_cache_area_size=map_cache_area_size,
        map_cached_tiles=map_cached_tiles, map_expected_tiles=map_expected_tiles,
    )


@bp.route("/karta/mgrs")
def map_center_mgrs():
    """Tiny JSON endpoint backing Kart-vy's crosshair position box: the
    map itself only knows plain lat/lon (Leaflet has no MGRS support), so
    the crosshair's JS asks this route to convert whatever point is
    currently under it, the same conversion Kartcentrum's own MGRS line
    uses (coordinates.to_mgrs) -- offline, same-origin, no different than
    any other local page asset."""
    lat = request.args.get("lat", type=float)
    lon = request.args.get("lon", type=float)
    if lat is None or lon is None:
        return jsonify({"mgrs": None}), 400
    return jsonify({"mgrs": coordinates.to_mgrs(lat, lon)})


@bp.route("/events")
def list_events():
    preset = request.args.get("since", "7d")
    review_filter = request.args.get("needs_review")
    needs_review = None
    if review_filter == "1":
        needs_review = True
    elif review_filter == "0":
        needs_review = False

    with db.get_connection() as conn:
        events = db.list_events(conn, since=_since_iso(preset), needs_review=needs_review)
        duplicate_ids = duplicates.classify_duplicate_events(conn, events)
        rows = []
        for event in events:
            message = db.get_message(conn, event["message_id"])
            attachments = db.list_attachments_for_message(conn, event["message_id"])
            rows.append({
                "event": event, "message": message, "attachments": attachments,
                "is_duplicate": event["id"] in duplicate_ids,
            })

    # Ordered by when the app received each report (created_at), newest
    # first -- the same timestamp TNR itself now displays.
    rows.sort(key=lambda row: row["event"]["created_at"], reverse=True)

    return render_template(
        "events_list.html", rows=rows, since=preset, review_filter=review_filter
    )


@bp.route("/events/new", methods=["GET", "POST"])
def new_event():
    return _new_event_view(adjacent=False)


@bp.route("/events/new-adjacent", methods=["GET", "POST"])
def new_adjacent_event():
    """Same manual-entry flow as new_event, but for logging an event
    received *from* an angränsande enhet (phone call, radio, a status
    report that mentioned a specific sighting) rather than one of this
    unit's own reports -- the resulting event is tagged with
    source_unit so it's tracked separately: shown but excluded from this
    unit's own threat analysis and generated reports (db.list_events'
    own_only), and toggleable on Karta."""
    return _new_event_view(adjacent=True)


def _new_event_view(*, adjacent: bool):
    template = "event_form_adjacent.html" if adjacent else "event_form.html"
    if request.method == "GET":
        with db.get_connection() as conn:
            map_center = db.get_map_center(conn)
            adjacent_units = db.list_adjacent_units(conn) if adjacent else []
        return render_template(
            template, map_has_center=True,
            map_fallback_lat=map_center[0], map_fallback_lon=map_center[1],
            map_min_zoom=config.MAP_CACHE_MIN_ZOOM,
            map_max_zoom=config.MAP_CACHE_MAX_ZOOM,
            adjacent_units=adjacent_units,
        )

    fields = _field_form_values()
    notes = request.form.get("notes", "").strip()
    fields["raw_text"] = notes or None
    fields["needs_review"] = 0 if request.form.get("mark_reviewed") else 1

    if adjacent:
        source_unit = request.form.get("source_unit", "").strip()
        if not source_unit:
            flash("Ange vilken angränsande enhet händelsen kommer från.", "error")
            return redirect(url_for("events.new_adjacent_event"))
        fields["source_unit"] = source_unit

    position = _position_form_values()
    if position:
        fields.update(position)
    else:
        # No pin dropped -- fall back to the same auto-extraction a
        # Signal-ingested report gets, in case the place/notes text
        # happens to contain a position (MGRS or otherwise) typed by hand.
        latlon = coordinates.extract_position(fields.get("place")) or coordinates.extract_position(notes)
        if latlon is not None:
            fields["lat"], fields["lon"] = latlon

    with db.get_connection() as conn:
        # No Signal message backs a manually entered report, so we create a
        # synthetic one (negative timestamp -> never collides with a real,
        # always-positive Signal timestamp) to keep storage/display uniform
        # with Signal-sourced events.
        message_id = db.insert_message(
            conn,
            signal_timestamp=-time.time_ns(),
            sender_number=None,
            sender_name=fields["reported_by"],
            body=notes,
            raw_json=json.dumps({"source": "manual_adjacent" if adjacent else "manual"}),
        )
        _save_uploaded_photos(conn, message_id)
        event_id = db.insert_event(conn, message_id=message_id, fields=fields)
        entities.sync_event_entities(conn, event_id)

    return redirect(url_for("events.event_detail", event_id=event_id))


@bp.route("/events/import", methods=["GET", "POST"])
def import_events():
    if request.method == "GET":
        return render_template("import_form.html")

    file = request.files.get("file")
    if not file or not file.filename:
        flash("Välj en textfil att importera.", "error")
        return redirect(url_for("events.import_events"))

    try:
        text = file.read().decode("utf-8")
    except UnicodeDecodeError:
        flash("Kunde inte läsa filen som UTF-8-text.", "error")
        return redirect(url_for("events.import_events"))

    default_reported_by = request.form.get("reported_by", "").strip() or None

    with db.get_connection() as conn:
        event_ids = importer.import_text(
            conn, text, filename=secure_filename(file.filename),
            default_reported_by=default_reported_by,
        )

    if not event_ids:
        flash("Inga rapporter hittades i filen (tom, eller inga block hittades).", "error")
        return redirect(url_for("events.import_events"))

    flash(f"Importerade {len(event_ids)} rapport(er) – granska dem innan de tas med i en rapport.")
    return redirect(url_for("events.list_events", since="all", needs_review="1"))


@bp.route("/events/import/demo")
def demo_import():
    with db.get_connection() as conn:
        training_days = [
            {
                "day": day,
                "available": (TRAINING_DAYS_DIR / f"dag_{day:02d}.txt").exists(),
                "imported_count": db.count_messages_by_import_filename(
                    conn, f"dag_{day:02d}.txt"
                ),
                "sensor_available": (TRAINING_DAYS_DIR / f"dag_{day:02d}_sensor.txt").exists(),
                "sensor_imported_count": db.count_messages_by_import_filename(
                    conn, f"dag_{day:02d}_sensor.txt"
                ),
            }
            for day in range(1, TRAINING_DAYS_COUNT + 1)
        ]
    return render_template(
        "demo_import.html", training_days=training_days,
        include_sensors=session.get("demo_include_sensors", False),
    )


@bp.route("/events/import/demo/sensor-toggle", methods=["POST"])
def demo_sensor_toggle():
    session["demo_include_sensors"] = bool(request.form.get("include_sensors"))
    flash(
        "Sensorhändelser kommer nu tas med vid import av kommande dagar."
        if session["demo_include_sensors"] else
        "Sensorhändelser tas inte längre med vid import av kommande dagar."
    )
    return redirect(url_for("events.demo_import"))


@bp.route("/events/import/demo/clear", methods=["POST"])
def demo_clear():
    with db.get_connection() as conn:
        message_ids, adjacent_attachment_paths = db.clear_demo_events(conn)

    for message_id in message_ids:
        shutil.rmtree(config.ATTACHMENTS_DIR / str(message_id), ignore_errors=True)
    for path in adjacent_attachment_paths:
        Path(path).unlink(missing_ok=True)

    if message_ids:
        flash(f"Demohändelser borttagna ({len(message_ids)} st).")
    else:
        flash("Inga demohändelser att ta bort.")

    return redirect(url_for("events.demo_import"))


def _clear_event_attachment_files() -> None:
    """Removes only the event-log's attachment files on disk (organized
    as ATTACHMENTS_DIR/<message_id>/...) -- leaves
    ATTACHMENTS_DIR/adjacent/<report_id>/... alone, since those belong to
    adjacent-unit reports, not the event log."""
    if not config.ATTACHMENTS_DIR.is_dir():
        return
    for child in config.ATTACHMENTS_DIR.iterdir():
        if child.name == "adjacent":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink(missing_ok=True)


@bp.route("/database/reset-events", methods=["POST"])
def reset_event_log():
    with db.get_connection() as conn:
        db.reset_events(conn)
    _clear_event_attachment_files()
    flash("Händelseloggen har rensats – alla rapporter, meddelanden och bilagor är borttagna.")
    return redirect(url_for("events.settings"))


@bp.route("/database/reset", methods=["POST"])
def reset_database():
    with db.get_connection() as conn:
        db.reset_all(conn)
    shutil.rmtree(config.ATTACHMENTS_DIR, ignore_errors=True)
    config.ensure_dirs()
    flash(
        "Databasen har rensats – alla rapporter, meddelanden, bilagor, "
        "mottagna statusrapporter och inställningar är borttagna."
    )
    return redirect(url_for("events.settings"))


def _exit_process() -> None:
    os._exit(0)


@bp.route("/settings/stop-server", methods=["POST"])
def stop_server():
    """Shuts the running signal-events process down -- the counterpart to
    "Starta server.command" (see README) for someone who started it that
    way and has no terminal open to Ctrl-C it again. The actual exit
    happens a moment later from a background thread, after this request's
    own response has already been handed to Werkzeug to send -- calling
    os._exit() directly in the handler risks the connection closing before
    the confirmation page reaches the browser."""
    with db.get_connection() as conn:
        db.log_system_event(conn, "server_stop", "Stoppad via Inställningar")

    def _stop_soon():
        time.sleep(0.5)
        _exit_process()

    threading.Thread(target=_stop_soon, daemon=True).start()
    return render_template("server_stopped.html")


def _attach_training_images(conn: sqlite3.Connection, event_ids: list[int], day: int) -> int:
    """Copies the cartoon stand-in photo (see TRAINING_IMAGES_DIR) onto
    whichever of this day's just-imported events are listed in
    event_images.json, matched by TNR -- the same file-under-
    ATTACHMENTS_DIR/<message_id>/ convention real Signal attachments use
    (see signal_client._copy_attachment), so they display and get
    cleaned up (demo_clear) exactly like any other attachment. Returns
    how many were attached, for the caller's flash message."""
    if not EVENT_IMAGES_PATH.exists():
        return 0
    manifest = json.loads(EVENT_IMAGES_PATH.read_text(encoding="utf-8"))
    by_tnr = {entry["tnr"]: entry["image"] for entry in manifest.get(str(day), [])}
    if not by_tnr:
        return 0

    attached = 0
    for event_id in event_ids:
        event = db.get_event(conn, event_id)
        image_name = by_tnr.get(event["event_time"]) if event else None
        if not image_name:
            continue
        src = TRAINING_IMAGES_DIR / image_name
        if not src.exists():
            continue
        dest_dir = config.ATTACHMENTS_DIR / str(event["message_id"])
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / image_name
        shutil.copyfile(src, dest)
        db.insert_attachment(conn, message_id=event["message_id"], file_path=str(dest), content_type="image/png")
        attached += 1
    return attached


@bp.route("/events/import/training/<int:day>", methods=["POST"])
def import_training_day(day: int):
    if not 1 <= day <= TRAINING_DAYS_COUNT:
        abort(404)

    filename = f"dag_{day:02d}.txt"
    path = TRAINING_DAYS_DIR / filename
    if not path.exists():
        flash(f"Hittar ingen övningsfil för Dag {day}.", "error")
        return redirect(url_for("events.demo_import"))

    text = path.read_text(encoding="utf-8")
    with db.get_connection() as conn:
        event_ids = importer.import_text(conn, text, filename=filename)

        sensor_count = 0
        if session.get("demo_include_sensors", False):
            sensor_filename = f"dag_{day:02d}_sensor.txt"
            sensor_path = TRAINING_DAYS_DIR / sensor_filename
            if sensor_path.exists():
                sensor_ids = importer.import_text(
                    conn, sensor_path.read_text(encoding="utf-8"), filename=sensor_filename,
                    is_sensor=True,
                )
                event_ids += sensor_ids
                sensor_count = len(sensor_ids)

        image_count = _attach_training_images(conn, event_ids, day)

        adjacent_count = 0
        if ADJACENT_STATUS_PATH.exists():
            all_days = json.loads(ADJACENT_STATUS_PATH.read_text(encoding="utf-8"))
            for i, entry in enumerate(all_days.get(str(day), [])):
                db.insert_adjacent_report(
                    conn,
                    signal_timestamp=time.time_ns() - _SYNTHETIC_TIMESTAMP_OFFSET - i,
                    sender_number=None,
                    sender_name=entry["unit_name"],
                    unit_name=entry["unit_name"],
                    body=entry["body"],
                )
                adjacent_count += 1

    if not event_ids:
        flash(f"Inga rapporter hittades i Dag {day}-filen.", "error")
        return redirect(url_for("events.demo_import"))

    message = (
        f"Importerade {len(event_ids)} rapport(er) från Dag {day} – "
        "granska dem innan de tas med i en rapport."
    )
    if sensor_count:
        message += f" Av dessa är {sensor_count} automatiska sensorhändelser."
    if adjacent_count:
        message += (
            f" Även {adjacent_count} statusrapport(er) från angränsande "
            "enheter mottagna."
        )
    if image_count:
        message += f" {image_count} rapport(er) har en bifogad bild."
    flash(message)
    return redirect(url_for("events.list_events", since="all", needs_review="1"))


@bp.route("/events/<int:event_id>", methods=["GET", "POST"])
def event_detail(event_id: int):
    with db.get_connection() as conn:
        event = db.get_event(conn, event_id)
        if event is None:
            abort(404)

        if request.method == "POST":
            fields = _field_form_values()
            fields.update(_position_form_values())
            fields["needs_review"] = 0 if request.form.get("mark_reviewed") else 1
            fields["is_trivial"] = 1 if request.form.get("mark_trivial") else 0
            # A human just made a deliberate call on the "Trivial"
            # checkbox (whether they changed it or left it as-is) -- lock
            # it so the auto-classifier in triviality.py never overrides
            # that judgment on a later report generation.
            fields["is_trivial_reviewed"] = 1
            fields["is_duplicate"] = 1 if request.form.get("mark_duplicate") else 0
            # Same lock, same reasoning, for duplicates.py's classifier --
            # this is the only way to clear a false-positive duplicate
            # flag (or confirm one the heuristic missed) and have it stick.
            fields["is_duplicate_reviewed"] = 1
            fields["is_important"] = 1 if request.form.get("mark_important") else 0
            db.update_event(conn, event_id, fields)
            entities.sync_event_entities(conn, event_id)
            _save_uploaded_photos(conn, event["message_id"])
            return redirect(url_for("events.event_detail", event_id=event_id))

        message = db.get_message(conn, event["message_id"])
        attachments = db.list_attachments_for_message(conn, event["message_id"])
        map_center = db.get_map_center(conn)
        map_cache_radius_km = db.get_map_cache_radius_km(conn)
        linked_entities = db.list_entities_for_event(conn, event_id)

    source = json.loads(message["raw_json"]).get("source", "signal")
    map_position_outside_cache = (
        event["lat"] is not None
        and event["lon"] is not None
        and not tiles.point_in_cached_area(
            event["lat"], event["lon"], map_center[0], map_center[1], map_cache_radius_km
        )
    )
    return render_template(
        "event_detail.html", event=event, message=message, attachments=attachments,
        source=source, map_has_center=True,
        map_fallback_lat=map_center[0], map_fallback_lon=map_center[1],
        map_min_zoom=config.MAP_CACHE_MIN_ZOOM,
        map_max_zoom=config.MAP_CACHE_MAX_ZOOM,
        map_position_outside_cache=map_position_outside_cache,
        linked_entities=linked_entities,
        entity_type_labels=_ENTITY_TYPE_LABELS,
    )


@bp.route("/events/<int:event_id>/delete", methods=["POST"])
def delete_event(event_id: int):
    with db.get_connection() as conn:
        event = db.get_event(conn, event_id)
        if event is None:
            abort(404)
        attachments = db.list_attachments_for_message(conn, event["message_id"])
        message_also_deleted = db.delete_event(conn, event_id)

    if message_also_deleted:
        for attachment in attachments:
            Path(attachment["file_path"]).unlink(missing_ok=True)

    flash("Händelsen har tagits bort.")
    return redirect(url_for("events.list_events"))


_ENTITY_TYPES = ["person", "vehicle", "object"]
_ENTITY_TYPE_LABELS = {"person": "Person", "vehicle": "Fordon", "object": "Objekt"}

# A person has no registration plate -- these are its structured identity
# fields instead, stored as attributes JSON keys (same keys entities.py's
# composer-block parser already produces for auto-extracted persons, see
# event_form.html's person panel) rather than dedicated columns, since
# unlike a vehicle's plate they need no cross-report dedup lookup.
_PERSON_IDENTITY_FIELDS = [
    ("name", "Namn"), ("alias", "Alias"), ("nationality", "Nationalitet"),
    ("date_of_birth", "Födelsedatum"),
]


def _person_identity_attributes_from_form(existing: dict[str, str] | None = None) -> dict[str, str]:
    attributes = dict(existing) if existing else {}
    for field_name, attr_key in _PERSON_IDENTITY_FIELDS:
        value = request.form.get(field_name, "").strip()
        if value:
            attributes[attr_key] = value
        else:
            attributes.pop(attr_key, None)
    return attributes


@bp.route("/entities")
def list_entities():
    entity_type = request.args.get("type") or None
    if entity_type not in _ENTITY_TYPES:
        entity_type = None
    query = request.args.get("q", "").strip()

    with db.get_connection() as conn:
        rows = db.list_entities(conn, entity_type=entity_type, query=query or None)
        entities_view = [_entity_view(conn, row) for row in rows]

    return render_template(
        "entities_list.html", entities=entities_view, entity_type=entity_type, query=query,
        entity_type_labels=_ENTITY_TYPE_LABELS, recurring_group_name=_recurring_group_name(),
    )


@bp.route("/entities/<int:entity_id>/watchlist", methods=["POST"])
def set_entity_watchlist(entity_id: int):
    """Per-row "Bevaka" checkbox on Personer, fordon och objekt -- each
    row is its own tiny auto-submitting form (see entities_list.html), so
    this only ever toggles one entity and redirects back to the same
    filtered/searched view it came from."""
    with db.get_connection() as conn:
        if db.get_entity(conn, entity_id) is None:
            abort(404)
        db.update_entity(conn, entity_id, {"watchlist": bool(request.form.get("watchlist"))})
    return redirect(url_for(
        "events.list_entities",
        type=request.form.get("type") or None,
        q=request.form.get("q") or None,
    ))


@bp.route("/entities/new", methods=["GET", "POST"])
def new_entity():
    if request.method == "GET":
        preselect_type = request.args.get("type") or "person"
        link_event_id = request.args.get("event_id", type=int)
        return render_template(
            "entity_form.html", entity=None, entity_type=preselect_type,
            entity_type_labels=_ENTITY_TYPE_LABELS, link_event_id=link_event_id,
        )

    entity_type = request.form.get("entity_type", "").strip()
    if entity_type not in _ENTITY_TYPES:
        flash("Ogiltig typ.", "error")
        return redirect(url_for("events.new_entity"))
    label = request.form.get("label", "").strip()
    if not label:
        flash("Ange ett namn/en beteckning.", "error")
        return redirect(url_for("events.new_entity", type=entity_type))
    notes = request.form.get("notes", "").strip() or None
    link_event_id = request.form.get("link_event_id", type=int)

    registration_norm = None
    attributes = None
    if entity_type == "vehicle":
        registration = request.form.get("registration", "").strip() or None
        registration_norm = entities._normalize_plate(registration) if registration else None
    elif entity_type == "person":
        attributes = _person_identity_attributes_from_form() or None

    with db.get_connection() as conn:
        entity_id = db.insert_entity(
            conn, entity_type=entity_type, label=label, registration=registration_norm,
            attributes=attributes, notes=notes, source="manual",
        )
        if link_event_id is not None and db.get_event(conn, link_event_id) is not None:
            db.link_entity_to_event(conn, entity_id, link_event_id, source="manual")
        _save_entity_photo(conn, entity_id, request.files.get("photo"))

    flash(f"{_ENTITY_TYPE_LABELS[entity_type]} tillagd.")
    return redirect(url_for("events.entity_detail", entity_id=entity_id))


@bp.route("/entities/<int:entity_id>", methods=["GET", "POST"])
def entity_detail(entity_id: int):
    with db.get_connection() as conn:
        entity = db.get_entity(conn, entity_id)
        if entity is None:
            abort(404)

        if request.method == "POST":
            label = request.form.get("label", "").strip()
            notes = request.form.get("notes", "").strip() or None
            if label:
                updates = {
                    "label": label,
                    "notes": notes,
                    "watchlist": bool(request.form.get("watchlist")),
                }
                if entity["entity_type"] == "vehicle":
                    registration = request.form.get("registration", "").strip() or None
                    updates["registration"] = entities._normalize_plate(registration) if registration else None
                elif entity["entity_type"] == "person":
                    existing_attributes = json.loads(entity["attributes"]) if entity["attributes"] else {}
                    updates["attributes"] = _person_identity_attributes_from_form(existing_attributes)
                db.update_entity(conn, entity_id, updates)
            return redirect(url_for("events.entity_detail", entity_id=entity_id))

        attributes = json.loads(entity["attributes"]) if entity["attributes"] else {}
        linked_events = db.list_events_for_entity(conn, entity_id)
        seen_with = [_entity_view(conn, row) for row in db.list_entities_seen_with(conn, entity_id)]

    return render_template(
        "entity_detail.html", entity=entity, attributes=attributes,
        linked_events=linked_events, seen_with=seen_with,
        entity_type_labels=_ENTITY_TYPE_LABELS,
    )


@bp.route("/entities/<int:entity_id>/delete", methods=["POST"])
def delete_entity(entity_id: int):
    with db.get_connection() as conn:
        entity = db.get_entity(conn, entity_id)
        if entity is None:
            abort(404)
        db.delete_entity(conn, entity_id)
    if entity["photo_path"]:
        Path(entity["photo_path"]).unlink(missing_ok=True)
    flash("Posten har tagits bort.")
    return redirect(url_for("events.list_entities"))


@bp.route("/entities/<int:entity_id>/photo", methods=["POST"])
def entity_photo(entity_id: int):
    with db.get_connection() as conn:
        if db.get_entity(conn, entity_id) is None:
            abort(404)
        _save_entity_photo(conn, entity_id, request.files.get("photo"))
    return redirect(url_for("events.entity_detail", entity_id=entity_id))


@bp.route("/entities/<int:entity_id>/photo/delete", methods=["POST"])
def delete_entity_photo(entity_id: int):
    with db.get_connection() as conn:
        entity = db.get_entity(conn, entity_id)
        if entity is None:
            abort(404)
        if entity["photo_path"]:
            Path(entity["photo_path"]).unlink(missing_ok=True)
            db.update_entity(conn, entity_id, {"photo_path": None})
    return redirect(url_for("events.entity_detail", entity_id=entity_id))


@bp.route("/entities/<int:entity_id>/photo/file")
def entity_photo_file(entity_id: int):
    with db.get_connection() as conn:
        entity = db.get_entity(conn, entity_id)
    if entity is None or not entity["photo_path"]:
        abort(404)
    return send_file(entity["photo_path"])


@bp.route("/entities/<int:entity_id>/link", methods=["POST"])
def link_entity(entity_id: int):
    event_id = request.form.get("event_id", type=int)
    with db.get_connection() as conn:
        if db.get_entity(conn, entity_id) is None:
            abort(404)
        if event_id is None or db.get_event(conn, event_id) is None:
            flash("Hittade ingen händelse med det ID:t.", "error")
            return redirect(url_for("events.entity_detail", entity_id=entity_id))
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")
    return redirect(url_for("events.entity_detail", entity_id=entity_id))


@bp.route("/events/<int:event_id>/link-entity", methods=["POST"])
def link_entity_to_event(event_id: int):
    """Same link as link_entity, from the other end -- for the "Länka en
    befintlig post" form on event_detail.html, which knows the event_id
    already and asks the human for the entity_id."""
    entity_id = request.form.get("entity_id", type=int)
    with db.get_connection() as conn:
        if db.get_event(conn, event_id) is None:
            abort(404)
        if entity_id is None or db.get_entity(conn, entity_id) is None:
            flash("Hittade ingen post med det ID:t.", "error")
            return redirect(url_for("events.event_detail", event_id=event_id))
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")
    return redirect(url_for("events.event_detail", event_id=event_id))


@bp.route("/entities/<int:entity_id>/unlink/<int:event_id>", methods=["POST"])
def unlink_entity(entity_id: int, event_id: int):
    redirect_to = request.form.get("redirect_to")
    with db.get_connection() as conn:
        db.unlink_entity_from_event(conn, entity_id, event_id)
    if redirect_to == "event":
        return redirect(url_for("events.event_detail", event_id=event_id))
    return redirect(url_for("events.entity_detail", entity_id=entity_id))


def _entity_view(conn, row: sqlite3.Row) -> dict:
    return {
        "row": row,
        "attributes": json.loads(row["attributes"]) if row["attributes"] else {},
        "event_count": len(db.list_events_for_entity(conn, row["id"])),
    }


@bp.route("/attachments/<int:attachment_id>")
def attachment_file(attachment_id: int):
    with db.get_connection() as conn:
        attachment = db.get_attachment(conn, attachment_id)
    if attachment is None:
        abort(404)
    return send_file(attachment["file_path"])


@bp.route("/adjacent-attachments/<int:attachment_id>")
def adjacent_attachment_file(attachment_id: int):
    with db.get_connection() as conn:
        attachment = db.get_adjacent_attachment(conn, attachment_id)
    if attachment is None:
        abort(404)
    return send_file(attachment["file_path"])


@bp.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "GET":
        return render_template("report_form.html", report_group_name=_report_group_name())

    preset = request.form.get("since", "7d")
    fmt = request.form.get("format", "pdf")
    include_unreviewed = bool(request.form.get("include_unreviewed"))

    with db.get_connection() as conn:
        needs_review = None if include_unreviewed else False
        # own_only=True: a generated report is this unit's own account --
        # events received from an angränsande enhet stay out of it.
        events = db.list_events(
            conn, since=_since_iso(preset), needs_review=needs_review, own_only=True
        )
        trivial_ids = triviality.classify_trivial_events(conn, events)
        rows = []
        for event in events:
            if event["id"] in trivial_ids:
                continue
            message = db.get_message(conn, event["message_id"])
            attachments = db.list_attachments_for_message(conn, event["message_id"])
            rows.append({"event": event, "message": message, "attachments": attachments})

    unit_name = _unit_name()
    if fmt == "markdown":
        content_bytes = generator.render_markdown(rows, since_label=preset).encode("utf-8")
        mimetype = "text/markdown"
        filename = naming.build_report_filename(unit_name, "handelserapport", "md")
    elif fmt == "text":
        content_bytes = generator.render_text(rows, since_label=preset).encode("utf-8")
        mimetype = "text/plain"
        filename = naming.build_report_filename(unit_name, "handelserapport", "txt")
    else:
        content_bytes = generator.render_pdf(rows, since_label=preset).read()
        mimetype = "application/pdf"
        filename = naming.build_report_filename(unit_name, "handelserapport", "pdf")

    with db.get_connection() as conn:
        _write_report_to_reports_dir(conn, content_bytes, filename)
    return send_file(
        io.BytesIO(content_bytes), mimetype=mimetype, as_attachment=True, download_name=filename,
    )


def _write_report_to_reports_dir(conn, content: bytes, filename: str) -> Path:
    """Persists a copy of every generated report (hotbedömning,
    händelserapport, bevakningslista) to the Inställningar-configured
    Rapportmapp (see db.get_reports_dir), alongside whatever the caller
    still does with the same bytes (typically also streaming it back as
    a browser download) -- this app runs on the user's own laptop, so
    there's always a meaningful local folder to keep an archival copy
    in, without waiting on the browser's own download location/prompt."""
    reports_dir = db.get_reports_dir(conn)
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / filename
    path.write_bytes(content)
    return path


def _send_pdf_to_group(buf: io.BytesIO, caption: str, group_name: str, filename: str) -> None:
    """Write a generated PDF to a temp file named `filename` (so the
    Signal attachment shows a meaningful name, not a random tmp name),
    send it to `group_name`, then clean up. Raises
    signal_client.SignalCliError on failure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / filename
        tmp_path.write_bytes(buf.read())
        signal_client.send_to_group_by_name(
            group_name, message=caption, attachment_paths=[str(tmp_path)]
        )


@bp.route("/report/send", methods=["POST"])
def report_send():
    preset = request.form.get("since", "7d")
    include_unreviewed = bool(request.form.get("include_unreviewed"))

    with db.get_connection() as conn:
        needs_review = None if include_unreviewed else False
        # own_only=True: a generated report is this unit's own account --
        # events received from an angränsande enhet stay out of it.
        events = db.list_events(
            conn, since=_since_iso(preset), needs_review=needs_review, own_only=True
        )
        trivial_ids = triviality.classify_trivial_events(conn, events)
        rows = []
        for event in events:
            if event["id"] in trivial_ids:
                continue
            message = db.get_message(conn, event["message_id"])
            attachments = db.list_attachments_for_message(conn, event["message_id"])
            rows.append({"event": event, "message": message, "attachments": attachments})

    buf = generator.render_pdf(rows, since_label=preset)
    caption = f"Händelserapport – {preset} – {len(rows)} händelse(r)"
    filename = naming.build_report_filename(_unit_name(), "handelserapport", "pdf")
    report_group = _report_group_name()
    try:
        _send_pdf_to_group(buf, caption, report_group, filename)
    except signal_client.SignalCliError as exc:
        flash(f"Kunde inte skicka rapporten till Signal: {exc}", "error")
    else:
        with db.get_connection() as conn:
            db.set_last_adjacent_send(conn)
        flash(f"Rapporten skickad till Signal-gruppen '{report_group}'.")

    return redirect(url_for("events.report"))


def _compute_summary(preset: str, include_unreviewed: bool) -> analysis.Summary:
    """The single choke point every summary view/export/send goes
    through (including the header status strip's inject_header_status),
    so a human-set threat-level override (see db.get_threat_override)
    only needs to be applied here to reach all of them consistently.
    own_only=True: events received from an angränsande enhet (see
    events.new_adjacent_event) never feed this unit's own threat
    assessment, the same way they're kept out of generated reports.
    Duplicates and trivial/routine events are excluded the same way
    generated reports already exclude them (see triviality.py/
    duplicates.py) -- a routine wildlife sighting or a repeated report
    of the same incident shouldn't move the needle on the threat level
    any more than it does on the report text itself."""
    with db.get_connection() as conn:
        needs_review = None if include_unreviewed else False
        events = db.list_events(
            conn, since=_since_iso(preset), needs_review=needs_review, own_only=True
        )
        duplicate_ids = duplicates.classify_duplicate_events(conn, events)
        trivial_ids = triviality.classify_trivial_events(conn, events)
        events = [e for e in events if e["id"] not in duplicate_ids and e["id"] not in trivial_ids]
        summary_data = analysis.build_summary(events, period_label=preset)
        override = db.get_threat_override(conn)
    return analysis.apply_threat_override(summary_data, override)


def _adjacent_status_rows(preset: str) -> list[dict]:
    """Latest status report received from each adjacent unit within the
    period, for display on the summary page."""
    with db.get_connection() as conn:
        reports = db.list_latest_adjacent_reports_per_unit(conn, since=_since_iso(preset))
        return [
            {
                "report": report,
                "attachments": db.list_attachments_for_adjacent_report(conn, report["id"]),
            }
            for report in reports
        ]


def _truncate(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " […]"


# Row caps for _build_ai_context -- keep the prompt sent to the local LLM
# within a sane size (this is an 8B model with a limited context window,
# not a hosted frontier model) while still covering "current and older"
# data as asked for, newest first so the most relevant material survives
# the cap. Sized with headroom above config.OLLAMA_NUM_CTX's verified
# real-world usage (a full ~300-event/~80-adjacent-report log fit
# comfortably) -- if a deployment's own log grows past these, the
# "N av totalt M" note in the context text still discloses the cut
# rather than silently answering from a partial picture.
_AI_CONTEXT_EVENT_LIMIT = 400
_AI_CONTEXT_SUMMARY_LOG_LIMIT = 75
_AI_CONTEXT_ADJACENT_REPORT_LIMIT = 150


def _build_ai_context() -> str:
    """Assembles the underlag (source material) handed to the local LLM on
    every turn of the AI-analys chat -- see llm.generate_chat_reply. Three
    sections, each covering both current and historical data rather than
    just "right now": this unit's own saved event reports, this unit's own
    threat-level assessment history (summary_log), and the reports received
    from adjacent units (their full history, not just each unit's latest --
    see db.list_adjacent_reports)."""
    with db.get_connection() as conn:
        unit_name = db.get_unit_name(conn)
        all_events = db.list_events(conn)
        events = all_events[:_AI_CONTEXT_EVENT_LIMIT]
        all_summary_log = db.list_summary_log(conn)
        summary_log = all_summary_log[:_AI_CONTEXT_SUMMARY_LOG_LIMIT]
        all_adjacent_reports = db.list_adjacent_reports(conn)
        adjacent_reports = all_adjacent_reports[:_AI_CONTEXT_ADJACENT_REPORT_LIMIT]
        override = db.get_threat_override(conn)
        photo_message_ids = db.list_message_ids_with_attachments(conn)

    events_with_photo = sum(1 for e in all_events if e["message_id"] in photo_message_ids)

    # Stated once, explicitly, before any list -- so a plain "how many
    # events/reports are there" question can be answered by reading a
    # single stated number instead of the model having to count rows
    # itself (small local models are unreliable at exactly that, e.g.
    # answering wildly different made-up totals from one turn to the
    # next). Labeled distinctly from summary_log's per-entry "byggd på N
    # rapporter" below, which is a different number (how many events one
    # specific past assessment covered) that was previously worded
    # ambiguously enough ("antal händelser=") to get confused for this.
    lines = [
        f"Egen enhet: {unit_name or 'ej angivet'}",
        f"Totalt antal sparade händelser i händelseloggen just nu: {len(all_events)}",
        f"Totalt antal loggade hotbedömningar (Logg-sidan): {len(all_summary_log)}",
        f"Totalt antal mottagna rapporter från angränsande enheter: {len(all_adjacent_reports)}",
        f"Totalt antal av dessa händelser som har ett bifogat foto: {events_with_photo}",
    ]

    if override:
        lines.append(
            f"Manuellt satt hotnivå just nu: {override['level'].upper()} "
            f"(satt {_format_dt(override['set_at'])}"
            f"{', anteckning: ' + override['notes'] if override['notes'] else ''})"
        )

    lines.append(
        f"\n### Egna sparade händelser ({len(events)} av totalt {len(all_events)} visas, nyast först)"
    )
    if not events:
        lines.append("Inga händelser sparade.")
    for e in events:
        tnr = naming.event_tnr(e["created_at"])
        flags = []
        if e["needs_review"]:
            flags.append("ogranskad")
        if e["is_trivial"]:
            flags.append("bedömd trivial")
        if e["is_duplicate"]:
            flags.append("duplikat")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        foto = "ja" if e["message_id"] in photo_message_ids else "nej"
        lines.append(
            f"- TNR {tnr}{flag_text}: plats={e['place'] or '-'}, antal={e['count'] or '-'}, "
            f"föremål={e['object'] or '-'}, verksamhet={_truncate(e['activity'], 200)}, "
            f"kännetecken={_truncate(e['marks'], 200)}, rapporterad av={e['reported_by'] or '-'}, "
            f"åtgärd/uppföljning={_truncate(e['next_steps'], 150)}, bifogat foto={foto}"
        )

    lines.append(
        f"\n### Egen hotbedömningshistorik ({len(summary_log)} av totalt "
        f"{len(all_summary_log)} visas, nyast först)"
    )
    if not summary_log:
        lines.append("Inga tidigare hotbedömningar loggade.")
    for entry in summary_log:
        lines.append(
            f"- {_format_dt(entry['created_at'])}: period={entry['period_label']}, "
            f"nivå={entry['level'].upper()}, poäng={entry['score']}, "
            f"byggd på {entry['total_events']} rapporter, källa={entry['source']}"
        )

    lines.append(
        f"\n### Rapporter från angränsande enheter ({len(adjacent_reports)} av totalt "
        f"{len(all_adjacent_reports)} visas, nyast först)"
    )
    if not adjacent_reports:
        lines.append("Inga rapporter mottagna från angränsande enheter.")
    for report in adjacent_reports:
        who = report["unit_name"] or report["sender_name"] or "okänd avsändare"
        lines.append(
            f"- {_format_dt(report['received_at'])} ({who}): {_truncate(report['body'], 400)}"
        )

    # Repeated verbatim at the very end, right before the model reads the
    # actual question -- local models attend far more reliably to the
    # start and (especially) the end of a long context than to a fact
    # buried a few hundred lines back, even when that fact was already
    # stated up top. Confirmed necessary against this unit's real ~300-
    # event log: a plain top-of-context total was answered correctly with
    # a handful of events but got answered as "none" once the underlag
    # grew to real size, even though the exact same sentence was right
    # there in the prompt the whole time.
    lines.append(
        f"\n### Sammanfattning (samma siffror som högst upp, upprepade här)\n"
        f"Totalt antal sparade händelser i händelseloggen just nu: {len(all_events)}\n"
        f"Totalt antal av dessa händelser som har ett bifogat foto: {events_with_photo}\n"
        f"Totalt antal loggade hotbedömningar (Logg-sidan): {len(all_summary_log)}\n"
        f"Totalt antal mottagna rapporter från angränsande enheter: {len(all_adjacent_reports)}"
    )

    return "\n".join(lines)


def _log_summary_generation(
    summary_data: analysis.Summary, preset: str, source: str, format: str | None = None
) -> str:
    """Records a threat-level summary generation/send for the "Logg"
    page's time-ordered history, identified as "TNR Unit-name" -- the
    same convention used for the report's own filename. Returns the TNR
    used, so the caller builds the actual downloaded/sent filename with
    that exact same value (naming.build_report_filename(..., tnr=tnr)),
    keeping the log entry and the artifact's name in sync. Not called on
    plain page views -- only when an actual report artifact is produced
    (downloaded or sent)."""
    unit_name = _unit_name()
    tnr = naming.generate_tnr()
    with db.get_connection() as conn:
        db.insert_summary_log_entry(
            conn, tnr=tnr, unit_name=unit_name, period_label=preset,
            total_events=summary_data.total_events,
            level=summary_data.threat.level, score=summary_data.threat.score,
            source=source, format=format,
        )
    return tnr


@bp.route("/summary")
def summary():
    # Falls back to whatever was last viewed (see session assignment
    # below) when no ?since=/?include_unreviewed= is given at all -- so
    # the plain nav link back to this page shows the same period you had
    # before navigating away, rather than resetting to the "7d" default.
    # An explicit query value (from a period link or a filter toggle)
    # always wins and becomes the new "last viewed" for next time.
    since_param = request.args.get("since")
    include_unreviewed_param = request.args.get("include_unreviewed")
    preset = since_param if since_param is not None else session.get("summary_since", "7d")
    include_unreviewed = (
        include_unreviewed_param == "1" if include_unreviewed_param is not None
        else session.get("summary_include_unreviewed", False)
    )
    session["summary_since"] = preset
    session["summary_include_unreviewed"] = include_unreviewed

    summary_data = _compute_summary(preset, include_unreviewed)
    return _render_summary_page(preset, include_unreviewed, summary_data)


def _render_summary_page(
    preset: str, include_unreviewed: bool, summary_data: analysis.Summary,
    narrative: str | None = None,
):
    report_group = _report_group_name()
    return render_template(
        "summary.html", summary=summary_data, since=preset,
        include_unreviewed=include_unreviewed, unit_name=_unit_name(),
        report_group_name=report_group,
        adjacent_reports_group_name=report_group,
        adjacent_rows=_adjacent_status_rows(preset),
        override=_threat_override_display(),
        narrative=narrative,
    )


@bp.route("/summary/narrative", methods=["POST"])
def summary_narrative():
    """Generates (or regenerates) the editable hotbildsbedömning draft: the
    threat level itself is unchanged (still the deterministic analysis.py
    verdict), but this asks the local Ollama server for a prose narrative
    (llm.generate_narrative) and shows it in an editable textarea on the
    same page, so a human reviews/adjusts the wording before it's saved
    as a file or sent to Signal (see summary_save_text/_pdf/summary_send,
    which take whatever text is actually in that textarea at submit
    time -- never regenerated server-side). The button that triggers this
    lives in the same form as the draft textarea, so a failed regeneration
    (Ollama not running, timeout, ...) falls back to whatever draft was
    already there rather than wiping out a human's prior edits -- only a
    successful call replaces it."""
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    previous_draft = request.form.get("narrative_text", "").strip() or None

    summary_data = _compute_summary(preset, include_unreviewed)
    with db.get_connection() as conn:
        base_url = llm.resolve_ollama_url(db.get_ollama_port(conn))
    try:
        narrative = llm.generate_narrative(summary_data, site_name=config.SITE_NAME, base_url=base_url)
    except llm.LLMError as exc:
        flash(str(exc), "error")
        narrative = previous_draft

    return _render_summary_page(preset, include_unreviewed, summary_data, narrative=narrative)


def _threat_override_display() -> dict | None:
    """The override dict for the edit form's own display (its
    level/notes/when-set) -- separate from _compute_summary's internal
    use of the same data to bake the override into summary.threat."""
    with db.get_connection() as conn:
        override = db.get_threat_override(conn)
    if override is None:
        return None
    return {**override, "set_at_label": _format_dt(override["set_at"])}


@bp.route("/summary/override", methods=["POST"])
def summary_override():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    level = request.form.get("level", "")
    notes = request.form.get("notes", "")

    if level not in ("green", "yellow", "red"):
        flash("Ogiltig hotnivå.", "error")
    else:
        with db.get_connection() as conn:
            db.set_threat_override(conn, level, notes)
        flash("Manuell hotnivå sparad.")

    return redirect(
        url_for("events.summary", since=preset, include_unreviewed=1 if include_unreviewed else 0)
    )


@bp.route("/summary/override/clear", methods=["POST"])
def summary_override_clear():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"

    with db.get_connection() as conn:
        db.clear_threat_override(conn)
    flash("Återgår till automatisk hotbedömning.")

    return redirect(
        url_for("events.summary", since=preset, include_unreviewed=1 if include_unreviewed else 0)
    )


@bp.route("/summary/log")
def summary_log():
    with db.get_connection() as conn:
        entries = db.list_summary_log(conn)
    return render_template("summary_log.html", entries=entries)


@bp.route("/summary/save-text", methods=["POST"])
def summary_save_text():
    """Saves the hotbildsbedömning draft as a text file -- `narrative_text`
    is whatever's currently in the review textarea (edited or not), never
    regenerated here, so what's saved is exactly what was reviewed."""
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    narrative = request.form.get("narrative_text", "").strip() or None

    summary_data = _compute_summary(preset, include_unreviewed)
    tnr = _log_summary_generation(summary_data, preset, source="download", format="text")
    content = generator.render_summary_text(summary_data, site_name=config.SITE_NAME, narrative=narrative)
    content_bytes = content.encode("utf-8")
    filename = naming.build_report_filename(_unit_name(), "hotbedomning", "txt", tnr=tnr)
    with db.get_connection() as conn:
        _write_report_to_reports_dir(conn, content_bytes, filename)
    return send_file(
        io.BytesIO(content_bytes), mimetype="text/plain", as_attachment=True,
        download_name=filename,
    )


@bp.route("/summary/save-pdf", methods=["POST"])
def summary_save_pdf():
    """Same as summary_save_text, but PDF -- see that route's docstring."""
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    narrative = request.form.get("narrative_text", "").strip() or None

    summary_data = _compute_summary(preset, include_unreviewed)
    tnr = _log_summary_generation(summary_data, preset, source="download", format="pdf")
    content_bytes = generator.render_summary_pdf(summary_data, site_name=config.SITE_NAME, narrative=narrative).read()
    filename = naming.build_report_filename(_unit_name(), "hotbedomning", "pdf", tnr=tnr)
    with db.get_connection() as conn:
        _write_report_to_reports_dir(conn, content_bytes, filename)
    return send_file(
        io.BytesIO(content_bytes), mimetype="application/pdf", as_attachment=True,
        download_name=filename,
    )


@bp.route("/summary/send", methods=["POST"])
def summary_send():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    narrative = request.form.get("narrative_text", "").strip() or None

    summary_data = _compute_summary(preset, include_unreviewed)
    tnr = _log_summary_generation(summary_data, preset, source="send", format="pdf")
    buf = generator.render_summary_pdf(summary_data, site_name=config.SITE_NAME, narrative=narrative)
    threat = summary_data.threat
    caption = (
        f"Sammanställd hotbedömning – {config.SITE_NAME} – "
        f"Hotnivå: {threat.level.upper()} (poäng {threat.score})"
    )
    filename = naming.build_report_filename(_unit_name(), "hotbedomning", "pdf", tnr=tnr)
    report_group = _report_group_name()
    try:
        _send_pdf_to_group(buf, caption, report_group, filename)
    except signal_client.SignalCliError as exc:
        flash(f"Kunde inte skicka hotbedömningen till Signal: {exc}", "error")
    else:
        with db.get_connection() as conn:
            db.set_last_adjacent_send(conn)
        flash(f"Hotbedömningen skickad till Signal-gruppen '{report_group}'.")

    return redirect(
        url_for("events.summary", since=preset, include_unreviewed=1 if include_unreviewed else 0)
    )


@bp.route("/entities/send-watchlist", methods=["POST"])
def entities_send_watchlist():
    """Sends a PDF of the bevakningslista -- every person/vehicle/object
    linked to 2+ events (recurring, straight from the entities database)
    plus every one manually flagged via a "Bevaka" checkbox -- to the
    configured recurring-list Signal group. Replaces the old hotbild
    "Skicka lista över återkommande" button, which built its list from
    analysis.py's regex/Jaccard text clustering over raw events instead
    of the entities database this tool now maintains."""
    with db.get_connection() as conn:
        entries = _watchlist_entries(conn)

    if not entries:
        flash("Inga poster på bevakningslistan att skicka.", "error")
        return redirect(url_for("events.list_entities"))

    buf = generator.render_watchlist_pdf(entries, site_name=config.SITE_NAME)
    caption = f"Bevakningslista – {config.SITE_NAME} – {len(entries)} poster"
    filename = naming.build_report_filename(_unit_name(), "bevakningslista", "pdf")
    recurring_group = _recurring_group_name()
    try:
        _send_pdf_to_group(buf, caption, recurring_group, filename)
    except signal_client.SignalCliError as exc:
        flash(f"Kunde inte skicka bevakningslistan till Signal: {exc}", "error")
    else:
        flash(f"Bevakningslistan skickad till Signal-gruppen '{recurring_group}'.")

    return redirect(url_for("events.list_entities"))


def _watchlist_entries(conn) -> list[dict]:
    rows = db.list_watchlist_entities(conn)
    return [
        {"entity": row, "events": db.list_events_for_entity(conn, row["id"])}
        for row in rows
    ]


@bp.route("/entities/save-watchlist", methods=["POST"])
def entities_save_watchlist():
    """Saves the bevakningslista as a file -- both to the
    Inställningar-configured Rapportmapp and as a browser download, same
    dual behaviour as the hotbild/händelserapport "Spara"/"Skapa"
    actions (see _write_report_to_reports_dir)."""
    fmt = request.form.get("format", "pdf")
    with db.get_connection() as conn:
        entries = _watchlist_entries(conn)
        if not entries:
            flash("Inga poster på bevakningslistan att spara.", "error")
            return redirect(url_for("events.list_entities"))

        if fmt == "text":
            content_bytes = generator.render_watchlist_markdown(
                entries, site_name=config.SITE_NAME
            ).encode("utf-8")
            mimetype = "text/markdown"
            filename = naming.build_report_filename(_unit_name(), "bevakningslista", "md")
        else:
            content_bytes = generator.render_watchlist_pdf(entries, site_name=config.SITE_NAME).read()
            mimetype = "application/pdf"
            filename = naming.build_report_filename(_unit_name(), "bevakningslista", "pdf")

        _write_report_to_reports_dir(conn, content_bytes, filename)

    return send_file(
        io.BytesIO(content_bytes), mimetype=mimetype, as_attachment=True, download_name=filename,
    )


@bp.route("/entities/import-watchlist", methods=["POST"])
def entities_import_watchlist():
    """Reads a previously saved/sent bevakningslista Markdown file back
    into entity records -- e.g. picking up a list another unit exported
    and sent over. See entities.parse_watchlist_markdown/
    import_watchlist_entries for the format and dedup rules."""
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Välj en bevakningslista-fil (Markdown, .md/.txt) att importera.", "error")
        return redirect(url_for("events.list_entities"))
    try:
        text = file.read().decode("utf-8")
    except UnicodeDecodeError:
        flash("Kunde inte läsa filen som UTF-8-text.", "error")
        return redirect(url_for("events.list_entities"))

    parsed = entities.parse_watchlist_markdown(text)
    if not parsed:
        flash("Inga poster hittades i filen -- är det en bevakningslista sparad härifrån?", "error")
        return redirect(url_for("events.list_entities"))

    with db.get_connection() as conn:
        created, updated = entities.import_watchlist_entries(conn, parsed)

    flash(f"Importerade {created + updated} poster från bevakningslistan ({created} nya, {updated} uppdaterade).")
    return redirect(url_for("events.list_entities"))


# Session key for the AI-analys chat's running conversation, and how many
# of its most recent messages (user + assistant turns combined) to keep --
# capped mainly to keep the signed session cookie small, since Flask's
# default session storage is client-side. The underlag itself (see
# _build_ai_context) is rebuilt fresh from the database on every turn
# regardless, so trimming old chat turns never loses access to any event
# or report -- only to the earlier back-and-forth about them.
_AI_CHAT_SESSION_KEY = "ai_chat_history"
_AI_CHAT_MAX_MESSAGES = 12

# Set when the most recent reply attempt failed (see summary_ai_respond),
# so the page shows a manual retry button instead of auto-submitting
# straight back into another failing call -- e.g. Ollama simply isn't
# running. Cleared as soon as a question is asked or answered.
_AI_CHAT_FAILED_KEY = "ai_chat_failed"


@bp.route("/summary/ai")
def summary_ai():
    """Landing page for the "AI-analys" tab: a chat-bot (not a one-shot
    narrative generator) that can be asked about this unit's saved events
    and both this unit's and adjacent units' threat-level report history
    -- see _build_ai_context for exactly what it's given on every turn.
    Conversation state lives in the session, so it's per browser/user.

    Asking a question is split into two requests (see summary_ai_chat and
    summary_ai_respond) specifically so the question itself is saved to
    the session immediately, before the slow part (an actual local-LLM
    call regularly takes 30-190+ seconds -- see llm.OLLAMA_TIMEOUT_SECONDS)
    even starts. Otherwise, a user who gets impatient and navigates to
    another tab mid-wait would cancel the browser's wait for that one
    slow response before its Set-Cookie ever lands, silently losing both
    their question and the reply -- exactly what "the chat disappears
    when I switch tabs" reports were. With the question saved first, at
    worst only the reply is still pending, and this page notices that
    (`pending` below) and resumes waiting for it automatically."""
    history = session.get(_AI_CHAT_SESSION_KEY, [])
    pending = bool(history) and history[-1]["role"] == "user"

    search_query = request.args.get("q", "").strip()
    with db.get_connection() as conn:
        search_results = db.search_events(conn, search_query) if search_query else []

    return render_template(
        "summary_ai.html", chat_history=history, pending=pending,
        failed=pending and session.get(_AI_CHAT_FAILED_KEY, False),
        search_query=search_query, search_results=search_results,
    )


@bp.route("/summary/ai/chat", methods=["POST"])
def summary_ai_chat():
    """Saves the question and redirects immediately -- see summary_ai's
    docstring for why the actual LLM call happens in a separate request
    (summary_ai_respond) instead of right here."""
    message = request.form.get("message", "").strip()
    if message:
        history = session.get(_AI_CHAT_SESSION_KEY, [])
        history.append({"role": "user", "content": message})
        session[_AI_CHAT_SESSION_KEY] = history[-_AI_CHAT_MAX_MESSAGES:]
        session[_AI_CHAT_FAILED_KEY] = False
    return redirect(url_for("events.summary_ai"))


@bp.route("/summary/ai/respond", methods=["POST"])
def summary_ai_respond():
    """The slow half of asking a question -- see summary_ai's docstring.
    Triggered by the page itself (auto-submitted, or manually via a retry
    button after a failure) whenever the session ends on an unanswered
    user message. Safe to call again if a previous attempt failed or was
    interrupted: it just regenerates the reply for whatever question is
    still pending, it never re-appends the question itself."""
    history = session.get(_AI_CHAT_SESSION_KEY, [])
    if history and history[-1]["role"] == "user":
        with db.get_connection() as conn:
            base_url = llm.resolve_ollama_url(db.get_ollama_port(conn))
        try:
            reply = llm.generate_chat_reply(history, _build_ai_context(), base_url=base_url)
        except llm.LLMError as exc:
            session[_AI_CHAT_FAILED_KEY] = True
            flash(str(exc), "error")
        else:
            history.append({"role": "assistant", "content": reply})
            session[_AI_CHAT_SESSION_KEY] = history[-_AI_CHAT_MAX_MESSAGES:]
            session[_AI_CHAT_FAILED_KEY] = False
    return redirect(url_for("events.summary_ai"))


@bp.route("/summary/ai/clear", methods=["POST"])
def summary_ai_clear():
    session.pop(_AI_CHAT_SESSION_KEY, None)
    session.pop(_AI_CHAT_FAILED_KEY, None)
    return redirect(url_for("events.summary_ai"))
