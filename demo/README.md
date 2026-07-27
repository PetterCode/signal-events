# Demo scenario

A three-day fictional scenario for showing off the full pipeline: a
recurring grey van, a recurring person in dark clothing, one sabotage
sign, and two separate armed-person sightings — enough for the
recalibrated threat scale to reach **RED**, plus a status report from an
adjacent unit ("Kompani 2").

Runs against an isolated `demo_data/` directory so it never touches your
real `data/`.

## Option A — see the finished result immediately

Seeds the same 8 reports directly into the database, already marked
reviewed, plus the adjacent-unit report and unit name.

```bash
SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events init-db
SIGNAL_EVENTS_DATA_DIR=./demo_data python demo/seed_demo.py
SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events serve --port 5001
```

Open <http://127.0.0.1:5001> and go straight to **Sammanställd
hotbedömning** — threat level should read RÖD, with the two armed
sightings and the recurring van/person groups all listed in the
motivering. Check **Inställningar** to see the unit name and the
adjacent-unit roster, and the bottom of the summary page for Kompani 2's
status report.

## Option B — see the ingestion + review step too

Same scenario, but written in the labeled 7S rapport format (Till/Från/
TNR/Stund/Ställe/Styrka/Slag/Sysselsättning/Symbol/Reg.Nr/Sagesman/Sedan),
imported through the web UI so you can show off ingestion and the review
queue from scratch.

```bash
SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events init-db
SIGNAL_EVENTS_DATA_DIR=./demo_data python -m signal_events serve --port 5001
```

Open <http://127.0.0.1:5001> → **Importera från fil** → upload
`demo/scenario_import.txt` → review/confirm each of the 8 events (every
field extracts cleanly since the 7S labels are matched directly, but
`needs_review` is still set on import — a human still has to confirm
each one before it counts) → then check **Sammanställd hotbedömning**.

Note: the labeled 7S format is used here rather than plain prose because
the generic free-text heuristics (`extract_place`/`extract_activity`/etc.
in `signal_events/parser.py`) key off English trigger words ("near",
"wearing", "recommend", ...) — a leftover from before the UI moved to
Swedish. Plain Swedish prose currently extracts close to nothing outside
the 7S fast path; that's a real gap worth fixing separately, not
something this demo works around.

Want the adjacent-unit report and unit name too, without duplicating the
8 events? Run the seed script with `--skip-events` afterwards, against
the same `demo_data/`:

```bash
SIGNAL_EVENTS_DATA_DIR=./demo_data python demo/seed_demo.py --skip-events
```

## Option C — the 10-day training scenario (built into the GUI)

A longer, self-paced scenario for training or live demonstration: the
**Importera från fil** page has a "Demo och övning" card with 10 buttons,
"Dag 1" through "Dag 10", each importing that day's bundled file
(`demo/training_days/dag_01.txt` .. `dag_10.txt`, ~30 reports each,
also in 7S format) straight from the running app — no manual file
upload needed, and it works against your normal `data/` just as well as
an isolated one.

Click through the days one at a time and watch **Sammanställd
hotbedömning** change: days 1-3 are pure noise (wildlife, deliveries,
routine patrols) and stay GRÖN; a recurring grey van (Reg.Nr QAB456) and
a recurring person in dark clothing start appearing from day 4 onward,
pushing it to GUL; a single armed-person sighting (day 7) and a single
sabotage sign (day 6) each stay capped at GUL on their own; a *second*
armed sighting on day 9 is what finally confirms a recurring severe
indicator and flips it to RÖD, reinforced by a second sabotage sign on
day 10.

Each day also delivers a status report from each of two adjacent units,
**2.Kompani** and **3.Kompani** — visible in the "Status från
angränsande enheter" card at the bottom of the summary page. They mirror
this unit's own green/yellow/red rhythm, but shifted by a day in opposite
directions: 2.Kompani reaches RÖD on day 8 (one day *before* this unit),
3.Kompani only on day 10 (one day *after*) — so the three units'
escalations are visibly out of sync as you click through, the way real
adjacent units rarely move in perfect lockstep. See
`generate_training_days.py`'s `ADJACENT_UNITS`/`offset` if you want to
change the shift or add a third unit.

Regenerate the files (deterministic, fixed random seed) with:

```bash
python demo/generate_training_days.py
```

## Cleaning up

Everything lives under `demo_data/`; delete it to reset:

```bash
rm -rf demo_data
```
