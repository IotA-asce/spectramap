"""Survey configuration: parsing, validation, and the warnings that protect a survey.

Validation exists so that a mistake costs a message before the walk rather than a
useless dataset after it.
"""

from __future__ import annotations

import pytest
import yaml

from spectramap.survey.config import SurveyConfig, SurveyMode, SweepSettings, parse_frequency

# ---------------------------------------------------------------------------
# Frequency parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("433.92MHz", 433_920_000.0),
        ("433.92 mhz", 433_920_000.0),
        ("433920 kHz", 433_920_000.0),
        ("1.2GHz", 1_200_000_000.0),
        ("433920000", 433_920_000.0),
        (433_920_000, 433_920_000.0),
        ("433_920_000Hz", 433_920_000.0),
    ],
)
def test_frequency_parsing(text, expected):
    assert parse_frequency(text) == pytest.approx(expected)


def test_a_bare_number_is_always_hertz():
    """Guessing units from magnitude would make 433.92 mean different things."""
    assert parse_frequency("433.92") == pytest.approx(433.92)


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_config_survives_a_yaml_round_trip(tmp_path):
    """A survey must be reproducible from its recorded configuration."""
    original = SurveyConfig.from_dict(
        {
            "survey": {"name": "neighbourhood-433", "mode": "narrowband", "notes": "evening"},
            "receiver": {"driver": "rtl-sdr", "gain": 28.0, "freq_correction_ppm": 12},
            "capture": {"center_frequency_hz": "433.92MHz", "sample_rate_hz": "2.4MHz"},
            "fft": {"size": 8192, "window": "blackman-harris", "overlap": 0.75},
            "aggregation": {"interval_ms": 250, "bin_decimation": 2},
            "antenna": {"id": "yagi-7", "type": "yagi", "gain_dbi": 9.5},
        }
    )
    path = tmp_path / "survey.yaml"
    path.write_text(original.to_yaml())
    restored = SurveyConfig.load(path)

    assert restored.name == original.name
    assert restored.mode is SurveyMode.NARROWBAND
    assert restored.receiver.gain_db == 28.0
    assert restored.receiver.freq_correction_ppm == 12
    assert restored.capture.center_frequency_hz == 433_920_000.0
    assert restored.fft.size == 8192
    assert restored.fft.window == "blackman-harris"
    assert restored.aggregation.bin_decimation == 2
    assert restored.antenna.antenna_id == "yagi-7"
    assert restored.antenna.gain_dbi == 9.5


def test_yaml_is_human_readable():
    text = SurveyConfig().to_yaml()
    parsed = yaml.safe_load(text)
    assert parsed["survey"]["name"]
    assert parsed["capture"]["offset_tuning_hz"] == 250_000.0


# ---------------------------------------------------------------------------
# Gain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["auto", "agc", None])
def test_auto_gain_means_agc(value):
    config = SurveyConfig.from_dict({"receiver": {"gain": value}})
    assert config.receiver.gain_db is None
    assert config.receiver.agc is True


def test_agc_produces_a_loud_warning():
    """The single most damaging setting for a spatial survey."""
    warnings = SurveyConfig.from_dict({"receiver": {"gain": "auto"}}).validate()
    message = " ".join(warnings).lower()
    assert "automatic gain" in message
    assert "cannot be compared" in message


def test_fixed_gain_is_the_default():
    config = SurveyConfig()
    assert config.receiver.agc is False
    assert config.receiver.gain_db == 24.0


# ---------------------------------------------------------------------------
# Offset tuning
# ---------------------------------------------------------------------------


def test_default_offset_tuning_keeps_the_target_off_dc():
    config = SurveyConfig()
    assert config.capture.offset_tuning_hz > 0
    assert config.capture.tuned_center_hz < config.capture.center_frequency_hz
    assert config.capture.span_hz.contains(config.capture.center_frequency_hz)
    assert config.validate() == []


def test_zero_offset_warns_about_the_dc_artefact():
    config = SurveyConfig.from_dict({"capture": {"offset_tuning_hz": 0}})
    message = " ".join(config.validate()).lower()
    assert "dc" in message and "read as empty" in message


def test_offset_larger_than_the_span_is_an_error_not_a_warning():
    """Silently measuring nothing is much worse than refusing to start."""
    with pytest.raises(ValueError, match="outside the captured span"):
        SurveyConfig.from_dict(
            {"capture": {"sample_rate_hz": 250_000, "offset_tuning_hz": 250_000}}
        ).validate()


def test_offset_near_the_band_edge_warns_about_filter_rolloff():
    config = SurveyConfig.from_dict(
        {"capture": {"sample_rate_hz": 2_400_000, "offset_tuning_hz": 1_100_000}}
    )
    assert any("analogue filter rolls off" in w for w in config.validate())


# ---------------------------------------------------------------------------
# Structural validation
# ---------------------------------------------------------------------------


def test_block_shorter_than_the_fft_is_rejected():
    with pytest.raises(ValueError, match="must be at least"):
        SurveyConfig.from_dict(
            {"capture": {"block_samples": 1024}, "fft": {"size": 4096}}
        ).validate()


def test_block_longer_than_the_interval_warns_about_effective_resolution():
    config = SurveyConfig.from_dict(
        {
            "capture": {
                "sample_rate_hz": 250_000,
                "block_samples": 262_144,
                "offset_tuning_hz": 50_000,
            },
            "aggregation": {"interval_ms": 100},
        }
    )
    assert any("cannot be shorter than a block" in w for w in config.validate())


def test_sweep_mode_requires_a_sweep_block():
    with pytest.raises(ValueError, match="requires a capture.sweep"):
        SurveyConfig.from_dict({"survey": {"mode": "sweep"}}).validate()


def test_sweep_warns_that_it_is_not_a_simultaneous_capture():
    config = SurveyConfig.from_dict(
        {
            "survey": {"mode": "sweep"},
            "capture": {
                "sample_rate_hz": 2_400_000,
                "sweep": {"start_hz": "433MHz", "stop_hz": "435MHz", "step_hz": "2MHz"},
            },
        }
    )
    message = " ".join(config.validate())
    assert "not a simultaneous wideband capture" in message
    assert "revisit period" in message


def test_sweep_step_wider_than_the_bandwidth_is_rejected():
    """Otherwise the sweep leaves unmeasured gaps that the map would hide."""
    with pytest.raises(ValueError, match="unmeasured gaps"):
        SurveyConfig.from_dict(
            {
                "survey": {"mode": "sweep"},
                "capture": {
                    "sample_rate_hz": 1_000_000,
                    "sweep": {"start_hz": 433e6, "stop_hz": 440e6, "step_hz": 4e6},
                },
            }
        ).validate()


def test_sweep_steps_cover_the_requested_span():
    sweep = SweepSettings(start_hz=433e6, stop_hz=434e6, step_hz=250e3)
    steps = sweep.steps()
    assert len(steps) == 4
    assert steps[0] == pytest.approx(433.125e6)
    assert steps[-1] == pytest.approx(433.875e6)


def test_nmea_provider_requires_a_port():
    with pytest.raises(ValueError, match="requires location.port"):
        SurveyConfig.from_dict({"location": {"provider": "nmea"}}).validate()


def test_static_position_with_a_moving_mode_warns():
    config = SurveyConfig.from_dict(
        {
            "survey": {"mode": "fixed"},
            "location": {"provider": "static", "latitude": 1.0, "longitude": 2.0},
        }
    )
    assert any("same coordinate" in w for w in config.validate())


def test_invalid_fft_size_is_rejected():
    with pytest.raises(ValueError, match="power of two"):
        SurveyConfig.from_dict({"fft": {"size": 1000}})


# ---------------------------------------------------------------------------
# Data-rate estimation
# ---------------------------------------------------------------------------


def test_data_rate_estimate_is_sane():
    """Used to warn a user before they fill a card. 4096 bins at 2 obs/s ~= 120 MB/h."""
    config = SurveyConfig.from_dict(
        {"fft": {"size": 4096}, "aggregation": {"interval_ms": 500}}
    )
    assert config.observations_per_second() == pytest.approx(2.0)
    assert config.bytes_per_observation() == 4096 * 4
    per_hour_mb = config.estimated_bytes_per_hour() / 1e6
    assert 110 < per_hour_mb < 130


def test_bin_decimation_reduces_stored_size():
    config = SurveyConfig.from_dict(
        {"fft": {"size": 4096}, "aggregation": {"bin_decimation": 4}}
    )
    assert config.stored_bins() == 1024
    assert config.bytes_per_observation() == 1024 * 4
