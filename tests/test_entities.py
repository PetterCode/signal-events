import json

from signal_events import db, entities


def _make_event(conn, **fields) -> int:
    message_id = db.insert_message(
        conn, signal_timestamp=fields.pop("signal_timestamp", 1),
        sender_number=None, sender_name=None, body="text",
        raw_json=json.dumps({}),
    )
    return db.insert_event(conn, message_id=message_id, fields=fields)


# --- extract_entities (pure parser) ----------------------------------------

def test_extract_entities_parses_a_person_block():
    marks = "Person 1 (A – Age: 30-40, B – Build: Muskulös, C – Colour: Mörk hy)"
    found = entities.extract_entities(marks)
    assert len(found) == 1
    assert found[0].entity_type == "person"
    assert found[0].label == "Person 1"
    assert found[0].attributes == {
        "Age": "30-40", "Build": "Muskulös", "Colour": "Mörk hy",
    }
    assert found[0].registration is None


def test_extract_entities_parses_a_vehicle_block_and_normalizes_the_plate():
    marks = "Fordon 1 (S – Size: Kombi, C – Colour: Svart, R – Registration: ABC 123)"
    found = entities.extract_entities(marks)
    assert len(found) == 1
    assert found[0].entity_type == "vehicle"
    assert found[0].registration == "ABC123"
    assert found[0].attributes["Registration"] == "ABC 123"


def test_extract_entities_parses_multiple_blocks_joined_by_the_composer():
    marks = (
        "Person 1 (A – Age: 30-40); Fordon 1 (R – Registration: ABC123); "
        "Person 2 (B – Build: Smal)"
    )
    found = entities.extract_entities(marks)
    labels = [(e.entity_type, e.label) for e in found]
    assert labels == [("person", "Person 1"), ("vehicle", "Fordon 1"), ("person", "Person 2")]


def test_extract_entities_finds_a_standalone_reg_nr_mention():
    found = entities.extract_entities("Silver Volvo, Reg.nr KRN482, körde västerut")
    assert len(found) == 1
    assert found[0].entity_type == "vehicle"
    assert found[0].registration == "KRN482"


def test_extract_entities_does_not_duplicate_a_plate_already_inside_a_fordon_block():
    marks = "Fordon 1 (R – Registration: ABC123). Reg.Nr: ABC123 syns även i fritext."
    found = entities.extract_entities(marks)
    assert len(found) == 1


def test_extract_entities_checks_raw_text_too():
    found = entities.extract_entities(marks=None, raw_text="Bilen hade Reg.Nr: XYZ999")
    assert len(found) == 1
    assert found[0].registration == "XYZ999"


def test_extract_entities_parses_the_unlettered_identity_fields_on_a_person():
    marks = (
        "Person 1 (Namn: Kalle Karlsson, Alias: Kalles, Nationalitet: Svensk, "
        "Födelsedatum: 1990-01-01, A – Age: 30-40, B – Build: Muskulös)"
    )
    found = entities.extract_entities(marks)
    assert len(found) == 1
    assert found[0].attributes == {
        "Namn": "Kalle Karlsson", "Alias": "Kalles", "Nationalitet": "Svensk",
        "Födelsedatum": "1990-01-01", "Age": "30-40", "Build": "Muskulös",
    }


def test_extract_entities_parses_identity_fields_with_no_ah_fields_present():
    marks = "Person 1 (Namn: Okänd, Alias: Räven)"
    found = entities.extract_entities(marks)
    assert len(found) == 1
    assert found[0].attributes == {"Namn": "Okänd", "Alias": "Räven"}


def test_extract_entities_returns_empty_list_for_blank_input():
    assert entities.extract_entities(None) == []
    assert entities.extract_entities("") == []
    assert entities.extract_entities("Bara vanlig fritext, inga kännetecken.") == []


def test_extract_entities_falls_back_to_a_freeform_person_when_object_reads_as_a_person():
    """Regression: a report typed up outside the app's own kännetecken
    composer (e.g. a file import from another system) never contains a
    "Person N (...)" block, so without this fallback it contributed
    nothing to the entities database at all -- never showing up as
    recurring no matter how many times the same person was described."""
    found = entities.extract_entities(
        marks="Man i mörka kläder, ca 30 år", raw_text=None, object_type="Civil",
    )
    assert len(found) == 1
    assert found[0].entity_type == "person"
    assert found[0].label == "Civil: Man i mörka kläder, ca 30 år"
    assert found[0].match == "similarity"


def test_extract_entities_freeform_person_fallback_skips_non_person_object_types():
    found = entities.extract_entities(marks="Grå skåpbil", raw_text=None, object_type="Bil")
    assert found == []


def test_extract_entities_freeform_person_fallback_does_not_duplicate_a_composer_block():
    marks = "Person 1 (A – Age: 30-40)"
    found = entities.extract_entities(marks, raw_text=None, object_type="Civil")
    assert len(found) == 1
    assert found[0].label == "Person 1"


def test_extract_entities_freeform_person_fallback_with_no_marks_uses_the_object_type_alone():
    found = entities.extract_entities(marks=None, raw_text=None, object_type="Man")
    assert len(found) == 1
    assert found[0].label == "Man"


# --- sync_event_entities -----------------------------------------------------

def test_sync_creates_and_links_an_auto_person_entity():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)

        linked = db.list_entities_for_event(conn, event_id)
        assert len(linked) == 1
        assert linked[0]["entity_type"] == "person"
        assert linked[0]["source"] == "auto"
        assert linked[0]["link_source"] == "auto"


def test_sync_is_idempotent_across_repeated_saves_of_the_same_event():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)
        first_id = db.list_entities_for_event(conn, event_id)[0]["id"]

        entities.sync_event_entities(conn, event_id)
        entities.sync_event_entities(conn, event_id)

        linked = db.list_entities_for_event(conn, event_id)
        assert len(linked) == 1
        assert linked[0]["id"] == first_id


def test_sync_removes_stale_auto_links_when_marks_no_longer_mentions_the_entity():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)
        entity_id = db.list_entities_for_event(conn, event_id)[0]["id"]

        db.update_event(conn, event_id, {"marks": "Ingenting kvar att extrahera"})
        entities.sync_event_entities(conn, event_id)

        assert db.list_entities_for_event(conn, event_id) == []
        # the now-orphaned auto entity is pruned entirely, not just unlinked
        assert db.get_entity(conn, entity_id) is None


def test_sync_matches_the_same_vehicle_across_two_different_events_by_plate():
    with db.get_connection() as conn:
        first_event = _make_event(
            conn, signal_timestamp=1, marks="Fordon 1 (R – Registration: ABC123)"
        )
        entities.sync_event_entities(conn, first_event)
        second_event = _make_event(
            conn, signal_timestamp=2,
            marks="Fordon 1 (S – Size: Kombi, R – Registration: ABC123)",
        )
        entities.sync_event_entities(conn, second_event)

        first_entities = db.list_entities_for_event(conn, first_event)
        second_entities = db.list_entities_for_event(conn, second_event)
        assert len(first_entities) == 1
        assert len(second_entities) == 1
        assert first_entities[0]["id"] == second_entities[0]["id"]

        # attributes merge: the fuller second report's Size isn't lost,
        # and the fuller report doesn't erase what the plate-only first
        # report established either.
        attrs = json.loads(second_entities[0]["attributes"])
        assert attrs["Registration"] == "ABC123"
        assert attrs["Size"] == "Kombi"


def test_sync_does_not_merge_different_persons_across_different_events_with_the_same_label():
    with db.get_connection() as conn:
        first_event = _make_event(conn, signal_timestamp=1, marks="Person 1 (A – Age: 20)")
        entities.sync_event_entities(conn, first_event)
        second_event = _make_event(conn, signal_timestamp=2, marks="Person 1 (A – Age: 60)")
        entities.sync_event_entities(conn, second_event)

        first_id = db.list_entities_for_event(conn, first_event)[0]["id"]
        second_id = db.list_entities_for_event(conn, second_event)[0]["id"]
        assert first_id != second_id


def test_sync_merges_similar_freeform_person_descriptions_across_two_events():
    """The bug this fallback fixes: two file-imported reports, never
    touching the app's kännetecken composer, describing what's plausibly
    the same person -- must land on the same entity so it shows up as
    recurring on the bevakningslista, exactly like a repeated vehicle
    plate already does."""
    with db.get_connection() as conn:
        first_event = _make_event(
            conn, signal_timestamp=1, object="Civil",
            marks="Man i mörka kläder, ca 30 år, kort mörkt hår",
        )
        entities.sync_event_entities(conn, first_event)
        second_event = _make_event(
            conn, signal_timestamp=2, object="Civil",
            marks="Man i mörka kläder, ca 30 år, kort mörkt hår, mörk keps",
        )
        entities.sync_event_entities(conn, second_event)

        first_id = db.list_entities_for_event(conn, first_event)[0]["id"]
        second_id = db.list_entities_for_event(conn, second_event)[0]["id"]
        assert first_id == second_id

        watchlist = db.list_watchlist_entities(conn)
        assert len(watchlist) == 1
        assert watchlist[0]["event_count"] == 2


def test_sync_does_not_merge_dissimilar_freeform_person_descriptions():
    with db.get_connection() as conn:
        first_event = _make_event(
            conn, signal_timestamp=1, object="Civil", marks="Man i mörka kläder, ca 30 år",
        )
        entities.sync_event_entities(conn, first_event)
        second_event = _make_event(
            conn, signal_timestamp=2, object="Civil", marks="Kvinna, röd jacka, cykel",
        )
        entities.sync_event_entities(conn, second_event)

        first_id = db.list_entities_for_event(conn, first_event)[0]["id"]
        second_id = db.list_entities_for_event(conn, second_event)[0]["id"]
        assert first_id != second_id


def test_sync_never_removes_a_manual_link_even_if_marks_changes():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="")
        other_event_id = _make_event(conn, signal_timestamp=2, marks="")
        entity_id = db.insert_entity(conn, entity_type="person", label="Känd person", source="manual")
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        entities.sync_event_entities(conn, event_id)

        linked = db.list_entities_for_event(conn, event_id)
        assert len(linked) == 1
        assert linked[0]["id"] == entity_id
        assert linked[0]["link_source"] == "manual"


def test_sync_leaves_manually_added_entities_with_no_report_mention_untouched():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="")
        entity_id = db.insert_entity(conn, entity_type="object", label="Kikare funnen vid stängslet")

        entities.sync_event_entities(conn, event_id)

        assert db.get_entity(conn, entity_id) is not None


# --- db.py entity CRUD/links -------------------------------------------------

def test_insert_and_get_entity_round_trip():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(
            conn, entity_type="vehicle", label="Vit skåpbil", registration="ABC123",
            attributes={"Colour": "Vit"}, notes="Sedd vid grinden",
        )
        entity = db.get_entity(conn, entity_id)
        assert entity["entity_type"] == "vehicle"
        assert entity["label"] == "Vit skåpbil"
        assert entity["registration"] == "ABC123"
        assert json.loads(entity["attributes"]) == {"Colour": "Vit"}
        assert entity["source"] == "manual"


def test_update_entity_overwrites_only_given_fields():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(conn, entity_type="person", label="Okänd")
        db.update_entity(conn, entity_id, {"notes": "Sedd två gånger"})
        entity = db.get_entity(conn, entity_id)
        assert entity["label"] == "Okänd"
        assert entity["notes"] == "Sedd två gånger"


def test_new_entity_has_no_photo_by_default_and_photo_path_can_be_set_and_cleared():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(conn, entity_type="person", label="Okänd")
        assert db.get_entity(conn, entity_id)["photo_path"] is None

        db.update_entity(conn, entity_id, {"photo_path": "/tmp/photo.jpg"})
        assert db.get_entity(conn, entity_id)["photo_path"] == "/tmp/photo.jpg"

        db.update_entity(conn, entity_id, {"photo_path": None})
        assert db.get_entity(conn, entity_id)["photo_path"] is None


def test_find_entity_by_registration_only_matches_the_given_type():
    with db.get_connection() as conn:
        db.insert_entity(conn, entity_type="vehicle", label="Bil", registration="ABC123")
        assert db.find_entity_by_registration(conn, "vehicle", "ABC123") is not None
        assert db.find_entity_by_registration(conn, "person", "ABC123") is None
        assert db.find_entity_by_registration(conn, "vehicle", "OTHER") is None


def test_list_entities_filters_by_type_and_search_query():
    with db.get_connection() as conn:
        db.insert_entity(conn, entity_type="person", label="Man i keps")
        db.insert_entity(conn, entity_type="vehicle", label="Vit skåpbil", registration="ABC123")

        assert len(db.list_entities(conn)) == 2
        assert len(db.list_entities(conn, entity_type="vehicle")) == 1
        assert len(db.list_entities(conn, query="keps")) == 1
        assert len(db.list_entities(conn, query="ABC123")) == 1
        assert len(db.list_entities(conn, query="nomatch")) == 0


def test_link_entity_to_event_manual_upgrades_an_existing_auto_link():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        entity_id = db.insert_entity(conn, entity_type="person", label="Person 1", source="auto")
        db.link_entity_to_event(conn, entity_id, event_id, source="auto")

        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        linked = db.list_entities_for_event(conn, event_id)
        assert linked[0]["link_source"] == "manual"


def test_link_entity_to_event_auto_never_downgrades_a_manual_link():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        entity_id = db.insert_entity(conn, entity_type="person", label="Person 1")
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        db.link_entity_to_event(conn, entity_id, event_id, source="auto")

        linked = db.list_entities_for_event(conn, event_id)
        assert linked[0]["link_source"] == "manual"


def test_unlink_entity_from_event_removes_the_link_only():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        entity_id = db.insert_entity(conn, entity_type="object", label="Kikare")
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        db.unlink_entity_from_event(conn, entity_id, event_id)

        assert db.list_entities_for_event(conn, event_id) == []
        assert db.get_entity(conn, entity_id) is not None


def test_list_events_for_entity_and_list_entities_for_event_are_symmetric():
    with db.get_connection() as conn:
        event_id = _make_event(conn, place="Norra grinden")
        entity_id = db.insert_entity(conn, entity_type="person", label="Person 1")
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        events = db.list_events_for_entity(conn, entity_id)
        assert len(events) == 1
        assert events[0]["id"] == event_id
        assert events[0]["link_source"] == "manual"


def test_list_entities_seen_with_finds_entities_sharing_an_event():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        person_id = db.insert_entity(conn, entity_type="person", label="Person 1")
        vehicle_id = db.insert_entity(conn, entity_type="vehicle", label="Fordon 1", registration="ABC123")
        unrelated_id = db.insert_entity(conn, entity_type="person", label="Ej kopplad")
        db.link_entity_to_event(conn, person_id, event_id, source="manual")
        db.link_entity_to_event(conn, vehicle_id, event_id, source="manual")

        seen_with_person = db.list_entities_seen_with(conn, person_id)
        assert [row["id"] for row in seen_with_person] == [vehicle_id]

        seen_with_unrelated = db.list_entities_seen_with(conn, unrelated_id)
        assert seen_with_unrelated == []


def test_delete_entity_also_removes_its_event_links():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        entity_id = db.insert_entity(conn, entity_type="person", label="Person 1")
        db.link_entity_to_event(conn, entity_id, event_id, source="manual")

        db.delete_entity(conn, entity_id)

        assert db.get_entity(conn, entity_id) is None
        assert db.list_entities_for_event(conn, event_id) == []


def test_prune_orphaned_auto_entities_only_removes_unlinked_auto_entities():
    with db.get_connection() as conn:
        event_id = _make_event(conn)
        auto_linked_id = db.insert_entity(conn, entity_type="person", label="Linkad auto", source="auto")
        db.link_entity_to_event(conn, auto_linked_id, event_id, source="auto")
        auto_orphan_id = db.insert_entity(conn, entity_type="person", label="Orphan auto", source="auto")
        manual_orphan_id = db.insert_entity(conn, entity_type="person", label="Orphan manual", source="manual")

        db.prune_orphaned_auto_entities(conn, [auto_linked_id, auto_orphan_id, manual_orphan_id])

        assert db.get_entity(conn, auto_linked_id) is not None
        assert db.get_entity(conn, auto_orphan_id) is None
        assert db.get_entity(conn, manual_orphan_id) is not None


# --- list_watchlist_entities --------------------------------------------------

def test_list_watchlist_entities_includes_entities_linked_to_two_or_more_events():
    with db.get_connection() as conn:
        recurring_id = db.insert_entity(conn, entity_type="vehicle", label="Bil", registration="ABC123")
        once_id = db.insert_entity(conn, entity_type="person", label="Person 1")
        event_a = _make_event(conn, signal_timestamp=1)
        event_b = _make_event(conn, signal_timestamp=2)
        db.link_entity_to_event(conn, recurring_id, event_a, source="auto")
        db.link_entity_to_event(conn, recurring_id, event_b, source="auto")
        db.link_entity_to_event(conn, once_id, event_a, source="auto")

        watchlisted = {row["id"] for row in db.list_watchlist_entities(conn)}
        assert recurring_id in watchlisted
        assert once_id not in watchlisted


def test_list_watchlist_entities_includes_manually_flagged_entities_regardless_of_event_count():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(conn, entity_type="object", label="Kikare")
        db.update_entity(conn, entity_id, {"watchlist": True})

        watchlisted = {row["id"] for row in db.list_watchlist_entities(conn)}
        assert entity_id in watchlisted


def test_list_watchlist_entities_excludes_entities_with_neither_recurrence_nor_flag():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(conn, entity_type="object", label="Kikare")

        watchlisted = {row["id"] for row in db.list_watchlist_entities(conn)}
        assert entity_id not in watchlisted


def test_list_watchlist_entities_reports_the_correct_event_count():
    with db.get_connection() as conn:
        entity_id = db.insert_entity(conn, entity_type="vehicle", label="Bil", registration="ABC123")
        event_a = _make_event(conn, signal_timestamp=1)
        event_b = _make_event(conn, signal_timestamp=2)
        db.link_entity_to_event(conn, entity_id, event_a, source="auto")
        db.link_entity_to_event(conn, entity_id, event_b, source="auto")

        row = next(r for r in db.list_watchlist_entities(conn) if r["id"] == entity_id)
        assert row["event_count"] == 2


# --- cascading deletes/resets ------------------------------------------------

def test_delete_event_prunes_an_orphaned_auto_entity_but_keeps_a_manual_one():
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)
        auto_entity_id = db.list_entities_for_event(conn, event_id)[0]["id"]
        manual_entity_id = db.insert_entity(conn, entity_type="object", label="Kikare")
        db.link_entity_to_event(conn, manual_entity_id, event_id, source="manual")

        db.delete_event(conn, event_id)

        assert db.get_entity(conn, auto_entity_id) is None
        assert db.get_entity(conn, manual_entity_id) is not None


def test_delete_event_keeps_an_auto_entity_still_linked_to_another_event():
    with db.get_connection() as conn:
        first_event = _make_event(conn, signal_timestamp=1, marks="Fordon 1 (R – Registration: ABC123)")
        entities.sync_event_entities(conn, first_event)
        second_event = _make_event(conn, signal_timestamp=2, marks="Fordon 1 (R – Registration: ABC123)")
        entities.sync_event_entities(conn, second_event)
        shared_entity_id = db.list_entities_for_event(conn, first_event)[0]["id"]

        db.delete_event(conn, first_event)

        assert db.get_entity(conn, shared_entity_id) is not None
        assert db.list_entities_for_event(conn, second_event)[0]["id"] == shared_entity_id


def test_reset_events_clears_every_entity_including_manual_ones():
    """"Rensa händelselogg" clears the whole Personer/fordon/objekt
    database along with the event log it was built from -- manually
    catalogued/watchlisted entities included, not just entities.py's
    "auto" extractions, since a recurring-persons list left behind after
    its source events are gone would just be stale, orphaned reference
    data, unlike the adjacent-unit roster this reset genuinely leaves
    alone."""
    with db.get_connection() as conn:
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)
        manual_entity_id = db.insert_entity(conn, entity_type="object", label="Katalogiserad post")

        db.reset_events(conn)

        assert db.list_events(conn) == []
        assert db.list_entities(conn) == []
        assert db.get_entity(conn, manual_entity_id) is None


def test_reset_all_clears_every_entity_including_manual_ones():
    with db.get_connection() as conn:
        db.insert_entity(conn, entity_type="object", label="Katalogiserad post")
        event_id = _make_event(conn, marks="Person 1 (A – Age: 30-40)")
        entities.sync_event_entities(conn, event_id)

        db.reset_all(conn)

        assert db.list_entities(conn) == []


# --- parse_watchlist_markdown / import_watchlist_entries -------------------

def _fake_watchlist_entries(conn):
    from signal_events.reports import generator  # noqa: F401  (avoids a cycle at module load)

    vehicle_id = db.insert_entity(
        conn, entity_type="vehicle", label="Fordon 1", registration="ABC123",
        attributes={"Registration": "ABC 123", "Colour": "Svart"}, source="auto",
    )
    person_id = db.insert_entity(
        conn, entity_type="person", label="Civil, grön jacka",
        attributes={"Colour": "grön jacka"}, source="manual",
    )
    db.update_entity(conn, person_id, {"watchlist": True})
    vehicle = conn.execute("SELECT *, 2 AS event_count FROM entities WHERE id = ?", (vehicle_id,)).fetchone()
    person = conn.execute("SELECT *, 1 AS event_count FROM entities WHERE id = ?", (person_id,)).fetchone()

    event_row = {"id": 1, "created_at": "2026-01-01T10:00:00", "event_time": "10:00", "place": "Norra grinden"}
    return [
        {"entity": vehicle, "events": [event_row]},
        {"entity": person, "events": []},
    ]


def test_parse_watchlist_markdown_recovers_type_label_registration_and_attributes():
    from signal_events.reports import generator

    with db.get_connection() as conn:
        markdown = generator.render_watchlist_markdown(_fake_watchlist_entries(conn), site_name="Kvarn")

    parsed = entities.parse_watchlist_markdown(markdown)

    vehicle = next(e for e in parsed if e.entity_type == "vehicle")
    assert vehicle.label == "Fordon 1"
    assert vehicle.registration == "ABC123"
    assert vehicle.attributes.get("Colour") == "Svart"
    assert vehicle.watchlist is False

    person = next(e for e in parsed if e.entity_type == "person")
    assert person.label == "Civil, grön jacka"
    assert person.attributes.get("Colour") == "grön jacka"
    assert person.watchlist is True  # manually bevakad in the export


def test_parse_watchlist_markdown_ignores_indented_event_lines():
    markdown = (
        "## Fordon\n"
        "### Fordon 1 (2 händelser)\n"
        "- Reg.nr: ABC123\n"
        "  - Händelse 011000 — Norra grinden\n"
    )
    parsed = entities.parse_watchlist_markdown(markdown)
    assert len(parsed) == 1
    assert parsed[0].registration == "ABC123"


def test_parse_watchlist_markdown_returns_empty_list_for_unrelated_text():
    assert entities.parse_watchlist_markdown("Not a bevakningslista at all.") == []


def test_import_watchlist_entries_creates_new_entities_with_watchlist_flag_set():
    entry = entities.ImportedWatchlistEntry(
        entity_type="vehicle", label="Fordon 1", attributes={"Colour": "Svart"},
        registration="ABC123", notes=None, watchlist=False,
    )
    with db.get_connection() as conn:
        created, updated = entities.import_watchlist_entries(conn, [entry])
        assert (created, updated) == (1, 0)

        rows = db.list_entities(conn, entity_type="vehicle")
        assert len(rows) == 1
        assert rows[0]["label"] == "Fordon 1"
        assert bool(rows[0]["watchlist"]) is True


def test_import_watchlist_entries_dedupes_vehicle_by_plate_and_merges_attributes():
    with db.get_connection() as conn:
        existing_id = db.insert_entity(
            conn, entity_type="vehicle", label="Fordon 1", registration="ABC123",
            attributes={"Colour": "Svart"}, source="auto",
        )
        entry = entities.ImportedWatchlistEntry(
            entity_type="vehicle", label="Fordon 1 (annan enhet)", attributes={"Model": "Volvo"},
            registration="ABC123", notes=None, watchlist=False,
        )

        created, updated = entities.import_watchlist_entries(conn, [entry])
        assert (created, updated) == (0, 1)

        row = db.get_entity(conn, existing_id)
        attrs = json.loads(row["attributes"])
        assert attrs == {"Colour": "Svart", "Model": "Volvo"}
        assert bool(row["watchlist"]) is True


def test_import_watchlist_entries_dedupes_person_by_exact_label():
    with db.get_connection() as conn:
        existing_id = db.insert_entity(conn, entity_type="person", label="Civil, grön jacka", source="manual")
        entry = entities.ImportedWatchlistEntry(
            entity_type="person", label="Civil, grön jacka", attributes={},
            registration=None, notes="Sedd igen", watchlist=False,
        )

        created, updated = entities.import_watchlist_entries(conn, [entry])
        assert (created, updated) == (0, 1)
        assert db.get_entity(conn, existing_id)["notes"] == "Sedd igen"


def test_import_watchlist_markdown_round_trip_via_generator():
    """Exports a bevakningslista (from a couple of pre-existing entities,
    one not yet manually watchlisted), parses that export back, and
    imports it into the *same* database (there's only one isolated DB
    per test, so this stands in for "another unit's copy") -- since both
    entities already exist here by plate/label, the import must be
    recognized as updates, not duplicate creates, and both must end up
    watchlist=True regardless of their pre-import flag state."""
    from signal_events.reports import generator

    with db.get_connection() as conn:
        markdown = generator.render_watchlist_markdown(_fake_watchlist_entries(conn), site_name="Kvarn")

    parsed = entities.parse_watchlist_markdown(markdown)
    with db.get_connection() as conn:
        created, updated = entities.import_watchlist_entries(conn, parsed)
        assert (created, updated) == (0, 2)

        rows = db.list_watchlist_entities(conn)
        assert len(rows) == 2
        assert all(bool(row["watchlist"]) for row in rows)
