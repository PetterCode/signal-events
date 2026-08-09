from signal_events import db, importer


def test_split_on_dash_separator():
    text = "First report text.\n---\nSecond report text.\n---\nThird."
    blocks = importer.split_report_blocks(text)
    assert blocks == ["First report text.", "Second report text.", "Third."]


def test_split_on_blank_lines_when_no_dashes():
    text = "First report,\nstill first.\n\nSecond report.\n\n\nThird report."
    blocks = importer.split_report_blocks(text)
    assert blocks == ["First report,\nstill first.", "Second report.", "Third report."]


def test_extract_sender_line():
    block = "From: Alice\n3 trucks near the bridge at 14:30."
    sender, rest = importer.extract_sender_line(block)
    assert sender == "Alice"
    assert rest == "3 trucks near the bridge at 14:30."


def test_extract_sender_line_reported_by_variant():
    block = "Reported by: Bo\nTwo vans parked at the market."
    sender, rest = importer.extract_sender_line(block)
    assert sender == "Bo"
    assert rest == "Two vans parked at the market."


def test_extract_sender_line_swedish_variants():
    sender, rest = importer.extract_sender_line("Från: Alice\nTre lastbilar vid bron.")
    assert sender == "Alice"
    assert rest == "Tre lastbilar vid bron."

    sender, rest = importer.extract_sender_line("Rapporterad av: Bo\nTvå bilar vid torget.")
    assert sender == "Bo"
    assert rest == "Två bilar vid torget."


def test_extract_sender_line_absent():
    block = "No sender line here, just the report."
    sender, rest = importer.extract_sender_line(block)
    assert sender is None
    assert rest == block


def test_import_text_creates_events():
    text = (
        "From: Alice\n"
        "At 14:30 near the old bridge, 3 trucks seen parked.\n"
        "---\n"
        "2 people walking near the market, wearing high-vis vests."
    )
    with db.get_connection() as conn:
        event_ids = importer.import_text(conn, text, filename="notes.txt")
        assert len(event_ids) == 2

    with db.get_connection() as conn:
        events = db.list_events(conn)
        assert len(events) == 2
        by_reporter = {e["reported_by"] for e in events}
        assert "Alice" in by_reporter
        assert all(e["needs_review"] == 1 for e in events)


def test_import_text_captures_a_recurring_freeform_person_across_two_7s_reports():
    """Regression: file-imported 7S-format reports typed up outside the
    app's own kännetecken composer describe a person in plain prose in
    the "Symbol" line, not the "Person N (...)" block format -- before
    entities.py's freeform fallback, that meant a recurring person could
    be described identically in a dozen imported reports and never once
    show up on the bevakningslista, since no person entity was ever
    created at all."""
    text = (
        "Till: Stabsassistent\n"
        "Från: Vakt A\n"
        "TNR: 010100\n"
        "Stund: 010100\n"
        "Ställe: Norra grinden\n"
        "Styrka: 1\n"
        "Slag: Civil\n"
        "Sysselsättning: Stod och tittade mot stängslet en stund\n"
        "Symbol: Man i mörka kläder, ca 30 år, kort mörkt hår\n"
        "Sagesman: Vakt A\n"
        "Sedan: Fortsatt bevakning\n"
        "---\n"
        "Till: Stabsassistent\n"
        "Från: Vakt B\n"
        "TNR: 020200\n"
        "Stund: 020200\n"
        "Ställe: Södra grinden\n"
        "Styrka: 1\n"
        "Slag: Civil\n"
        "Sysselsättning: Gick fram och tillbaka utanför stängslet\n"
        "Symbol: Man i mörka kläder, ca 30 år, kort mörkt hår, mörk keps\n"
        "Sagesman: Vakt B\n"
        "Sedan: Noterat i vaktloggen"
    )
    with db.get_connection() as conn:
        event_ids = importer.import_text(conn, text, filename="historik.txt")
        assert len(event_ids) == 2

        persons = db.list_entities(conn, entity_type="person")
        assert len(persons) == 1

        watchlist = db.list_watchlist_entities(conn)
        assert len(watchlist) == 1
        assert watchlist[0]["event_count"] == 2
