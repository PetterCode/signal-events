"""Consolidated analysis across all stored events: looks for the same
vehicle or person recurring across multiple reports, and produces a
heuristic, rule-based threat-level recommendation (green/yellow/red) for
the guarded site.

This is entirely offline (regex + set similarity, no ML, no network) and
is decision support, not a verdict: every score component is listed with
the evidence behind it (which events, which groups) so a human can check
the reasoning, the same way every parsed field elsewhere in this tool is
flagged for review rather than trusted blindly.

RED is reserved for *recurring* threats of violent action -- repeated
sightings of armed individuals, repeated discovery of explosive devices,
or repeated signs of attempted sabotage (or similar) -- rather than being
reachable purely by accumulating recurrence points, or by a single
one-off report in any of those categories. A recurring vehicle or person,
however suspicious-looking, caps out at YELLOW: recurrence of an ordinary
pattern is a reason to pay attention, not a confirmed severe threat. A
single, unrepeated report of a weapon/explosive/sabotage attempt also
caps out at YELLOW -- serious enough to flag, but not yet a *pattern* of
violent intent. This mirrors how a human analyst would triage: volume of
low-grade pattern data shouldn't outrank real severity, but a single
ambiguous report shouldn't trigger the highest alert level either.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace

from . import naming

# [ \t]* (not \s*) between tokens so this never crosses a newline and
# swallows the next line's label when the Reg.Nr field itself is blank
# (a real case seen in imported data: "Reg.Nr: \nSagesman: ALFA").
_PLATE_RE = re.compile(
    r"reg\.?[ \t]*nr\.?[ \t]*:?[ \t]*([A-ZÅÄÖ0-9-]{2,15})", re.IGNORECASE
)

_STOPWORDS = {
    "och", "vid", "med", "på", "en", "ett", "den", "det", "som", "för",
    "the", "and", "with", "near", "at", "a", "an", "of", "in", "on",
}

_SUSPICIOUS_KEYWORDS = [
    "fotograf", "filmar", "filmning", "tittar mot", "tittar på", "spanar",
    "avvaktar", "väntar", "återkommer", "kör förbi", "cirklar", "observerar",
    "antecknar", "noterar", "smyger", "gömmer", "undviker",
    "photograph", "films", "filming", "watch", "loiter", "returns",
    "circles", "observing", "monitors", "hides", "avoids",
]

_WEAPON_KEYWORDS = [
    "vapen", "beväpnad", "beväpnat", "beväpnade", "pistol", "gevär",
    "automatkarbin", "kpist", "skjutvapen", "handeldvapen",
    "armed", "weapon", "weapons", "rifle", "handgun", "firearm",
]
_EXPLOSIVE_KEYWORDS = [
    "sprängladdning", "sprängämne", "sprängmedel", "bomb", "explosiv",
    "explosivt", "misstänkt paket", "misstänkt föremål", "ied",
    "explosive", "explosives",
]
_SABOTAGE_KEYWORDS = [
    "sabotage", "saboterad", "saboterat", "sabotera", "skadegörelse",
    "vandalisering", "vandaliserat", "vandaliserad", "manipulerat",
    "manipulerad", "manipulation av", "forcerat", "forcerad",
    "uppbruten", "uppbrutet", "brutet lås", "klippt stängsel",
    "klippt staket", "avskuret staket", "genomskuren", "kapad kabel",
    "kapat larm", "skadad utrustning", "intrångsförsök", "inbrottsförsök",
    "tillträdesförsök",
    "sabotage", "tampering", "tampered", "cut fence", "cut wire",
    "forced entry", "break-in attempt", "intrusion attempt", "vandalism",
    "vandalized",
]
_THREAT_KEYWORDS = [
    "hot om våld", "hotade med", "hotat med", "hotfullt uttalande",
    "dödshot", "skriftligt hot", "verbalt hot", "ska döda", "ska skada",
    "kommer att döda", "kommer att skada", "utpressning",
    "threat of violence", "threatened to kill", "threatened to harm",
    "death threat", "threatened with", "verbal threat",
]

# Applied uniformly to armed/explosive/sabotage sightings: RED requires the
# indicator to *recur*, not just appear once.
_MIN_SEVERE_SIGHTINGS_FOR_RED = 2

# Individually noteworthy content that gates onto "Övriga anmärkningsvärda
# observationer" even without recurring -- unlike the RED-gating categories
# above, a single occurrence is enough to be worth a human's attention here,
# it just won't ever form a RecurrenceGroup on its own since there's only
# one event. Order matters only for which label wins if an event somehow
# matches more than one category.
_NOTABLE_SINGLE_OCCURRENCE_CATEGORIES = [
    ("Hot om våld", _THREAT_KEYWORDS),
    ("Beväpnad person", _WEAPON_KEYWORDS),
    ("Misstänkt sprängladdning", _EXPLOSIVE_KEYWORDS),
    ("Tecken på sabotageförsök", _SABOTAGE_KEYWORDS),
]
_SCORE_NOTABLE_SINGLE_OCCURRENCE = 3

_VEHICLE_KEYWORDS = {
    "bil", "fordon", "lastbil", "skåpbil", "mc", "moped", "van", "truck",
    "vehicle", "car", "buss", "husbil", "motorcykel",
}
_PERSON_KEYWORDS = {
    "civil", "civila", "person", "man", "kvinna", "people", "individual",
    "fotgängare", "personer",
}

_DESC_SIMILARITY_THRESHOLD = 0.5

_SCORE_RECURRENCE_PLATE = 3
_SCORE_RECURRENCE_DESC = 2
_SCORE_PER_EXTRA_OCCURRENCE = 1
_SCORE_PER_EXTRA_PLACE = 2
_SCORE_PER_SUSPICIOUS_HIT = 2

_GREEN_MAX = 3
_YELLOW_MAX = 8
_MIN_EVENTS_FOR_ASSESSMENT = 3


@dataclass
class EventRef:
    id: int
    event_time: str | None
    place: str | None
    created_at: str


@dataclass
class RecurrenceGroup:
    label: str
    kind: str  # "plate" or "description"
    object_type: str | None
    events: list[EventRef]
    distinct_places: set[str]
    suspicious_hits: int
    score: int = 0
    reasons: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.events)


@dataclass
class ThreatAssessment:
    level: str  # "green" | "yellow" | "red"
    score: int
    reasons: list[str]
    armed_sightings: int = 0
    explosive_sightings: int = 0
    sabotage_sightings: int = 0


@dataclass
class Summary:
    total_events: int
    period_label: str
    vehicle_groups: list[RecurrenceGroup]
    person_groups: list[RecurrenceGroup]
    other_groups: list[RecurrenceGroup]
    threat: ThreatAssessment


def _extract_plate(event: sqlite3.Row) -> str | None:
    for text in (event["marks"], event["raw_text"]):
        if not text:
            continue
        match = _PLATE_RE.search(text)
        if match:
            return match.group(1).upper().replace("-", "").replace(" ", "")
    return None


def _tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    words = re.findall(r"[a-zA-ZåäöÅÄÖ0-9]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _has_suspicious_text(event: sqlite3.Row) -> bool:
    text = " ".join(
        filter(None, [event["activity"], event["next_steps"], event["raw_text"]])
    ).lower()
    return any(keyword in text for keyword in _SUSPICIOUS_KEYWORDS)


def _event_text(event: sqlite3.Row) -> str:
    return " ".join(
        filter(None, [
            event["object"], event["marks"], event["activity"],
            event["next_steps"], event["raw_text"],
        ])
    ).lower()


def _find_severe_indicators(events: list[sqlite3.Row]) -> tuple[int, int, int, list[str]]:
    """Scans every event -- not just recurring groups, since these
    indicators are about content, not correlation -- for the three
    categories that gate RED (when they *recur*): armed persons,
    explosive devices, and signs of attempted sabotage. Returns
    (armed_sightings, explosive_sightings, sabotage_sightings, reasons)."""
    def _tnr(e: sqlite3.Row) -> str:
        return naming.event_tnr(e["created_at"])

    armed = [e for e in events if any(k in _event_text(e) for k in _WEAPON_KEYWORDS)]
    explosive = [e for e in events if any(k in _event_text(e) for k in _EXPLOSIVE_KEYWORDS)]
    sabotage = [e for e in events if any(k in _event_text(e) for k in _SABOTAGE_KEYWORDS)]

    reasons = []
    if armed:
        reasons.append(
            f"Beväpnad(e) person(er) rapporterad(e) i {len(armed)} "
            f"rapport(er): {', '.join(f'Händelse {_tnr(e)}' for e in armed)}"
        )
    if explosive:
        reasons.append(
            f"Misstänkt sprängladdning/föremål rapporterat i "
            f"{len(explosive)} rapport(er): "
            f"{', '.join(f'Händelse {_tnr(e)}' for e in explosive)}"
        )
    if sabotage:
        reasons.append(
            f"Tecken på sabotageförsök rapporterat i {len(sabotage)} "
            f"rapport(er): {', '.join(f'Händelse {_tnr(e)}' for e in sabotage)}"
        )
    return len(armed), len(explosive), len(sabotage), reasons


def _classify_object_type(object_type: str) -> str:
    ot = object_type.lower()
    if any(k in ot for k in _VEHICLE_KEYWORDS):
        return "vehicle"
    if any(k in ot for k in _PERSON_KEYWORDS):
        return "person"
    return "other"


def _make_group(label: str, kind: str, events: list[sqlite3.Row], object_type: str | None) -> RecurrenceGroup:
    places = {e["place"] for e in events if e["place"]}
    suspicious_hits = sum(1 for e in events if _has_suspicious_text(e))
    return RecurrenceGroup(
        label=label,
        kind=kind,
        object_type=object_type,
        events=[
            EventRef(e["id"], e["event_time"], e["place"], e["created_at"])
            for e in events
        ],
        distinct_places=places,
        suspicious_hits=suspicious_hits,
    )


def _group_by_plate(events: list[sqlite3.Row]) -> tuple[list[RecurrenceGroup], list[sqlite3.Row]]:
    by_plate: dict[str, list[sqlite3.Row]] = defaultdict(list)
    unmatched: list[sqlite3.Row] = []
    for event in events:
        plate = _extract_plate(event)
        if plate:
            by_plate[plate].append(event)
        else:
            unmatched.append(event)

    groups = []
    for plate, evs in by_plate.items():
        if len(evs) < 2:
            unmatched.extend(evs)
            continue
        groups.append(_make_group(f"Reg.nr {plate}", "plate", evs, object_type=None))
    return groups, unmatched


def _group_by_description(events: list[sqlite3.Row]) -> list[RecurrenceGroup]:
    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for event in events:
        key = (event["object"] or "").strip().lower() or "okänt"
        buckets[key].append(event)

    groups: list[RecurrenceGroup] = []
    for object_type, evs in buckets.items():
        clusters: list[list[sqlite3.Row]] = []
        cluster_tokens: list[set[str]] = []
        for event in evs:
            tokens = _tokenize(event["marks"]) | _tokenize(event["object"])
            placed = False
            for i, existing in enumerate(cluster_tokens):
                if _jaccard(tokens, existing) >= _DESC_SIMILARITY_THRESHOLD:
                    clusters[i].append(event)
                    cluster_tokens[i] = existing | tokens
                    placed = True
                    break
            if not placed:
                clusters.append([event])
                cluster_tokens.append(tokens)

        for cluster in clusters:
            if len(cluster) < 2:
                continue
            sample = cluster[0]["marks"] or cluster[0]["object"] or object_type
            groups.append(
                _make_group(
                    f"{object_type.capitalize()}: {sample}", "description", cluster,
                    object_type=object_type,
                )
            )
    return groups


def _build_notable_observation_groups(
    events: list[sqlite3.Row], grouped_event_ids: set[int]
) -> list[RecurrenceGroup]:
    """Individually noteworthy observations that don't recur -- a single
    mention of a threat of violence, an armed person, a suspected
    explosive, or a sabotage sign -- so they still surface in "Övriga
    anmärkningsvärda observationer" instead of only being tallied in the
    top-level motivering. Skips any event already shown via a recurring
    vehicle/person/other group, so nothing is ever listed twice."""
    groups: list[RecurrenceGroup] = []
    for event in events:
        if event["id"] in grouped_event_ids:
            continue
        text = _event_text(event)
        for label, keywords in _NOTABLE_SINGLE_OCCURRENCE_CATEGORIES:
            if any(keyword in text for keyword in keywords):
                excerpt = event["object"] or event["marks"] or event["place"] or "okänd händelse"
                groups.append(RecurrenceGroup(
                    label=f"{label}: {excerpt}",
                    kind="notable",
                    object_type=None,
                    events=[EventRef(event["id"], event["event_time"], event["place"], event["created_at"])],
                    distinct_places=set(filter(None, [event["place"]])),
                    suspicious_hits=0,
                    score=_SCORE_NOTABLE_SINGLE_OCCURRENCE,
                    reasons=[
                        "Enstaka observation, inte en återkommande grupp — "
                        "bedöms ändå anmärkningsvärd nog att lyftas fram."
                    ],
                ))
                break
    return groups


def _score_group(group: RecurrenceGroup) -> None:
    """Fills in group.score and group.reasons in place."""
    points = _SCORE_RECURRENCE_PLATE if group.kind == "plate" else _SCORE_RECURRENCE_DESC
    match_basis = "registreringsnummer" if group.kind == "plate" else "liknande beskrivning"
    reasons = [f"Återkommer {group.count} gånger ({match_basis})"]

    extra_occurrences = max(0, group.count - 2)
    if extra_occurrences:
        points += extra_occurrences * _SCORE_PER_EXTRA_OCCURRENCE

    if len(group.distinct_places) > 1:
        points += (len(group.distinct_places) - 1) * _SCORE_PER_EXTRA_PLACE
        reasons.append(f"Observerad på {len(group.distinct_places)} olika platser")

    if group.suspicious_hits:
        points += group.suspicious_hits * _SCORE_PER_SUSPICIOUS_HIT
        reasons.append(f"{group.suspicious_hits} observation(er) med spaningsliknande beteende")

    group.score = points
    group.reasons = reasons


def _assess_threat(
    all_groups: list[RecurrenceGroup],
    total_events: int,
    armed_sightings: int,
    explosive_sightings: int,
    sabotage_sightings: int,
    severe_reasons: list[str],
) -> ThreatAssessment:
    # A confirmed, *recurring* severe indicator overrides everything else --
    # it doesn't need a minimum report count to matter, but a single,
    # unrepeated report in any of these categories isn't enough for RED on
    # its own (see module docstring).
    any_severe_hit = bool(armed_sightings or explosive_sightings or sabotage_sightings)
    confirmed_severe = (
        armed_sightings >= _MIN_SEVERE_SIGHTINGS_FOR_RED
        or explosive_sightings >= _MIN_SEVERE_SIGHTINGS_FOR_RED
        or sabotage_sightings >= _MIN_SEVERE_SIGHTINGS_FOR_RED
    )

    if total_events < _MIN_EVENTS_FOR_ASSESSMENT and not confirmed_severe:
        reasons = severe_reasons or [
            f"Endast {total_events} rapport(er) i underlaget — för få för att bedöma mönster."
        ]
        level = "yellow" if any_severe_hit else "green"
        return ThreatAssessment(
            level, 0, reasons, armed_sightings, explosive_sightings, sabotage_sightings
        )

    total_score = sum(g.score for g in all_groups)
    reasons = list(severe_reasons) + [f"{g.label}: {'; '.join(g.reasons)}" for g in all_groups]

    if confirmed_severe:
        level = "red"
    elif total_score > _YELLOW_MAX:
        # High recurrence/pattern score alone is capped at YELLOW: RED is
        # reserved for *recurring* armed-person/explosive/sabotage
        # indicators above, not pattern volume by itself.
        level = "yellow"
        reasons.append(
            "Mönsterpoängen är hög, men RÖD kräver återkommande allvarliga "
            "indikationer (upprepade beväpnade personer, upprepad "
            "misstänkt sprängladdning, eller upprepade tecken på "
            "sabotageförsök) — manuell bedömning rekommenderas ändå."
        )
    elif total_score > _GREEN_MAX or any_severe_hit:
        level = "yellow"
    else:
        level = "green"

    if not reasons:
        reasons = ["Inga återkommande fordon eller personer identifierade i underlaget."]

    return ThreatAssessment(
        level, total_score, reasons, armed_sightings, explosive_sightings, sabotage_sightings
    )


def build_summary(events: list[sqlite3.Row], period_label: str) -> Summary:
    events = list(events)
    plate_groups, remaining = _group_by_plate(events)
    desc_groups = _group_by_description(remaining)

    vehicle_groups: list[RecurrenceGroup] = list(plate_groups)
    person_groups: list[RecurrenceGroup] = []
    other_groups: list[RecurrenceGroup] = []

    for group in desc_groups:
        kind = _classify_object_type(group.object_type or "")
        if kind == "vehicle":
            vehicle_groups.append(group)
        elif kind == "person":
            person_groups.append(group)
        else:
            other_groups.append(group)

    for group in vehicle_groups + person_groups + other_groups:
        _score_group(group)

    # Individually noteworthy events (a threat of violence, an armed
    # person, ...) that don't recur, so they never formed a group above --
    # added to "Övriga" alongside genuine recurring clusters, but scored
    # and labeled separately (see _build_notable_observation_groups).
    grouped_event_ids = {
        ref.id for g in (vehicle_groups + person_groups + other_groups) for ref in g.events
    }
    other_groups.extend(_build_notable_observation_groups(events, grouped_event_ids))

    all_groups = vehicle_groups + person_groups + other_groups

    vehicle_groups.sort(key=lambda g: -g.count)
    person_groups.sort(key=lambda g: -g.count)
    other_groups.sort(key=lambda g: -g.count)

    armed_sightings, explosive_sightings, sabotage_sightings, severe_reasons = (
        _find_severe_indicators(events)
    )
    threat = _assess_threat(
        all_groups, len(events), armed_sightings, explosive_sightings,
        sabotage_sightings, severe_reasons,
    )

    return Summary(
        total_events=len(events),
        period_label=period_label,
        vehicle_groups=vehicle_groups,
        person_groups=person_groups,
        other_groups=other_groups,
        threat=threat,
    )


def apply_threat_override(summary: Summary, override: dict | None) -> Summary:
    """Layers a human-set override (see db.get_threat_override) on top of
    the automatic assessment for display and export. The override
    replaces the *level* only -- score and the automatic reasons are kept
    and a note explaining the override is prepended to them, so a human
    correction is never presented as if it silently erased the
    rule-based analysis behind it; both are always visible together."""
    if override is None:
        return summary
    labels = {"green": "GRÖN", "yellow": "GUL", "red": "RÖD"}
    override_label = labels.get(override["level"], override["level"].upper())
    auto_label = labels.get(summary.threat.level, summary.threat.level.upper())
    note = (
        f"Manuellt satt till {override_label} "
        f"(automatisk bedömning: {auto_label}, poäng {summary.threat.score})."
    )
    if override["notes"]:
        note += f" Anteckning: {override['notes']}"
    threat = replace(summary.threat, level=override["level"], reasons=[note, *summary.threat.reasons])
    return replace(summary, threat=threat)


# Ordered most-to-least severe: an adjacent unit's status report is free
# text, not a structured field, so if it mentions more than one level
# (e.g. summarizing a recent change, "läget har lugnat ned sig, från GUL
# till GRÖN") the more severe one wins rather than whichever happens to
# appear first -- consistent with this app's own rule that RED requires
# no benefit of the doubt.
_ADJACENT_LEVEL_KEYWORDS = [("röd", "red"), ("gul", "yellow"), ("grön", "green")]


def parse_adjacent_level(body: str | None) -> str | None:
    """Best-effort extraction of a GRÖN/GUL/RÖD level from an adjacent
    unit's free-text status report for the header status strip -- these
    arrive as plain prose (see demo/generate_training_days.py's own
    "Bedömning: ..." convention, which real adjacent units aren't
    guaranteed to follow), not a structured field like this unit's own
    threat level. Prefers a line that actually starts with "Bedömning"
    if there is one (the clearest, most deliberate signal), otherwise
    falls back to the most severe level keyword mentioned anywhere in
    the body. Returns None -- not a guess -- when nothing matches at
    all, so the caller can show "okänd" rather than a fabricated level."""
    if not body:
        return None
    for line in body.splitlines():
        stripped_lower = line.strip().lower()
        if stripped_lower.startswith("bedömning"):
            for keyword, level in _ADJACENT_LEVEL_KEYWORDS:
                if keyword in stripped_lower:
                    return level
    lowered = body.lower()
    for keyword, level in _ADJACENT_LEVEL_KEYWORDS:
        if keyword in lowered:
            return level
    return None
