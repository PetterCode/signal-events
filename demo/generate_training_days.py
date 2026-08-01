"""Generates the 10-day training scenario:

- `demo/training_days/dag_01.txt` .. `dag_10.txt`, each ~30 reports in
  7S-labeled format, for this unit's own incident stream.
- `demo/training_days/dag_01_sensor.txt` .. `dag_10_sensor.txt`, each 3
  automated sensor-trigger reports (tripwire, motion detector, camera) in
  the same 7S format, reported by a sensor gateway rather than a guard --
  optional, brought in via the "Inkludera sensorhändelser" toggle on
  Demo och övning rather than always included with the day's human story.
- `demo/training_days/adjacent_status.json`, one status report per day
  from each of two adjacent units ("2.Kompani", "3.Kompani"), for the
  "Status från angränsande enheter" card on the summary page.
- `demo/training_days/event_images.json`, TNR -> image filename for
  whichever events (human or sensor) have a cartoon-style photo attached
  (see generate_training_images.py).

All of these are read by the "Dag 1".."Dag 10" import buttons on the
Demo och övning page (see webapp/routes.py:import_training_day).

The story: mostly mundane guard-log noise (wildlife, deliveries, routine
patrols) every day, with a small number of deliberate "signal" events
woven in that escalate over the ten days -- a recurring grey van, a
recurring person in dark clothing, then a sabotage sign, then a first
armed sighting (single -- stays capped at YELLOW), then a second armed
sighting on day 9 (recurring -- triggers RED under the recalibrated
threat scale), reinforced by a second sabotage sign on day 10.

The two adjacent units mirror this same green(1-3)/yellow(4-8)/red(9-10)
rhythm in their own status-report text, but shifted by one day: 2.Kompani
is a day ahead (reaches red on day 8), 3.Kompani a day behind (reaches
red only on day 10) -- so all three units' escalation is visible on the
summary page at slightly different points as the trainee clicks through
the days.

Re-run this to regenerate the files (deterministic: fixed random seed,
and the adjacent-status text has no randomness at all).

    python demo/generate_training_days.py
"""

from __future__ import annotations

import json
import random
import sys
import warnings
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mgrs as _mgrs_lib

from signal_events.analysis import _DESC_SIMILARITY_THRESHOLD, _jaccard, _tokenize

OUT_DIR = Path(__file__).resolve().parent / "training_days"
SEED = 20260725
EVENTS_PER_DAY = 30

# Fixed (lat, lon) for every named post around the scenario's fictional
# compound -- centred on demo_map._DEMO_CENTER_LAT/LON (the same point the
# cartoon dummy basemap's river/road are drawn relative to, see
# signal_events/demo_map.py), so imported events land right on top of the
# illustrated terrain rather than off to one side of it. Coordinates
# themselves aren't meant to depict a real place -- see demo_map.py's
# docstring for why the map itself is a cartoon rather than a real
# provider's imagery.
#
# Two places are deliberately left out ("Sjön utanför området" -- "the
# lake outside the area", too diffuse to give one grid reference for; and
# "Gångstigen längs stängslet" -- "the path along the fence", not really
# a single point either) so not every place in the scenario has a
# coordinate to begin with -- see _place_with_position below for the
# further per-event chance of a position being included even when one
# exists, mirroring a guard not always bothering to note a grid
# reference.
PLACE_COORDS: dict[str, tuple[float, float]] = {
    "Norra vägen": (59.2985, 18.0973),
    "Huvudentrén": (59.2965, 18.0973),
    "Vaktkuren": (59.2966, 18.0976),
    "Parkeringen vid huvudbyggnaden": (59.2968, 18.0969),
    "Förrådsbyggnaden": (59.2978, 18.0990),
    "Östra grinden": (59.2972, 18.1006),
    "Västra infarten": (59.2971, 18.0939),
    "Skogsbrynet vid förrådet": (59.2981, 18.0996),
    "Kajen": (59.2950, 18.1012),
    "Bommen vid infarten": (59.2970, 18.0942),
    "Skjutbanan": (59.2996, 18.0973),
    "Bortre parkeringen": (59.2959, 18.1001),
    "Södra hörnet av stängslet": (59.2955, 18.0975),
    "Vid transformatorstationen": (59.2960, 18.0949),
    "Norra skogsbrynet": (59.2989, 18.0967),
    "Trådlarm vid Östra grinden": (59.2973, 18.1004),
    "Trådlarm vid Västra infarten": (59.2970, 18.0940),
    "Trådlarm bakom förrådet": (59.2979, 18.0993),
    "Trådlarm vid Bommen vid infarten": (59.2969, 18.0943),
    "Rörelsedetektor vid Skogsbrynet vid förrådet": (59.2982, 18.0997),
    "Rörelsedetektor vid Bortre parkeringen": (59.2958, 18.1000),
    "Rörelsedetektor vid transformatorstationen": (59.2961, 18.0948),
    "Rörelsedetektor vid Kajen": (59.2951, 18.1011),
    "Kamera vid Huvudentrén": (59.2964, 18.0974),
    "Kamera vid Östra grinden": (59.2972, 18.1005),
    "Kamera vid Kajen": (59.2949, 18.1013),
    "Kamera vid Vaktkuren": (59.2967, 18.0975),
}

# Human-reported events (noise + signal) get a position roughly this
# often even when their place has a known coordinate -- not every guard
# bothers writing a grid reference every time. Sensor events always get
# one (see generate_sensor_day/_place_with_position) since an automated
# gateway logs its own fixed installation position every time, unlike a
# person.
_POSITION_INCLUDE_RATE = 0.8

_MGRS = _mgrs_lib.MGRS()


def _mgrs_token(lat: float, lon: float) -> str:
    with warnings.catch_warnings():
        # Same cosmetic ctypes "Latitude Warning" signal_events/coordinates.py
        # already silences on the decode side -- harmless, not a sign the
        # coordinate is wrong.
        warnings.simplefilter("ignore")
        return _MGRS.toMGRS(lat, lon)


def _position_roll(day: int, tnr: str, place: str) -> float:
    """A deterministic pseudo-random value in [0, 1) for the position-
    inclusion decision below, derived independently of the shared per-day
    `random.Random` (rather than drawing from it) -- so whether a given
    event gets a position never shifts the sequence of random draws
    everything else in this file depends on (report times, which guard
    reported it, noise-event content), which several existing tests treat
    as pinned-down/deterministic output."""
    digest = zlib.crc32(f"{day}:{tnr}:{place}".encode("utf-8"))
    return digest / 0xFFFFFFFF


def _place_with_position(day: int, tnr: str, place: str, *, always: bool = False) -> str:
    """Appends a real MGRS grid reference for `place` to its own text
    (e.g. "Kajen (34VCL...)") when a coordinate is known for it and this
    event happens to get one -- the exact same intake path a real guard's
    report would use (signal_events.coordinates.extract_mgrs_latlon reads
    the Ställe field for a token like this at import time), not a
    separate demo-only mechanism. Returns `place` unchanged when there's
    no coordinate for it, or (for non-`always` calls) when the per-event
    roll skips it."""
    coords = PLACE_COORDS.get(place)
    if coords is None:
        return place
    if not always and _position_roll(day, tnr, place) >= _POSITION_INCLUDE_RATE:
        return place
    return f"{place} ({_mgrs_token(*coords)})"

GUARDS = [
    "Vakt Andersson", "Vakt Lindqvist", "Vakt Berg", "Vakt Nilsson",
    "Vakt Karlsson", "Vakt Ström", "Vakt Holm", "Vakt Ekberg",
]

NOISE_PLACES = [
    "Norra vägen", "Huvudentrén", "Vaktkuren", "Parkeringen vid huvudbyggnaden",
    "Förrådsbyggnaden", "Östra grinden", "Västra infarten",
    "Skogsbrynet vid förrådet", "Kajen", "Bommen vid infarten",
    "Skjutbanan", "Bortre parkeringen", "Sjön utanför området",
    "Gångstigen längs stängslet",
]

# Short, low-overlap filler phrases used as the Symbol (kännetecken) field
# for noise events -- deliberately varied so near-identical wildlife/routine
# sightings don't accidentally cluster into a "recurring" pattern group in
# analysis.py (which buckets by Slag then measures Jaccard similarity of
# Symbol+Slag tokens). Kept free of any weapon/explosive/sabotage/
# suspicious-behavior keyword.
MARKS_FILLERS = [
    "Ingen närmare beskrivning", "Inga särskilda kännetecken",
    "Vanligt utseende", "Kort varaktighet", "Ingen ytterligare information",
    "Sedd på håll", "Rörde sig i buskaget", "Försvann snabbt",
    "Inga avvikelser", "Enstaka sikt", "Kortvarig sikt",
    "Ingen tydlig avvikelse",
]

# A second, independent pool combined with MARKS_FILLERS above (see
# _unique_symbol below) so two noise events of the same Slag essentially
# never accidentally look like a "recurring" pattern to analysis.py's
# description-similarity grouping -- one filler pool alone is short enough
# that two picks can coincidentally share enough tokens to cross the 0.5
# Jaccard threshold.
MARKS_DETAILS = [
    "noterat i fältanteckningarna", "inget som krävde närmare kontroll",
    "avvek inte från vad som är brukligt", "bedömdes som ofarligt",
    "krävde ingen vidare uppföljning", "föranledde ingen ytterligare notering",
    "låg inom det förväntade", "gav inte anledning till oro",
    "bedömdes som en engångsföreteelse", "inget ovanligt i övrigt",
]

# (slag, styrka, activity-templates...) -- one activity is chosen at random
# per occurrence; marks/symbol comes from MARKS_FILLERS independently, so
# the same animal/vehicle/person type can recur without necessarily
# clustering (only Slag+Symbol tokens feed the similarity check).
ANIMALS = [
    ("Rådjur", "1"), ("Räv", "1"), ("Hare", "1"), ("Katt", "1"),
    ("Ekorre", "1"), ("Fasan", "1"), ("Grävling", "1"), ("Fågelflock", "6"),
]
ANIMAL_ACTIONS = [
    "Passerade genom området och försvann in i skogen",
    "Betade en stund vid stängslet",
    "Sprang tvärs över vägen",
    "Vilade i gräset nära förrådet",
    "Rörde sig längs stängslet innan den försvann",
]

VEHICLES = [
    ("Sopbil", "1"), ("Leveransbil", "1"), ("Postbil", "1"),
    ("Servicebil", "1"), ("Traktor", "1"), ("Besiktningsbil", "1"),
    ("Renhållningsfordon", "1"),
]
VEHICLE_ACTIONS = [
    "Passerade på allmän väg utan att stanna",
    "Levererade gods vid förrådet",
    "Utförde planerat underhållsarbete",
    "Vände och körde tillbaka samma väg",
    "Stod kort stilla innan den körde vidare",
]

PEOPLE_NOISE = [
    ("Brevbärare", "1"), ("Cyklist", "1"), ("Löpare", "1"),
    ("Fotgängare", "1"), ("Hundpromenerare", "1"), ("Fiskare", "1"),
]
PEOPLE_ACTIONS = [
    "Passerade utanför stängslet",
    "Rastade en stund i närheten",
    "Frågade om vägbeskrivning",
    "Fortsatte vidare utan att stanna",
]

MISC = [
    ("Rutinpatrullering", "-", "Inget avvikande att rapportera"),
    ("Väderobservation", "-", "Nedsatt sikt på grund av dimma"),
    ("Vindfälld gren", "-", "Låg över gångvägen, forslades bort av personal"),
    ("Skolklass", "14", "Passerade på guidad rundtur utanför stängslet"),
]

NOISE_NEXT_STEPS = [
    "Fortsatt bevakning enligt rutin", "Ingen åtgärd krävs",
    "Noterat i vaktloggen", "Inget vidare agerande",
]


def _rand_time(rng: random.Random, day: int) -> str:
    return f"{day:02d}{rng.randint(0, 23):02d}{rng.randint(0, 59):02d}"


class _NoiseClusterGuard:
    """Mirrors analysis._group_by_description's own bucketing (by Slag) and
    Jaccard-similarity clustering (on Symbol+Slag tokens), so noise events
    can be rejection-sampled until none of them would actually cluster --
    verified against the real scoring logic rather than hoped for. Each
    accepted noise event is kept as its own singleton "cluster" (tracked
    per Slag bucket) precisely because a cluster only grows past size 1 by
    matching an existing member; refusing every such match guarantees the
    real analysis code will see nothing but singletons here too, which
    `_group_by_description` then silently drops (cluster size < 2)."""

    def __init__(self) -> None:
        self._buckets: dict[str, list[set[str]]] = {}

    def would_cluster(self, slag: str, symbol: str) -> bool:
        key = slag.strip().lower()
        tokens = _tokenize(symbol) | _tokenize(slag)
        return any(
            _jaccard(tokens, existing) >= _DESC_SIMILARITY_THRESHOLD
            for existing in self._buckets.get(key, [])
        )

    def accept(self, slag: str, symbol: str) -> None:
        key = slag.strip().lower()
        tokens = _tokenize(symbol) | _tokenize(slag)
        self._buckets.setdefault(key, []).append(tokens)


def _noise_event(rng: random.Random, guard: _NoiseClusterGuard) -> dict:
    category = rng.choice(["animal", "vehicle", "person", "misc"])
    if category == "animal":
        slag, styrka = rng.choice(ANIMALS)
        activity = rng.choice(ANIMAL_ACTIONS)
    elif category == "vehicle":
        slag, styrka = rng.choice(VEHICLES)
        activity = rng.choice(VEHICLE_ACTIONS)
    elif category == "person":
        slag, styrka = rng.choice(PEOPLE_NOISE)
        activity = rng.choice(PEOPLE_ACTIONS)
    else:
        slag, styrka, activity = rng.choice(MISC)

    for attempt in range(50):
        symbol = f"{rng.choice(MARKS_FILLERS)}, {rng.choice(MARKS_DETAILS)}"
        if attempt >= 25:
            # Extremely unlucky run of collisions -- force a disambiguating
            # word in as a last resort so this can never loop forever.
            symbol = f"{symbol} (logg {rng.randint(1000, 9999)})"
        if not guard.would_cluster(slag, symbol):
            break
    guard.accept(slag, symbol)

    return {
        "place": rng.choice(NOISE_PLACES),
        "styrka": styrka,
        "slag": slag,
        "activity": activity,
        "symbol": symbol,
        "reg_nr": None,
        "next_steps": rng.choice(NOISE_NEXT_STEPS),
    }


# Deliberate escalating "signal" events, one list per day (1-indexed).
# The van (QAB456) and the person in dark clothing recur across days by
# reusing identical Symbol wording each time, so analysis.py's recurrence
# grouping picks them up; the two armed sightings and two sabotage signs
# are what push the threat level to RED once they've each happened twice.
VAN_SYMBOL = "Grå skåpbil"
PERSON_SYMBOL = "Mörka kläder, mörk keps, mörk ryggsäck"

# Each signal event may carry an "image" key naming a file under
# demo/training_days/images/ (see generate_training_images.py) -- a
# cartoon stand-in for a phone photo the guard would attach. Not every
# entry gets one: the story's mundane noise events never do, and a few
# signal events (the two ordinary-looking van-passing-by ones) are left
# without a photo too, same as a real guard wouldn't necessarily always
# think to snap one.
IMG_VAN = "van.png"
IMG_PERSON = "dark_figure.png"
IMG_CUT_FENCE = "cut_fence.png"
IMG_ARMED_SINGLE = "armed_single.png"
IMG_ARMED_PAIR = "armed_pair.png"
IMG_BROKEN_LOCK = "broken_lock.png"

SIGNAL_EVENTS: dict[int, list[dict]] = {
    3: [
        dict(place="Norra vägen", styrka="1", slag="Skåpbil",
             activity="Passerade i normal hastighet, ingen avvikelse",
             symbol=VAN_SYMBOL, reg_nr="QAB456",
             next_steps="Noterat i vaktloggen"),
    ],
    4: [
        dict(place="Östra grinden", styrka="1", slag="Skåpbil",
             activity="Saktade ner vid grinden innan den körde vidare",
             symbol=VAN_SYMBOL, reg_nr="QAB456",
             next_steps="Noterat i vaktloggen", image=IMG_VAN),
    ],
    5: [
        dict(place="Västra infarten", styrka="1", slag="Skåpbil",
             activity="Stannade en kort stund utanför infarten innan den körde vidare",
             symbol=VAN_SYMBOL, reg_nr="QAB456",
             next_steps="Fortsatt uppmärksamhet vid infarten", image=IMG_VAN),
        dict(place="Skogsbrynet vid förrådet", styrka="1", slag="Person",
             activity="Fotograferade stängslet under en längre stund",
             symbol=PERSON_SYMBOL, reg_nr=None,
             next_steps="Noterat, fortsatt uppmärksamhet", image=IMG_PERSON),
    ],
    6: [
        dict(place="Huvudentrén", styrka="1", slag="Person",
             activity="Observerade entrén och antecknade i ett block",
             symbol=PERSON_SYMBOL, reg_nr=None,
             next_steps="Bildmaterial sparat, fortsatt bevakning", image=IMG_PERSON),
        dict(place="Södra hörnet av stängslet", styrka="-", slag="Skadegörelse",
             activity="Klippt stängsel upptäckt vid kvällsrond",
             symbol="Hål i stängslet, ca 1 meter", reg_nr=None,
             next_steps="Teknisk avdelning kontaktad, ökad bevakning av området",
             image=IMG_CUT_FENCE),
    ],
    7: [
        dict(place="Bortre parkeringen", styrka="1", slag="Beväpnad person",
             activity="Siktades kortvarigt innan personen försvann in i skogen",
             symbol="Bar vad som såg ut som ett handeldvapen", reg_nr=None,
             next_steps="Polis underrättad, skärpt bevakning", image=IMG_ARMED_SINGLE),
    ],
    8: [
        dict(place="Vid transformatorstationen", styrka="1", slag="Skåpbil",
             activity="Parkerad en stund innan den körde iväg",
             symbol=VAN_SYMBOL, reg_nr="QAB456",
             next_steps="Fortsatt övervakning, jämförs med tidigare observationer"),
    ],
    9: [
        dict(place="Norra skogsbrynet", styrka="2", slag="Beväpnade personer",
             activity="Avvek söderut mot allmän väg vid upptäckt",
             symbol="En av personerna bar ett gevär", reg_nr=None,
             next_steps="Polis underrättad, förhöjd beredskap", image=IMG_ARMED_PAIR),
        dict(place="Huvudentrén", styrka="1", slag="Person",
             activity="Fotograferade kameror och belysning vid entrén",
             symbol=PERSON_SYMBOL, reg_nr=None,
             next_steps="Bildmaterial sparat, förhöjd beredskap", image=IMG_PERSON),
    ],
    10: [
        dict(place="Förrådsbyggnaden", styrka="-", slag="Skadegörelse",
             activity="Uppbrutet lås vid förrådets bakdörr upptäckt",
             symbol="Skador på dörrkarm", reg_nr=None,
             next_steps="Teknisk avdelning kontaktad, förhöjd beredskap kvarstår",
             image=IMG_BROKEN_LOCK),
        dict(place="Kajen", styrka="1", slag="Skåpbil",
             activity="Sågs lasta okänt gods innan den körde iväg",
             symbol=VAN_SYMBOL, reg_nr="QAB456",
             next_steps="Polis underrättad", image=IMG_VAN),
    ],
}


def _render_block(day: int, reporter: str, tnr: str, event: dict) -> str:
    """Slag/Sysselsättning/Symbol/Sedan are only included when the event
    actually has something to say there -- same treatment Reg.Nr already
    got. Every noise/signal (human-reported) event fills all of these in,
    but the sensor-gateway events (see _tripwire_event/_motion_event/
    _camera_event) leave Slag/Symbol/Sedan blank -- a bare-bones automated
    integration, not a guard writing prose about what/why -- and give
    Sysselsättning only the same generic SENSOR_ACTIVITY line every time."""
    lines = [
        "Till: Stabsassistent",
        f"Från: {reporter}",
        f"TNR: {tnr}",
        f"Stund: {tnr}",
        f"Ställe: {event['place']}",
        f"Styrka: {event['styrka']}",
    ]
    if event.get("slag"):
        lines.append(f"Slag: {event['slag']}")
    if event.get("activity"):
        lines.append(f"Sysselsättning: {event['activity']}")
    if event.get("symbol"):
        lines.append(f"Symbol: {event['symbol']}")
    if event.get("reg_nr"):
        lines.append(f"Reg.Nr: {event['reg_nr']}")
    lines.append(f"Sagesman: {reporter}")
    if event.get("next_steps"):
        lines.append(f"Sedan: {event['next_steps']}")
    return "\n".join(lines)


def generate_day(
    day: int, rng: random.Random, guard: _NoiseClusterGuard
) -> tuple[str, list[dict]]:
    """Returns the day's report text plus a list of {"tnr", "image"}
    entries for whichever events (always signal events, never noise) had
    an "image" key -- the caller writes these out as event_images.json,
    a side-channel manifest read by webapp/routes.py's
    import_training_day the same way it already reads adjacent_status.json,
    so a report's own TNR is what ties it back to its picture after import."""
    signal = SIGNAL_EVENTS.get(day, [])
    noise_count = EVENTS_PER_DAY - len(signal)
    events = [_noise_event(rng, guard) for _ in range(noise_count)] + list(signal)

    timed = [(_rand_time(rng, day), event) for event in events]
    timed.sort(key=lambda pair: pair[0])

    blocks = []
    images = []
    for tnr, event in timed:
        reporter = rng.choice(GUARDS)
        positioned = {**event, "place": _place_with_position(day, tnr, event["place"])}
        blocks.append(_render_block(day, reporter, tnr, positioned))
        if event.get("image"):
            images.append({"tnr": tnr, "image": event["image"]})

    return "\n\n---\n\n".join(blocks) + "\n", images


# Automated sensor-trigger events -- a separate, optional layer on top of
# the human-reported story above. Written to their own per-day file
# (dag_NN_sensor.txt) rather than mixed into dag_NN.txt, so the "Inkludera
# sensorhändelser" toggle on Demo och övning (see webapp/routes.py's
# import_training_day) can bring them in or leave them out independently
# of the human-report story, which stays identical either way. Reported
# by SENSOR_GATEWAY rather than one of GUARDS, since these were never
# filed by a person. Deliberately bare-bones: Slag/Sysselsättning/Symbol/
# Sedan are all left blank (see _render_block's handling of that) --
# an automated gateway integration reporting "something happened at
# place X" rather than writing prose about what/why, unlike a guard's
# own report. The only place any of that ever shows up is the camera's
# capture, which cycles through car/person/deer and gets a matching
# photo attached (see demo/generate_training_images.py) -- never as text.
SENSOR_GATEWAY = "Sensorgateway"

TRIPWIRE_PLACES = [
    "Trådlarm vid Östra grinden", "Trådlarm vid Västra infarten",
    "Trådlarm bakom förrådet", "Trådlarm vid Bommen vid infarten",
]
MOTION_PLACES = [
    "Rörelsedetektor vid Skogsbrynet vid förrådet", "Rörelsedetektor vid Bortre parkeringen",
    "Rörelsedetektor vid transformatorstationen", "Rörelsedetektor vid Kajen",
]
CAMERA_PLACES = [
    "Kamera vid Huvudentrén", "Kamera vid Östra grinden",
    "Kamera vid Kajen", "Kamera vid Vaktkuren",
]
CAMERA_IMAGES = ["camera_car.png", "camera_person.png", "camera_deer.png"]

# Fixed, distinctive times (day + HHMM) rather than random ones -- keeps
# this whole layer free of any RNG dependency (genuinely "static", as
# asked for) and, since they're clustered pre-dawn/midday/evening rather
# than spread across all 1440 minutes like the human noise generator,
# they're vanishingly unlikely to collide with a human report's TNR for
# the same day (verified empirically after generation, see test_training_days.py).
SENSOR_TNR_SUFFIX = {"tripwire": "0247", "motion": "1139", "camera": "1958"}

# The one field every sensor event does fill in -- Slag/Symbol/Sedan stay
# blank regardless of sensor type, but a bare "something happened" with
# no Sysselsättning at all read as too little even for an automated
# gateway, so this generic line stands in for all three sensor types.
SENSOR_ACTIVITY = "Sensor aktiverad"


def _tripwire_event(day: int) -> dict:
    return dict(
        place=TRIPWIRE_PLACES[(day - 1) % len(TRIPWIRE_PLACES)], styrka="-", slag="",
        activity=SENSOR_ACTIVITY, symbol="", reg_nr=None, next_steps="",
    )


def _motion_event(day: int) -> dict:
    return dict(
        place=MOTION_PLACES[(day - 1) % len(MOTION_PLACES)], styrka="-", slag="",
        activity=SENSOR_ACTIVITY, symbol="", reg_nr=None, next_steps="",
    )


def _camera_event(day: int) -> tuple[dict, str]:
    """Slag/Symbol/Sedan stay blank like the other two sensor types --
    what the camera actually saw (see CAMERA_IMAGES) only shows up as
    the attached photo, not as text; Sysselsättning is the same generic
    SENSOR_ACTIVITY line every sensor event gets."""
    event = dict(
        place=CAMERA_PLACES[(day - 1) % len(CAMERA_PLACES)], styrka="1", slag="",
        activity=SENSOR_ACTIVITY, symbol="", reg_nr=None, next_steps="",
    )
    image = CAMERA_IMAGES[(day - 1) % len(CAMERA_IMAGES)]
    return event, image


def generate_sensor_day(day: int) -> tuple[str, list[dict]]:
    """A day's automated sensor-trigger events: one tripwire, one motion
    detector, one camera. Returns the day's report text plus a list of
    {"tnr", "image"} entries (only ever the camera's), in the exact same
    shape generate_day returns for its own images -- both get merged into
    the same event_images.json manifest."""
    events_by_kind = {
        "tripwire": _tripwire_event(day),
        "motion": _motion_event(day),
    }
    camera_event, camera_image = _camera_event(day)
    events_by_kind["camera"] = camera_event

    blocks = []
    images = []
    for kind, event in events_by_kind.items():
        tnr = f"{day:02d}{SENSOR_TNR_SUFFIX[kind]}"
        positioned = {**event, "place": _place_with_position(day, tnr, event["place"], always=True)}
        blocks.append(_render_block(day, SENSOR_GATEWAY, tnr, positioned))
        if kind == "camera":
            images.append({"tnr": tnr, "image": camera_image})

    return "\n\n---\n\n".join(blocks) + "\n", images


ADJACENT_UNITS = {
    "2.Kompani": {
        "offset": 1,  # one day ahead of this unit's own story -> red on day 8
        "plate": "LMN234",
        "places": [
            "Södra vägen", "Bro vid ån", "Östra utposten", "Norra skjutfältet",
            "Vid bygrinden", "Skogsvägen bakom lägret", "Ladugården",
            "Åkerkanten", "Vid vattentornet",
        ],
    },
    "3.Kompani": {
        "offset": -1,  # one day behind this unit's own story -> red on day 10
        "plate": "RST789",
        "places": [
            "Västra ängen", "Gamla stenbron", "Norra utposten",
            "Kraftledningsgatan", "Vid kolonistugorna", "Myrstigen",
            "Kvarnvägen", "Vid järnvägsövergången", "Bergsknallen",
        ],
    },
}

# One narrative per "story position" (1-10), reused for whichever calendar
# day maps to that position for a given adjacent unit (see
# build_adjacent_reports_for_day). Mirrors this unit's own green(1-3)/
# yellow(4-8)/red(9-10) rhythm: a recurring vehicle from position 3, a
# recurring person from position 5, one sabotage sign at 6, one armed
# sighting at 7 (still capped at yellow -- needs to recur), a second armed
# sighting at 9 (confirms red), reinforced by a second sabotage sign at 10.
_ADJACENT_STATUS_TEMPLATES = {
    1: "Inga avvikande iakttagelser i vårt område. Rutinbevakning genomförd "
       "enligt schema.\nBedömning: GRÖN.",
    2: "Fortsatt lugnt i vårt område. Inga återkommande mönster identifierade.\n"
       "Bedömning: GRÖN.",
    3: "Ett fordon (Reg.Nr {plate}) passerade vid {place0}, inget avvikande "
       "i övrigt.\nBedömning: GRÖN.",
    4: "Reg.Nr {plate} observerat igen, denna gång vid {place1} -- ett "
       "möjligt mönster noteras.\nBedömning: GUL.",
    5: "Reg.Nr {plate} sett en tredje gång vid {place2}. Även en person i "
       "mörka kläder observerad vid {place3}.\nBedömning: GUL.",
    6: "Återkommande fordon (Reg.Nr {plate}) och person bekräftat. Tecken "
       "på skadegörelse upptäckt vid {place4} (enstaka iakttagelse).\n"
       "Bedömning: GUL, ökad uppmärksamhet.",
    7: "En beväpnad person siktades vid {place5} (enstaka iakttagelse).\n"
       "Bedömning: fortsatt GUL -- kräver upprepning för RÖD enligt "
       "gällande hotskala.",
    8: "Fordonsmönstret (Reg.Nr {plate}) kvarstår, ytterligare observation "
       "vid {place6}.\nBedömning: GUL.",
    9: "En andra beväpnad-person-iakttagelse gjord vid {place7}.\n"
       "Bedömning: RÖD -- återkommande allvarlig indikation.",
    10: "Ytterligare tecken på sabotageförsök vid {place8}, utöver "
        "tidigare mönster med beväpnad person.\nBedömning: fortsatt RÖD "
        "-- skärpt beredskap kvarstår.",
}


def _adjacent_status_body(unit_name: str, calendar_day: int, position: int, cfg: dict) -> str:
    template = _ADJACENT_STATUS_TEMPLATES[position]
    placeholders = {"plate": cfg["plate"]}
    placeholders.update({f"place{i}": place for i, place in enumerate(cfg["places"])})
    return f"Status {unit_name}, dag {calendar_day}:\n" + template.format(**placeholders)


def build_adjacent_reports_for_day(day: int) -> list[dict]:
    """The status reports 2.Kompani and 3.Kompani would send on calendar
    `day` -- each mapped to their own "story position" (this unit's own
    green/yellow/red rhythm, shifted by their offset), so 2.Kompani hits
    red one day before this unit and 3.Kompani one day after."""
    entries = []
    for unit_name, cfg in ADJACENT_UNITS.items():
        position = min(10, max(1, day + cfg["offset"]))
        entries.append({
            "unit_name": unit_name,
            "body": _adjacent_status_body(unit_name, day, position, cfg),
        })
    return entries


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    # One guard shared across all 10 days -- the threat analysis considers
    # every imported event cumulatively (since=all), so noise generated on
    # day 9 still must not accidentally match noise from day 2. The
    # deliberate signal events (registered first) are exempt from rejection
    # -- they're *supposed* to cluster with each other -- but noise is still
    # checked against them too, so it never accidentally piggybacks on the
    # van/person/armed/sabotage storyline.
    guard = _NoiseClusterGuard()
    for events in SIGNAL_EVENTS.values():
        for event in events:
            guard.accept(event["slag"], event["symbol"])

    event_images: dict[str, list[dict]] = {}
    for day in range(1, 11):
        text, images = generate_day(day, rng, guard)
        path = OUT_DIR / f"dag_{day:02d}.txt"
        path.write_text(text, encoding="utf-8")
        if images:
            event_images[str(day)] = images
        print(f"Wrote {path} ({text.count('---') + 1} reports, {len(images)} with an image)")

    adjacent_data = {str(day): build_adjacent_reports_for_day(day) for day in range(1, 11)}
    adjacent_path = OUT_DIR / "adjacent_status.json"
    adjacent_path.write_text(
        json.dumps(adjacent_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {adjacent_path} (2 adjacent units x 10 days)")

    # Sensor events are a separate optional file per day (see
    # generate_sensor_day's docstring) -- generated with no RNG at all, so
    # this loop can never perturb the human-report generation above it,
    # regardless of order.
    for day in range(1, 11):
        sensor_text, sensor_images = generate_sensor_day(day)
        sensor_path = OUT_DIR / f"dag_{day:02d}_sensor.txt"
        sensor_path.write_text(sensor_text, encoding="utf-8")
        if sensor_images:
            event_images.setdefault(str(day), []).extend(sensor_images)
        print(f"Wrote {sensor_path} ({len(sensor_images)} with an image)")

    images_path = OUT_DIR / "event_images.json"
    images_path.write_text(json.dumps(event_images, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {images_path} ({sum(len(v) for v in event_images.values())} events with images)")


if __name__ == "__main__":
    main()
