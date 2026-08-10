from unittest.mock import MagicMock, patch

import pytest

from signal_events import cli, db, signal_client


def test_resolve_watch_group_prefers_an_explicit_argument():
    with db.get_connection() as conn:
        db.set_watch_group_name(conn, "Från Inställningar")
    assert cli._resolve_watch_group("Explicit --group") == "Explicit --group"


def test_resolve_watch_group_falls_back_to_the_configured_setting():
    with db.get_connection() as conn:
        db.set_watch_group_name(conn, "Från Inställningar")
    assert cli._resolve_watch_group(None) == "Från Inställningar"


def test_resolve_watch_group_falls_back_to_config_when_nothing_is_set():
    assert cli._resolve_watch_group(None) == cli.config.WATCH_GROUP_NAME


def test_run_watch_loop_prints_heartbeat_after_silent_polls(capsys, monkeypatch):
    monkeypatch.setattr(cli.config, "REPORT_GROUP_NAME", "Report Group")
    with patch.object(
        signal_client, "watch_multi",
        return_value=iter([(0, 0, 0)] * cli._WATCH_HEARTBEAT_EVERY),
    ):
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    out = capsys.readouterr().out
    assert "Incident Group" in out
    assert "Report Group" in out
    assert "Fortfarande bevakar" in out


def test_run_watch_loop_prints_ingested_counts_and_resets_heartbeat(capsys, monkeypatch):
    monkeypatch.setattr(cli.config, "REPORT_GROUP_NAME", "Report Group")
    with patch.object(
        signal_client, "watch_multi", return_value=iter([(2, 1, 0), (0, 0, 0), (0, 0, 0)])
    ):
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    out = capsys.readouterr().out
    assert "Hämtade 2 ny(a) rapport(er) från 'Incident Group'." in out
    assert "1 statusuppdatering(ar)" in out
    assert "Report Group" in out
    assert "Fortfarande bevakar" not in out  # only 2 silent polls, below heartbeat threshold


def test_run_watch_loop_prints_sensor_event_counts(capsys, monkeypatch):
    monkeypatch.setattr(cli.config, "SENSOR_GROUP_NAME", "Sensor Group")
    with patch.object(signal_client, "watch_multi", return_value=iter([(0, 0, 3)])):
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    out = capsys.readouterr().out
    assert "Hämtade 3 sensorhändelse(r) från 'Sensor Group'." in out


def test_run_watch_loop_polls_the_report_and_sensor_groups(monkeypatch):
    """The adjacent-unit status channel is no longer independently
    configurable -- it's always whatever REPORT_GROUP_NAME is, since
    that's the same group this unit's own reports are sent to. Same
    story for the sensor channel and SENSOR_GROUP_NAME."""
    monkeypatch.setattr(cli.config, "REPORT_GROUP_NAME", "Report Group")
    monkeypatch.setattr(cli.config, "SENSOR_GROUP_NAME", "Sensor Group")
    with patch.object(
        signal_client, "watch_multi", return_value=iter([(0, 0, 0)])
    ) as mock_watch_multi:
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    mock_watch_multi.assert_called_once_with(
        "Incident Group", "Report Group", "Sensor Group", poll_timeout_seconds=5
    )


def test_run_watch_loop_logs_watch_started():
    with patch.object(signal_client, "watch_multi", return_value=iter([(0, 0, 0)])):
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    with db.get_connection() as conn:
        entries = db.list_system_log(conn)
    assert any(e["event_type"] == "watch_started" for e in entries)


def test_run_watch_loop_prints_and_clears_receive_error_immediately(capsys):
    """The error must show up in the terminal the moment it appears (not
    wait for the periodic heartbeat), and again the moment it clears --
    signal_client.watch_multi retries a failing cycle internally now
    (see its own docstring) rather than raising, so without this the
    terminal would show nothing different at all during an outage."""
    with patch.object(
        signal_client, "watch_multi",
        return_value=iter([(0, 0, 0), (0, 0, 0)]),
    ), patch.object(
        cli.db, "get_last_receive_error", side_effect=["nätverksfel", None],
    ):
        cli._run_watch_loop("Incident Group", poll_timeout=5)

    captured = capsys.readouterr()
    assert "nätverksfel" in captured.err
    assert "Försöker igen automatiskt" in captured.err
    assert "fungerar igen" in captured.out


def test_cmd_watch_exits_nonzero_on_signal_cli_error():
    args = MagicMock(group="Incident Group", poll_timeout=20)
    with patch.object(signal_client, "watch_multi", side_effect=signal_client.SignalCliError("no group")):
        with pytest.raises(SystemExit) as exc_info:
            cli.cmd_watch(args)
    assert exc_info.value.code == 1


def test_cmd_watch_logs_watch_stopped_on_signal_cli_error():
    args = MagicMock(group="Incident Group", poll_timeout=20)
    with patch.object(signal_client, "watch_multi", side_effect=signal_client.SignalCliError("no group")):
        with pytest.raises(SystemExit):
            cli.cmd_watch(args)

    with db.get_connection() as conn:
        entries = db.list_system_log(conn)
    stopped = [e for e in entries if e["event_type"] == "watch_stopped"]
    assert len(stopped) == 1
    assert "no group" in stopped[0]["detail"]


def test_cmd_serve_starts_background_thread_when_watch_set():
    args = MagicMock(
        watch=True, watch_group="Incident Group",
        watch_poll_timeout=20, host="127.0.0.1", port=5000,
    )
    fake_app = MagicMock()
    with patch("signal_events.webapp.create_app", return_value=fake_app), \
         patch("threading.Thread") as mock_thread_cls:
        cli.cmd_serve(args)

    mock_thread_cls.assert_called_once()
    _, kwargs = mock_thread_cls.call_args
    assert kwargs["target"] is cli._run_watch_in_background
    assert kwargs["args"] == ("Incident Group", 20)
    assert kwargs["daemon"] is True
    mock_thread_cls.return_value.start.assert_called_once()
    fake_app.run.assert_called_once_with(host="127.0.0.1", port=5000, debug=False)


def test_cmd_serve_skips_thread_without_watch_flag():
    args = MagicMock(watch=False, host="127.0.0.1", port=5000)
    fake_app = MagicMock()
    with patch("signal_events.webapp.create_app", return_value=fake_app), \
         patch("threading.Thread") as mock_thread_cls:
        cli.cmd_serve(args)

    mock_thread_cls.assert_not_called()


def test_cmd_serve_logs_a_server_start_event():
    args = MagicMock(watch=False, host="0.0.0.0", port=5001)
    fake_app = MagicMock()
    with patch("signal_events.webapp.create_app", return_value=fake_app):
        cli.cmd_serve(args)

    with db.get_connection() as conn:
        entries = db.list_system_log(conn)

    assert len(entries) == 1
    assert entries[0]["event_type"] == "server_start"
    assert "host=0.0.0.0" in entries[0]["detail"]
    assert "port=5001" in entries[0]["detail"]


def test_run_watch_in_background_reports_signalclierror_without_raising(capsys):
    with patch.object(signal_client, "watch_multi", side_effect=signal_client.SignalCliError("no group")):
        cli._run_watch_in_background("Incident Group", 20)  # must not raise

    err = capsys.readouterr().err
    assert "misslyckades" in err
    assert "webbgränssnittet" in err

    with db.get_connection() as conn:
        entries = db.list_system_log(conn)
    stopped = [e for e in entries if e["event_type"] == "watch_stopped"]
    assert len(stopped) == 1
    assert "no group" in stopped[0]["detail"]


def test_run_watch_in_background_survives_an_unexpected_exception(capsys):
    """Safety net for a genuinely unexpected bug -- signal_client.watch_multi
    already retries anything it recognises internally (see its own
    docstring), so this exercises the last-resort case: something it
    couldn't handle escapes the generator entirely. Must not propagate
    out of the thread target (Python's default thread excepthook would
    just print a bare traceback with no trace anywhere the web UI can
    show), and must still be logged."""
    with patch.object(signal_client, "watch_multi", side_effect=RuntimeError("kaboom")):
        cli._run_watch_in_background("Incident Group", 20)  # must not raise

    err = capsys.readouterr().err
    assert "oväntat fel" in err
    assert "kaboom" in err

    with db.get_connection() as conn:
        entries = db.list_system_log(conn)
    stopped = [e for e in entries if e["event_type"] == "watch_stopped"]
    assert len(stopped) == 1
    assert "kaboom" in stopped[0]["detail"]


def test_cmd_report_excludes_newly_classified_trivial_events(tmp_path):
    """End-to-end regression for the stale-row bug: a trivial event
    detected *during this same report generation* must not appear in the
    output, even though it wasn't marked is_trivial in the database until
    this very call."""
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Skogsbrynet", "object": "Rådjur",
                "activity": "Betade vid stängslet", "needs_review": False,
            },
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Bortre parkeringen", "object": "Beväpnad person",
                "activity": "Siktades vid bortre parkeringen", "needs_review": False,
            },
        )

    output_path = tmp_path / "report.md"
    args = MagicMock(
        since="all", include_unreviewed=False, format="markdown", output=str(output_path),
    )
    cli.cmd_report(args)

    content = output_path.read_text(encoding="utf-8")
    assert "Beväpnad person" in content
    assert "Rådjur" not in content

    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        trivial = [e for e in events if e["object"] == "Rådjur"]
        assert trivial[0]["is_trivial"] == 1


def test_cmd_report_excludes_events_received_from_adjacent_units(tmp_path):
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "Egen", "object": "Beväpnad person", "needs_review": False},
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Angränsande", "object": "Misstänkt fordon", "needs_review": False,
                "source_unit": "2.Pluton",
            },
        )

    output_path = tmp_path / "report.md"
    args = MagicMock(
        since="all", include_unreviewed=False, format="markdown", output=str(output_path),
    )
    cli.cmd_report(args)

    content = output_path.read_text(encoding="utf-8")
    assert "Beväpnad person" in content
    assert "Misstänkt fordon" not in content


def test_cmd_report_text_format_writes_plain_text_file(tmp_path):
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Norra grinden", "object": "Personbil",
                "activity": "Passerade grinden", "needs_review": False,
            },
        )

    output_path = tmp_path / "report.txt"
    args = MagicMock(
        since="all", include_unreviewed=False, format="text", output=str(output_path),
    )
    cli.cmd_report(args)

    content = output_path.read_text(encoding="utf-8")
    assert "HÄNDELSERAPPORT" in content
    assert "Norra grinden" in content
    assert not any(line.startswith("#") for line in content.splitlines())


def test_cmd_summary_text_format_writes_plain_text_file_and_logs_entry(tmp_path):
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "A", "object": "Civil", "needs_review": False},
        )

    output_path = tmp_path / "summary.txt"
    args = MagicMock(
        since="all", include_unreviewed=False, format="text", output=str(output_path), llm=False,
    )
    cli.cmd_summary(args)

    content = output_path.read_text(encoding="utf-8")
    assert "SAMMANSTÄLLD HOTBEDÖMNING" in content
    assert not any(line.startswith("#") for line in content.splitlines())

    with db_module.get_connection() as conn:
        entries = db_module.list_summary_log(conn)

    assert len(entries) == 1
    assert entries[0]["source"] == "cli"
    assert entries[0]["format"] == "text"
    assert entries[0]["period_label"] == "all"


def test_cmd_summary_excludes_events_received_from_adjacent_units(tmp_path):
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={"place": "A", "object": "Civil", "needs_review": False},
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "B", "object": "Civil", "needs_review": False,
                "source_unit": "2.Pluton",
            },
        )

    output_path = tmp_path / "summary.txt"
    args = MagicMock(
        since="all", include_unreviewed=False, format="text", output=str(output_path), llm=False,
    )
    cli.cmd_summary(args)

    content = output_path.read_text(encoding="utf-8")
    assert "Rapporter i underlaget: 1" in content


def test_cmd_summary_excludes_trivial_routine_events(tmp_path):
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        message_id = db_module.insert_message(
            conn, signal_timestamp=1, sender_number=None, sender_name=None,
            body="text", raw_json="{}",
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Skogsbrynet", "object": "Rådjur",
                "activity": "Passerade genom området", "needs_review": False,
            },
        )
        db_module.insert_event(
            conn, message_id=message_id,
            fields={
                "place": "Huvudentrén", "object": "Civil",
                "activity": "Fotograferade stängslet", "needs_review": False,
            },
        )

    output_path = tmp_path / "summary.txt"
    args = MagicMock(
        since="all", include_unreviewed=False, format="text", output=str(output_path), llm=False,
    )
    cli.cmd_summary(args)

    content = output_path.read_text(encoding="utf-8")
    assert "Rapporter i underlaget: 1" in content
