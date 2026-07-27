"""Seeds a realistic multi-day demo scenario directly into the database,
already marked reviewed, so you can jump straight to the recurrence
analysis and threat assessment without typing anything in by hand.

Run this against an isolated demo database, not your real data/ dir:

    SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events init-db
    SIGNAL_EVENTS_DATA_DIR=./demo_data python demo/seed_demo.py
    SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events serve --port 5001

Then open http://127.0.0.1:5001 and look at Händelser, Sammanställd
hotbedömning, and Inställningar.

The scenario: a grey van (Reg.Nr ABC123) and a person in dark clothing
each recur across the site over three days, one sign of attempted
sabotage is found once, and two separate armed-person sightings occur on
different days -- enough, under the recalibrated threat scale, to reach
RED (recurring severe indicator), while the one-off sabotage sign alone
would only have reached YELLOW. An adjacent unit ("Kompani 2") also has a
status report waiting on the summary page.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from signal_events import db

UNIT_NAME = "Kompani 1"
ADJACENT_UNIT_NAME = "Kompani 2"

EVENTS = [
    dict(
        event_time="221430", place="Östra grinden", count="1", object="Skåpbil",
        activity="Fotograferade stängslet, körde sedan iväg norrut",
        marks="Grå skåpbil, Reg.Nr: ABC123",
        reported_by="Vakt Andersson",
        next_steps="Fortsatt bevakning av östra grinden",
        raw_text=(
            "Kl 14:30 vid östra grinden observerades en grå skåpbil, "
            "Reg.Nr: ABC123. Föraren fotograferade stängslet innan "
            "fordonet körde iväg norrut."
        ),
    ),
    dict(
        event_time="221610", place="Skogsbrynet vid förrådet", count="1", object="Person",
        activity="Stod stilla länge och tittade mot vakttornet",
        marks="Mörka kläder, svart keps, mörk ryggsäck",
        reported_by="Vakt Lindqvist",
        next_steps="Noterat, fortsatt uppmärksamhet vid förrådet",
        raw_text=(
            "Kl 16:10 vid skogsbrynet nära förrådet observerades en person "
            "i mörka kläder, svart keps och mörk ryggsäck. Personen stod "
            "stilla en längre stund och tittade mot vakttornet innan denne "
            "försvann in i skogen."
        ),
    ),
    dict(
        event_time="230915", place="Västra infarten", count="1", object="Skåpbil",
        activity="Saktade ner och stannade kort innan den körde vidare",
        marks="Grå skåpbil, Reg.Nr: ABC123",
        reported_by="Vakt Berg",
        next_steps="Registrerat i vaktloggen",
        raw_text=(
            "Kl 09:15 vid västra infarten passerade samma gråa skåpbil "
            "(Reg.Nr: ABC123) som sågs vid östra grinden föregående dag. "
            "Fordonet saktade ner och stannade kort innan det körde vidare."
        ),
    ),
    dict(
        event_time="231205", place="Södra hörnet av stängslet", count="-", object="Skadegörelse",
        activity="Klippt stängsel upptäckt, ingen person närvarande vid upptäckt",
        marks="Hål i stängslet, ca 1 meter",
        reported_by="Vakt Andersson",
        next_steps="Teknisk avdelning kontaktad för reparation, ökad bevakning av området",
        raw_text=(
            "Kl 12:05 upptäcktes ett klippt stängsel vid södra hörnet, ett "
            "hål på ca 1 meter. Ingen person var närvarande vid "
            "upptäckten. Teknisk avdelning kontaktad."
        ),
    ),
    dict(
        event_time="232030", place="Bortre parkeringen", count="1", object="Beväpnad person",
        activity="Försvann in i skogen innan vakt hann agera",
        marks="Bar vad som såg ut som ett gevär",
        reported_by="Vakt Lindqvist",
        next_steps="Polis underrättad, skärpt bevakning",
        raw_text=(
            "Kl 20:30 vid bortre parkeringen siktades en beväpnad person "
            "som bar vad som såg ut som ett gevär. Personen försvann in i "
            "skogen innan vakt hann agera. Polis underrättad."
        ),
    ),
    dict(
        event_time="240740", place="Huvudentrén", count="1", object="Person",
        activity="Fotograferade skyltar och kameror vid entrén",
        marks="Mörka kläder, svart keps, mörk ryggsäck",
        reported_by="Vakt Berg",
        next_steps="Bildmaterial sparat, fortsatt bevakning",
        raw_text=(
            "Kl 07:40 vid huvudentrén observerades samma person som "
            "tidigare (mörka kläder, svart keps, mörk ryggsäck) "
            "fotografera skyltar och kameror vid entrén."
        ),
    ),
    dict(
        event_time="241900", place="Norra skogsbrynet", count="2", object="Beväpnade personer",
        activity="Avvek söderut mot allmän väg",
        marks="En av personerna bar vapen",
        reported_by="Vakt Andersson",
        next_steps="Polis underrättad, förhöjd beredskap",
        raw_text=(
            "Kl 19:00 vid norra skogsbrynet siktades två personer varav en "
            "beväpnad. De avvek söderut mot allmän väg. Polis underrättad, "
            "förhöjd beredskap gäller."
        ),
    ),
    dict(
        event_time="242215", place="Vid transformatorstationen", count="1", object="Skåpbil",
        activity="Parkerad i ca 10 minuter innan den körde iväg",
        marks="Grå skåpbil, samma utseende som tidigare, Reg.Nr: ABC123",
        reported_by="Vakt Lindqvist",
        next_steps="Fortsatt övervakning, jämförs med tidigare observationer",
        raw_text=(
            "Kl 22:15 vid transformatorstationen sågs samma gråa skåpbil "
            "(Reg.Nr: ABC123) parkerad i ca 10 minuter innan den körde "
            "iväg."
        ),
    ),
]

ADJACENT_REPORT_BODY = """Status Kompani 2, 24 juli:
- Ingen förändring vid vårt område sedan senaste rapport.
- En vit personbil, Reg.Nr: XYZ789, har passerat vår sida av stängslet vid två tillfällen.
- Inga beväpnade personer eller sabotageförsök observerade."""


def main() -> None:
    arg_parser = argparse.ArgumentParser(description=__doc__)
    arg_parser.add_argument(
        "--skip-events", action="store_true",
        help="Only seed the unit name + adjacent-unit report, skip the 8 "
        "demo events -- use this after importing demo/scenario_import.txt "
        "yourself, so the events aren't duplicated.",
    )
    args = arg_parser.parse_args()

    with db.get_connection() as conn:
        db.set_unit_name(conn, UNIT_NAME)
        db.add_adjacent_unit(conn, ADJACENT_UNIT_NAME)

        if not args.skip_events:
            for i, fields in enumerate(EVENTS):
                message_id = db.insert_message(
                    conn,
                    signal_timestamp=-time.time_ns() - i,
                    sender_number=None,
                    sender_name=fields["reported_by"],
                    body=fields["raw_text"],
                    raw_json='{"source": "demo_seed"}',
                )
                db.insert_event(
                    conn,
                    message_id=message_id,
                    fields={**fields, "needs_review": False},
                )

        db.insert_adjacent_report(
            conn,
            signal_timestamp=-time.time_ns() - 1000,
            sender_number=None,
            sender_name="Stabsassistent, Kompani 2",
            unit_name=ADJACENT_UNIT_NAME,
            body=ADJACENT_REPORT_BODY,
        )

    if args.skip_events:
        print("Seeded unit name + 1 adjacent-unit report (events skipped).")
    else:
        print(f"Seeded {len(EVENTS)} reviewed events + 1 adjacent-unit report.")
    print(f"Unit name set to: {UNIT_NAME}")
    print("Open the web UI and check Sammanställd hotbedömning — expect RED.")


if __name__ == "__main__":
    main()
