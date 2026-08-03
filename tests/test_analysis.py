from signal_events import analysis, db


def _add_event(conn, ts, **fields):
    message_id = db.insert_message(
        conn, signal_timestamp=ts, sender_number=None, sender_name=fields.get("reported_by"),
        body=fields.get("raw_text", ""), raw_json="{}",
    )
    return db.insert_event(conn, message_id=message_id, fields=fields)


def test_extract_plate_ignores_blank_field_followed_by_next_label():
    # Regression: "Reg.Nr: \nSagesman: ALFA" must not extract "SAGESMAN" as
    # a plate -- the blank value should mean no plate found at all.
    row = {
        "marks": None,
        "raw_text": "Reg.Nr: \nSagesman: ALFA\nSedan: Kör mot huvudvägen",
    }
    assert analysis._extract_plate(row) is None


def test_extract_plate_handles_longer_plate_values():
    row = {"marks": "Reg.Nr: TEST-REG-01", "raw_text": None}
    assert analysis._extract_plate(row) == "TESTREG01"


def test_groups_recurring_vehicle_by_plate():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Norra grinden", object="Personbil",
                    marks="Silver Volvo, Reg.nr KRN482", raw_text="")
        _add_event(conn, 2, place="Södra vägen", object="Personbil",
                    marks="Silver Volvo, Reg.nr KRN482", raw_text="")
        _add_event(conn, 3, place="Parkeringen", object="Personbil",
                    marks="Annan bil, Reg.nr XYZ999", raw_text="")

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert len(summary.vehicle_groups) == 1
    group = summary.vehicle_groups[0]
    assert group.kind == "plate"
    assert group.count == 2
    assert group.distinct_places == {"Norra grinden", "Södra vägen"}


def test_groups_recurring_person_by_description_similarity():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Skogsbrynet", object="Civil",
                    marks="man i grön jacka och keps", raw_text="")
        _add_event(conn, 2, place="Huvudinfarten", object="Civil",
                    marks="man med grön jacka, bär keps", raw_text="")
        _add_event(conn, 3, place="Vägskälet", object="Civil",
                    marks="kvinna i röd klänning", raw_text="")

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert len(summary.person_groups) == 1
    group = summary.person_groups[0]
    assert group.kind == "description"
    assert group.count == 2


def test_no_recurrence_stays_green_with_too_few_events():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Civil", marks="man i blå jacka")
        _add_event(conn, 2, place="B", object="Personbil", marks="röd bil")

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.level == "green"
    assert summary.threat.score == 0
    assert "för få" in summary.threat.reasons[0].lower()


def test_apply_threat_override_replaces_level_but_keeps_automatic_reasons():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Civil", marks="man i blå jacka")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.level == "green"
    original_reasons = list(summary.threat.reasons)

    overridden = analysis.apply_threat_override(
        summary, {"level": "red", "notes": "Bekräftad av chefvakt"}
    )

    assert overridden.threat.level == "red"
    assert overridden.threat.score == summary.threat.score  # score itself is untouched
    assert "Manuellt satt till RÖD" in overridden.threat.reasons[0]
    assert "GRÖN" in overridden.threat.reasons[0]  # names the automatic level too
    assert "Bekräftad av chefvakt" in overridden.threat.reasons[0]
    assert overridden.threat.reasons[1:] == original_reasons  # automatic reasons preserved


def test_apply_threat_override_with_none_returns_summary_unchanged():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Civil", marks="man i blå jacka")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert analysis.apply_threat_override(summary, None) is summary


def test_no_recurrence_with_enough_events_is_still_green():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Civil", marks="man i blå jacka")
        _add_event(conn, 2, place="B", object="Personbil", marks="röd bil")
        _add_event(conn, 3, place="C", object="Luftfarkost", marks="drönare")

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.level == "green"
    assert summary.vehicle_groups == []
    assert summary.person_groups == []
    assert summary.other_groups == []


def test_threat_escalates_with_suspicious_activity_and_multiple_places():
    with db.get_connection() as conn:
        for i, place in enumerate(["Norra grinden", "Södra vägen", "Västra stängslet"], start=1):
            _add_event(
                conn, i, place=place, object="Personbil",
                marks="Silver Volvo, Reg.nr KRN482",
                activity="Fotograferar stängslet och tittar mot vakttornet",
                next_steps="Återkommer om en timme",
            )

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert len(summary.vehicle_groups) == 1
    group = summary.vehicle_groups[0]
    assert group.count == 3
    assert len(group.distinct_places) == 3
    assert group.suspicious_hits == 3
    # High recurrence/pattern score alone is capped at yellow -- no weapons
    # or explosives were reported, so red must not be reachable here.
    assert summary.threat.level == "yellow"
    assert summary.threat.score > 0


def test_pattern_score_alone_never_reaches_red_without_severe_indicators():
    with db.get_connection() as conn:
        for i, place in enumerate(
            ["Norra grinden", "Södra vägen", "Västra stängslet", "Östra vägen", "Skogsbrynet"],
            start=1,
        ):
            _add_event(
                conn, i, place=place, object="Personbil",
                marks="Silver Volvo, Reg.nr KRN482",
                activity="Fotograferar och filmar stängslet, tittar mot vakttornet",
                next_steps="Återkommer och cirklar området, avvaktar i bilen",
            )
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.level == "yellow"
    assert summary.threat.armed_sightings == 0
    assert summary.threat.explosive_sightings == 0


def test_single_armed_sighting_elevates_to_yellow_not_red():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Norra grinden", object="Civil",
                    marks="man i grön jacka, verkar beväpnad med pistol")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.armed_sightings == 1
    assert summary.threat.level == "yellow"


def test_multiple_armed_sightings_reach_red():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Norra grinden", object="Civil",
                    marks="man beväpnad med gevär")
        _add_event(conn, 2, place="Södra vägen", object="Civil",
                    marks="samma man, beväpnad, gevär synligt")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.armed_sightings == 2
    assert summary.threat.level == "red"
    assert any("Beväpnad" in reason for reason in summary.threat.reasons)


def test_severe_indicator_reasons_identify_events_by_tnr_not_database_id():
    with db.get_connection() as conn:
        event_id = _add_event(conn, 1, place="Norra grinden", object="Civil",
                               event_time="221430", marks="man beväpnad med gevär")
        # TNR is when the app received the report (created_at), not the
        # event's own event_time -- pin it down for a deterministic assertion.
        conn.execute(
            "UPDATE events SET created_at = '2026-07-22T14:30:00+00:00' WHERE id = ?", (event_id,)
        )
        summary = analysis.build_summary(db.list_events(conn), "all")

    reason = next(r for r in summary.threat.reasons if "Beväpnad" in r)
    assert "Händelse 221430" in reason
    assert "#" not in reason


def test_single_explosive_discovery_elevates_to_yellow_not_red():
    # Recalibrated: RED requires a *recurring* severe indicator, so a single
    # unrepeated explosive report is serious enough for YELLOW but not RED.
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Västra stängslet", object="Föremål",
                    marks="Misstänkt sprängladdning upptäckt vid stängslet")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.explosive_sightings == 1
    assert summary.threat.level == "yellow"
    assert any("sprängladdning" in reason.lower() for reason in summary.threat.reasons)


def test_recurring_explosive_discovery_reaches_red():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Västra stängslet", object="Föremål",
                    marks="Misstänkt sprängladdning upptäckt vid stängslet")
        _add_event(conn, 2, place="Östra stängslet", object="Föremål",
                    marks="Ytterligare en misstänkt sprängladdning påträffad")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.explosive_sightings == 2
    assert summary.threat.level == "red"


def test_single_sabotage_sign_elevates_to_yellow_not_red():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Västra stängslet", object="Stängsel",
                    marks="Tecken på sabotage, klippt stängsel upptäckt")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.sabotage_sightings == 1
    assert summary.threat.level == "yellow"


def test_recurring_sabotage_signs_reach_red():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Västra stängslet", object="Stängsel",
                    marks="Klippt stängsel, tecken på sabotage")
        _add_event(conn, 2, place="Östra grinden", object="Lås",
                    marks="Uppbrutet lås, misstänkt sabotageförsök")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.sabotage_sightings == 2
    assert summary.threat.level == "red"
    assert any("sabotageförsök" in reason.lower() for reason in summary.threat.reasons)


def test_classifies_other_object_types_separately():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Luftfarkost", marks="Lågflygande drönare")
        _add_event(conn, 2, place="B", object="Luftfarkost", marks="Lågflygande drönare")

        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.vehicle_groups == []
    assert summary.person_groups == []
    assert len(summary.other_groups) == 1


def test_single_threat_of_violence_appears_as_notable_observation():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Huvudentrén", object="Person",
                    marks="Person uttalade hot om våld mot personalen")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert len(summary.other_groups) == 1
    group = summary.other_groups[0]
    assert group.kind == "notable"
    assert "Hot om våld" in group.label
    assert group.count == 1
    assert group.score > 0


def test_single_armed_sighting_appears_as_notable_observation_too():
    """The top-level motivering already tallies armed_sightings, but a
    single sighting should also be visible directly in "Övriga
    anmärkningsvärda observationer" with a link to the event."""
    with db.get_connection() as conn:
        event_id = _add_event(conn, 1, place="Norra grinden", object="Civil",
                               marks="man i grön jacka, verkar beväpnad med pistol")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert len(summary.other_groups) == 1
    group = summary.other_groups[0]
    assert group.kind == "notable"
    assert "Beväpnad" in group.label
    assert group.events[0].id == event_id


def test_notable_observation_not_duplicated_when_event_already_in_a_recurring_group():
    with db.get_connection() as conn:
        _add_event(conn, 1, place="Skogsbrynet", object="Civil",
                    marks="man i grön jacka och keps, verkar beväpnad")
        _add_event(conn, 2, place="Huvudinfarten", object="Civil",
                    marks="man med grön jacka, bär keps, beväpnad")

        summary = analysis.build_summary(db.list_events(conn), "all")

    # The two events recur as a "person" group -- they must not *also*
    # show up as separate single-occurrence "notable" entries.
    assert len(summary.person_groups) == 1
    assert summary.person_groups[0].count == 2
    assert all(g.kind != "notable" for g in summary.other_groups)


def test_notable_observations_do_not_affect_red_gating():
    """A single threat mention nudges the pattern score but must never,
    by itself, be enough to reach red -- red still requires a *recurring*
    armed/explosive/sabotage indicator specifically."""
    with db.get_connection() as conn:
        _add_event(conn, 1, place="A", object="Person", marks="hot om våld uttalat")
        _add_event(conn, 2, place="B", object="Person", marks="dödshot mottaget via telefon")
        summary = analysis.build_summary(db.list_events(conn), "all")

    assert summary.threat.level != "red"
    assert summary.threat.armed_sightings == 0
    assert summary.threat.explosive_sightings == 0
    assert summary.threat.sabotage_sightings == 0


def test_parse_adjacent_level_prefers_an_explicit_bedomning_line():
    body = "Läget lugnt i vårt område.\nBedömning: RÖD -- återkommande allvarlig indikation."
    assert analysis.parse_adjacent_level(body) == "red"


def test_parse_adjacent_level_falls_back_to_most_severe_keyword_anywhere():
    """No line literally starts with "Bedömning", but the text still
    mentions a level -- and if it mentions more than one (e.g. describing
    a change over time), the more severe one should win."""
    body = "Läget har eskalerat från GRÖN till GUL under natten."
    assert analysis.parse_adjacent_level(body) == "yellow"


def test_parse_adjacent_level_returns_none_when_nothing_matches():
    assert analysis.parse_adjacent_level("Inget särskilt att rapportera idag.") is None
    assert analysis.parse_adjacent_level("") is None
    assert analysis.parse_adjacent_level(None) is None
