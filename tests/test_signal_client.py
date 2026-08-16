import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from signal_events import config, signal_client


def test_receive_uses_global_output_json_flag_not_receive_json(monkeypatch):
    # Regression: signal-cli's JSON output is the global `-o json` flag
    # (before the subcommand), not a `--json` flag on `receive` itself --
    # `signal-cli receive --json` is a hard error on real signal-cli.
    monkeypatch.setattr(config, "PHONE_NUMBER", "+46701234567")
    result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=result) as mock_run:
        signal_client.receive(timeout_seconds=5)

    cmd = mock_run.call_args[0][0]
    assert "--json" not in cmd
    assert "-o" in cmd
    assert cmd[cmd.index("-o") + 1] == "json"
    # the global flags must come before the `receive` subcommand
    assert cmd.index("-o") < cmd.index("receive")


def test_receive_trusts_new_identities_so_new_group_members_are_not_silently_dropped(monkeypatch):
    # Regression: without this, a message from someone signal-cli hasn't
    # seen before (a newly added group member) fails to decrypt and never
    # shows up in the JSON output at all -- not a filtering bug in our
    # code, but an upstream identity-trust default worth overriding.
    monkeypatch.setattr(config, "PHONE_NUMBER", "+46701234567")
    result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=result) as mock_run:
        signal_client.receive(timeout_seconds=5)

    cmd = mock_run.call_args[0][0]
    assert "--trust-new-identities" in cmd
    assert cmd[cmd.index("--trust-new-identities") + 1] == "always"
    assert cmd.index("--trust-new-identities") < cmd.index("receive")


def test_receive_raises_on_nonzero_exit_even_when_stdout_has_partial_output(monkeypatch):
    """Regression: a nonzero exit code used to only raise when stdout was
    *also* empty -- a partial/corrupted receive (exit code nonzero, some
    JSON lines still on stdout before signal-cli crashed) was silently
    treated as a clean success, discarding the real error on stderr
    entirely. That's exactly the kind of failure that made receive
    "seem to not work" with no visible cause."""
    monkeypatch.setattr(config, "PHONE_NUMBER", "+46701234567")
    result = MagicMock(
        returncode=1,
        stdout='{"envelope": {"timestamp": 1}}\n',
        stderr="signal-cli crashed mid-receive",
    )
    with patch("subprocess.run", return_value=result):
        with pytest.raises(signal_client.SignalCliError, match="signal-cli crashed mid-receive"):
            signal_client.receive(timeout_seconds=5)


class _FakePopen:
    def __init__(self, lines: list[str], returncode: int = 0):
        self.stdout = iter(lines)
        self._returncode = returncode
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        self.returncode = self._returncode
        return self._returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_link_device_success():
    lines = [
        "Please scan the QR code: sgnl://linkdevice?uuid=abc&pub_key=xyz\n",
        "Associated with: +46701234567\n",
    ]
    with patch("subprocess.Popen", return_value=_FakePopen(lines, returncode=0)), \
         patch.object(signal_client, "print_qr") as mock_print_qr:
        number = signal_client.link_device(name="test-device", show_qr=True)

    assert number == "+46701234567"
    mock_print_qr.assert_called_once()
    assert mock_print_qr.call_args[0][0].startswith("sgnl://linkdevice")


def test_link_device_skips_qr_when_disabled():
    lines = ["sgnl://linkdevice?uuid=abc\n", "Associated with: +46701234567\n"]
    with patch("subprocess.Popen", return_value=_FakePopen(lines, returncode=0)), \
         patch.object(signal_client, "print_qr") as mock_print_qr:
        signal_client.link_device(show_qr=False)

    mock_print_qr.assert_not_called()


def test_link_device_missing_number_raises():
    lines = ["Waiting for scan...\n"]
    with patch("subprocess.Popen", return_value=_FakePopen(lines, returncode=0)), \
         patch.object(signal_client, "print_qr"):
        with pytest.raises(signal_client.SignalCliError):
            signal_client.link_device(show_qr=False)


def test_link_device_nonzero_exit_raises():
    lines = ["Associated with: +46701234567\n"]
    with patch("subprocess.Popen", return_value=_FakePopen(lines, returncode=1)), \
         patch.object(signal_client, "print_qr"):
        with pytest.raises(signal_client.SignalCliError):
            signal_client.link_device(show_qr=False)


def test_link_device_binary_not_found():
    with patch("subprocess.Popen", side_effect=FileNotFoundError()):
        with pytest.raises(signal_client.SignalCliError, match="not found"):
            signal_client.link_device()


def test_list_accounts_success():
    result = MagicMock(returncode=0, stdout="+46701234567\n+46709999999\n", stderr="")
    with patch("subprocess.run", return_value=result):
        accounts = signal_client.list_accounts()
    assert accounts == ["+46701234567", "+46709999999"]


def test_list_accounts_failure_raises():
    result = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=result):
        with pytest.raises(signal_client.SignalCliError, match="boom"):
            signal_client.list_accounts()


def test_list_accounts_timeout_raises_clean_error():
    # Regression: subprocess.run(..., timeout=N) raises TimeoutExpired,
    # which must be turned into a SignalCliError, not left to crash the
    # caller (e.g. the serve --watch background thread) with a raw traceback.
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="signal-cli", timeout=15)):
        with pytest.raises(signal_client.SignalCliError, match="timed out"):
            signal_client.list_accounts()


def test_list_devices_success():
    result = MagicMock(returncode=0, stdout="Device 1 (this device)\nDevice 2\n", stderr="")
    with patch("subprocess.run", return_value=result):
        devices = signal_client.list_devices("+46701234567")
    assert devices == ["Device 1 (this device)", "Device 2"]


def test_list_devices_binary_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError()):
        with pytest.raises(signal_client.SignalCliError, match="not found"):
            signal_client.list_devices("+46701234567")


def test_list_devices_timeout_raises_clean_error():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="signal-cli", timeout=15)):
        with pytest.raises(signal_client.SignalCliError, match="timed out"):
            signal_client.list_devices("+46701234567")


def _group_envelope(timestamp: int, group_id: str | None, message: str = "hej") -> dict:
    inner = {
        "sourceNumber": "+46701111111",
        "sourceName": "Alice",
        "timestamp": timestamp,
        "dataMessage": {
            "timestamp": timestamp,
            "message": message,
            "attachments": [],
        },
    }
    if group_id is not None:
        inner["dataMessage"]["groupInfo"] = {"groupId": group_id}
    return {"envelope": inner}


def _own_sync_envelope(timestamp: int, group_id: str | None, message: str = "test") -> dict:
    """Shape of a `syncMessage` sent-transcript -- what signal-cli
    delivers instead of a `dataMessage` when the linked account sends a
    message itself (from another device/client using the same number)."""
    sent_message = {"timestamp": timestamp, "message": message}
    if group_id is not None:
        sent_message["groupInfo"] = {"groupId": group_id}
    return {"envelope": {"timestamp": timestamp, "syncMessage": {"sentMessage": sent_message}}}


def test_envelope_group_id_extracts_from_group_info():
    envelope = _group_envelope(1, "GROUP_A")
    inner = envelope["envelope"]
    assert signal_client._envelope_group_id(inner) == "GROUP_A"


def test_own_sync_message_group_id_extracts_from_sent_message():
    envelope = _own_sync_envelope(1, "GROUP_A")
    inner = envelope["envelope"]
    assert signal_client._own_sync_message_group_id(inner) == "GROUP_A"


def test_own_sync_message_group_id_none_for_ordinary_data_message():
    envelope = _group_envelope(1, "GROUP_A")
    inner = envelope["envelope"]
    assert signal_client._own_sync_message_group_id(inner) is None


def test_envelope_group_id_none_for_direct_message():
    envelope = _group_envelope(1, None)
    inner = envelope["envelope"]
    assert signal_client._envelope_group_id(inner) is None


def test_ingest_envelope_filters_by_group_id():
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        in_group = signal_client.ingest_envelope(
            conn, _group_envelope(1, "GROUP_A"), group_id="GROUP_A"
        )
        other_group = signal_client.ingest_envelope(
            conn, _group_envelope(2, "GROUP_B"), group_id="GROUP_A"
        )
        direct_message = signal_client.ingest_envelope(
            conn, _group_envelope(3, None), group_id="GROUP_A"
        )

    assert in_group is True
    assert other_group is False
    assert direct_message is False


def test_ingest_envelope_tags_events_as_sensor_when_requested():
    """See duplicates.py: is_sensor exempts the resulting event from
    duplicate detection, since a real sensor gateway firing the same
    templated message at the same place repeatedly is expected, not
    double entry."""
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        signal_client.ingest_envelope(conn, _group_envelope(1, "GROUP_S"), is_sensor=True)
        signal_client.ingest_envelope(conn, _group_envelope(2, "GROUP_S"), is_sensor=False)

        events = db_module.list_events(conn)
        sensor_flags = sorted(e["is_sensor"] for e in events)

    assert sensor_flags == [0, 1]


def test_list_groups_success_handles_key_variants():
    payload = json.dumps([
        {"id": "GROUP_A", "name": "Stabsassistent test"},
        {"groupId": "GROUP_B", "groupName": "Other group"},
    ])
    result = MagicMock(returncode=0, stdout=payload, stderr="")
    with patch("subprocess.run", return_value=result), \
         patch.object(config, "PHONE_NUMBER", "+46701234567"):
        groups = signal_client.list_groups()

    assert groups == [
        {"id": "GROUP_A", "name": "Stabsassistent test"},
        {"id": "GROUP_B", "name": "Other group"},
    ]


def test_list_groups_requires_phone_number(monkeypatch):
    monkeypatch.setattr(config, "PHONE_NUMBER", None)
    with pytest.raises(signal_client.SignalCliError, match="PHONE_NUMBER"):
        signal_client.list_groups()


def test_list_groups_timeout_raises_clean_error():
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="signal-cli", timeout=30)), \
         patch.object(config, "PHONE_NUMBER", "+46701234567"):
        with pytest.raises(signal_client.SignalCliError, match="timed out"):
            signal_client.list_groups()


def test_list_groups_invalid_json_raises():
    result = MagicMock(returncode=0, stdout="not json", stderr="")
    with patch("subprocess.run", return_value=result), \
         patch.object(config, "PHONE_NUMBER", "+46701234567"):
        with pytest.raises(signal_client.SignalCliError, match="JSON"):
            signal_client.list_groups()


def test_find_group_id_by_name_exact_match():
    with patch.object(
        signal_client, "list_groups",
        return_value=[{"id": "GROUP_A", "name": "Stabsassistent test"}],
    ):
        assert signal_client.find_group_id_by_name("stabsassistent test") == "GROUP_A"


def test_find_group_id_by_name_no_match_lists_available():
    with patch.object(
        signal_client, "list_groups",
        return_value=[{"id": "GROUP_A", "name": "Other group"}],
    ):
        with pytest.raises(signal_client.SignalCliError, match="Other group"):
            signal_client.find_group_id_by_name("Stabsassistent test")


def test_find_group_id_by_name_ambiguous_raises():
    with patch.object(
        signal_client, "list_groups",
        return_value=[
            {"id": "GROUP_A", "name": "Stabsassistent test"},
            {"id": "GROUP_B", "name": "Stabsassistent test"},
        ],
    ):
        with pytest.raises(signal_client.SignalCliError, match="Multiple"):
            signal_client.find_group_id_by_name("Stabsassistent test")


def test_watch_group_polls_sync_with_resolved_group_id_and_stops_at_max_iterations():
    with patch.object(signal_client, "find_group_id_by_name", return_value="GROUP_A") as mock_find, \
         patch.object(signal_client, "sync", side_effect=[2, 0, 1]) as mock_sync:
        counts = list(
            signal_client.watch_group("Stabsassistent test", poll_timeout_seconds=15, max_iterations=3)
        )

    assert counts == [2, 0, 1]
    mock_find.assert_called_once_with("Stabsassistent test")
    assert mock_sync.call_count == 3
    for call in mock_sync.call_args_list:
        assert call.kwargs == {"timeout_seconds": 15, "group_id": "GROUP_A"}


def test_sync_records_a_successful_receive_attempt():
    from signal_events import db as db_module

    with patch.object(signal_client, "receive", return_value=[]):
        count = signal_client.sync(timeout_seconds=5)

    assert count == 0
    with db_module.get_connection() as conn:
        assert db_module.get_last_receive_success(conn) is not None
        assert db_module.get_last_receive_error(conn) is None


def test_sync_records_and_reraises_a_failed_receive_attempt():
    """A one-shot `sync` failure used to be visible only in whatever
    terminal happened to run it (see cli.cmd_sync's own print) -- now
    it's also recorded the same way a watch-loop failure is, so it shows
    up on the header status strip regardless of who/what called sync()."""
    from signal_events import db as db_module

    with patch.object(
        signal_client, "receive", side_effect=signal_client.SignalCliError("no network"),
    ):
        with pytest.raises(signal_client.SignalCliError, match="no network"):
            signal_client.sync(timeout_seconds=5)

    with db_module.get_connection() as conn:
        assert db_module.get_last_receive_success(conn) is None
        assert db_module.get_last_receive_error(conn) == "no network"


def test_send_message_builds_correct_command(monkeypatch):
    monkeypatch.setattr(config, "PHONE_NUMBER", "+46701234567")
    result = MagicMock(returncode=0, stdout="", stderr="")
    with patch("subprocess.run", return_value=result) as mock_run:
        signal_client.send_message(
            "GROUP_A", message="Hej", attachment_paths=["/tmp/report.pdf"]
        )

    cmd = mock_run.call_args[0][0]
    assert cmd[:3] == [config.SIGNAL_CLI_BIN, "-u", "+46701234567"]
    assert "send" in cmd
    assert cmd[cmd.index("-g") + 1] == "GROUP_A"
    assert cmd[cmd.index("-m") + 1] == "Hej"
    assert cmd[cmd.index("-a") + 1] == "/tmp/report.pdf"
    # so sending to a group with a not-yet-trusted (e.g. newly joined)
    # member doesn't get refused
    assert cmd[cmd.index("--trust-new-identities") + 1] == "always"
    assert cmd.index("--trust-new-identities") < cmd.index("send")


def test_send_message_requires_phone_number(monkeypatch):
    monkeypatch.setattr(config, "PHONE_NUMBER", None)
    with pytest.raises(signal_client.SignalCliError, match="PHONE_NUMBER"):
        signal_client.send_message("GROUP_A", message="Hej")


def test_send_message_failure_raises():
    result = MagicMock(returncode=1, stdout="", stderr="not-a-group")
    with patch("subprocess.run", return_value=result), \
         patch.object(config, "PHONE_NUMBER", "+46701234567"):
        with pytest.raises(signal_client.SignalCliError, match="not-a-group"):
            signal_client.send_message("GROUP_A", message="Hej")


def test_send_message_binary_not_found():
    with patch("subprocess.run", side_effect=FileNotFoundError()), \
         patch.object(config, "PHONE_NUMBER", "+46701234567"):
        with pytest.raises(signal_client.SignalCliError, match="not found"):
            signal_client.send_message("GROUP_A", message="Hej")


def _adjacent_envelope(
    timestamp: int, group_id: str | None, filename: str | None = None,
    message: str = "Status", sender_name: str = "Bob", sender_number: str = "+46702222222",
) -> dict:
    inner = {
        "sourceNumber": sender_number,
        "sourceName": sender_name,
        "timestamp": timestamp,
        "dataMessage": {
            "timestamp": timestamp,
            "message": message,
            "attachments": [{"id": "att1", "filename": filename}] if filename else [],
        },
    }
    if group_id is not None:
        inner["dataMessage"]["groupInfo"] = {"groupId": group_id}
    return {"envelope": inner}


def test_ingest_adjacent_report_extracts_unit_name_from_attachment_filename():
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        ingested = signal_client.ingest_adjacent_report(
            conn,
            _adjacent_envelope(1, "GROUP_B", filename="Kompani_2_301842_aterkommande.pdf"),
            group_id="GROUP_B",
        )
        assert ingested is True

        reports = db_module.list_latest_adjacent_reports_per_unit(conn)
        assert len(reports) == 1
        assert reports[0]["unit_name"] == "Kompani_2"
        assert reports[0]["body"] == "Status"


def test_ingest_adjacent_report_stores_unidentified_message_but_excludes_it_from_the_unit_list():
    """This group is shared with this unit's own report distribution, so
    it can carry plain chat from people not posting a named report at
    all (e.g. someone just messaging in the group) -- that shouldn't be
    surfaced as if "Bob" were an adjacent unit's status. The message is
    still stored (for completeness), just with unit_name=None, and
    list_latest_adjacent_reports_per_unit filters those out."""
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        ingested = signal_client.ingest_adjacent_report(
            conn, _adjacent_envelope(1, "GROUP_B", filename=None, sender_name="Bob"),
            group_id="GROUP_B",
        )
        assert ingested is True

        reports = db_module.list_latest_adjacent_reports_per_unit(conn)
        assert reports == []

        stored = conn.execute("SELECT * FROM adjacent_reports").fetchall()
        assert len(stored) == 1
        assert stored[0]["unit_name"] is None
        assert stored[0]["sender_name"] == "Bob"


def test_ingest_adjacent_report_filters_by_group_id():
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        in_group = signal_client.ingest_adjacent_report(
            conn, _adjacent_envelope(1, "GROUP_B"), group_id="GROUP_B"
        )
        other_group = signal_client.ingest_adjacent_report(
            conn, _adjacent_envelope(2, "GROUP_OTHER"), group_id="GROUP_B"
        )
    assert in_group is True
    assert other_group is False


def test_ingest_adjacent_report_dedupes_by_signal_timestamp():
    from signal_events import db as db_module

    with db_module.get_connection() as conn:
        first = signal_client.ingest_adjacent_report(
            conn, _adjacent_envelope(1, "GROUP_B"), group_id="GROUP_B"
        )
        second = signal_client.ingest_adjacent_report(
            conn, _adjacent_envelope(1, "GROUP_B"), group_id="GROUP_B"
        )
    assert first is True
    assert second is False


def test_watch_multi_dispatches_envelopes_to_correct_store():
    from signal_events import db as db_module

    envelopes = [
        _group_envelope(1, "INCIDENT_GID", message="3 lastbilar vid grinden"),
        _adjacent_envelope(2, "ADJACENT_GID", filename="Kompani_2_301842_aterkommande.pdf"),
        _group_envelope(3, "OTHER_GID", message="ignored"),
        _group_envelope(4, "SENSOR_GID", message="Rörelselarm utlöst vid västra stängslet"),
    ]

    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(signal_client, "receive", return_value=envelopes):
        counts = list(
            signal_client.watch_multi(
                "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group", max_iterations=1
            )
        )

    assert counts == [(1, 1, 1, 0)]
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        assert len(events) == 2  # one incident report, one sensor event
        # the sensor-group event must be tagged is_sensor (see
        # duplicates.py, which never evaluates those for duplicate status)
        by_body = {e["raw_text"]: e["is_sensor"] for e in events}
        assert by_body["3 lastbilar vid grinden"] == 0
        assert by_body["Rörelselarm utlöst vid västra stängslet"] == 1
        adjacent = db_module.list_latest_adjacent_reports_per_unit(conn)
        assert len(adjacent) == 1
        assert adjacent[0]["unit_name"] == "Kompani_2"


def test_watch_multi_dispatches_tak_bridge_group_and_tags_is_tak_bridge():
    """The TAK-bridge group is a 4th leg alongside incident/adjacent/
    sensor -- a message there is ingested as a normal event (same
    parser.parse_event_fields path as any Signal report), tagged
    is_tak_bridge for provenance/display, but -- unlike is_sensor --
    NOT exempted from duplicates.py: a report relayed from an ATAK
    operator is a one-off human observation, not a repeating automated
    trigger, so it must stay eligible for duplicate detection like any
    other incident report."""
    from signal_events import db as db_module

    envelopes = [_group_envelope(1, "TAK_GID", message="Person siktad vid östra grinden")]
    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(signal_client, "receive", return_value=envelopes):
        counts = list(signal_client.watch_multi(
            "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group", max_iterations=1,
        ))

    assert counts == [(0, 0, 0, 1)]
    with db_module.get_connection() as conn:
        events = db_module.list_events(conn)
        assert len(events) == 1
        assert events[0]["is_tak_bridge"] == 1
        assert events[0]["is_sensor"] == 0  # not exempted from duplicates.py, unlike a sensor event


def test_watch_multi_records_receive_status_on_success():
    from signal_events import db as db_module

    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(signal_client, "receive", return_value=[]):
        list(signal_client.watch_multi(
            "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group", max_iterations=1,
        ))

    with db_module.get_connection() as conn:
        assert db_module.get_last_receive_attempt(conn) is not None
        assert db_module.get_last_receive_success(conn) is not None
        assert db_module.get_last_receive_error(conn) is None


def test_watch_multi_survives_a_failing_cycle_and_keeps_polling():
    """The bug this fixes: previously any exception during a poll cycle
    (a transient network blip, signal-cli briefly failing) propagated
    straight out of the generator and killed the whole watch loop
    permanently -- a single bad cycle meant no more ingestion until a
    human noticed and manually restarted the process. Now it's recorded
    (db.record_receive_attempt) and logged once (not once per retry --
    see the edge-triggered system_log assertions below), and polling
    continues."""
    from signal_events import db as db_module

    envelopes = [_group_envelope(1, "INCIDENT_GID", message="Fordon vid grinden")]
    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(
        signal_client, "receive",
        side_effect=[signal_client.SignalCliError("network unreachable"), envelopes],
    ):
        counts = list(signal_client.watch_multi(
            "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group",
            max_iterations=2, retry_delay_seconds=0,
        ))

    # First cycle failed (no exception raised to the caller, just an
    # empty result); second cycle succeeded and ingested normally.
    assert counts == [(0, 0, 0, 0), (1, 0, 0, 0)]

    with db_module.get_connection() as conn:
        assert db_module.get_last_receive_error(conn) is None  # cleared by the recovery
        assert db_module.get_last_receive_success(conn) is not None
        log_entries = db_module.list_system_log(conn)
        error_entries = [e for e in log_entries if e["event_type"] == "watch_error"]
        recovered_entries = [e for e in log_entries if e["event_type"] == "watch_recovered"]
        assert len(error_entries) == 1
        assert "network unreachable" in error_entries[0]["detail"]
        assert len(recovered_entries) == 1


def test_watch_multi_logs_a_self_sent_message_in_the_incident_group():
    """Regression test for the "signal receive seems to hang" report: a
    human testing ingestion by messaging the incident group from the same
    phone/account signal-cli is linked to sees nothing happen, with no
    explanation anywhere -- Signal delivers that as a syncMessage, which
    ingest_envelope correctly never ingests (it's not a real incoming
    report), but that silence looked identical to a broken receive path.
    Now it's called out by name in the system log instead."""
    from signal_events import db as db_module

    envelopes = [_own_sync_envelope(1, "INCIDENT_GID", message="test från min egen telefon")]
    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(signal_client, "receive", return_value=envelopes):
        counts = list(signal_client.watch_multi(
            "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group", max_iterations=1,
        ))

    assert counts == [(0, 0, 0, 0)]  # never ingested as a normal event
    with db_module.get_connection() as conn:
        assert db_module.list_events(conn) == []
        log_entries = db_module.list_system_log(conn)
        skipped = [e for e in log_entries if e["event_type"] == "watch_self_message_skipped"]
        assert len(skipped) == 1


def test_watch_multi_does_not_log_self_sent_messages_in_other_groups():
    """The adjacent/sensor groups legitimately carry this app's own
    outgoing traffic (reports/recurring lists sent via send_to_group_by_
    name) -- logging every echo of those would be pure noise, so only the
    incident group (which this app never sends to) gets called out."""
    from signal_events import db as db_module

    envelopes = [_own_sync_envelope(1, "ADJACENT_GID", message="ett skickat rapport-eko")]
    with patch.object(
        signal_client, "find_group_id_by_name",
        side_effect=["INCIDENT_GID", "ADJACENT_GID", "SENSOR_GID", "TAK_GID"],
    ), patch.object(signal_client, "receive", return_value=envelopes):
        list(signal_client.watch_multi(
            "Incident Group", "Adjacent Group", "Sensor Group", "TAK Group", max_iterations=1,
        ))

    with db_module.get_connection() as conn:
        log_entries = db_module.list_system_log(conn)
        skipped = [e for e in log_entries if e["event_type"] == "watch_self_message_skipped"]
        assert len(skipped) == 0


def test_send_to_group_by_name_resolves_then_sends():
    with patch.object(signal_client, "find_group_id_by_name", return_value="GROUP_A") as mock_find, \
         patch.object(signal_client, "send_message") as mock_send:
        signal_client.send_to_group_by_name(
            "Stabsassistent test-rapport", message="Hej", attachment_paths=["/tmp/x.pdf"]
        )

    mock_find.assert_called_once_with("Stabsassistent test-rapport")
    mock_send.assert_called_once_with(
        "GROUP_A", message="Hej", attachment_paths=["/tmp/x.pdf"], timeout_seconds=60
    )
