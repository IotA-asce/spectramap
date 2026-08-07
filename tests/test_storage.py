"""The ``.smap`` container: round-trips, the band query, and crash behaviour."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

import numpy as np
import pytest

from spectramap.storage.store import SPECTRA_DTYPE, SurveyReader, SurveyWriter
from spectramap.survey.config import SurveyConfig
from spectramap.types import (
    DeviceCapabilities,
    DeviceInfo,
    Evidence,
    FFTConfig,
    FixQuality,
    FrequencyRange,
    Normalization,
    Position,
    PowerSpectrum,
    PowerUnit,
    QualityFlag,
    ReceiverConfig,
    ResolvedPosition,
    RFObservation,
)

N_BINS = 64
F_START = 433_000_000.0
F_STEP = 1_000.0


def _device():
    return DeviceInfo(driver="mock", index=0, serial="TEST1")


def _caps():
    return DeviceCapabilities(
        driver="mock",
        tuning_range=FrequencyRange(24e6, 1766e6),
        sample_rates_hz=(250e3,),
        gains_db=(24.0,),
        supports_agc=True,
        max_bandwidth_hz=250e3,
        full_scale_bits=8,
    )


def _observation(index: int, power: np.ndarray, lat=1.0, lon=2.0, flags=QualityFlag.NONE):
    spectrum = PowerSpectrum(
        frequencies_hz=F_START + np.arange(N_BINS) * F_STEP,
        power_db=power,
        unit=PowerUnit.DBFS_PER_HZ,
        normalization=Normalization.DENSITY,
        enbw_hz=1500.0,
        bin_width_hz=F_STEP,
        n_averages=8,
    )
    position = ResolvedPosition(
        Position(
            t_utc_ns=1_000_000_000 * index,
            t_mono_ns=1_000_000_000 * index,
            latitude=lat,
            longitude=lon,
            altitude_m=5.0,
            horizontal_accuracy_m=3.0,
        ),
        Evidence.DERIVED,
        120.0,
        QualityFlag.POSITION_INTERPOLATED,
    )
    return RFObservation(
        observation_id=index,
        t_start_utc_ns=1_000_000_000 * index,
        t_end_utc_ns=1_000_000_000 * index + 500_000_000,
        t_start_mono_ns=1_000_000_000 * index,
        position=position,
        receiver_id="mock:TEST1",
        antenna_id="whip",
        receiver_config=ReceiverConfig(433.5e6, 250e3, 24.0, False),
        fft_config=FFTConfig(size=N_BINS),
        spectrum=spectrum,
        n_ffts=8,
        flags=flags,
    )


@pytest.fixture
def written(tmp_path):
    """A small survey with a planted peak at bin 20 that grows along the route."""
    config = SurveyConfig.from_dict({"survey": {"name": "unit"}})
    writer = SurveyWriter(tmp_path / "u.smap", config, _device(), _caps())
    for i in range(12):
        power = np.full(N_BINS, -120.0)
        power[20] = -100.0 + i  # A rising signal.
        writer.add_position(
            Position(
                t_utc_ns=1_000_000_000 * i,
                t_mono_ns=1_000_000_000 * i,
                latitude=1.0 + i * 1e-4,
                longitude=2.0,
                horizontal_accuracy_m=3.0,
                fix_quality=FixQuality.GPS,
            )
        )
        writer.append_observation(
            _observation(i, power, lat=1.0 + i * 1e-4),
            noise_floor_db=-120.0,
            peak_power_db=float(power[20]),
            peak_frequency_hz=F_START + 20 * F_STEP,
        )
    writer.log_event("warning", "test", "something happened")
    writer.close(summary={"complete": True})
    return tmp_path / "u.smap"


def test_container_layout_is_as_documented(written):
    assert (written / "manifest.json").exists()
    assert (written / "survey.sqlite").exists()
    assert (written / "route.geojson").exists()
    assert (written / "spectra").is_dir()
    assert (written / "calibration").is_dir()
    assert (written / "processing").is_dir()
    assert list((written / "spectra").glob("*.f32"))


def test_spectra_are_readable_without_spectramap(written):
    """The whole point of the format: another tool can read it with numpy alone."""
    manifest = json.loads((written / "manifest.json").read_text())
    chunk = manifest["spectra"]["chunks"][0]
    raw = np.memmap(written / chunk["path"], dtype="<f4", mode="r").reshape(-1, chunk["n_bins"])
    assert raw.shape == (12, N_BINS)
    assert raw[5, 20] == pytest.approx(-95.0)
    frequencies = chunk["f_start_hz"] + np.arange(chunk["n_bins"]) * chunk["f_step_hz"]
    assert frequencies[20] == pytest.approx(F_START + 20 * F_STEP)


def test_manifest_declares_units_and_that_they_are_uncalibrated(written):
    manifest = json.loads((written / "manifest.json").read_text())
    assert manifest["format"] == "spectramap-survey"
    assert manifest["units"]["calibrated"] is False
    assert "not absolute field strength" in manifest["units"]["reference"].lower()
    assert manifest["configuration"]["capture"]["center_frequency_hz"]


def test_round_trip_preserves_spectra_and_metadata(written):
    with SurveyReader(written) as reader:
        assert reader.n_observations == 12
        spectrum = reader.spectrum(7)
        assert spectrum.n_bins == N_BINS
        assert spectrum.power_db[20] == pytest.approx(-93.0)
        assert spectrum.unit is PowerUnit.DBFS_PER_HZ
        assert spectrum.frequencies_hz[0] == pytest.approx(F_START)
        assert reader.power_unit is PowerUnit.DBFS_PER_HZ
        assert reader.is_calibrated is False


def test_raw_position_track_survives_independently(written):
    with SurveyReader(written) as reader:
        fixes = reader.positions()
        assert len(fixes) == 12
        assert fixes[0].fix_quality is FixQuality.GPS
        assert fixes[-1].latitude == pytest.approx(1.0011)


def test_events_are_retained(written):
    with SurveyReader(written) as reader:
        events = reader.events()
        assert any(e["kind"] == "test" and e["level"] == "warning" for e in events)


def test_band_power_extracts_a_frequency_across_the_survey(written):
    """The core query: pick a band, get a spatial field, without reading every FFT."""
    with SurveyReader(written) as reader:
        band = reader.band_power(F_START + 19_500, F_START + 20_500, method="peak")
        assert band["power_db"].size == 12
        assert band["power_db"][0] == pytest.approx(-100.0)
        assert band["power_db"][-1] == pytest.approx(-89.0)
        assert np.all(np.diff(band["power_db"]) > 0)
        assert band["latitude"].size == band["power_db"].size


@pytest.mark.parametrize("method", ["peak", "mean", "integrated"])
def test_band_power_methods_differ_and_are_all_valid(written, method):
    with SurveyReader(written) as reader:
        values = reader.band_power(F_START, F_START + 40_000, method=method)["power_db"]
        assert values.size == 12
        assert np.all(np.isfinite(values))


def test_band_power_outside_the_measured_span_returns_nothing(written):
    with SurveyReader(written) as reader:
        assert reader.band_power(900e6, 901e6)["power_db"].size == 0


def test_band_power_rejects_an_unknown_method(written):
    with SurveyReader(written) as reader:
        with pytest.raises(ValueError, match="unknown band power method"):
            reader.band_power(F_START, F_START + 1000, method="median")


def test_quality_filter_excludes_but_does_not_delete(tmp_path):
    config = SurveyConfig.from_dict({"survey": {"name": "flags"}})
    writer = SurveyWriter(tmp_path / "f.smap", config, _device(), _caps())
    for i in range(6):
        flags = QualityFlag.SDR_OVERLOAD if i % 2 else QualityFlag.NONE
        writer.append_observation(_observation(i, np.full(N_BINS, -110.0), flags=flags))
    writer.close()

    with SurveyReader(tmp_path / "f.smap") as reader:
        assert reader.n_observations == 6, "flagged observations remain in the survey"
        filtered = reader.band_power(
            F_START, F_START + 5000, exclude_flags=QualityFlag.SDR_OVERLOAD
        )
        assert filtered["power_db"].size == 3


def test_reader_rejects_a_directory_that_is_not_a_survey(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(FileNotFoundError, match="not a SpectraMap survey"):
        SurveyReader(tmp_path / "empty")


def test_truncated_final_row_is_ignored_rather_than_read_as_data(written):
    """A survey killed mid-write must still read cleanly.

    The row count comes from the file size, so a partially written row is simply not
    there -- as opposed to being read as garbage powers at a real location.
    """
    chunk_path = next((written / "spectra").glob("*.f32"))
    data = chunk_path.read_bytes()
    # Chop the last row in half, as a power cut during a write would.
    chunk_path.write_bytes(data[: -N_BINS * SPECTRA_DTYPE.itemsize // 2])
    with SurveyReader(written) as reader:
        chunk = reader.chunks()[0]
        assert chunk.n_rows == 11
        assert reader.spectrum(10).power_db[20] == pytest.approx(-90.0)


def test_writer_can_be_closed_twice(tmp_path):
    config = SurveyConfig.from_dict({"survey": {"name": "twice"}})
    writer = SurveyWriter(tmp_path / "t.smap", config, _device(), _caps())
    writer.append_observation(_observation(0, np.full(N_BINS, -110.0)))
    writer.close()
    assert writer.close() == tmp_path / "t.smap"


def test_retune_starts_a_new_chunk(tmp_path):
    """A chunk is homogeneous in frequency axis, so a sweep step must roll it."""
    config = SurveyConfig.from_dict({"survey": {"name": "sweep"}})
    writer = SurveyWriter(tmp_path / "s.smap", config, _device(), _caps())
    writer.append_observation(_observation(0, np.full(N_BINS, -110.0)))

    shifted = _observation(1, np.full(N_BINS, -110.0))
    moved = replace(shifted.spectrum, frequencies_hz=shifted.spectrum.frequencies_hz + 1e6)
    writer.append_observation(replace(shifted, spectrum=moved))
    writer.close()
    with SurveyReader(tmp_path / "s.smap") as reader:
        assert len(reader.chunks()) == 2
        assert len(reader.frequency_coverage()) == 2


def test_route_geojson_separates_track_from_observations(written):
    geojson = json.loads((written / "route.geojson").read_text())
    layers = {f["properties"]["layer"] for f in geojson["features"]}
    assert layers == {"gps_track", "observation"}
    points = [f for f in geojson["features"] if f["properties"]["layer"] == "observation"]
    assert len(points) == 12
    assert "POSITION_INTERPOLATED" in points[0]["properties"]["flags"]


def test_sqlite_schema_is_queryable_by_plain_sql(written):
    """A survey must be inspectable with the sqlite3 CLI, not only through this code."""
    connection = sqlite3.connect(f"file:{written / 'survey.sqlite'}?mode=ro", uri=True)
    (count,) = connection.execute(
        "SELECT COUNT(*) FROM observations WHERE peak_power_db > -95"
    ).fetchone()
    assert count == 6
    connection.close()
