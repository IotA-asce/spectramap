"""Position parsing and the RF/position time join.

The NMEA parser is tested with canned sentences, so the real GPS code path is
exercised in CI with no receiver attached. The track tests pin the rule that matters
most: an interpolated or extrapolated position must say so.
"""

from __future__ import annotations

import pytest

from spectramap.positioning.nmea import NMEAParser, nmea_checksum_ok, parse_ddmm
from spectramap.positioning.track import PositionTrack
from spectramap.types import Evidence, FixQuality, Position, QualityFlag


def _checksummed(body: str) -> str:
    """Build a valid sentence from a body, so fixtures cannot drift out of sync."""
    checksum = 0
    for ch in body:
        checksum ^= ord(ch)
    return f"${body}*{checksum:02X}"


GGA = _checksummed("GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
RMC = _checksummed("GPRMC,123519,A,4807.038,N,01131.000,E,022.4,084.4,230326,003.1,W")
GSA = _checksummed("GPGSA,A,3,04,05,,09,12,,,24,,,,,2.5,1.3,2.0")


# ---------------------------------------------------------------------------
# Sentence handling
# ---------------------------------------------------------------------------


def test_checksum_accepts_valid_and_rejects_corrupt():
    good = _checksummed("GPGGA,123519.00,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,")
    assert nmea_checksum_ok(good)
    corrupted = good.replace("4807.038", "4807.039")
    assert not nmea_checksum_ok(corrupted)


@pytest.mark.parametrize("bad", ["", "not a sentence", "$GPGGA,1,2,3", "$GPGGA,1*ZZ"])
def test_malformed_sentences_are_rejected(bad):
    assert not nmea_checksum_ok(bad)


def test_corrupt_sentence_produces_no_fix():
    """A sentence that fails its checksum must never become a map point."""
    parser = NMEAParser()
    assert parser.feed(GGA.replace("4807.038", "9907.038")) is None


def test_ddmm_parsing_handles_variable_width_degrees():
    """Latitude has 2 degree digits, longitude 3. Getting this wrong moves a fix by km."""
    assert parse_ddmm("4807.038", "N") == pytest.approx(48.1173, abs=1e-4)
    assert parse_ddmm("01131.000", "E") == pytest.approx(11.5166667, abs=1e-6)
    assert parse_ddmm("4807.038", "S") == pytest.approx(-48.1173, abs=1e-4)
    assert parse_ddmm("01131.000", "W") == pytest.approx(-11.5166667, abs=1e-6)
    assert parse_ddmm("", "N") is None


def test_gga_produces_a_complete_fix():
    parser = NMEAParser()
    fix = parser.feed(GGA)
    assert fix is not None
    assert fix.latitude == pytest.approx(48.1173, abs=1e-4)
    assert fix.longitude == pytest.approx(11.5167, abs=1e-4)
    assert fix.altitude_m == pytest.approx(545.4)
    assert fix.satellites == 8
    assert fix.fix_quality is FixQuality.GPS
    # HDOP 0.9 x the assumed 4 m UERE.
    assert fix.horizontal_accuracy_m == pytest.approx(3.6, abs=0.01)


def test_no_fix_sentence_yields_nothing():
    """Quality 0 means the receiver has no solution; it must not produce a position."""
    body = "GPGGA,123519.00,4807.038,N,01131.000,E,0,00,99.9,,M,,M,,"
    assert NMEAParser().feed(_checksummed(body)) is None


def test_rmc_date_is_combined_with_gga_time():
    """GGA has no date; without RMC the timestamp would fall back to the host clock."""
    parser = NMEAParser()
    parser.feed(RMC)
    fix = parser.feed(GGA)
    assert fix is not None
    # 2026-03-23T12:35:19Z
    assert fix.t_utc_ns == 1774269319000000000
    assert fix.speed_mps == pytest.approx(22.4 * 0.514444, rel=1e-4)


def test_gsa_dop_refines_accuracy_estimate():
    parser = NMEAParser()
    parser.feed(GSA)  # HDOP 1.3, VDOP 2.0
    fix = parser.feed(GGA)
    assert fix is not None
    # GGA's own HDOP field wins when present, and vertical comes from GSA.
    assert fix.vertical_accuracy_m == pytest.approx(2.0 * 4.0, abs=0.01)


# ---------------------------------------------------------------------------
# The time join
# ---------------------------------------------------------------------------


def _fix(mono_s: float, lat: float, lon: float, accuracy: float = 3.0) -> Position:
    return Position(
        t_utc_ns=int(mono_s * 1e9),
        t_mono_ns=int(mono_s * 1e9),
        latitude=lat,
        longitude=lon,
        altitude_m=10.0,
        horizontal_accuracy_m=accuracy,
        fix_quality=FixQuality.GPS,
    )


def test_empty_track_reports_no_location():
    resolved = PositionTrack().resolve(0)
    assert resolved.position is None
    assert QualityFlag.NO_LOCATION & resolved.flags


def test_position_between_fixes_is_interpolated_and_says_so():
    track = PositionTrack()
    track.add(_fix(0.0, 10.0, 20.0))
    track.add(_fix(2.0, 10.0002, 20.0002))
    resolved = track.resolve(int(1.0e9))
    assert resolved.position is not None
    assert resolved.position.latitude == pytest.approx(10.0001)
    assert resolved.evidence is Evidence.DERIVED
    assert QualityFlag.POSITION_INTERPOLATED & resolved.flags
    assert not (QualityFlag.POSITION_EXTRAPOLATED & resolved.flags)


def test_position_past_the_last_fix_is_extrapolated_and_downgraded():
    track = PositionTrack()
    track.add(_fix(0.0, 10.0, 20.0))
    track.add(_fix(1.0, 10.0001, 20.0001))
    resolved = track.resolve(int(2.0e9))
    assert resolved.evidence is Evidence.ESTIMATED
    assert QualityFlag.POSITION_EXTRAPOLATED & resolved.flags
    assert resolved.age_ms == pytest.approx(1000.0)


def test_stale_fix_yields_no_position_at_all():
    """Beyond max_age the honest answer is 'unknown', not 'probably over there'."""
    track = PositionTrack()
    track.add(_fix(0.0, 10.0, 20.0))
    resolved = track.resolve(int(60e9), max_age_ms=5000.0)
    assert resolved.position is None
    assert QualityFlag.NO_LOCATION & resolved.flags
    assert QualityFlag.GPS_STALE & resolved.flags


def test_poor_accuracy_is_flagged_but_the_position_is_kept():
    track = PositionTrack()
    track.add(_fix(0.0, 10.0, 20.0, accuracy=50.0))
    resolved = track.resolve(0, accuracy_limit_m=15.0)
    assert resolved.position is not None  # Flagged, not discarded.
    assert QualityFlag.GPS_LOW_ACCURACY & resolved.flags


def test_interpolated_accuracy_takes_the_worse_endpoint():
    """Interpolation cannot be more certain than the fixes it sits between."""
    track = PositionTrack()
    track.add(_fix(0.0, 10.0, 20.0, accuracy=2.0))
    track.add(_fix(2.0, 10.001, 20.001, accuracy=9.0))
    resolved = track.resolve(int(1.0e9))
    assert resolved.position.horizontal_accuracy_m == pytest.approx(9.0)


def test_out_of_order_fixes_are_dropped_not_inserted():
    """A time-travelling fix would corrupt every later lookup."""
    track = PositionTrack()
    track.add(_fix(5.0, 10.0, 20.0))
    track.add(_fix(1.0, 99.0, 99.0))
    assert len(track) == 1
    assert track.latest.latitude == 10.0


def test_exact_hit_on_a_fix_is_measured_not_derived():
    track = PositionTrack()
    track.add(_fix(3.0, 10.0, 20.0))
    resolved = track.resolve(int(3.0e9))
    assert resolved.evidence is Evidence.MEASURED
    assert resolved.flags == QualityFlag.NONE
