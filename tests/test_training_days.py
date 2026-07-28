"""Regression coverage for the bundled demo/training_days/dag_NN.txt
scenario used by the "Dag 1".."Dag 10" import buttons: every file must
still exist and parse cleanly, and importing them one at a time must
still reproduce the intended escalation (trivial/GREEN -> a couple of
recurring patterns/YELLOW -> a recurring severe indicator/RED), so a
future change to the parser, the analysis thresholds, or the scenario
content itself can't silently break the training story."""

import json
import re
import time
from pathlib import Path

from signal_events import db, importer, analysis

TRAINING_DAYS_DIR = Path(__file__).resolve().parent.parent / "demo" / "training_days"
ADJACENT_STATUS_PATH = TRAINING_DAYS_DIR / "adjacent_status.json"
EXPECTED_REPORTS_PER_DAY = 30


def test_all_ten_day_files_exist_with_30_reports():
    for day in range(1, 11):
        path = TRAINING_DAYS_DIR / f"dag_{day:02d}.txt"
        assert path.exists(), f"missing {path}"
        text = path.read_text(encoding="utf-8")
        blocks = importer.split_report_blocks(text)
        assert len(blocks) == EXPECTED_REPORTS_PER_DAY, (
            f"dag_{day:02d}.txt has {len(blocks)} blocks, expected "
            f"{EXPECTED_REPORTS_PER_DAY}"
        )


def test_importing_each_day_reports_zero_needs_review_failures():
    """Every block should be recognized via the 7S-labeled fast path (not
    fall through to the generic English-keyed heuristics), i.e. every
    field should extract -- this is what lets a trainee mark a whole day
    reviewed quickly instead of fighting the parser."""
    with db.get_connection() as conn:
        for day in range(1, 11):
            text = (TRAINING_DAYS_DIR / f"dag_{day:02d}.txt").read_text(encoding="utf-8")
            ids = importer.import_text(conn, text, filename=f"dag_{day:02d}.txt")
            assert len(ids) == EXPECTED_REPORTS_PER_DAY
            for event_id in ids:
                event = db.get_event(conn, event_id)
                assert event["place"]
                assert event["object"]
                assert event["activity"]


def test_cumulative_import_reproduces_the_escalation_story():
    with db.get_connection() as conn:
        levels = {}
        for day in range(1, 11):
            text = (TRAINING_DAYS_DIR / f"dag_{day:02d}.txt").read_text(encoding="utf-8")
            ids = importer.import_text(conn, text, filename=f"dag_{day:02d}.txt")
            for event_id in ids:
                db.update_event(conn, event_id, {"needs_review": 0})

            events = db.list_events(conn)
            summary = analysis.build_summary(events, period_label="all")
            levels[day] = summary.threat.level

        # Trivial-only opening days: no pattern at all yet.
        assert levels[1] == "green"
        assert levels[2] == "green"
        assert levels[3] == "green"

        # Middle days: recurring van/person patterns and single severe
        # sightings have appeared, but nothing has *recurred* yet, so this
        # stays capped at yellow (never red) the whole way through day 8.
        for day in range(4, 9):
            assert levels[day] == "yellow", f"day {day} was {levels[day]!r}"

        # Day 9's second armed sighting is the recurrence that finally
        # confirms a severe indicator -- from here on it's red.
        assert levels[9] == "red"
        assert levels[10] == "red"


def test_adjacent_status_json_has_both_units_for_every_day():
    data = json.loads(ADJACENT_STATUS_PATH.read_text(encoding="utf-8"))
    for day in range(1, 11):
        entries = data[str(day)]
        unit_names = {e["unit_name"] for e in entries}
        assert unit_names == {"2.Kompani", "3.Kompani"}
        for entry in entries:
            assert entry["body"].strip()


def test_adjacent_units_escalate_one_day_earlier_and_later():
    """2.Kompani mirrors this unit's own escalation shifted one day
    earlier (red on day 8, not day 9); 3.Kompani shifted one day later
    (red only on day 10)."""
    data = json.loads(ADJACENT_STATUS_PATH.read_text(encoding="utf-8"))

    def level_of(unit_name: str, day: int) -> str:
        # Read the explicit "Bedömning: <level>" line rather than
        # searching the whole body -- e.g. the day-7 text legitimately
        # mentions "RÖD" in passing while explaining that a single
        # sighting *isn't* red yet ("kräver upprepning för RÖD").
        body = next(e["body"] for e in data[str(day)] if e["unit_name"] == unit_name)
        match = re.search(r"Bedömning:\s*(?:fortsatt\s+)?(GRÖN|GUL|RÖD)", body)
        assert match, f"no Bedömning line found for {unit_name} day {day}: {body!r}"
        return {"GRÖN": "green", "GUL": "yellow", "RÖD": "red"}[match.group(1)]

    for day in range(1, 8):
        assert level_of("2.Kompani", day) != "red", f"2.Kompani already red on day {day}"
    assert level_of("2.Kompani", 8) == "red"
    assert level_of("2.Kompani", 9) == "red"
    assert level_of("2.Kompani", 10) == "red"

    for day in range(1, 10):
        assert level_of("3.Kompani", day) != "red", f"3.Kompani already red on day {day}"
    assert level_of("3.Kompani", 10) == "red"


def test_import_training_day_inserts_both_adjacent_reports():
    """Mirrors what webapp/routes.py:import_training_day does with the
    JSON file, without needing a Flask test client -- including its exact
    synthetic-timestamp formula, since a naive `-time.time_ns()` would
    silently break the "most recent per unit" ordering below (later real
    inserts get a *more negative*, not less negative, value)."""
    from signal_events.webapp.routes import _SYNTHETIC_TIMESTAMP_OFFSET

    data = json.loads(ADJACENT_STATUS_PATH.read_text(encoding="utf-8"))

    def _insert_day(conn, day: str) -> None:
        for i, entry in enumerate(data[day]):
            db.insert_adjacent_report(
                conn, signal_timestamp=time.time_ns() - _SYNTHETIC_TIMESTAMP_OFFSET - i,
                sender_number=None, sender_name=entry["unit_name"],
                unit_name=entry["unit_name"], body=entry["body"],
            )

    with db.get_connection() as conn:
        # Import days out of chronological order, like a trainee clicking
        # buttons in sequence over real time -- day 8 (2.Kompani red) must
        # still be treated as the *latest* status even though day 1
        # (green) already exists in the table.
        _insert_day(conn, "1")
        time.sleep(0.01)
        _insert_day(conn, "8")

        latest = db.list_latest_adjacent_reports_per_unit(conn)
        by_unit = {row["unit_name"]: row["body"] for row in latest}
        assert set(by_unit) == {"2.Kompani", "3.Kompani"}
        assert "RÖD" in by_unit["2.Kompani"]


def test_all_ten_sensor_files_exist_with_three_static_reports():
    """Sensor events (tripwire/motion detector/camera) are a separate,
    optional file per day -- see generate_training_days.py -- so the
    "Inkludera sensorhändelser" toggle can bring them in independently of
    the human-report story. Deliberately bare-bones: Slag/Symbol/Sedan
    are all left blank and Sysselsättning is always the same generic
    "Sensor aktiverad" line -- place is what identifies the sensor type
    (its text literally says "Trådlarm"/"Rörelsedetektor"/"Kamera")."""
    with db.get_connection() as conn:
        for day in range(1, 11):
            path = TRAINING_DAYS_DIR / f"dag_{day:02d}_sensor.txt"
            assert path.exists(), f"missing {path}"
            text = path.read_text(encoding="utf-8")
            blocks = importer.split_report_blocks(text)
            assert len(blocks) == 3, f"dag_{day:02d}_sensor.txt has {len(blocks)} blocks, expected 3"

            ids = importer.import_text(conn, text, filename=f"dag_{day:02d}_sensor.txt")
            assert len(ids) == 3
            places = []
            for event_id in ids:
                event = db.get_event(conn, event_id)
                assert event["reported_by"] == "Sensorgateway"
                assert event["place"]
                assert event["object"] is None
                assert event["activity"] == "Sensor aktiverad"
                assert event["marks"] is None
                assert event["next_steps"] is None
                places.append(event["place"])
            assert any("Trådlarm" in p for p in places)
            assert any("Rörelsedetektor" in p for p in places)
            assert any("Kamera" in p for p in places)


def test_sensor_tnrs_never_collide_with_that_day_own_human_report_tnrs():
    """TNR is a display label, not a primary key (see naming.py), so a
    collision wouldn't corrupt anything -- but the sensor times are fully
    static/deterministic (no RNG), so there's no reason not to keep them
    distinct from whatever the human-report generator happened to draw
    for that day, and this pins that down as a regression guard."""
    tnr_re = re.compile(r"TNR: (\d{6})")
    for day in range(1, 11):
        human_tnrs = set(tnr_re.findall((TRAINING_DAYS_DIR / f"dag_{day:02d}.txt").read_text(encoding="utf-8")))
        sensor_tnrs = set(tnr_re.findall((TRAINING_DAYS_DIR / f"dag_{day:02d}_sensor.txt").read_text(encoding="utf-8")))
        assert not (human_tnrs & sensor_tnrs), f"day {day}: TNR collision {human_tnrs & sensor_tnrs}"


def test_event_images_json_includes_a_camera_capture_image_for_every_day():
    images_path = TRAINING_DAYS_DIR / "event_images.json"
    data = json.loads(images_path.read_text(encoding="utf-8"))
    camera_images = {"camera_car.png", "camera_person.png", "camera_deer.png"}
    for day in range(1, 11):
        entries = data[str(day)]
        camera_entries = [e for e in entries if e["image"] in camera_images]
        assert len(camera_entries) == 1, f"day {day}: expected exactly one camera image entry"


def test_count_messages_by_import_filename_tracks_repeated_imports():
    with db.get_connection() as conn:
        assert db.count_messages_by_import_filename(conn, "dag_01.txt") == 0

        text = (TRAINING_DAYS_DIR / "dag_01.txt").read_text(encoding="utf-8")
        importer.import_text(conn, text, filename="dag_01.txt")
        assert db.count_messages_by_import_filename(conn, "dag_01.txt") == 30

        importer.import_text(conn, text, filename="dag_01.txt")
        assert db.count_messages_by_import_filename(conn, "dag_01.txt") == 60
        assert db.count_messages_by_import_filename(conn, "dag_02.txt") == 0
