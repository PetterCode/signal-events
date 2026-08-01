import pytest

from signal_events.parser import extract_count_and_object, parse_event_fields


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
    # "Sagesman" (the actual informant) wins over "Från" (who relayed the
    # Signal message) -- see test_7s_sagesman_field_takes_priority_over_from
    # and its neighbours below for the full priority chain.
    assert fields["reported_by"] == "CHARLIE"
    assert fields["next_steps"] == "Kör mot huvudvägen"
    assert fields["needs_review"] is True


def test_7s_sagesman_field_takes_priority_over_from_and_the_signal_sender():
    """"Sagesman" names who actually observed and is vouching for the
    report -- which can genuinely differ from "Från" (e.g. a duty officer
    relaying someone else's account) and from the Signal sender (whoever's
    account it was sent from) -- so it should win over both when present."""
    text = (
        "Till: RJ\nFrån: Vakt Andersson\nTNR: 300634\nStund: 300631\n"
        "Ställe: Östra grinden\nStyrka: 1\nSlag: Person\n"
        "Sysselsättning: Observerade stängslet.\nSagesman: Vakt Lindqvist\n"
        "Sedan: Fortsatt bevakning\n"
    )
    fields = parse_event_fields(text, reported_by="Signal-avsändare")
    assert fields["reported_by"] == "Vakt Lindqvist"


def test_7s_falls_back_to_fran_when_sagesman_is_missing():
    text = (
        "Till: RJ\nFrån: Vakt Andersson\nTNR: 300634\nStund: 300631\n"
        "Ställe: Östra grinden\nStyrka: 1\nSlag: Person\n"
        "Sysselsättning: Observerade stängslet.\nSedan: Fortsatt bevakning\n"
    )
    fields = parse_event_fields(text, reported_by="Signal-avsändare")
    assert fields["reported_by"] == "Vakt Andersson"


def test_7s_falls_back_to_the_signal_sender_when_neither_field_is_present():
    text = (
        "Till: RJ\nTNR: 300634\nStund: 300631\n"
        "Ställe: Östra grinden\nStyrka: 1\nSlag: Person\n"
        "Sysselsättning: Observerade stängslet.\nSedan: Fortsatt bevakning\n"
    )
    fields = parse_event_fields(text, reported_by="Signal-avsändare")
    assert fields["reported_by"] == "Signal-avsändare"


def test_7s_report_generated_by_7srapport_com_parses_correctly():
    """Real-world interop check: 7srapport.com (a third-party field-report
    tool, not part of this project) generates 7S-labeled reports that
    combine Styrka/Slag/Sysselsättning into one free-text "Händelse" field
    instead of three separate lines, and emits a lone "-" for its blank
    optional "Sedan" field -- this is the literal text a real generated
    report looks like, confirmed by actually running the tool. Both
    quirks must be handled, not just the textbook 7S template."""
    text = (
        "7S RAPPORT\n"
        "Till: 1A PLUT\n"
        "Från: AQ / 2A GRP\n"
        "TNR: 302242\n"
        "Stund: 302238\n"
        "Ställe: Östra grinden\n"
        "Händelse: 1 grå skåpbil siktad, saktade ner vid grinden innan den körde vidare norrut\n"
        "Symbol: Grå skåpbil, Reg.Nr ABC123\n"
        "Sagesman: AND111. 3E P, 2A GRP\n"
        "Sedan: -\n"
    )
    fields = parse_event_fields(text)

    assert fields["event_time"] == "302238"
    assert fields["place"] == "Östra grinden"
    assert fields["count"] == "1"
    assert fields["object"] == "grå"
    assert fields["activity"] == (
        "1 grå skåpbil siktad, saktade ner vid grinden innan den körde vidare norrut"
    )
    assert fields["marks"] == "Grå skåpbil, Reg.Nr ABC123"
    assert fields["reported_by"] == "AND111. 3E P, 2A GRP"
    assert fields["next_steps"] is None
    assert fields["needs_review"] is True


def test_7s_explicit_styrka_slag_sysselsattning_win_over_a_handelse_field():
    """If a message somehow has both the strict per-field labels and a
    combined "Händelse" line, the explicit fields are the more reliable
    source and should win -- Händelse is only a fallback for when they're
    genuinely absent."""
    text = (
        "Till: RJ\nFrån: VAKT\nTNR: 300634\nStund: 300631\n"
        "Ställe: Östra grinden\nStyrka: 2\nSlag: Person\n"
        "Sysselsättning: Observerade stängslet.\n"
        "Händelse: Detta ska ignoreras eftersom de tre fälten redan finns\n"
        "Sedan: Fortsatt bevakning\n"
    )
    fields = parse_event_fields(text)
    assert fields["count"] == "2"
    assert fields["object"] == "Person"
    assert fields["activity"] == "Observerade stängslet."


def test_7s_handelse_falls_back_to_the_full_text_when_no_activity_keyword_matches():
    """When the combined Händelse text doesn't contain any of the
    activity trigger words extract_activity looks for, the whole sentence
    is kept as `activity` rather than losing it entirely -- a human
    reviewing the event still sees what happened, even if it isn't
    split into separate structured fields."""
    text = (
        "Till: RJ\nFrån: VAKT\nTNR: 300634\nStund: 300631\n"
        "Ställe: Östra grinden\n"
        "Händelse: Ett äldre par plockade svamp nära stängslet\n"
        "Sagesman: VAKT\n"
    )
    fields = parse_event_fields(text)
    assert fields["activity"] == "Ett äldre par plockade svamp nära stängslet"


def test_extract_count_and_object_does_not_truncate_swedish_letters():
    """Regression: [a-z] (ASCII-only) in the count/object regex used to
    cut a Swedish word off at the first å/ä/ö -- "1 grå skåpbil" came back
    as object "gr" instead of "grå", found while checking the fix above
    against a real 7srapport.com report."""
    count, obj = extract_count_and_object("1 grå skåpbil siktad vid grinden")
    assert count == "1"
    assert obj == "grå"


def test_7s_format_not_detected_on_generic_text_with_one_label():
    text = "Sagesman said something about the weather today."
    fields = parse_event_fields(text, reported_by="Alice")
    assert fields["reported_by"] == "Alice"


def test_7s_report_with_mgrs_place_auto_fills_lat_lon():
    text = (
        "Till: RJ\nFrån: VAKT\nTNR: 300634\nStund: 300631\n"
        "Ställe: 33VWE 18190 99510, parkering V Kvarn\nStyrka: 1\n"
        "Slag: Personbil\nSysselsättning: KRN482 återkommer.\n"
        "Sagesman: CHARLIE\nSedan: Kör mot huvudvägen\n"
    )
    fields = parse_event_fields(text)
    assert fields["lat"] == pytest.approx(58.64, abs=0.01)
    assert fields["lon"] == pytest.approx(15.31, abs=0.01)


def test_7s_report_with_decimal_degrees_place_auto_fills_lat_lon():
    """Regression: parse_event_fields must recognize position formats
    beyond MGRS too (coordinates.extract_position), not just the one
    the app itself displays."""
    text = (
        "Till: RJ\nFrån: VAKT\nTNR: 300634\nStund: 300631\n"
        "Ställe: 59.3269, 18.0717\nStyrka: 1\n"
        "Slag: Personbil\nSysselsättning: KRN482 återkommer.\n"
        "Sagesman: CHARLIE\nSedan: Kör mot huvudvägen\n"
    )
    fields = parse_event_fields(text)
    assert fields["lat"] == pytest.approx(59.3269)
    assert fields["lon"] == pytest.approx(18.0717)


def test_generic_free_text_without_an_mgrs_reference_has_no_lat_lon():
    text = "At 14:30 near the old bridge, 3 trucks seen parked, camo painted."
    fields = parse_event_fields(text)
    assert "lat" not in fields
    assert "lon" not in fields


def test_generic_free_text_with_decimal_degrees_auto_fills_lat_lon():
    text = "Fordon parkerat vid 59.3269, 18.0717, ingen aktivitet."
    fields = parse_event_fields(text)
    assert fields["lat"] == pytest.approx(59.3269)
    assert fields["lon"] == pytest.approx(18.0717)


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
