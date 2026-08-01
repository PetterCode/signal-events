import pytest

from signal_events.coordinates import extract_mgrs_latlon, extract_position, to_mgrs


def test_extracts_lat_lon_from_a_compact_mgrs_token():
    lat, lon = extract_mgrs_latlon("Ställe: 33VVN1234567890")
    assert lat == pytest.approx(65.52, abs=0.01)
    assert lon == pytest.approx(13.10, abs=0.01)


def test_extracts_lat_lon_from_a_spaced_mgrs_token_with_trailing_text():
    # The exact place-field format used in tests/test_parser.py's 7S fixture.
    lat, lon = extract_mgrs_latlon("33VWE 18190 99510, parkering V Kvarn")
    assert lat == pytest.approx(58.64, abs=0.01)
    assert lon == pytest.approx(15.31, abs=0.01)


def test_returns_none_when_no_mgrs_token_present():
    assert extract_mgrs_latlon("Norra grinden, silver Volvo") is None


def test_returns_none_for_empty_or_missing_text():
    assert extract_mgrs_latlon("") is None
    assert extract_mgrs_latlon(None) is None


def test_returns_none_for_an_mgrs_looking_but_invalid_token():
    # Zone 99 doesn't exist (valid zones are 1-60) -- matches the regex's
    # loose shape, but the underlying conversion must reject it rather
    # than raising.
    assert extract_mgrs_latlon("99CAB1234567890") is None


def test_to_mgrs_round_trips_with_extract_mgrs_latlon():
    lat, lon = extract_mgrs_latlon("Ställe: 33VVN1234567890")
    token = to_mgrs(lat, lon)
    assert token is not None
    round_tripped_lat, round_tripped_lon = extract_mgrs_latlon(token)
    assert round_tripped_lat == pytest.approx(lat, abs=0.001)
    assert round_tripped_lon == pytest.approx(lon, abs=0.001)


def test_to_mgrs_handles_the_new_default_kartcentrum():
    # 59°19'37"N 18°04'18"E
    token = to_mgrs(59.326944, 18.071667)
    assert token is not None
    assert token.startswith("34V")


def test_to_mgrs_returns_none_for_an_out_of_range_position():
    assert to_mgrs(999, 999) is None


def test_extract_position_still_prefers_mgrs_when_present():
    lat, lon = extract_position("Ställe: 33VVN1234567890")
    assert lat == pytest.approx(65.52, abs=0.01)
    assert lon == pytest.approx(13.10, abs=0.01)


def test_extract_position_reads_decimal_degrees():
    lat, lon = extract_position("Position 59.3269, 18.0717 vid grinden")
    assert lat == pytest.approx(59.3269)
    assert lon == pytest.approx(18.0717)


def test_extract_position_reads_decimal_degrees_with_direction_letters():
    lat, lon = extract_position("N59.3269, E18.0717")
    assert lat == pytest.approx(59.3269)
    assert lon == pytest.approx(18.0717)


def test_extract_position_reads_southern_western_decimal_degrees():
    lat, lon = extract_position("S59.3269, W18.0717")
    assert lat == pytest.approx(-59.3269)
    assert lon == pytest.approx(-18.0717)


def test_extract_position_ignores_a_decimal_pair_with_no_comma_or_direction():
    # Two plausible-looking decimals in ordinary prose, not a coordinate.
    assert extract_position("kl. 14.30 dag 18.05, ingen koordinat") is None


def test_extract_position_reads_degrees_minutes_seconds():
    # Kartcentrum, 59°19'37"N 18°04'18"E.
    lat, lon = extract_position("Ställe: 59°19'37\"N 18°04'18\"E")
    assert lat == pytest.approx(59.326944, abs=0.0001)
    assert lon == pytest.approx(18.071667, abs=0.0001)


def test_extract_position_reads_degrees_minutes_seconds_with_prime_characters():
    lat, lon = extract_position("Ställe: 59°19′37″N 18°04′18″E")
    assert lat == pytest.approx(59.326944, abs=0.0001)
    assert lon == pytest.approx(18.071667, abs=0.0001)


def test_extract_position_reads_degrees_decimal_minutes():
    lat, lon = extract_position("N59°19.617' E18°04.300'")
    assert lat == pytest.approx(59.326944, abs=0.0001)
    assert lon == pytest.approx(18.071667, abs=0.0001)


def test_extract_position_reads_degrees_decimal_minutes_with_trailing_directions():
    lat, lon = extract_position("59°19.617'N 18°04.300'E")
    assert lat == pytest.approx(59.326944, abs=0.0001)
    assert lon == pytest.approx(18.071667, abs=0.0001)


def test_extract_position_rejects_a_degrees_minutes_seconds_match_with_no_hemisphere_letters():
    # No N/S/E/W anywhere -- can't resolve a sign, so this must not be
    # mistaken for a coordinate at all.
    assert extract_position("59°19'37\" 18°04'18\"") is None


def test_extract_position_returns_none_when_nothing_matches_any_format():
    assert extract_position("Norra grinden, silver Volvo") is None


def test_extract_position_returns_none_for_empty_or_missing_text():
    assert extract_position("") is None
    assert extract_position(None) is None
