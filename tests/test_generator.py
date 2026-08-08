from signal_events import analysis
from signal_events.reports import generator


def _fake_summary():
    threat = analysis.ThreatAssessment(level="yellow", score=6, reasons=["some reason"])
    vehicle = analysis.RecurrenceGroup(
        label="Reg.nr ABC123", kind="plate", object_type=None,
        events=[analysis.EventRef(1, "10:00", "Norra grinden", "2026-01-01T10:00:00")],
        distinct_places={"Norra grinden"}, suspicious_hits=0, score=3,
        reasons=["Återkommer 2 gånger (registreringsnummer)"],
    )
    person = analysis.RecurrenceGroup(
        label="Civil: grön jacka", kind="description", object_type="civil",
        events=[analysis.EventRef(2, "11:00", "Huvudinfarten", "2026-01-01T11:00:00")],
        distinct_places={"Huvudinfarten"}, suspicious_hits=1, score=5,
        reasons=["Återkommer 3 gånger (liknande beskrivning)"],
    )
    return analysis.Summary(
        total_events=10, period_label="7d", vehicle_groups=[vehicle],
        person_groups=[person], other_groups=[], threat=threat,
    )


def _fake_watchlist_entries():
    from signal_events import db

    with db.get_connection() as conn:
        vehicle_id = db.insert_entity(
            conn, entity_type="vehicle", label="Fordon 1", registration="ABC123",
            attributes={"Registration": "ABC 123", "Colour": "Svart"}, source="auto",
        )
        person_id = db.insert_entity(
            conn, entity_type="person", label="Civil, grön jacka",
            attributes={"Colour": "grön jacka"}, source="manual",
        )
        db.update_entity(conn, person_id, {"watchlist": True})
        vehicle = conn.execute(
            "SELECT *, 2 AS event_count FROM entities WHERE id = ?", (vehicle_id,)
        ).fetchone()
        person = conn.execute(
            "SELECT *, 1 AS event_count FROM entities WHERE id = ?", (person_id,)
        ).fetchone()

    event_row = {"id": 1, "created_at": "2026-01-01T10:00:00", "event_time": "10:00", "place": "Norra grinden"}
    return [
        {"entity": vehicle, "events": [event_row]},
        {"entity": person, "events": []},
    ]


def test_render_watchlist_markdown_includes_type_sections():
    text = generator.render_watchlist_markdown(_fake_watchlist_entries(), site_name="Kvarn")

    assert "Fordon" in text
    assert "Personer" in text
    assert "Objekt" in text
    assert "ABC123" in text
    assert "grön jacka" in text
    assert "Händelse 011000" in text  # event 1's TNR, derived from its created_at
    assert "Kvarn" in text


def test_render_watchlist_markdown_flags_manually_watchlisted_entries():
    text = generator.render_watchlist_markdown(_fake_watchlist_entries(), site_name="Kvarn")
    assert "bevakas manuellt" in text


def test_render_watchlist_markdown_empty_list_says_none_identified():
    text = generator.render_watchlist_markdown([], site_name="Kvarn")
    assert text.count("Inga identifierade.") == 3


def test_render_watchlist_pdf_produces_valid_pdf_bytes():
    buf = generator.render_watchlist_pdf(_fake_watchlist_entries(), site_name="Kvarn")
    data = buf.read()
    assert data.startswith(b"%PDF")
    assert len(data) > 100


def _fake_rows():
    return [{
        "event": {
            "event_time": "14:30", "place": "Norra grinden", "count": "1",
            "object": "Personbil", "activity": "Passerade grinden",
            "marks": "Röd Volvo", "reported_by": "Vakt Andersson",
            "next_steps": "Ingen åtgärd", "raw_text": "Fritext om händelsen",
        },
        "attachments": [],
    }]


def _has_no_markdown_headers_or_bold(text: str) -> bool:
    # What must be absent is Markdown header syntax (a line starting
    # with "#") and "**bold**".
    return "**" not in text and not any(
        line.startswith("#") for line in text.splitlines()
    )


def test_render_text_contains_event_fields_without_markdown_syntax():
    text = generator.render_text(_fake_rows(), since_label="7d")

    assert "HÄNDELSERAPPORT" in text
    assert "Norra grinden" in text
    assert "Personbil" in text
    assert "Fritext om händelsen" in text
    assert _has_no_markdown_headers_or_bold(text)


def test_render_summary_text_contains_threat_and_groups_without_markdown_syntax():
    text = generator.render_summary_text(_fake_summary(), site_name="Kvarn")

    assert "SAMMANSTÄLLD HOTBEDÖMNING" in text
    assert "Kvarn" in text
    assert "HOTNIVÅ" in text
    assert "some reason" in text
    assert "Reg.nr ABC123" in text
    assert "grön jacka" in text
    assert "Händelse 011000" in text  # event 1's TNR, derived from its created_at
    assert _has_no_markdown_headers_or_bold(text)


def test_render_summary_text_includes_narrative_when_given():
    text = generator.render_summary_text(
        _fake_summary(), site_name="Kvarn", narrative="AI-genererad löptext här."
    )
    assert "AI-genererad löptext här." in text
    assert "AI-SAMMANFATTNING" in text
