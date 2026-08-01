"""Best-effort, fully offline extraction of the 8 report fields from free
text. This is heuristic (regex/keyword based, no ML, no network) and is
expected to be wrong or incomplete sometimes -- every parsed event is stored
with needs_review=True so a human confirms or corrects it in the web UI
before it goes into a report.

Messages written in the Swedish military "7S rapport" template (labeled
lines: Till/Från/TNR/Stund/Ställe/Styrka/Slag/Sysselsättning/Symbol/
Reg.Nr/Sagesman/Sedan) are recognized and mapped directly instead of going
through the generic heuristics, since the labels already tell us exactly
which field is which. Some report-generating tools (e.g. 7srapport.com,
confirmed by generating a real sample) combine Styrka/Slag/Sysselsättning
into one free-text "Händelse" field instead -- that's recognized too, with
count/object/activity recovered from it via the same best-effort heuristics
the non-7S path below uses. A field's value is also treated as absent if
it's a lone "-", since some tools emit that for a blank optional field
rather than omitting the line.

If an MGRS grid reference (e.g. "33VVN1234567890") appears in the Ställe
field or the message body, it's converted to lat/lon via coordinates.py and
stored alongside the other fields -- see that module for details. A human
can always override this by pin-dropping a position on the event page.
"""

from __future__ import annotations

import re
from typing import Optional

from .coordinates import extract_position

_7S_LABELS = [
    "Till", "Från", "TNR", "Stund", "Ställe", "Styrka", "Slag",
    "Sysselsättning", "Händelse", "Symbol", "Reg.Nr", "Sagesman", "Sedan",
]
_7S_LINE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(label) for label in _7S_LABELS) + r")\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_7S_MIN_LABELS_MATCHED = 3


def _extract_7s_labels(text: str) -> Optional[dict[str, str]]:
    found: dict[str, str] = {}
    for match in _7S_LINE_RE.finditer(text):
        canonical = next(l for l in _7S_LABELS if l.lower() == match.group(1).lower())
        value = match.group(2).strip()
        # A lone "-" marks a field the sender left blank (e.g. 7srapport.com
        # emits "Sedan: -" for its optional field rather than omitting the
        # line) -- treat that as not actually present, both so callers see
        # None instead of a literal dash, and so a report with only blank
        # optional fields filled in doesn't count towards
        # _7S_MIN_LABELS_MATCHED.
        if value and value != "-":
            found[canonical] = value
    if len(found) < _7S_MIN_LABELS_MATCHED:
        return None
    return found


def _map_7s_fields(labels: dict[str, str], reported_by: Optional[str]) -> dict:
    marks_parts = [
        p for p in [
            labels.get("Symbol"),
            f"Reg.nr {labels['Reg.Nr']}" if labels.get("Reg.Nr") else None,
        ] if p
    ]
    count, obj, activity = labels.get("Styrka"), labels.get("Slag"), labels.get("Sysselsättning")
    handelse = labels.get("Händelse")
    if handelse and not (count or obj or activity):
        # Some 7S-generating tools (e.g. 7srapport.com) combine the three
        # S:en -- Styrka/Slag/Sysselsättning -- into one free-text
        # "Händelse" field instead of three separate labeled lines, since
        # reliably splitting a natural sentence into those three parts
        # isn't something a human writer can be expected to do either.
        # Recover count/object/activity from it with the same best-effort
        # heuristics the plain (non-7S) fallback path already uses below,
        # rather than silently losing the actual substance of the report.
        heuristic_count, heuristic_object = extract_count_and_object(handelse)
        count, obj = count or heuristic_count, obj or heuristic_object
        activity = activity or extract_activity(handelse) or handelse
    return {
        "event_time": labels.get("Stund"),
        "place": labels.get("Ställe"),
        "count": count,
        "object": obj,
        "activity": activity,
        "marks": ", ".join(marks_parts) or None,
        # "Sagesman" (informant/source) is who actually observed and is
        # vouching for the report -- what "Rapporterad av" is meant to
        # capture -- which can genuinely differ from "Från" (who relayed
        # this particular Signal message, e.g. a duty officer passing on
        # someone else's account) or from the Signal sender (whoever's
        # phone/account it was sent from). Prefer it when the message
        # actually names one; "Från" is still a message-content value
        # worth falling back to if Sagesman is missing, before finally
        # falling back to the Signal sender itself.
        "reported_by": labels.get("Sagesman") or labels.get("Från") or reported_by,
        "next_steps": labels.get("Sedan"),
        "needs_review": True,
    }

# Day-Hour-Minute date-time-group, e.g. "300631" = day 30, 06:31 -- the
# format used in the "TNR"/"Stund" fields of 7S-style military reports.
# Validated (day 01-31, hour 00-23, minute 00-59) so it doesn't swallow
# arbitrary 6-digit numbers (phone fragments, serials, ...).
_DDHHMM_RE = re.compile(r"(?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])[0-5]\d")

_TIME_RE = re.compile(
    r"""
    \b(
        [01]?\d[:.h][0-5]\d          # 14:30 / 2.30 / 14h30
        (?:\s*(?:am|pm))?
        |
        \d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2})?   # 2026-07-23 14:30
        |
        (?:today|yesterday|tonight)\b(?:\s+at\s+[01]?\d[:.h][0-5]\d)?
        |
        (?:0[1-9]|[12]\d|3[01])(?:[01]\d|2[0-3])[0-5]\d   # 300631 (DDHHMM / TNR-style)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

_PLACE_RE = re.compile(
    r"\b(?:at|near|by|in|along|outside|vid|nära|utanför|i|längs)\s+"
    r"((?:the\s+)?[A-Za-zÅÄÖåäö][\w'-]*(?:\s+[A-Za-zÅÄÖåäö][\w'-]*){0,3}|"
    r"(?:grid\s+)?[A-Z]{0,2}\d{2,10}(?:\s?[A-Z]{0,2}\d{0,10})?)",
)

_COUNT_OBJECT_RE = re.compile(
    r"(?<![:.\d])\b"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|dozen|several|few|multiple)\s+"
    # [A-Za-zÅÄÖåäö], not [a-z] -- otherwise a Swedish word starting with
    # å/ä/ö, or containing one (t.ex. "grå", "skåpbil"), gets silently
    # truncated at the first non-ASCII letter (a real bug hit importing a
    # 7srapport.com-style report with "1 grå skåpbil ..." -- came back as
    # object "gr" instead of "grå").
    r"([A-Za-zÅÄÖåäö]+(?:-[A-Za-zÅÄÖåäö]+)?)",
    re.IGNORECASE,
)

_COUNT_STOPWORDS = {
    "at", "am", "pm", "o'clock", "hours", "hour", "minutes", "minute",
    "near", "in", "on", "by", "along", "outside", "the", "seen", "from", "to",
}

_ACTIVITY_KEYWORDS = [
    "moving", "walking", "driving", "traveling", "travelling", "gathering",
    "digging", "patrolling", "standing", "running", "loading", "unloading",
    "meeting", "waiting", "entering", "leaving", "building", "setting up",
    "parked", "parking", "approaching", "departing", "arriving", "observed",
    "camping", "unloading", "constructing", "assembling", "dispersing",
    # Swedish
    "rör sig", "går in", "går", "kör", "gräver", "patrullerar", "står",
    "springer", "lastar av", "lastar", "samlas", "möts", "väntar",
    "lämnar", "bygger", "sätter upp", "parkerad", "parkerar",
    "närmar sig", "avgår", "anländer", "observerad", "campar",
    "monterar", "sprider sig",
]

_MARKS_TRIGGERS = [
    "wearing", "marked with", "markings", "marking", "logo", "flag",
    "uniform", "insignia", "license plate", "plate number", "patch",
    "camouflage", "camo", "armband", "symbol",
    # Swedish
    "klädd i", "bär", "märkt med", "märkning", "markering", "logga",
    "flagga", "beteckning", "registreringsskylt", "regnr", "kamouflage",
    "armbindel",
]

_NEXT_STEPS_TRIGGERS = [
    "recommend", "recommendation", "request", "requesting", "will",
    "plan to", "planning to", "next step", "suggest", "advise", "advising",
    # Swedish
    "rekommenderar", "rekommendation", "begär", "begäran", "kommer att",
    "planerar att", "avser att", "nästa steg", "föreslår", "råder",
]

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _find_trigger_clause(text: str, triggers: list[str]) -> Optional[str]:
    lowered = text.lower()
    for trigger in triggers:
        # Word-boundary match, not substring: plain find() let short
        # triggers like "flag" match inside unrelated words (e.g. the
        # Swedish "kamouflage"), cutting the clause off mid-word.
        match = re.search(r"\b" + re.escape(trigger) + r"\b", lowered)
        if match is None:
            continue
        idx = match.start()
        # Take from the trigger to the end of that sentence.
        rest = text[idx:]
        end = re.search(r"[.!?\n]", rest)
        clause = rest[: end.start()] if end else rest
        clause = clause.strip(" .,;:")
        if clause:
            return clause
    return None


def extract_time(text: str) -> Optional[str]:
    match = _TIME_RE.search(text)
    return match.group(1).strip() if match else None


def extract_place(text: str) -> Optional[str]:
    match = _PLACE_RE.search(text)
    return match.group(1).strip() if match else None


def extract_count_and_object(text: str) -> tuple[Optional[str], Optional[str]]:
    for match in _COUNT_OBJECT_RE.finditer(text):
        count, obj = match.group(1), match.group(2).strip()
        if obj.lower() in _COUNT_STOPWORDS:
            continue
        # Skip matches that are actually part of a time expression, e.g. "14:30".
        if re.match(r"^\d+$", count) and obj.lower() in {"am", "pm"}:
            continue
        # Skip a DDHHMM date-time-group (e.g. "300631") mistaken for a count.
        if len(count) == 6 and _DDHHMM_RE.fullmatch(count):
            continue
        return count, obj
    return None, None


def extract_activity(text: str) -> Optional[str]:
    return _find_trigger_clause(text, _ACTIVITY_KEYWORDS)


def extract_marks(text: str) -> Optional[str]:
    return _find_trigger_clause(text, _MARKS_TRIGGERS)


def extract_next_steps(text: str) -> Optional[str]:
    return _find_trigger_clause(text, _NEXT_STEPS_TRIGGERS)


def parse_event_fields(text: str, reported_by: Optional[str] = None) -> dict:
    """Extract the 8 report elements from free text. For a 7S-labeled
    message, "Rapporterad av" prefers whoever the message itself names as
    "Sagesman" (or "Från" if that's all it gives), since the report body
    is the more specific, authoritative source for who's actually vouching
    for it -- `reported_by` (normally the Signal message sender) is only
    the fallback for when the message doesn't say. For anything that
    doesn't match the 7S template, there's no reliable field to read this
    from, so it's always just `reported_by`, since people rarely name
    themselves in ordinary free-text report bodies."""
    text = text or ""

    labels = _extract_7s_labels(text)
    if labels is not None:
        fields = _map_7s_fields(labels, reported_by)
        fields["raw_text"] = text
        latlon = extract_position(labels.get("Ställe")) or extract_position(text)
        if latlon is not None:
            fields["lat"], fields["lon"] = latlon
        return fields

    count, obj = extract_count_and_object(text)
    place = extract_place(text)
    fields = {
        "event_time": extract_time(text),
        "place": place,
        "count": count,
        "object": obj,
        "activity": extract_activity(text),
        "marks": extract_marks(text),
        "reported_by": reported_by,
        "next_steps": extract_next_steps(text),
        "raw_text": text,
        "needs_review": True,
    }
    latlon = extract_position(place) or extract_position(text)
    if latlon is not None:
        fields["lat"], fields["lon"] = latlon
    return fields
