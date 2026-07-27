from signal_events.parser import parse_event_fields


def test_parses_time_place_count_object():
    text = "At 14:30 near the old bridge, 3 trucks seen parked, camo painted."
    fields = parse_event_fields(text, reported_by="Alice")

    assert fields["event_time"] == "14:30"
    assert fields["place"] == "the old bridge" or "bridge" in fields["place"].lower()
    assert fields["count"] == "3"
    assert fields["object"] == "trucks"
    assert fields["reported_by"] == "Alice"
    assert fields["needs_review"] is True


def test_parses_activity_marks_and_next_steps():
    text = (
        "Two people walking along the fence line, wearing high-vis vests. "
        "Recommend continued monitoring of the area."
    )
    fields = parse_event_fields(text)

    assert fields["activity"] is not None and "walking" in fields["activity"]
    assert fields["marks"] is not None and "vest" in fields["marks"].lower()
    assert fields["next_steps"] is not None and "monitoring" in fields["next_steps"].lower()


def test_handles_empty_text_gracefully():
    fields = parse_event_fields("")

    assert fields["event_time"] is None
    assert fields["place"] is None
    assert fields["count"] is None
    assert fields["object"] is None
    assert fields["raw_text"] == ""
    assert fields["needs_review"] is True


def test_ignores_time_like_numbers_as_counts():
    fields = parse_event_fields("Nothing unusual seen today.")
    assert fields["count"] is None


def test_parses_7s_report_format():
    text = (
        "7S RAPPORT\n"
        "Till: RJ\n"
        "Från: VAKT\n"
        "TNR: 300634\n"
        "Stund: 300631\n"
        "Ställe: 33VWE 18190 99510, parkering V Kvarn\n"
        "Styrka: 1\n"
        "Slag: Personbil\n"
        "Sysselsättning: KRN482 återkommer kort, parkerar ej.\n"
        "Symbol: Silver Volvo kombi\n"
        "Reg.Nr: KRN482\n"
        "Sagesman: CHARLIE\n"
        "Sedan: Kör mot huvudvägen\n"
    )
    fields = parse_event_fields(text)

    assert fields["event_time"] == "300631"
    assert fields["place"] == "33VWE 18190 99510, parkering V Kvarn"
    assert fields["count"] == "1"
    assert fields["object"] == "Personbil"
    assert fields["activity"] == "KRN482 återkommer kort, parkerar ej."
    assert fields["marks"] == "Silver Volvo kombi, Reg.nr KRN482"
    assert fields["reported_by"] == "VAKT"
    assert fields["next_steps"] == "Kör mot huvudvägen"
    assert fields["needs_review"] is True


def test_7s_format_not_detected_on_generic_text_with_one_label():
    text = "Sagesman said something about the weather today."
    fields = parse_event_fields(text, reported_by="Alice")
    assert fields["reported_by"] == "Alice"


def test_extracts_ddhhmm_time_format_in_free_text():
    fields = parse_event_fields("TNR 300631 vid bron, 3 lastbilar sedda.")
    assert fields["event_time"] == "300631"
    assert fields["count"] == "3"
    assert fields["object"] == "lastbilar"


def test_ddhhmm_not_mistaken_for_a_count():
    # Without the guard, "300631" immediately followed by a noun (not a
    # stopword like "vid") would be wrongly captured as (count, object).
    fields = parse_event_fields("300631 Personbil observerad nara grinden.")
    assert fields["event_time"] == "300631"
    assert fields["count"] is None
    assert fields["object"] is None


def test_invalid_ddhhmm_values_are_not_matched_as_time():
    # day 99 / hour 88 are not valid DDHHMM values.
    fields = parse_event_fields("Serienummer 998877 noterades.")
    assert fields["event_time"] is None


def test_ddhhmm_does_not_match_inside_a_longer_number():
    fields = parse_event_fields("Referens 1300631999 noterad.")
    assert fields["event_time"] is None


def test_parses_swedish_place_from_free_text():
    text = "Kl 14:30 sågs en person vid Grinden, ingen vidare aktivitet."
    fields = parse_event_fields(text)

    assert fields["place"] is not None
    assert "grinden" in fields["place"].lower()


def test_parses_swedish_activity_from_free_text():
    text = "Vid skogsbrynet rör sig en person med ryggsäck."
    fields = parse_event_fields(text)

    assert fields["activity"] is not None
    assert "rör sig" in fields["activity"].lower()


def test_parses_swedish_marks_from_free_text():
    text = "En person klädd i kamouflage sågs vid stigen."
    fields = parse_event_fields(text)

    assert fields["marks"] is not None
    assert "klädd i" in fields["marks"].lower()


def test_parses_swedish_next_steps_from_free_text():
    text = "Rekommenderar fortsatt övervakning av området."
    fields = parse_event_fields(text)

    assert fields["next_steps"] is not None
    assert "rekommenderar" in fields["next_steps"].lower()


def test_parses_swedish_activity_marks_and_next_steps_together():
    text = (
        "Två personer patrullerar längs stängslet, klädd i kamouflage. "
        "Rekommenderar fortsatt bevakning av området."
    )
    fields = parse_event_fields(text)

    assert fields["activity"] is not None and "patrullerar" in fields["activity"].lower()
    assert fields["marks"] is not None and "klädd i" in fields["marks"].lower()
    assert fields["next_steps"] is not None and "rekommenderar" in fields["next_steps"].lower()
