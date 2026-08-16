"""Thin wrapper around the `signal-cli` binary.

signal-cli must already be installed and linked/registered as a device
before any of this is used (see README). This is the part of the tool
that needs network access -- receiving (`sync`/`watch`) and sending
(`send_message`) reports over Signal. Everything else -- storage, review,
report generation -- runs entirely offline against the local database and
attachment files.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator, Optional

from . import config, db, entities, naming, parser


class SignalCliError(RuntimeError):
    pass


_URI_RE = re.compile(r"(sgnl://\S+|tsdevice:/\S+)")
_PHONE_RE = re.compile(r"\+\d{6,15}")


def _iter_envelopes(raw_output: str) -> Iterator[dict[str, Any]]:
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def receive(timeout_seconds: int = 5) -> list[dict[str, Any]]:
    """Run `signal-cli receive` once and return the parsed JSON envelopes.
    Requires network access and a linked/registered account."""
    if not config.PHONE_NUMBER:
        raise SignalCliError(
            "SIGNAL_EVENTS_PHONE_NUMBER is not set; see README for setup."
        )
    cmd = [
        config.SIGNAL_CLI_BIN,
        "-u", config.PHONE_NUMBER,
        "-o", "json",  # JSON output is a global flag, not a `receive` option
        # Otherwise a message from anyone signal-cli hasn't seen before (a
        # newly added group member, or someone who reinstalled Signal) is
        # silently undecryptable until their identity is trusted -- see
        # README "Newly added group members" for the tradeoff this makes.
        "--trust-new-identities", "always",
        "receive",
        "-t", str(timeout_seconds),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds + 30
        )
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SignalCliError("signal-cli receive timed out") from exc

    if result.returncode != 0:
        # Always an error, even when signal-cli happened to print some
        # stdout before failing -- previously this only raised when
        # stdout was *also* empty, so a partial/corrupted receive (exit
        # code nonzero, some JSON lines still on stdout) was silently
        # treated as a clean success, discarding the actual error on
        # stderr entirely. That's exactly the kind of failure that made
        # "receive seems to not work" hard to diagnose: nothing anywhere
        # said it had actually failed.
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise SignalCliError(f"signal-cli failed (exit {result.returncode}): {detail}")

    return list(_iter_envelopes(result.stdout))


def _copy_attachment(
    entity_id: int, attachment: dict[str, Any], subdir: str = ""
) -> Optional[Path]:
    attachment_id = attachment.get("id")
    if not attachment_id:
        return None
    src = config.SIGNAL_CLI_ATTACHMENTS_DIR / str(attachment_id)
    if not src.exists():
        return None
    dest_dir = config.ATTACHMENTS_DIR / subdir / str(entity_id) if subdir else (
        config.ATTACHMENTS_DIR / str(entity_id)
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = attachment.get("filename") or str(attachment_id)
    dest = dest_dir / filename
    shutil.copyfile(src, dest)
    return dest


def _envelope_group_id(inner: dict[str, Any]) -> Optional[str]:
    data_message = inner.get("dataMessage") or {}
    group_info = data_message.get("groupInfo") or {}
    return group_info.get("groupId") or group_info.get("id")


def _own_sync_message_group_id(inner: dict[str, Any]) -> Optional[str]:
    """Group id of a message the linked account sent *itself* (from
    another device/client using the same number), if any. Signal doesn't
    deliver these to `dataMessage` -- they arrive as a `syncMessage`
    sent-transcript instead, which `ingest_envelope` intentionally never
    ingests (see its "sync-of-own-messages" comment, so this unit's own
    outgoing reports don't get picked back up as if an adjacent unit sent
    them). That's correct, but it also means someone testing ingestion by
    messaging the incident group from the *same* phone/account signal-cli
    is linked to will see nothing happen with no obvious explanation --
    see watch_multi's use of this helper, which surfaces that specific
    case in the system log instead of leaving it silent."""
    sent_message = (inner.get("syncMessage") or {}).get("sentMessage") or {}
    group_info = sent_message.get("groupInfo") or {}
    return group_info.get("groupId") or group_info.get("id")


def ingest_envelope(
    conn: sqlite3.Connection, envelope: dict[str, Any], group_id: Optional[str] = None,
    is_sensor: bool = False, is_tak_bridge: bool = False,
) -> bool:
    """Store one envelope's data message (if any) and its attachments/parsed
    fields. Returns True if a new message was ingested.

    If `group_id` is given, only messages posted to that Signal group are
    ingested -- direct messages and messages in other groups are skipped.

    `is_sensor=True` (see watch_multi's sensor-group call) tags the
    resulting event so duplicates.py never evaluates it: an automated
    sensor gateway firing the same templated message at the same place
    repeatedly is expected, and each trigger is a genuine, separate
    occurrence, not a person accidentally filing the same report twice.

    `is_tak_bridge=True` (see watch_multi's TAK-bridge-group call) only
    tags the resulting event for display/provenance (event_detail.html
    shows "Mottaget via TAK-brygga") -- unlike is_sensor it does *not*
    exempt the event from duplicates.py, since a report relayed from an
    ATAK operator is a normal one-off human observation, not a repeating
    automated trigger."""
    inner = envelope.get("envelope", envelope)
    data_message = inner.get("dataMessage")
    if not data_message:
        return False  # receipts, typing indicators, sync-of-own-messages, etc.

    if group_id is not None and _envelope_group_id(inner) != group_id:
        return False

    timestamp = data_message.get("timestamp") or inner.get("timestamp")
    if timestamp is None or db.message_exists(conn, timestamp):
        return False

    sender_number = inner.get("sourceNumber") or inner.get("source")
    sender_name = inner.get("sourceName")
    body = data_message.get("message") or ""

    message_id = db.insert_message(
        conn,
        signal_timestamp=timestamp,
        sender_number=sender_number,
        sender_name=sender_name,
        body=body,
        raw_json=json.dumps(envelope),
    )

    for attachment in data_message.get("attachments", []) or []:
        dest = _copy_attachment(message_id, attachment)
        if dest is not None:
            db.insert_attachment(
                conn,
                message_id=message_id,
                file_path=str(dest),
                content_type=attachment.get("contentType"),
            )

    reported_by = sender_name or sender_number
    fields = parser.parse_event_fields(body, reported_by=reported_by)
    fields["is_sensor"] = is_sensor
    fields["is_tak_bridge"] = is_tak_bridge
    event_id = db.insert_event(conn, message_id=message_id, fields=fields)
    entities.sync_event_entities(conn, event_id)
    return True


def ingest_adjacent_report(
    conn: sqlite3.Connection, envelope: dict[str, Any], group_id: Optional[str] = None
) -> bool:
    """Store one envelope as a status report from an adjacent unit --
    identified from the plain-text unit name embedded in the report's own
    attachment filename (see naming.parse_report_filename), not from a
    separate sender mapping. Returns True if a new report was ingested.

    Since this group is shared with this unit's own report distribution
    (see config.REPORT_GROUP_NAME), it can also pick up plain chat
    messages from people who aren't posting a named report at all. Rather
    than guessing an identity from the sender (which isn't a unit name),
    `unit_name` is left None when no attachment matches the naming
    convention -- the message is still stored (for completeness/audit),
    but db.list_latest_adjacent_reports_per_unit only surfaces rows with
    an actually-identified unit, so unidentified chatter doesn't show up
    on the summary page as if it were a unit status report."""
    inner = envelope.get("envelope", envelope)
    data_message = inner.get("dataMessage")
    if not data_message:
        return False

    if group_id is not None and _envelope_group_id(inner) != group_id:
        return False

    timestamp = data_message.get("timestamp") or inner.get("timestamp")
    if timestamp is None or db.adjacent_report_exists(conn, timestamp):
        return False

    sender_number = inner.get("sourceNumber") or inner.get("source")
    sender_name = inner.get("sourceName")
    body = data_message.get("message") or ""

    attachments = data_message.get("attachments", []) or []
    unit_name = None
    for attachment in attachments:
        parsed = naming.parse_report_filename(attachment.get("filename") or "")
        if parsed:
            unit_name = parsed["unit_name"]
            break

    report_id = db.insert_adjacent_report(
        conn, signal_timestamp=timestamp, sender_number=sender_number,
        sender_name=sender_name, unit_name=unit_name, body=body,
    )

    for attachment in attachments:
        dest = _copy_attachment(report_id, attachment, subdir="adjacent")
        if dest is not None:
            db.insert_adjacent_report_attachment(
                conn, adjacent_report_id=report_id, file_path=str(dest),
                content_type=attachment.get("contentType"),
            )

    return True


def sync(timeout_seconds: int = 5, group_id: Optional[str] = None) -> int:
    """Fetch new messages from Signal and store them locally. Returns the
    number of new messages ingested. If `group_id` is given, only messages
    from that Signal group are ingested. Records the attempt via
    db.record_receive_attempt either way (success or failure) -- see that
    function's docstring -- so a one-shot `sync` failure shows up on the
    header status strip the same way a watch-loop failure does, instead
    of only ever being visible in whatever terminal happened to run it."""
    try:
        envelopes = receive(timeout_seconds=timeout_seconds)
    except SignalCliError as exc:
        with db.get_connection() as conn:
            db.record_receive_attempt(conn, error=str(exc))
        raise
    ingested = 0
    with db.get_connection() as conn:
        for envelope in envelopes:
            if ingest_envelope(conn, envelope, group_id=group_id):
                ingested += 1
        db.record_receive_attempt(conn)
    return ingested


def print_qr(data: str) -> None:
    """Render `data` as an ASCII QR code in the terminal. Falls back to
    printing the raw text if the optional `qrcode` package isn't
    installed -- linking still works, just without the visual code."""
    try:
        import qrcode
    except ImportError:
        print("(installera paketet 'qrcode' för en QR-kod i terminalen: pip install qrcode)")
        print(data)
        return

    qr = qrcode.QRCode(border=1)
    qr.add_data(data)
    qr.make()
    qr.print_ascii(invert=True)


def link_device(
    name: str = "signal-events", show_qr: bool = True, timeout_seconds: int = 300
) -> str:
    """Run `signal-cli link` to link this laptop as a secondary device on
    an existing Signal account. Blocks until the phone scans the QR code
    (or `timeout_seconds` elapses) and returns the linked phone number."""
    cmd = [config.SIGNAL_CLI_BIN, "link", "-n", name]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc

    number: Optional[str] = None
    shown_qr = False
    deadline = time.monotonic() + timeout_seconds
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            print(line)

            uri_match = _URI_RE.search(line)
            if uri_match and show_qr and not shown_qr:
                print_qr(uri_match.group(1))
                shown_qr = True

            if "associat" in line.lower():
                phone_match = _PHONE_RE.search(line)
                if phone_match:
                    number = phone_match.group(0)

            if time.monotonic() > deadline:
                proc.terminate()
                raise SignalCliError(
                    f"Timed out after {timeout_seconds}s waiting for the "
                    "phone to scan the QR code."
                )
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    if proc.returncode != 0 or number is None:
        raise SignalCliError(
            "signal-cli link did not complete successfully; see output above."
        )
    return number


def list_accounts() -> list[str]:
    """List phone numbers signal-cli already has registered/linked
    locally on this machine."""
    cmd = [config.SIGNAL_CLI_BIN, "listAccounts"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SignalCliError("signal-cli listAccounts timed out") from exc
    if result.returncode != 0:
        raise SignalCliError(f"signal-cli listAccounts failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def list_devices(phone_number: str) -> list[str]:
    """List devices linked to `phone_number`'s Signal account."""
    cmd = [config.SIGNAL_CLI_BIN, "-u", phone_number, "listDevices"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SignalCliError("signal-cli listDevices timed out") from exc
    if result.returncode != 0:
        raise SignalCliError(f"signal-cli listDevices failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def list_groups() -> list[dict[str, str]]:
    """List Signal groups the configured account is a member of, as
    [{"id": ..., "name": ...}, ...]."""
    if not config.PHONE_NUMBER:
        raise SignalCliError(
            "SIGNAL_EVENTS_PHONE_NUMBER is not set; see README for setup."
        )
    cmd = [config.SIGNAL_CLI_BIN, "-u", config.PHONE_NUMBER, "-o", "json", "listGroups"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SignalCliError("signal-cli listGroups timed out") from exc
    if result.returncode != 0:
        raise SignalCliError(f"signal-cli listGroups failed: {result.stderr.strip()}")

    try:
        raw_groups = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SignalCliError(
            "signal-cli listGroups returned unexpected output (not JSON)."
        ) from exc

    groups = []
    for group in raw_groups:
        group_id = group.get("id") or group.get("groupId")
        if not group_id:
            continue
        groups.append({"id": group_id, "name": group.get("name") or group.get("groupName") or ""})
    return groups


def find_group_id_by_name(name: str) -> str:
    """Resolve a Signal group's display name to its group id. Raises
    SignalCliError with the list of available group names if there's no
    exact match, or more than one."""
    groups = list_groups()
    matches = [g for g in groups if g["name"].strip().lower() == name.strip().lower()]
    if not matches:
        available = ", ".join(repr(g["name"]) for g in groups if g["name"]) or "(none found)"
        raise SignalCliError(
            f"No Signal group named {name!r} found for this account. "
            f"Groups available: {available}."
        )
    if len(matches) > 1:
        raise SignalCliError(
            f"Multiple Signal groups are named {name!r}; rename one of them "
            "in Signal so the name is unique."
        )
    return matches[0]["id"]


def watch_group(
    group_name: str,
    poll_timeout_seconds: int = 20,
    max_iterations: Optional[int] = None,
) -> Iterator[int]:
    """Resolve `group_name` to a group id once, then repeatedly long-poll
    signal-cli for new messages, ingesting only ones posted to that group.
    Yields the number of new events ingested after each poll cycle. Runs
    until `max_iterations` polls have completed, or forever if None (the
    caller stops it, e.g. on Ctrl+C)."""
    group_id = find_group_id_by_name(group_name)
    iterations = 0
    while max_iterations is None or iterations < max_iterations:
        yield sync(timeout_seconds=poll_timeout_seconds, group_id=group_id)
        iterations += 1


_WATCH_RETRY_DELAY_SECONDS = 5.0


def watch_multi(
    incident_group_name: str,
    adjacent_group_name: str,
    sensor_group_name: str,
    tak_bridge_group_name: str,
    poll_timeout_seconds: int = 20,
    max_iterations: Optional[int] = None,
    retry_delay_seconds: float = _WATCH_RETRY_DELAY_SECONDS,
) -> Iterator[tuple[int, int, int, int]]:
    """Resolve all four group names to ids once, then repeatedly long-poll
    signal-cli for new messages -- deliberately a *single* `receive` call
    per cycle covering all four groups (running multiple concurrent
    signal-cli processes against the same linked account can corrupt its
    local state), routing each incoming envelope to the right store based
    on which group it was posted to. Sensor-trigger events are parsed and
    stored exactly like human incident reports (see ingest_envelope) --
    same 7S format, just a separate source group -- so they show up in
    the same event log as everything else. The TAK-bridge group is the
    inbound half of the Signal-based ATAK bridge (see
    config.TAK_BRIDGE_GROUP_NAME): a future TAK-side plugin relays ATAK
    GeoChat messages here as plain text, parsed the same way. Yields
    (incident_count, adjacent_count, sensor_count, tak_bridge_count)
    after each poll cycle. Runs until `max_iterations` polls have
    completed, or forever if None (the caller stops it, e.g. on
    Ctrl+C).

    A single bad cycle -- a transient network blip, signal-cli briefly
    failing, an unexpected error from ingestion -- no longer kills the
    loop: any exception during a cycle is caught, recorded via
    db.record_receive_attempt (so it's visible on the header status
    strip), logged to the system log (see below), and the loop waits
    `retry_delay_seconds` before trying again rather than raising and
    ending the generator. Only the very first *unresolvable* problem
    (find_group_id_by_name failing above, before the loop starts --
    e.g. a typo'd or renamed group name) still raises immediately, since
    retrying that forever would just repeat the same failure. System-log
    writes are edge-triggered (once when a run of failures starts, once
    when it recovers), not one per cycle, so a multi-hour outage doesn't
    flood Systemlogg with hundreds of identical entries.

    One more case is logged (every time, not edge-triggered -- it's rare
    enough not to flood anything): a message sent to the *incident* group
    from the same account signal-cli is linked to. That's always a human
    testing ingestion from their own phone, never routine app traffic --
    this app only ever sends its own generated reports to the adjacent/
    recurring groups, never the incident group -- so unlike the same
    thing happening on the other two groups, it's unambiguous enough to
    call out by name (see _own_sync_message_group_id's docstring for why
    it's otherwise invisible)."""
    incident_group_id = find_group_id_by_name(incident_group_name)
    adjacent_group_id = find_group_id_by_name(adjacent_group_name)
    sensor_group_id = find_group_id_by_name(sensor_group_name)
    tak_bridge_group_id = find_group_id_by_name(tak_bridge_group_name)
    iterations = 0
    was_erroring = False
    while max_iterations is None or iterations < max_iterations:
        try:
            envelopes = receive(timeout_seconds=poll_timeout_seconds)
            incident_count = 0
            adjacent_count = 0
            sensor_count = 0
            tak_bridge_count = 0
            with db.get_connection() as conn:
                for envelope in envelopes:
                    if ingest_envelope(conn, envelope, group_id=incident_group_id):
                        incident_count += 1
                    if ingest_adjacent_report(conn, envelope, group_id=adjacent_group_id):
                        adjacent_count += 1
                    if ingest_envelope(conn, envelope, group_id=sensor_group_id, is_sensor=True):
                        sensor_count += 1
                    if ingest_envelope(conn, envelope, group_id=tak_bridge_group_id, is_tak_bridge=True):
                        tak_bridge_count += 1
                    inner = envelope.get("envelope", envelope)
                    if _own_sync_message_group_id(inner) == incident_group_id:
                        db.log_system_event(
                            conn, "watch_self_message_skipped",
                            "Ett meddelande skickat från samma Signal-konto som "
                            "signal-cli använder sågs i bevakningsgruppen men "
                            "hoppas över automatiskt -- Signal levererar egna "
                            "skickade meddelanden annorlunda än mottagna, så de "
                            "kan inte tas emot på samma sätt. Testa gärna genom "
                            "att skicka från ett annat Signal-konto/telefon.",
                        )
                db.record_receive_attempt(conn)
                if was_erroring:
                    db.log_system_event(
                        conn, "watch_recovered",
                        "signal-cli receive succeeded again after a failure.",
                    )
                    was_erroring = False
        except Exception as exc:
            incident_count = adjacent_count = sensor_count = tak_bridge_count = 0
            with db.get_connection() as conn:
                db.record_receive_attempt(conn, error=str(exc))
                if not was_erroring:
                    db.log_system_event(conn, "watch_error", str(exc))
                    was_erroring = True
            time.sleep(retry_delay_seconds)
        yield incident_count, adjacent_count, sensor_count, tak_bridge_count
        iterations += 1


def send_message(
    group_id: str,
    message: str = "",
    attachment_paths: Optional[list[str]] = None,
    timeout_seconds: int = 60,
) -> None:
    """Send a text message, optionally with file attachments, to a Signal
    group. Requires network access and a linked/registered account."""
    if not config.PHONE_NUMBER:
        raise SignalCliError(
            "SIGNAL_EVENTS_PHONE_NUMBER is not set; see README for setup."
        )
    cmd = [
        config.SIGNAL_CLI_BIN, "-u", config.PHONE_NUMBER,
        "--trust-new-identities", "always",
        "send", "-g", group_id,
    ]
    if message:
        cmd += ["-m", message]
    for path in attachment_paths or []:
        cmd += ["-a", path]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_seconds
        )
    except FileNotFoundError as exc:
        raise SignalCliError(
            f"signal-cli binary not found ({config.SIGNAL_CLI_BIN!r}); "
            "install it and/or set SIGNAL_EVENTS_SIGNAL_CLI_BIN."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise SignalCliError("signal-cli send timed out") from exc

    if result.returncode != 0:
        raise SignalCliError(f"signal-cli send failed: {result.stderr.strip()}")


def send_to_group_by_name(
    group_name: str,
    message: str = "",
    attachment_paths: Optional[list[str]] = None,
    timeout_seconds: int = 60,
) -> None:
    """Resolve `group_name` to a group id (via `signal-cli listGroups`) and
    send the message/attachments there."""
    group_id = find_group_id_by_name(group_name)
    send_message(
        group_id, message=message, attachment_paths=attachment_paths,
        timeout_seconds=timeout_seconds,
    )
