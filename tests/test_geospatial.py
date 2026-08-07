"""Projection, distance, aggregation and interpolation.

The recurring theme: nothing may quietly turn "we did not measure here" into a value,
and nothing may present a computed value as a measured one.
"""

from __future__ import annotations

import numpy as np
import pytest

from spectramap.geospatial.aggregate import aggregate_to_grid
from spectramap.geospatial.projection import (
    LocalTangentPlane,
    destination,
    haversine_m,
    path_length_m,
)
from spectramap.interpolation.idw import idw_grid
from spectramap.types import Evidence

# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("latitude", [0.0, 22.57, 51.5, -33.9, 70.0])
def test_enu_round_trip_is_exact(latitude):
    plane = LocalTangentPlane(latitude, 88.36)
    east, north, up = plane.to_enu(latitude + 0.001, 88.36 + 0.001, 12.0)
    lat, lon, alt = plane.to_geodetic(east, north, up)
    assert float(lat) == pytest.approx(latitude + 0.001, abs=1e-9)
    assert float(lon) == pytest.approx(88.36 + 0.001, abs=1e-9)
    assert float(alt) == pytest.approx(12.0, abs=1e-9)


@pytest.mark.parametrize(
    "latitude, metres_per_deg_lat, metres_per_deg_lon",
    [
        # Published WGS84 values for the length of one degree.
        (0.0, 110_574, 111_320),
        (45.0, 111_132, 78_847),
        (60.0, 111_412, 55_800),
    ],
)
def test_projection_scale_matches_published_wgs84_values(
    latitude, metres_per_deg_lat, metres_per_deg_lon
):
    """The plane must give true metres, not degrees times a fudge factor.

    Checked against published WGS84 degree lengths rather than against this module's
    own spherical helpers, which would only prove the two agree with each other.
    """
    plane = LocalTangentPlane(latitude, 10.0)
    east, north, _ = plane.to_enu(latitude + 1.0, 11.0)
    assert float(north) == pytest.approx(metres_per_deg_lat, rel=1e-3)
    assert float(east) == pytest.approx(metres_per_deg_lon, rel=1e-3)


def test_spherical_helpers_agree_with_the_plane_to_their_documented_accuracy():
    """``destination`` and ``haversine_m`` are spherical; the plane is ellipsoidal.

    They therefore disagree by a few parts per thousand, which is far below GPS
    error but must not be mistaken for a bug. This pins the size of that gap.
    """
    plane = LocalTangentPlane(22.5726, 88.3639)
    for bearing in (0, 45, 90, 180, 275):
        lat, lon = destination(22.5726, 88.3639, bearing, 500.0)
        east, north, _ = plane.to_enu(lat, lon)
        assert float(np.hypot(east, north)) == pytest.approx(500.0, rel=6e-3)


def test_longitude_scale_shrinks_with_latitude():
    """A degree of longitude is much shorter near the poles.

    Working in raw degrees is the classic way to produce a heatmap stretched by
    1/cos(latitude); this is the check that we do not.
    """
    equator, _, _ = LocalTangentPlane(0.0, 0.0).to_enu(0.0, 1.0)
    high, _, _ = LocalTangentPlane(60.0, 0.0).to_enu(60.0, 1.0)
    assert float(high) / float(equator) == pytest.approx(0.5, rel=0.01)


def test_path_length_smoothing_suppresses_fix_noise():
    """A raw fix-to-fix sum counts GPS scatter as distance travelled."""
    rng = np.random.default_rng(5)
    n = 400
    true_lat = np.zeros(n)
    true_lon = np.linspace(0, 0.0045, n)  # ~500 m due east.
    truth = path_length_m(true_lat, true_lon)
    noise_deg = 2.0 / 111_320.0  # ~2 m of scatter.
    noisy_lat = true_lat + rng.normal(0, noise_deg, n)
    noisy_lon = true_lon + rng.normal(0, noise_deg, n)

    raw = path_length_m(noisy_lat, noisy_lon)
    smoothed = path_length_m(noisy_lat, noisy_lon, smooth_window=5)
    assert raw > truth * 1.5, "noise should inflate the raw sum substantially"
    assert smoothed == pytest.approx(truth, rel=0.15)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def test_aggregation_averages_power_linearly_not_in_db():
    """A cell containing one strong sample must not be dragged down by a log mean."""
    lat = np.array([0.0, 0.0])
    lon = np.array([0.0, 0.0])
    power = np.array([-100.0, -80.0])
    stats = aggregate_to_grid(lat, lon, power, cell_size_m=10.0)
    assert stats.n_cells == 1
    cell = stats.cells[0]
    # Linear mean of 1e-10 and 1e-8 -> ~-83 dB. A dB mean would say -90.
    assert cell.mean_db == pytest.approx(-83.0, abs=0.1)
    assert cell.mean_db != pytest.approx(-90.0, abs=1.0)


def test_aggregation_preserves_spread():
    lat = np.zeros(4)
    lon = np.zeros(4)
    power = np.array([-74.0, -72.0, -77.0, -73.0])
    cell = aggregate_to_grid(lat, lon, power, cell_size_m=10.0).cells[0]
    assert cell.count == 4
    assert cell.min_db == -77.0
    assert cell.max_db == -72.0
    assert cell.spread_db == pytest.approx(5.0)
    assert cell.median_db == pytest.approx(-73.5)
    assert cell.std_db > 0
    assert cell.to_dict()["evidence"] == Evidence.DERIVED.value


def test_aggregation_separates_distant_points():
    plane = LocalTangentPlane(0.0, 0.0)
    lat_a, lon_a, _ = plane.to_geodetic(0.0, 0.0)
    lat_b, lon_b, _ = plane.to_geodetic(100.0, 100.0)
    stats = aggregate_to_grid(
        np.array([float(lat_a), float(lat_b)]),
        np.array([float(lon_a), float(lon_b)]),
        np.array([-70.0, -90.0]),
        cell_size_m=10.0,
    )
    assert stats.n_cells == 2


def test_aggregation_of_nothing_is_empty_not_an_error():
    stats = aggregate_to_grid(np.array([]), np.array([]), np.array([]), cell_size_m=5.0)
    assert stats.n_cells == 0


# ---------------------------------------------------------------------------
# Interpolation
# ---------------------------------------------------------------------------


def _scatter(n=40, seed=1):
    """Measurements on a ring, with power falling off from the centre."""
    rng = np.random.default_rng(seed)
    plane = LocalTangentPlane(0.0, 0.0)
    angles = rng.uniform(0, 2 * np.pi, n)
    radii = rng.uniform(20, 50, n)
    east, north = radii * np.cos(angles), radii * np.sin(angles)
    lat, lon, _ = plane.to_geodetic(east, north)
    power = -40.0 - 26.0 * np.log10(np.hypot(east, north) / 10.0)
    return np.asarray(lat), np.asarray(lon), power


def test_masked_cells_are_nan_not_zero():
    """Unmeasured ground must be absent, never rendered as 'quiet'."""
    lat, lon, power = _scatter()
    field = idw_grid(lat, lon, power, resolution_m=5.0, max_distance_m=8.0, margin_m=120.0)
    assert np.isnan(field.power_db).any(), "far-from-any-sample cells must be masked"
    assert field.coverage_fraction < 1.0
    # Nothing outside the data range leaked in.
    finite = field.power_db[np.isfinite(field.power_db)]
    assert finite.min() >= power.min() - 1e-6
    assert finite.max() <= power.max() + 1e-6


def test_idw_cannot_overshoot_the_measured_range():
    lat, lon, power = _scatter()
    field = idw_grid(lat, lon, power, resolution_m=4.0, max_distance_m=60.0)
    finite = field.power_db[np.isfinite(field.power_db)]
    assert finite.min() >= power.min() - 1e-6
    assert finite.max() <= power.max() + 1e-6


def test_confidence_falls_with_distance_from_measurements():
    lat, lon, power = _scatter()
    field = idw_grid(lat, lon, power, resolution_m=5.0, max_distance_m=40.0, margin_m=60.0)
    valid = np.isfinite(field.confidence)
    distance = field.distance_m[valid]
    confidence = field.confidence[valid]
    near = confidence[distance < 5]
    far = confidence[(distance > 25) & (distance < 40)]
    assert near.mean() > far.mean()
    assert confidence.min() >= 0.0 and confidence.max() <= 1.0


def test_interpolated_field_carries_full_provenance():
    lat, lon, power = _scatter()
    field = idw_grid(lat, lon, power, survey_id="survey-42", power=3.0)
    p = field.provenance
    assert p.evidence is Evidence.INTERPOLATED
    assert p.source_survey_id == "survey-42"
    assert p.algorithm == "inverse-distance-weighting"
    assert p.parameters["power"] == 3.0
    assert p.parameters["interpolation_domain"] == "linear power"


def test_field_argmax_finds_a_planted_maximum():
    """The reconstruction must put the peak where the strong measurements are."""
    plane = LocalTangentPlane(0.0, 0.0)
    rng = np.random.default_rng(2)
    east = rng.uniform(-60, 60, 300)
    north = rng.uniform(-60, 60, 300)
    source = np.array([18.0, -25.0])
    distance = np.hypot(east - source[0], north - source[1])
    power = -40.0 - 26.0 * np.log10(np.maximum(distance, 1.0) / 10.0)
    lat, lon, _ = plane.to_geodetic(east, north)
    field = idw_grid(np.asarray(lat), np.asarray(lon), power, resolution_m=2.0)
    peak_lat, peak_lon, _ = field.argmax_geodetic()
    peak_e, peak_n, _ = field.plane.to_enu(peak_lat, peak_lon)
    assert float(np.hypot(float(peak_e) - source[0], float(peak_n) - source[1])) < 6.0


def test_empty_input_is_rejected():
    with pytest.raises(ValueError, match="empty"):
        idw_grid(np.array([]), np.array([]), np.array([]))


def test_haversine_matches_known_distance():
    # London to Paris, ~343 km.
    d = haversine_m(51.5074, -0.1278, 48.8566, 2.3522)
    assert float(d) == pytest.approx(343_500, rel=0.01)
