from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import analysis, config, db, duplicates, naming, triviality
from .reports import generator

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


def cmd_init_db(_args: argparse.Namespace) -> None:
    db.init_db()
    print(f"Database ready at {config.DB_PATH}")


def cmd_sync(args: argparse.Namespace) -> None:
    from . import signal_client  # imported lazily: needs network only here

    try:
        count = signal_client.sync(timeout_seconds=args.timeout)
    except signal_client.SignalCliError as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Ingested {count} new message(s).")


def cmd_link(args: argparse.Namespace) -> None:
    from . import signal_client  # imported lazily: needs network only here

    print(
        f"Länkar som '{args.name}' — skanna QR-koden nedan med "
        "Signal-appen (Inställningar > Länkade enheter > Länka ny enhet)."
    )
    try:
        number = signal_client.link_device(
            name=args.name, show_qr=not args.no_qr, timeout_seconds=args.timeout
        )
    except signal_client.SignalCliError as exc:
        print(f"link misslyckades: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"\nLänkad som {number}.")
    print(f"Sätt: export SIGNAL_EVENTS_PHONE_NUMBER=\"{number}\"")


def cmd_accounts(_args: argparse.Namespace) -> None:
    from . import signal_client

    try:
        accounts = signal_client.list_accounts()
    except signal_client.SignalCliError as exc:
        print(f"accounts misslyckades: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if not accounts:
        print("Inga konton registrerade/länkade i signal-cli ännu. Kör 'signal-events link' först.")
        return
    print("Konton kända av signal-cli på den här datorn:")
    for account in accounts:
        marker = " (konfigurerad)" if account == config.PHONE_NUMBER else ""
        print(f"  {account}{marker}")


def cmd_devices(args: argparse.Namespace) -> None:
    from . import signal_client

    number = args.number or config.PHONE_NUMBER
    if not number:
        print(
            "Inget nummer angivet. Ange --number +46... eller sätt "
            "SIGNAL_EVENTS_PHONE_NUMBER.", file=sys.stderr,
        )
        raise SystemExit(1)
    try:
        devices = signal_client.list_devices(number)
    except signal_client.SignalCliError as exc:
        print(f"devices misslyckades: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(f"Enheter länkade till {number}:")
    for device in devices:
        print(f"  {device}")


_WATCH_HEARTBEAT_EVERY = 15  # print a "still watching" line every N silent polls


def _resolve_watch_group(explicit: str | None) -> str:
    """An explicit --group/--watch-group flag always wins; otherwise uses
    whatever's configured on Inställningar (see db.get_watch_group_name),
    falling back to SIGNAL_EVENTS_WATCH_GROUP if never set there."""
    if explicit:
        return explicit
    with db.get_connection() as conn:
        return db.get_watch_group_name(conn)


def _resolve_report_group() -> str:
    with db.get_connection() as conn:
        return db.get_report_group_name(conn)


def _resolve_sensor_group() -> str:
    with db.get_connection() as conn:
        return db.get_sensor_group_name(conn)


def _run_watch_loop(incident_group: str, poll_timeout: int, prefix: str = "") -> None:
    """Shared loop used by both `signal-events watch` and `serve --watch`.
    Polls the incident-intake group, the report group (which doubles as
    the adjacent-units exchange channel -- see db.get_report_group_name),
    and the sensor group (automated sensor-trigger events, parsed and
    stored the same way as human incident reports -- see
    db.get_sensor_group_name) in a single signal-cli receive call per
    cycle (see signal_client.watch_multi). Prints progress so it's
    obvious the poller is alive even when nothing new has arrived (silent
    long-running processes are hard to trust). All three group names are
    resolved once, here, at the start of the loop -- a later change on
    Inställningar only takes effect on the next restart of this loop."""
    from . import signal_client  # imported lazily: needs network only here

    adjacent_group = _resolve_report_group()
    sensor_group = _resolve_sensor_group()
    print(
        f"{prefix}Bevakar Signal-grupperna '{incident_group}' (händelser), "
        f"'{adjacent_group}' (rapport-gruppen, för angränsande enheters status) och "
        f"'{sensor_group}' (sensorgruppen, för automatiska sensorhändelser)..."
    )
    silent_polls = 0
    for incident_count, adjacent_count, sensor_count in signal_client.watch_multi(
        incident_group, adjacent_group, sensor_group, poll_timeout_seconds=poll_timeout
    ):
        if incident_count or adjacent_count or sensor_count:
            if incident_count:
                print(f"{prefix}Hämtade {incident_count} ny(a) rapport(er) från '{incident_group}'.")
            if adjacent_count:
                print(
                    f"{prefix}Hämtade {adjacent_count} statusuppdatering(ar) "
                    f"från angränsande enheter ('{adjacent_group}')."
                )
            if sensor_count:
                print(
                    f"{prefix}Hämtade {sensor_count} sensorhändelse(r) från '{sensor_group}'."
                )
            silent_polls = 0
        else:
            silent_polls += 1
            if silent_polls % _WATCH_HEARTBEAT_EVERY == 0:
                print(f"{prefix}Fortfarande bevakar, inga nya rapporter än.")


def cmd_watch(args: argparse.Namespace) -> None:
    from . import signal_client  # imported lazily: needs network only here

    try:
        _run_watch_loop(_resolve_watch_group(args.group), args.poll_timeout)
    except signal_client.SignalCliError as exc:
        print(f"watch misslyckades: {exc}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\nAvslutar bevakning.")


def _run_watch_in_background(incident_group: str, poll_timeout: int) -> None:
    from . import signal_client  # imported lazily: needs network only here

    try:
        _run_watch_loop(incident_group, poll_timeout, prefix="[watch] ")
    except signal_client.SignalCliError as exc:
        print(f"[watch] misslyckades: {exc}", file=sys.stderr)
        print(
            "[watch] Bakgrundsbevakningen är avstängd; webbgränssnittet "
            "fortsätter fungera som vanligt.", file=sys.stderr,
        )


def cmd_serve(args: argparse.Namespace) -> None:
    from .webapp import create_app

    if args.watch:
        import threading

        threading.Thread(
            target=_run_watch_in_background,
            args=(_resolve_watch_group(args.watch_group), args.watch_poll_timeout),
            daemon=True,
        ).start()

    app = create_app()
    # Lets webapp/routes.py tell whether the server is actually reachable
    # from other devices (see _lan_url) -- config.WEB_HOST only reflects
    # the env var/default, not this actual --host value.
    app.config["BIND_HOST"] = args.host
    with db.get_connection() as conn:
        db.log_system_event(conn, "server_start", f"host={args.host} port={args.port}")
    app.run(host=args.host, port=args.port, debug=False)


_FORMAT_EXT = {"markdown": "md", "text": "txt", "pdf": "pdf"}


def cmd_report(args: argparse.Namespace) -> None:
    db.init_db()
    with db.get_connection() as conn:
        needs_review = None if args.include_unreviewed else False
        events = db.list_events(
            conn, since=_since_iso(args.since), needs_review=needs_review, own_only=True
        )
        trivial_ids = triviality.classify_trivial_events(conn, events)
        unit_name = db.get_unit_name(conn)
        rows = []
        for event in events:
            if event["id"] in trivial_ids:
                continue
            message = db.get_message(conn, event["message_id"])
            attachments = db.list_attachments_for_message(conn, event["message_id"])
            rows.append({"event": event, "message": message, "attachments": attachments})

    output = Path(args.output) if args.output else Path(
        naming.build_report_filename(unit_name, "handelserapport", _FORMAT_EXT[args.format])
    )

    if args.format == "markdown":
        output.write_text(generator.render_markdown(rows, since_label=args.since), encoding="utf-8")
    elif args.format == "text":
        output.write_text(generator.render_text(rows, since_label=args.since), encoding="utf-8")
    else:
        buf = generator.render_pdf(rows, since_label=args.since)
        output.write_bytes(buf.read())

    trivial_note = f", {len(trivial_ids)} trivial event(s) excluded" if trivial_ids else ""
    print(f"Wrote {output} ({len(rows)} event(s){trivial_note}).")


def cmd_summary(args: argparse.Namespace) -> None:
    db.init_db()
    with db.get_connection() as conn:
        needs_review = None if args.include_unreviewed else False
        events = db.list_events(
            conn, since=_since_iso(args.since), needs_review=needs_review, own_only=True
        )
        duplicate_ids = duplicates.classify_duplicate_events(conn, events)
        trivial_ids = triviality.classify_trivial_events(conn, events)
        events = [e for e in events if e["id"] not in duplicate_ids and e["id"] not in trivial_ids]
        summary_data = analysis.build_summary(events, period_label=args.since)
        override = db.get_threat_override(conn)
        summary_data = analysis.apply_threat_override(summary_data, override)
        unit_name = db.get_unit_name(conn)
        tnr = naming.generate_tnr()
        db.insert_summary_log_entry(
            conn, tnr=tnr, unit_name=unit_name, period_label=args.since,
            total_events=summary_data.total_events,
            level=summary_data.threat.level, score=summary_data.threat.score,
            source="cli", format=args.format,
        )

    narrative = None
    if args.llm:
        from . import llm  # imported lazily: needs a local Ollama server only here

        with db.get_connection() as conn:
            base_url = llm.resolve_ollama_url(db.get_ollama_port(conn))
        print(f"Genererar AI-sammanfattning via Ollama ({config.OLLAMA_MODEL} på {base_url})...")
        try:
            narrative = llm.generate_narrative(summary_data, site_name=config.SITE_NAME, base_url=base_url)
        except llm.LLMError as exc:
            print(f"AI-sammanfattning misslyckades: {exc}", file=sys.stderr)
            print("Fortsätter utan AI-sammanfattning.", file=sys.stderr)

    output = Path(args.output) if args.output else Path(
        naming.build_report_filename(unit_name, "hotbedomning", _FORMAT_EXT[args.format], tnr=tnr)
    )

    if args.format == "markdown":
        content = generator.render_summary_markdown(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        output.write_text(content, encoding="utf-8")
    elif args.format == "text":
        content = generator.render_summary_text(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        output.write_text(content, encoding="utf-8")
    else:
        buf = generator.render_summary_pdf(
            summary_data, site_name=config.SITE_NAME, narrative=narrative
        )
        output.write_bytes(buf.read())

    print(
        f"Wrote {output} (threat level: {summary_data.threat.level}, "
        f"score {summary_data.threat.score}, {summary_data.total_events} event(s))."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signal-events")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create the local SQLite database.").set_defaults(
        func=cmd_init_db
    )

    p_sync = sub.add_parser(
        "sync", help="Fetch new Signal messages (requires network + signal-cli)."
    )
    p_sync.add_argument("--timeout", type=int, default=5, help="signal-cli receive timeout, seconds")
    p_sync.set_defaults(func=cmd_sync)

    p_link = sub.add_parser(
        "link",
        help="Link this laptop as a secondary Signal device (requires network + signal-cli).",
    )
    p_link.add_argument("--name", default="signal-events", help="Device name shown on the phone")
    p_link.add_argument("--no-qr", action="store_true", help="Print the link URI as text instead of a QR code")
    p_link.add_argument("--timeout", type=int, default=300, help="Seconds to wait for the phone to scan")
    p_link.set_defaults(func=cmd_link)

    sub.add_parser(
        "accounts", help="List Signal accounts signal-cli already knows about locally."
    ).set_defaults(func=cmd_accounts)

    p_devices = sub.add_parser(
        "devices", help="List devices linked to a Signal account (requires network + signal-cli)."
    )
    p_devices.add_argument("--number", help="Phone number, e.g. +46701234567 (defaults to SIGNAL_EVENTS_PHONE_NUMBER)")
    p_devices.set_defaults(func=cmd_devices)

    p_watch = sub.add_parser(
        "watch",
        help="Continuously poll a Signal group for new reports (requires network + signal-cli).",
    )
    p_watch.add_argument(
        "--group", default=None,
        help="Signal group to poll for incident reports. Defaults to whatever's "
             "configured on Inställningar (falls back to SIGNAL_EVENTS_WATCH_GROUP, "
             f'currently "{config.WATCH_GROUP_NAME}", if never set there). Also polls '
             "the configured report group (SIGNAL_EVENTS_REPORT_GROUP by default, "
             f'currently "{config.REPORT_GROUP_NAME}") for adjacent-unit status reports '
             "-- the same group this unit's own reports are sent to -- and the "
             "configured sensor group (SIGNAL_EVENTS_SENSOR_GROUP by default, "
             f'currently "{config.SENSOR_GROUP_NAME}") for automated sensor-trigger '
             "events, parsed and stored the same way as incident reports.",
    )
    p_watch.add_argument(
        "--poll-timeout", type=int, default=20,
        help="signal-cli long-poll timeout per cycle, seconds",
    )
    p_watch.set_defaults(func=cmd_watch)

    p_serve = sub.add_parser("serve", help="Start the local review/report web UI.")
    p_serve.add_argument("--host", default=config.WEB_HOST)
    p_serve.add_argument("--port", type=int, default=config.WEB_PORT)
    p_serve.add_argument(
        "--watch", action="store_true",
        help="Also continuously poll Signal groups for new reports in the background (requires network + signal-cli)",
    )
    p_serve.add_argument(
        "--watch-group", default=None,
        help="Signal group to poll for incident reports when --watch is set. Defaults "
             "to whatever's configured on Inställningar (falls back to "
             f'SIGNAL_EVENTS_WATCH_GROUP, currently "{config.WATCH_GROUP_NAME}", if never '
             "set there). Also polls the configured report group "
             f'(SIGNAL_EVENTS_REPORT_GROUP by default, currently "{config.REPORT_GROUP_NAME}") '
             "for adjacent-unit status reports -- the same group this unit's own reports "
             "are sent to -- and the configured sensor group "
             f'(SIGNAL_EVENTS_SENSOR_GROUP by default, currently "{config.SENSOR_GROUP_NAME}") '
             "for automated sensor-trigger events, parsed and stored the same way as "
             "incident reports.",
    )
    p_serve.add_argument(
        "--watch-poll-timeout", type=int, default=20,
        help="signal-cli long-poll timeout per cycle when --watch is set, seconds",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_report = sub.add_parser("report", help="Generate a summary report from stored events.")
    p_report.add_argument("--since", choices=list(_SINCE_PRESETS), default="7d")
    p_report.add_argument("--format", choices=["pdf", "markdown", "text"], default="pdf")
    p_report.add_argument("--output", help="Output file path")
    p_report.add_argument(
        "--include-unreviewed", action="store_true",
        help="Include events not yet confirmed in the web UI",
    )
    p_report.set_defaults(func=cmd_report)

    p_summary = sub.add_parser(
        "summary",
        help="Generate a consolidated threat-level summary (recurring vehicles/people).",
    )
    p_summary.add_argument("--since", choices=list(_SINCE_PRESETS), default="7d")
    p_summary.add_argument("--format", choices=["pdf", "markdown", "text"], default="pdf")
    p_summary.add_argument("--output", help="Output file path")
    p_summary.add_argument(
        "--include-unreviewed", action="store_true",
        help="Include events not yet confirmed in the web UI",
    )
    p_summary.add_argument(
        "--llm", action="store_true",
        help="Add an AI narrative via a local Ollama server (Llama 3.1 8B by default)",
    )
    p_summary.set_defaults(func=cmd_summary)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
