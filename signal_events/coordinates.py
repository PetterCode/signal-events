"""Best-effort extraction of a lat/lon position from report text, in
whichever of a handful of common formats a human happened to type it in --
normally inside the 7S report's "Ställe" field (e.g. "Ställe:
33VVN1234567890" or "vid 33V VN 12345 67890"). Conversion is done fully
offline (the `mgrs` package -- a Python binding for NGA's GeoTrans -- for
MGRS, plain arithmetic for the rest), no network involved.

MGRS (Military Grid Reference System) is still the format the app itself
displays a position in (see to_mgrs()) and the first one tried here, but
extract_position() also recognizes decimal degrees ("59.3269, 18.0717"),
degrees/minutes/seconds ("59°19'37\"N 18°04'18\"E"), and degrees/decimal
minutes ("59°19.617'N 18°04.300'E") -- other formats a report might
reasonably use instead, tried in that order, first match wins.

This is a convenience auto-fill, not the only way to get a position onto an
event -- a human can always place or move the pin manually on the event page
in the web UI, which always wins over whatever this module extracted (see
routes.py's event_detail POST handler).

to_mgrs() is the reverse conversion, used to show a plain decimal-degree
position (e.g. Kartcentrum) as its MGRS grid reference too."""

from __future__ import annotations

import re
import warnings
from typing import Optional

import mgrs as _mgrs

_MGRS_RE = re.compile(
    r"\b\d{1,2}[C-HJ-NP-X]\s?[A-HJ-NP-Z]{2}\s?\d{2,5}\s?\d{2,5}\b",
    re.IGNORECASE,
)

# Degrees/minutes/seconds, e.g. 59°19'37"N 18°04'18"E or N59°19'37"
# 18°04'18"E -- tried before DDM below since its mandatory seconds group
# is what tells the two apart (a DDM string has no seconds component at
# all). The direction letter can lead or trail each number; at least one
# of the two must be present (see _direction()).
_DMS_RE = re.compile(
    r"(?P<latdir1>[NSns])?\s*(?P<latdeg>\d{1,2})\s*°\s*(?P<latmin>\d{1,2})\s*['′’]\s*"
    r"(?P<latsec>\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')\s*(?P<latdir2>[NSns])?"
    r"[,;\s]+"
    r"(?P<londir1>[EWew])?\s*(?P<londeg>\d{1,3})\s*°\s*(?P<lonmin>\d{1,2})\s*['′’]\s*"
    r"(?P<lonsec>\d{1,2}(?:\.\d+)?)\s*(?:\"|″|'')\s*(?P<londir2>[EWew])?"
)

# Degrees/decimal minutes, e.g. 59°19.617'N 18°04.300'E or N59°19.617'
# E18°04.300' -- common on GPS handhelds and in boating, as opposed to
# the whole-minutes-plus-seconds form above.
_DDM_RE = re.compile(
    r"(?P<latdir1>[NSns])?\s*(?P<latdeg>\d{1,2})\s*°\s*(?P<latmin>\d{1,2}(?:\.\d+)?)\s*['′’]?\s*(?P<latdir2>[NSns])?"
    r"[,;\s]+"
    r"(?P<londir1>[EWew])?\s*(?P<londeg>\d{1,3})\s*°\s*(?P<lonmin>\d{1,2}(?:\.\d+)?)\s*['′’]?\s*(?P<londir2>[EWew])?"
)

# Plain decimal degrees, e.g. "59.3269, 18.0717" (what a copy-paste from
# Google Maps or a phone's GPS app looks like) -- the required comma and
# mandatory decimal point on both numbers keep this from firing on
# unrelated decimal pairs elsewhere in a report.
_DD_RE = re.compile(
    r"(?P<latdir1>[NSns])?\s*(?P<lat>-?\d{1,2}\.\d+)\s*(?P<latdir2>[NSns])?\s*,\s*"
    r"(?P<londir1>[EWew])?\s*(?P<lon>-?\d{1,3}\.\d+)\s*(?P<londir2>[EWew])?"
)

_converter = _mgrs.MGRS()


def _valid_latlon(lat: float, lon: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _direction(dir1: Optional[str], dir2: Optional[str]) -> Optional[str]:
    """A direction letter can lead or trail its number (e.g. "N59°..." or
    "59°...N") -- combines whichever one matched, uppercased, or None if
    neither did (regex groups make both optional, but a plain unsigned
    degree value with no hemisphere at all isn't something this module
    can safely resolve a sign for)."""
    letter = dir1 or dir2
    return letter.upper() if letter else None


def _dms_to_decimal(deg: str, minute: str, sec: str, direction: str) -> float:
    value = float(deg) + float(minute) / 60 + float(sec) / 3600
    return -value if direction in ("S", "W") else value


def _ddm_to_decimal(deg: str, minute: str, direction: str) -> float:
    value = float(deg) + float(minute) / 60
    return -value if direction in ("S", "W") else value


def extract_mgrs_latlon(text: Optional[str]) -> Optional[tuple[float, float]]:
    """Find the first MGRS-looking token in `text` and convert it to
    (lat, lon). Returns None if no token matches or the matched token isn't
    actually a valid grid reference (e.g. it collided with an unrelated
    digit/letter run) -- callers treat this purely as a nice-to-have."""
    if not text:
        return None
    match = _MGRS_RE.search(text)
    if match is None:
        return None
    compact = re.sub(r"\s", "", match.group(0)).upper()
    try:
        with warnings.catch_warnings():
            # The underlying GeoTrans C binding emits a cosmetic
            # "Latitude Warning" via ctypes on plenty of valid grid
            # references (a known quirk of the library, not a sign the
            # coordinate is wrong) -- suppress it, it would otherwise spam
            # the app's log tab on nearly every ingested report.
            warnings.simplefilter("ignore")
            lat, lon = _converter.toLatLon(compact)
    except Exception:
        return None
    return lat, lon


def extract_position(text: Optional[str]) -> Optional[tuple[float, float]]:
    """Like extract_mgrs_latlon, but also recognizes the other position
    formats a report might use instead: decimal degrees, degrees/minutes/
    seconds, and degrees/decimal minutes (see the module docstring for
    examples of each) -- MGRS is tried first and stays the format the app
    itself displays a position in (to_mgrs()); this only widens what a
    human-typed report can hand it. Returns the first valid match, or None
    if nothing recognizable was found."""
    if not text:
        return None

    latlon = extract_mgrs_latlon(text)
    if latlon is not None:
        return latlon

    match = _DMS_RE.search(text)
    if match is not None:
        latdir = _direction(match["latdir1"], match["latdir2"])
        londir = _direction(match["londir1"], match["londir2"])
        if latdir is not None and londir is not None:
            lat = _dms_to_decimal(match["latdeg"], match["latmin"], match["latsec"], latdir)
            lon = _dms_to_decimal(match["londeg"], match["lonmin"], match["lonsec"], londir)
            if _valid_latlon(lat, lon):
                return lat, lon

    match = _DDM_RE.search(text)
    if match is not None:
        latdir = _direction(match["latdir1"], match["latdir2"])
        londir = _direction(match["londir1"], match["londir2"])
        if latdir is not None and londir is not None:
            lat = _ddm_to_decimal(match["latdeg"], match["latmin"], latdir)
            lon = _ddm_to_decimal(match["londeg"], match["lonmin"], londir)
            if _valid_latlon(lat, lon):
                return lat, lon

    match = _DD_RE.search(text)
    if match is not None:
        lat = float(match["lat"])
        lon = float(match["lon"])
        # A direction letter, if present, always wins over whatever sign
        # the number itself carried -- consistent with DMS/DDM above.
        if _direction(match["latdir1"], match["latdir2"]) == "S":
            lat = -abs(lat)
        if _direction(match["londir1"], match["londir2"]) == "W":
            lon = -abs(lon)
        if _valid_latlon(lat, lon):
            return lat, lon

    return None


def to_mgrs(lat: float, lon: float) -> Optional[str]:
    """The reverse of extract_mgrs_latlon -- a plain decimal-degree
    position (e.g. Kartcentrum, or an event's pin-dropped position) shown
    as its MGRS grid reference too, for cross-referencing against a report
    written in that format. Returns None on the rare out-of-range/
    conversion failure rather than raising, same fail-safe spirit as
    extract_mgrs_latlon."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return _converter.toMGRS(lat, lon)
    except Exception:
        return None
