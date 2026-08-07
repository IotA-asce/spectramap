"""Shared fixtures.

The whole suite runs with no radio hardware and no network. The one expensive
fixture -- an actually recorded simulated survey -- is session scoped, so the full
acquisition path runs once and every downstream test reads the same artefact.
"""

from __future__ import annotations

import numpy as np
import pytest

from spectramap.simulation.world import compact_world
from spectramap.survey.config import SurveyConfig
from spectramap.survey.recorder import SurveyRecorder
from spectramap.survey.session import build_session

# The compact CI scenario: 250 kS/s, 512-point FFT, an 80 m grid walked at 5 m/s.
# offset_tuning is 50 kHz because a 250 kS/s capture only spans +/-125 kHz, so the
# 250 kHz default would put the target outside the measurable span.
COMPACT_CONFIG: dict = {
    "survey": {"name": "ci-compact", "mode": "fixed"},
    "receiver": {"driver": "mock", "gain": 24.0},
    "capture": {
        "center_frequency_hz": 433_920_000,
        "sample_rate_hz": 250_000,
        "block_samples": 8192,
        "offset_tuning_hz": 50_000,
    },
    "fft": {"size": 512, "window": "hann"},
    "aggregation": {"interval_ms": 500},
    "location": {"provider": "mock"},
    "antenna": {"id": "whip-433", "type": "quarter-wave whip", "polarization": "vertical"},
}

BEACON_HZ = 433_920_000.0
CONTROL_HZ = 433_950_000.0


@pytest.fixture(scope="session")
def world():
    return compact_world()


@pytest.fixture
def compact_config() -> SurveyConfig:
    return SurveyConfig.from_dict(COMPACT_CONFIG)


@pytest.fixture(scope="session")
def recorded_survey(tmp_path_factory, world):
    """Record the compact scenario once; return the path to the finished survey."""
    out = tmp_path_factory.mktemp("surveys") / "ci-compact.smap"
    config = SurveyConfig.from_dict(COMPACT_CONFIG)
    session = build_session(config, world=world)
    recorder = SurveyRecorder(
        config,
        session.device,
        session.positions,
        out,
        extra_manifest=session.manifest_extras(),
    )
    return recorder.run(max_duration_s=world.route.duration_s)


@pytest.fixture
def reader(recorded_survey):
    from spectramap.storage import SurveyReader

    with SurveyReader(recorded_survey) as r:
        yield r


def tone(
    frequency_hz: float,
    center_hz: float,
    sample_rate_hz: float,
    n: int,
    amplitude: float = 1.0,
    phase: float = 0.0,
) -> np.ndarray:
    """A clean complex tone at an absolute frequency, as the receiver would see it."""
    t = np.arange(n, dtype=np.float64) / sample_rate_hz
    return (amplitude * np.exp(1j * (2 * np.pi * (frequency_hz - center_hz) * t + phase))).astype(
        np.complex64
    )


def complex_noise(psd_dbfs_per_hz: float, sample_rate_hz: float, n: int, seed: int = 0):
    """Complex Gaussian noise with a known power spectral density."""
    rng = np.random.default_rng(seed)
    sigma = np.sqrt(10 ** (psd_dbfs_per_hz / 10) * sample_rate_hz / 2)
    return (rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)).astype(np.complex64)
