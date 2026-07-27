from __future__ import annotations

import io
import ipaddress
import json
import shutil
import socket
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, session,
    url_for,
)
from werkzeug.utils import secure_filename

from .. import analysis, config, db, duplicates, importer, llm, naming, signal_client, triviality
from ..reports import generator

bp = Blueprint("events", __name__)

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
    "events.add_adjacent_unit",
    "events.delete_adjacent_unit",
    "events.reset_event_log",
    "events.reset_database",
    "events.create_user",
    "events.delete_user",
    "events.lan_qrcode",
    "events.demo_import",
    "events.demo_clear",
    "events.import_training_day",
    "events.system_log",
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
    page: unit name, the threat level, and when a report (incident report
    or threat-level summary) was last sent to the Signal group that
    adjacent units' own status reports also come in on. The threat level
    here must always agree with the Sammanställd hotbedömning page, so it
    reuses that page's own computation (_compute_summary, which excludes
    duplicates) over the exact same session-remembered period/filter that
    page is currently showing -- not some independently fixed window that
    could silently disagree with it. Skipped for the login page itself --
    it doesn't extend base.html (so none of this would be shown anyway),
    and this avoids running a duplicate-classification DB write for every
    anonymous hit the login page gets from the network."""
    if request.endpoint in ("events.login", "events.logout"):
        return {}
    preset = session.get("summary_since", "7d")
    include_unreviewed = session.get("summary_include_unreviewed", False)
    with db.get_connection() as conn:
        unit_name = db.get_unit_name(conn)
        last_adjacent_send_at = db.get_last_adjacent_send(conn)
        demo_mode = db.has_demo_events(conn)
    threat = _compute_summary(preset, include_unreviewed).threat
    return {
        "header_unit_name": unit_name,
        "header_threat": threat,
        "header_threat_period_label": _SINCE_LABELS.get(preset, preset),
        "header_last_adjacent_send": _format_dt(last_adjacent_send_at),
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


def _save_uploaded_photos(conn, message_id: int) -> None:
    for file in request.files.getlist("photos"):
        if not file or not file.filename:
            continue
        if file.mimetype and not file.mimetype.startswith("image/"):
            continue
        dest_dir = config.ATTACHMENTS_DIR / str(message_id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / secure_filename(file.filename)
        file.save(dest)
        db.insert_attachment(
            conn, message_id=message_id, file_path=str(dest), content_type=file.mimetype
        )


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
        adjacent_units = db.list_adjacent_units(conn)
        watch_group = db.get_watch_group_name(conn)
        report_group = db.get_report_group_name(conn)
        recurring_group = db.get_recurring_group_name(conn)
        sensor_group = db.get_sensor_group_name(conn)
        users = db.list_users(conn)

    example_filename = naming.build_report_filename(unit_name, "hotbedomning", "pdf")
    return render_template(
        "settings.html", unit_name=unit_name, example_filename=example_filename,
        adjacent_units=adjacent_units, watch_group=watch_group,
        report_group=report_group, recurring_group=recurring_group,
        sensor_group=sensor_group, users=users,
        lan_url=_lan_url(),
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

    # Ordered by TNR (not created_at/ingestion order) so events from all
    # sources -- including automated sensor triggers, which report their
    # own precise DDHHMM Stund -- line up by when they actually happened,
    # newest first. See naming.event_tnr for the same fallback used
    # everywhere else an event's TNR is shown.
    rows.sort(
        key=lambda row: naming.event_tnr(row["event"]["event_time"], row["event"]["created_at"]),
        reverse=True,
    )

    return render_template(
        "events_list.html", rows=rows, since=preset, review_filter=review_filter
    )


@bp.route("/events/new", methods=["GET", "POST"])
def new_event():
    if request.method == "GET":
        return render_template("event_form.html")

    fields = _field_form_values()
    notes = request.form.get("notes", "").strip()
    fields["raw_text"] = notes or None
    fields["needs_review"] = 0 if request.form.get("mark_reviewed") else 1

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
            raw_json=json.dumps({"source": "manual"}),
        )
        _save_uploaded_photos(conn, message_id)
        event_id = db.insert_event(conn, message_id=message_id, fields=fields)

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
            }
            for day in range(1, TRAINING_DAYS_COUNT + 1)
        ]
    return render_template("demo_import.html", training_days=training_days)


@bp.route("/events/import/demo/clear", methods=["POST"])
def demo_clear():
    with db.get_connection() as conn:
        message_ids = db.clear_demo_events(conn)

    for message_id in message_ids:
        shutil.rmtree(config.ATTACHMENTS_DIR / str(message_id), ignore_errors=True)

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
    if adjacent_count:
        message += (
            f" Även {adjacent_count} statusrapport(er) från angränsande "
            "enheter mottagna."
        )
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
            fields["needs_review"] = 0 if request.form.get("mark_reviewed") else 1
            fields["is_trivial"] = 1 if request.form.get("mark_trivial") else 0
            # A human just made a deliberate call on the "Trivial"
            # checkbox (whether they changed it or left it as-is) -- lock
            # it so the auto-classifier in triviality.py never overrides
            # that judgment on a later report generation.
            fields["is_trivial_reviewed"] = 1
            db.update_event(conn, event_id, fields)
            _save_uploaded_photos(conn, event["message_id"])
            return redirect(url_for("events.event_detail", event_id=event_id))

        message = db.get_message(conn, event["message_id"])
        attachments = db.list_attachments_for_message(conn, event["message_id"])

    source = json.loads(message["raw_json"]).get("source", "signal")
    return render_template(
        "event_detail.html", event=event, message=message, attachments=attachments,
        source=source,
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
        events = db.list_events(conn, since=_since_iso(preset), needs_review=needs_review)
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
        content = generator.render_markdown(rows, since_label=preset)
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/markdown", as_attachment=True,
            download_name=naming.build_report_filename(unit_name, "handelserapport", "md"),
        )
    if fmt == "text":
        content = generator.render_text(rows, since_label=preset)
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/plain", as_attachment=True,
            download_name=naming.build_report_filename(unit_name, "handelserapport", "txt"),
        )

    buf = generator.render_pdf(rows, since_label=preset)
    return send_file(
        buf, mimetype="application/pdf", as_attachment=True,
        download_name=naming.build_report_filename(unit_name, "handelserapport", "pdf"),
    )


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
        events = db.list_events(conn, since=_since_iso(preset), needs_review=needs_review)
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
    only needs to be applied here to reach all of them consistently."""
    with db.get_connection() as conn:
        needs_review = None if include_unreviewed else False
        events = db.list_events(conn, since=_since_iso(preset), needs_review=needs_review)
        duplicate_ids = duplicates.classify_duplicate_events(conn, events)
        events = [e for e in events if e["id"] not in duplicate_ids]
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

    download = request.args.get("download")

    summary_data = _compute_summary(preset, include_unreviewed)

    if download in ("markdown", "pdf", "text"):
        tnr = _log_summary_generation(summary_data, preset, source="download", format=download)
    else:
        tnr = None

    if download == "markdown":
        content = generator.render_summary_markdown(summary_data, site_name=config.SITE_NAME)
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/markdown", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "md", tnr=tnr),
        )
    if download == "text":
        content = generator.render_summary_text(summary_data, site_name=config.SITE_NAME)
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/plain", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "txt", tnr=tnr),
        )
    if download == "pdf":
        buf = generator.render_summary_pdf(summary_data, site_name=config.SITE_NAME)
        return send_file(
            buf, mimetype="application/pdf", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "pdf", tnr=tnr),
        )

    report_group = _report_group_name()
    return render_template(
        "summary.html", summary=summary_data, since=preset,
        include_unreviewed=include_unreviewed, site_name=config.SITE_NAME,
        report_group_name=report_group,
        recurring_group_name=_recurring_group_name(),
        adjacent_reports_group_name=report_group,
        adjacent_rows=_adjacent_status_rows(preset),
        override=_threat_override_display(),
    )


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


@bp.route("/summary/send", methods=["POST"])
def summary_send():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"

    summary_data = _compute_summary(preset, include_unreviewed)
    tnr = _log_summary_generation(summary_data, preset, source="send", format="pdf")
    buf = generator.render_summary_pdf(summary_data, site_name=config.SITE_NAME)
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


@bp.route("/summary/send-recurring", methods=["POST"])
def summary_send_recurring():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"

    summary_data = _compute_summary(preset, include_unreviewed)
    buf = generator.render_recurring_pdf(summary_data, site_name=config.SITE_NAME)
    total_groups = (
        len(summary_data.vehicle_groups) + len(summary_data.person_groups)
        + len(summary_data.other_groups)
    )
    caption = (
        f"Återkommande fordon, personer och observationer – {config.SITE_NAME} "
        f"– {total_groups} identifierade mönster"
    )
    filename = naming.build_report_filename(_unit_name(), "aterkommande", "pdf")
    recurring_group = _recurring_group_name()
    try:
        _send_pdf_to_group(buf, caption, recurring_group, filename)
    except signal_client.SignalCliError as exc:
        flash(f"Kunde inte skicka listan över återkommande till Signal: {exc}", "error")
    else:
        flash(f"Listan över återkommande skickad till Signal-gruppen '{recurring_group}'.")

    return redirect(
        url_for("events.summary", since=preset, include_unreviewed=1 if include_unreviewed else 0)
    )


@bp.route("/summary/ai")
def summary_ai():
    """Landing page for the "AI-sammanfattning" tab -- always reflects
    whatever period/filter the Sammanställd hotbedömning page itself is
    currently showing (same session-remembered state as the header
    status strip), so there's one "current" underlag, not a second
    independent one to keep in sync by hand."""
    preset = session.get("summary_since", "7d")
    include_unreviewed = session.get("summary_include_unreviewed", False)
    return render_template(
        "summary_ai.html", since=preset, include_unreviewed=include_unreviewed, narrative=None,
    )


@bp.route("/summary/narrative", methods=["POST"])
def summary_narrative():
    preset = request.form.get("since", "7d")
    include_unreviewed = request.form.get("include_unreviewed") == "1"
    download = request.form.get("download")

    summary_data = _compute_summary(preset, include_unreviewed)

    try:
        narrative = llm.generate_narrative(summary_data, site_name=config.SITE_NAME)
    except llm.LLMError as exc:
        flash(str(exc), "error")
        return render_template(
            "summary_ai.html", since=preset, include_unreviewed=include_unreviewed, narrative=None,
        )

    if download in ("markdown", "pdf", "text"):
        tnr = _log_summary_generation(summary_data, preset, source="download", format=download)
    else:
        tnr = None

    if download == "markdown":
        content = generator.render_summary_markdown(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/markdown", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "md", tnr=tnr),
        )
    if download == "text":
        content = generator.render_summary_text(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        buf = io.BytesIO(content.encode("utf-8"))
        return send_file(
            buf, mimetype="text/plain", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "txt", tnr=tnr),
        )
    if download == "pdf":
        buf = generator.render_summary_pdf(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        return send_file(
            buf, mimetype="application/pdf", as_attachment=True,
            download_name=naming.build_report_filename(_unit_name(), "hotbedomning", "pdf", tnr=tnr),
        )

    return render_template(
        "summary_ai.html", since=preset, include_unreviewed=include_unreviewed, narrative=narrative,
    )
