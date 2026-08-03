from datetime import datetime, timezone

from signal_events import naming


def test_sanitize_filename_part_collapses_unsafe_chars():
    assert naming.sanitize_filename_part("Kompani 1", "enhet") == "Kompani_1"
    assert naming.sanitize_filename_part("Å/B\\C:D", "enhet") == "Å_B_C_D"
    assert naming.sanitize_filename_part("  spaced  ", "enhet") == "spaced"


def test_sanitize_filename_part_falls_back_when_empty():
    assert naming.sanitize_filename_part("", "enhet") == "enhet"
    assert naming.sanitize_filename_part("   ", "enhet") == "enhet"
    assert naming.sanitize_filename_part(None, "enhet") == "enhet"


def test_sanitize_filename_part_keeps_safe_swedish_chars():
    assert naming.sanitize_filename_part("Örebro-Enhet_1", "enhet") == "Örebro-Enhet_1"


def test_generate_tnr_is_day_hour_minute():
    dt = datetime(2026, 7, 30, 18, 42, tzinfo=timezone.utc)
    assert naming.generate_tnr(dt) == "301842"


def test_build_report_filename_uses_unit_tnr_and_type():
    dt = datetime(2026, 7, 30, 18, 42, tzinfo=timezone.utc)
    name = naming.build_report_filename("Kompani 1", "hotbedomning", "pdf", tnr=naming.generate_tnr(dt))
    assert name == "Kompani_1_301842_hotbedomning.pdf"


def test_build_report_filename_falls_back_to_enhet_when_unit_name_blank():
    name = naming.build_report_filename("", "handelserapport", "md", tnr="301842")
    assert name == "enhet_301842_handelserapport.md"


def test_build_report_filename_generates_tnr_when_not_given():
    name = naming.build_report_filename("Vakt", "aterkommande", "pdf")
    # Vakt_<6 digits>_aterkommande.pdf
    assert name.startswith("Vakt_")
    assert name.endswith("_aterkommande.pdf")
    tnr_part = name[len("Vakt_"):-len("_aterkommande.pdf")]
    assert len(tnr_part) == 6
    assert tnr_part.isdigit()


def test_parse_report_filename_recovers_unit_tnr_type_ext():
    parsed = naming.parse_report_filename("Kompani_1_301842_hotbedomning.pdf")
    assert parsed == {
        "unit_name": "Kompani_1",
        "tnr": "301842",
        "report_type": "hotbedomning",
        "ext": "pdf",
    }


def test_parse_report_filename_round_trips_with_build():
    built = naming.build_report_filename("Kompani 2", "aterkommande", "pdf", tnr="120530")
    parsed = naming.parse_report_filename(built)
    assert parsed["unit_name"] == "Kompani_2"
    assert parsed["tnr"] == "120530"
    assert parsed["report_type"] == "aterkommande"
    assert parsed["ext"] == "pdf"


def test_parse_report_filename_returns_none_for_unrecognized_name():
    assert naming.parse_report_filename("random_file.pdf") is None
    assert naming.parse_report_filename("") is None
    assert naming.parse_report_filename(None) is None


def test_event_tnr_is_derived_from_created_at_regardless_of_event_time():
    # TNR identifies when the app itself received the report, not when the
    # observation was made -- that's the separately displayed event_time.
    assert naming.event_tnr("2026-07-22T14:30:00+00:00") == "221430"
