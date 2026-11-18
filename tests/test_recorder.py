"""Acquisition: time alignment, quality flagging, and behaviour when things go wrong.

The theme throughout: a survey that went wrong must say so in its own manifest, and
must still be a valid, readable survey.
"""

from __future__ import annotations

import numpy as np
import pytest

from spectramap.sdr.mock import MockSDR
from spectramap.simulation.clock import SimClock
from spectramap.simulation.world import compact_world
from spectramap.storage import SurveyReader
from spectramap.survey.config import SurveyConfig
from spectramap.survey.recorder import SurveyRecorder
from spectramap.survey.session import build_session
from spectramap.types import QualityFlag
from tests.conftest import COMPACT_CONFIG


def _record(tmp_path, overrides=None, duration=25.0, world=None, device=None, name="t"):
    config = SurveyConfig.from_dict({**COMPACT_CONFIG, **(overrides or {})})
    world = world or compact_world()
    session = build_session(config, world=world)
    recorder = SurveyRecorder(
        config,
        device if device is not None else session.device,
        session.positions,
        tmp_path / f"{name}.smap",
        extra_manifest=session.manifest_extras(),
    )
    path = recorder.run(max_duration_s=duration)
    return recorder, path


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_survey_persists_and_reopens(recorded_survey):
    """Surveys must survive a restart: nothing is held in memory."""
    with SurveyReader(recorded_survey) as reader:
        assert reader.n_observations > 20
        assert reader.manifest["format"] == "spectramap-survey"
        assert reader.manifest["configuration"]["fft"]["size"] == 512
        assert reader.spectrum(0).n_bins == 512


def test_every_observation_carries_its_acquisition_context(recorded_survey):
    with SurveyReader(recorded_survey) as reader:
        row = reader.observation_rows()[5]
        assert row["center_frequency_hz"] > 0
        assert row["sample_rate_hz"] == 250_000
        assert row["gain_db"] is not None
        assert row["agc"] == 0
        assert row["n_ffts"] > 0
        assert row["n_bins"] == 512


def test_lo_is_tuned_off_the_target_frequency(recorded_survey):
    """Offset tuning must reach the hardware, not just the configuration file."""
    with SurveyReader(recorded_survey) as reader:
        row = reader.observation_rows()[0]
        config = reader.config
        assert row["center_frequency_hz"] == pytest.approx(config.capture.tuned_center_hz)
        assert row["center_frequency_hz"] != config.capture.center_frequency_hz


def test_positions_are_interpolated_not_extrapolated(recorded_survey):
    """Holding an observation for its bracketing fix is what buys this.

    Resolving immediately would leave roughly half the survey extrapolated, purely
    because the next 1 Hz fix had not arrived yet.
    """
    with SurveyReader(recorded_survey) as reader:
        rows = reader.observation_rows()
        extrapolated = sum(
            1 for r in rows if QualityFlag.POSITION_EXTRAPOLATED & QualityFlag(r["flags"])
        )
        # Only the tail of the survey, after the last fix, may be extrapolated.
        assert extrapolated <= 2
        assert all(r["latitude"] is not None for r in rows)


def test_raw_position_track_is_stored_alongside_observations(recorded_survey):
    with SurveyReader(recorded_survey) as reader:
        assert len(reader.positions()) > 10


def test_timestamps_are_monotonic_and_consistent(recorded_survey):
    with SurveyReader(recorded_survey) as reader:
        rows = reader.observation_rows()
        starts = np.array([r["t_start_utc_ns"] for r in rows])
        mono = np.array([r["t_start_mono_ns"] for r in rows])
        assert np.all(np.diff(starts) > 0)
        assert np.all(np.diff(mono) > 0)
        # The two clocks must advance together.
        assert np.allclose(np.diff(starts), np.diff(mono), rtol=1e-6)


def test_integration_interval_is_respected(recorded_survey):
    """Full windows honour the configured interval; a truncated tail says so."""
    with SurveyReader(recorded_survey) as reader:
        rows = reader.observation_rows()
        full, partial = [], []
        for r in rows:
            duration_ms = (r["t_end_utc_ns"] - r["t_start_utc_ns"]) / 1e6
            target = partial if QualityFlag.PARTIAL_INTEGRATION & QualityFlag(r["flags"]) else full
            target.append(duration_ms)

        # Cannot be shorter than the configured 500 ms, and should not overshoot by
        # more than one block (32.8 ms at these settings).
        assert min(full) >= 500.0
        assert max(full) < 560.0
        # At most the final window is truncated, and it is flagged rather than being
        # silently averaged in as if it were a full-length integration.
        assert len(partial) <= 1
        assert all(d < 500.0 for d in partial)


def test_measurements_can_be_reprocessed_from_stored_spectra(recorded_survey):
    """Reprocessing must not need the original IQ, only what the survey stored."""
    from spectramap.dsp.detect import detect_peaks

    with SurveyReader(recorded_survey) as reader:
        spectrum = reader.spectrum(reader.n_observations // 2)
        peaks = detect_peaks(spectrum, threshold_db=10.0)
        assert peaks
        assert any(abs(p.frequency_hz - 433_920_000.0) < 2000 for p in peaks)


# ---------------------------------------------------------------------------
# Things going wrong
# ---------------------------------------------------------------------------


def test_dropped_samples_are_flagged_and_logged(tmp_path):
    world = compact_world()
    clock = SimClock()
    device = MockSDR(world, clock, drop_blocks=frozenset(range(20, 40)))
    recorder, path = _record(tmp_path, duration=20.0, world=world, device=device, name="drop")

    assert recorder.status.dropped_sample_events == 20
    with SurveyReader(path) as reader:
        assert any(e["kind"] == "dropped_samples" for e in reader.events())
        flagged = [
            r for r in reader.observation_rows()
            if QualityFlag.DROPPED_SAMPLES & QualityFlag(r["flags"])
        ]
        assert flagged, "affected observations must be marked"
        assert reader.n_observations > 0, "the survey is still usable"


def test_device_failure_still_produces_a_readable_survey(tmp_path):
    """A receiver that dies mid-walk must not cost the data already collected."""
    world = compact_world()
    clock = SimClock()
    device = MockSDR(world, clock, fail_at_block=200)
    recorder, path = _record(tmp_path, duration=60.0, world=world, device=device, name="fail")

    assert recorder.status.errors
    with SurveyReader(path) as reader:
        assert reader.n_observations > 0
        assert reader.manifest["summary"]["complete"] is False
        assert any(e["kind"] == "device_failure" for e in reader.events())


def test_gps_dropout_produces_flagged_observations_not_missing_ones(tmp_path):
    """RF data collected without a position is still RF data. Flag it, keep it."""
    recorder, path = _record(
        tmp_path,
        overrides={
            "simulation": {"gps_dropout_windows": [[5.0, 20.0]]},
            "location": {"provider": "mock", "max_age_ms": 2000.0},
        },
        duration=25.0,
        name="gps",
    )
    with SurveyReader(path) as reader:
        rows = reader.observation_rows()
        without = [r for r in rows if r["latitude"] is None]
        assert without, "the dropout must produce positionless observations"
        assert all(
            QualityFlag.NO_LOCATION & QualityFlag(r["flags"]) for r in without
        )
        # They are stored, not dropped.
        assert len(rows) > len(without)


def test_agc_survey_is_flagged_on_every_observation(tmp_path):
    recorder, path = _record(
        tmp_path, overrides={"receiver": {"driver": "mock", "gain": "auto"}},
        duration=15.0, name="agc",
    )
    with SurveyReader(path) as reader:
        rows = reader.observation_rows()
        assert all(QualityFlag.AGC_ACTIVE & QualityFlag(r["flags"]) for r in rows)
        assert any("automatic gain" in w for w in reader.warnings)


def test_configuration_warnings_are_written_into_the_manifest(tmp_path):
    """A later reader must see the same caveats the surveyor saw."""
    recorder, path = _record(
        tmp_path,
        overrides={
            "capture": {**COMPACT_CONFIG["capture"], "offset_tuning_hz": 0},
        },
        duration=8.0,
        name="warn",
    )
    with SurveyReader(path) as reader:
        assert any("DC" in w or "dc" in w for w in reader.warnings)


def test_max_observations_limit_is_honoured(tmp_path):
    recorder, path = _record(
        tmp_path, overrides={"quality": {"max_observations": 5}}, duration=600.0, name="lim"
    )
    with SurveyReader(path) as reader:
        assert reader.n_observations == 5


def test_stop_request_finishes_cleanly(tmp_path):
    """Ctrl+C must end a survey, not corrupt one."""
    config = SurveyConfig.from_dict(COMPACT_CONFIG)
    world = compact_world()
    session = build_session(config, world=world)
    recorder = SurveyRecorder(
        config, session.device, session.positions, tmp_path / "stop.smap"
    )

    def stop_after_a_few(status):
        if status.observations >= 4:
            recorder.request_stop()

    recorder.on_status = stop_after_a_few
    path = recorder.run(max_duration_s=600.0)
    with SurveyReader(path) as reader:
        assert 4 <= reader.n_observations <= 6
        assert reader.manifest["summary"]["complete"] is True


def test_sweep_mode_records_multiple_windows(tmp_path):
    """Sweep steps must be distinguishable in the data, not blurred together."""
    recorder, path = _record(
        tmp_path,
        overrides={
            "survey": {"name": "sweep", "mode": "sweep"},
            "capture": {
                **COMPACT_CONFIG["capture"],
                "sweep": {
                    "start_hz": 433_800_000,
                    "stop_hz": 434_050_000,
                    "step_hz": 125_000,
                    "dwell_ms": 1000,
                },
            },
        },
        duration=30.0,
        name="sweep",
    )
    with SurveyReader(path) as reader:
        steps = {r["sweep_step"] for r in reader.observation_rows()}
        assert len(steps) >= 2
        assert len(reader.frequency_coverage()) >= 2
        assert any(e["kind"] == "retune" for e in reader.events())
        assert reader.manifest["mode"] == "sweep"
        assert any("not a simultaneous wideband capture" in w for w in reader.warnings)


def test_simulation_ground_truth_is_recorded_but_segregated(recorded_survey):
    """Truth is kept for scoring, under its own key, never mixed into measurements."""
    with SurveyReader(recorded_survey) as reader:
        truth = reader.manifest["simulation_ground_truth"]
        assert truth["simulated"] is True
        assert any(tx["tx_id"] == "beacon-433" for tx in truth["transmitters"])
        # It is not part of the configuration or of any observation.
        assert "simulation_ground_truth" not in reader.manifest["configuration"]
