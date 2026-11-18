"""The V0 validation: can SpectraMap reconstruct a transmitter it was never told about?

This is the test that decides whether the core idea works.

A synthetic beacon is placed at a known virtual coordinate. A simulated receiver
walks a grid around it, recording geotagged spectra through the *entire real
pipeline* -- acquisition, DSP, storage, band selection, aggregation, interpolation.
The reconstruction code is never given the beacon's position; it is recovered from
the measurements alone, and only then compared against the truth stored separately
in the manifest.

The control case matters as much as the positive one. A second emitter sits far
outside the surveyed area, so it is clearly detectable but has no usable spatial
gradient. A pipeline that "localises" that one is finding structure in noise, and the
test asserts that SpectraMap declines to claim it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from spectramap.analysis.pipeline import BandSelection, reconstruct, write_products
from spectramap.analysis.summary import summarise
from spectramap.geospatial.projection import haversine_m
from spectramap.storage import SurveyReader
from spectramap.types import Evidence, PowerUnit
from tests.conftest import BEACON_HZ, CONTROL_HZ


@pytest.fixture(scope="module")
def truth(recorded_survey):
    """Ground truth, read from the manifest -- never passed to the analysis."""
    with SurveyReader(recorded_survey) as reader:
        transmitters = reader.manifest["simulation_ground_truth"]["transmitters"]
    return {tx["tx_id"]: tx for tx in transmitters}


@pytest.fixture(scope="module")
def beacon_reconstruction(recorded_survey):
    with SurveyReader(recorded_survey) as reader:
        return reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"))


# ---------------------------------------------------------------------------
# The positive case
# ---------------------------------------------------------------------------


def test_survey_measured_a_real_spatial_gradient(beacon_reconstruction):
    powers = beacon_reconstruction.measurements["power_db"]
    assert powers.size > 20
    assert np.ptp(powers) > 15.0, "walking around a beacon must produce real contrast"


def test_measured_power_falls_off_with_distance_from_the_beacon(
    beacon_reconstruction, truth
):
    """The physical relationship the whole project depends on."""
    beacon = truth["beacon-433"]
    m = beacon_reconstruction.measurements
    distances = np.array(
        [
            float(haversine_m(lat, lon, beacon["latitude"], beacon["longitude"]))
            for lat, lon in zip(m["latitude"], m["longitude"], strict=True)
        ]
    )
    correlation = np.corrcoef(np.log10(np.maximum(distances, 1.0)), m["power_db"])[0, 1]
    assert correlation < -0.85


def test_reconstruction_recovers_the_beacon_region(beacon_reconstruction, truth):
    """The headline claim: the map converges on the transmitter without being told."""
    beacon = truth["beacon-433"]
    hotspot = beacon_reconstruction.hotspot
    assert hotspot is not None
    error_m = float(
        haversine_m(
            hotspot.latitude, hotspot.longitude, beacon["latitude"], beacon["longitude"]
        )
    )
    assert error_m < 20.0, f"estimated region is {error_m:.1f} m from the true beacon"


def test_interpolated_field_peaks_near_the_beacon(beacon_reconstruction, truth):
    beacon = truth["beacon-433"]
    lat, lon, _ = beacon_reconstruction.field.argmax_geodetic()
    error_m = float(haversine_m(lat, lon, beacon["latitude"], beacon["longitude"]))
    assert error_m < 25.0


def test_stated_uncertainty_actually_covers_the_error(beacon_reconstruction, truth):
    """An uncertainty that does not contain the truth is worse than none at all."""
    beacon = truth["beacon-433"]
    hotspot = beacon_reconstruction.hotspot
    error_m = float(
        haversine_m(
            hotspot.latitude, hotspot.longitude, beacon["latitude"], beacon["longitude"]
        )
    )
    # The radius is a 1-sigma-equivalent, so allow the usual 2-sigma coverage.
    assert error_m <= 2.5 * hotspot.radius_m


def test_uncertainty_is_never_finer_than_the_position_accuracy(
    beacon_reconstruction, recorded_survey
):
    with SurveyReader(recorded_survey) as reader:
        median_accuracy = summarise(reader).median_accuracy_m
    assert beacon_reconstruction.hotspot.radius_m >= median_accuracy


# ---------------------------------------------------------------------------
# The control case
# ---------------------------------------------------------------------------


def test_distant_carrier_is_detected_but_not_localised(recorded_survey, truth):
    """Detectable, spatially flat -- and SpectraMap must refuse to place it."""
    with SurveyReader(recorded_survey) as reader:
        control = reconstruct(reader, BandSelection.around(CONTROL_HZ, 20_000.0, "peak"))
        floor = summarise(reader).noise_floor_db

    powers = control.measurements["power_db"]
    assert powers.mean() > floor + 10, "the control carrier must be clearly detected"
    assert np.ptp(powers) < 5.0, "and must show no usable spatial gradient"

    hotspot = control.hotspot
    assert hotspot.confidence == "low"
    assert any("NOT USABLE as a location" in c for c in hotspot.caveats)

    # The true emitter is kilometres away; the estimate is nowhere near it, which is
    # exactly why claiming a location here would be wrong.
    carrier = truth["carrier-433-95"]
    error_m = float(
        haversine_m(hotspot.latitude, hotspot.longitude, carrier["latitude"],
                    carrier["longitude"])
    )
    assert error_m > 500.0


def test_frequency_selection_actually_selects(recorded_survey):
    """Two bands of the same survey must give genuinely different spatial fields."""
    with SurveyReader(recorded_survey) as reader:
        beacon = reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"))
        control = reconstruct(reader, BandSelection.around(CONTROL_HZ, 20_000.0, "peak"))
    assert np.ptp(beacon.measurements["power_db"]) > 5 * np.ptp(
        control.measurements["power_db"]
    )


def test_empty_band_raises_a_helpful_error(recorded_survey):
    with SurveyReader(recorded_survey) as reader:
        with pytest.raises(ValueError, match="no usable observations cover"):
            reconstruct(reader, BandSelection.around(900e6, 20_000.0, "peak"))


# ---------------------------------------------------------------------------
# Scientific integrity of the output
# ---------------------------------------------------------------------------


def test_unmeasured_ground_is_masked_not_invented(recorded_survey):
    """A tight mask must leave holes where the survey did not go."""
    with SurveyReader(recorded_survey) as reader:
        recon = reconstruct(
            reader,
            BandSelection.around(BEACON_HZ, 20_000.0, "peak"),
            resolution_m=4.0,
            max_distance_m=6.0,
        )
    assert np.isnan(recon.field.power_db).any()
    assert recon.field.coverage_fraction < 1.0


def test_everything_is_labelled_with_its_evidence(beacon_reconstruction):
    assert beacon_reconstruction.field.provenance.evidence is Evidence.INTERPOLATED
    assert beacon_reconstruction.hotspot.evidence is Evidence.ESTIMATED
    assert beacon_reconstruction.provenance.evidence is Evidence.DERIVED
    assert beacon_reconstruction.summary.power_unit is PowerUnit.DBFS_PER_HZ
    assert beacon_reconstruction.summary.calibrated is False


def test_no_output_claims_absolute_dbm(beacon_reconstruction):
    """V0 is uncalibrated. Nothing may present relative units as field strength."""
    blob = json.dumps(beacon_reconstruction.to_dict())
    assert '"dBm"' not in blob
    assert beacon_reconstruction.summary.power_unit.is_calibrated is False
    assert "relative, uncalibrated" in beacon_reconstruction.summary.power_unit.label


def test_reconstruction_is_reproducible(recorded_survey):
    """Same survey, same parameters, same answer."""
    with SurveyReader(recorded_survey) as reader:
        band = BandSelection.around(BEACON_HZ, 20_000.0, "peak")
        first = reconstruct(reader, band)
        second = reconstruct(reader, band)
    assert first.hotspot.latitude == second.hotspot.latitude
    assert first.hotspot.longitude == second.hotspot.longitude
    np.testing.assert_array_equal(
        np.nan_to_num(first.field.power_db), np.nan_to_num(second.field.power_db)
    )


def test_changing_interpolation_settings_changes_the_recorded_provenance(recorded_survey):
    """If the map changes because settings changed, it must be obvious why."""
    with SurveyReader(recorded_survey) as reader:
        band = BandSelection.around(BEACON_HZ, 20_000.0, "peak")
        a = reconstruct(reader, band, idw_power=2.0)
        b = reconstruct(reader, band, idw_power=4.0)
    assert a.field.provenance.parameters["power"] == 2.0
    assert b.field.provenance.parameters["power"] == 4.0
    assert not np.allclose(
        np.nan_to_num(a.field.power_db), np.nan_to_num(b.field.power_db)
    )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


def test_products_are_written_with_provenance(recorded_survey, tmp_path):
    with SurveyReader(recorded_survey) as reader:
        recon = reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"))
        outputs = write_products(reader, recon, out_dir=tmp_path / "out")

    for key in (
        "observations_geojson",
        "observations_csv",
        "field_geojson",
        "field_png",
        "provenance",
        "reconstruction",
    ):
        assert outputs[key].exists(), key

    provenance = json.loads(outputs["provenance"].read_text())
    assert provenance["source_survey_id"]
    assert provenance["software_version"]
    assert provenance["algorithm"]
    assert provenance["parameters"]["band"]["method"] == "peak"

    png = outputs["field_png"].read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    sidecar = json.loads(outputs["field_png"].with_suffix(".json").read_text())
    assert sidecar["evidence"] == Evidence.INTERPOLATED.value
    assert sidecar["bounds"]["north"] > sidecar["bounds"]["south"]


def test_exported_geojson_distinguishes_measured_from_interpolated(
    recorded_survey, tmp_path
):
    with SurveyReader(recorded_survey) as reader:
        recon = reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"))
        outputs = write_products(reader, recon, out_dir=tmp_path / "out")

    points = json.loads(outputs["observations_geojson"].read_text())
    assert points["properties"]["evidence"] == Evidence.MEASURED.value
    assert points["properties"]["calibrated"] is False
    assert all(
        f["properties"]["evidence"] == Evidence.MEASURED.value for f in points["features"]
    )

    field = json.loads(outputs["field_geojson"].read_text())
    assert field["properties"]["evidence"] == Evidence.INTERPOLATED.value
    assert all(
        f["properties"]["evidence"] == Evidence.INTERPOLATED.value
        for f in field["features"]
    )
    # Every interpolated cell carries the evidence for its own trustworthiness.
    sample = field["features"][0]["properties"]
    assert "confidence" in sample
    assert "distance_to_nearest_sample_m" in sample
