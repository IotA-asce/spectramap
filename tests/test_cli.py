"""CLI smoke tests: every command runs, and none of them overstate the data."""

from __future__ import annotations

import json

import pytest

from spectramap.cli import format_hz, main


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_devices_lists_the_simulator(capsys):
    code, out, _ = run(capsys, "devices")
    assert code == 0
    assert "Mock SDR" in out
    # The tuning range must be labelled as assumed, because it is.
    assert "assumed from tuner model" in out


def test_devices_json_is_machine_readable(capsys):
    code, out, _ = run(capsys, "devices", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["devices"][0]["driver"] == "mock"
    assert payload["devices"][0]["capabilities"]["tuning_evidence"] == "assumed"
    assert any(d["driver"] == "rtl-sdr" for d in payload["drivers"])


def test_gps_reports_a_simulated_fix(capsys):
    code, out, _ = run(capsys, "gps", "--provider", "mock")
    assert code == 0
    assert "GPS connected" in out
    assert "Accuracy" in out


@pytest.mark.parametrize(
    "arguments, source",
    [
        (("--provider", "mock"), "mock"),
        (("--provider", "static", "--latitude", "12.5", "--longitude", "-25"), "static"),
    ],
)
def test_gps_json_is_machine_readable(capsys, arguments, source):
    code, out, err = run(capsys, "gps", *arguments, "--json")
    assert code == 0
    fixes = json.loads(out)
    assert len(fixes) == 1
    assert fixes[0]["source"] == source
    assert "Position source" not in out
    assert not err


def test_gps_json_no_fix_reports_only_to_stderr(capsys):
    code, out, err = run(capsys, "gps", "--provider", "mock", "--timeout", "0", "--json")
    assert code == 2
    assert not out
    assert "No fix" in err


def test_gps_json_invalid_provider_configuration_keeps_stdout_empty(capsys):
    code, out, err = run(capsys, "gps", "--provider", "nmea", "--json")
    assert code == 1
    assert not out
    assert "--port is required" in err


def test_gps_nmea_without_a_port_fails_clearly(capsys):
    code, _, err = run(capsys, "gps", "--provider", "nmea")
    assert code == 1
    assert "--port is required" in err


def test_dry_run_validates_without_recording(capsys, tmp_path):
    out_path = tmp_path / "dry.smap"
    code, out, _ = run(
        capsys, "survey", "--driver", "mock", "-f", "433.92MHz",
        "-o", str(out_path), "--dry-run",
    )
    assert code == 0
    assert "configuration validated" in out
    assert not out_path.exists()


def test_survey_shows_the_offset_tuning_it_will_use(capsys, tmp_path):
    code, out, _ = run(
        capsys, "survey", "--driver", "mock", "-f", "433.92MHz",
        "-o", str(tmp_path / "x.smap"), "--dry-run",
    )
    assert "Tuned to" in out
    assert "offset" in out and "clear DC" in out


def test_agc_survey_warns_before_recording(capsys, tmp_path):
    code, _, err = run(
        capsys, "survey", "--driver", "mock", "-f", "433.92MHz", "--agc",
        "-o", str(tmp_path / "y.smap"), "--dry-run",
    )
    assert code == 0
    assert "automatic gain" in err.lower()


def test_bad_frequency_for_the_sample_rate_is_refused(capsys, tmp_path):
    """Offset tuning larger than the span would silently measure nothing."""
    code, _, err = run(
        capsys, "survey", "--driver", "mock", "-f", "433.92MHz",
        "--sample-rate", "250kHz", "--offset-tuning", "250kHz",
        "-o", str(tmp_path / "z.smap"), "--dry-run",
    )
    assert code == 1
    assert "outside the captured span" in err


def test_refuses_to_overwrite_without_force(capsys, tmp_path, recorded_survey):
    code, _, err = run(
        capsys, "survey", "--driver", "mock", "-o", str(recorded_survey), "--dry-run"
    )
    # --dry-run short-circuits before the existence check only if it passes; the
    # existence check comes first, so this must fail.
    assert code == 1
    assert "already exists" in err


@pytest.mark.parametrize("command", ["status", "analyze"])
def test_missing_survey_fails_gracefully(capsys, tmp_path, command):
    code, _, err = run(capsys, command, str(tmp_path / "nope.smap"))
    assert code == 1
    assert "not a SpectraMap survey" in err


def test_status_reports_conditions_and_flags(capsys, recorded_survey):
    code, out, _ = run(capsys, "status", str(recorded_survey))
    assert code == 0
    assert "Observations" in out
    assert "relative, uncalibrated" in out
    assert "dB (fixed)" in out
    assert "observations are flagged, never discarded" in out


def test_status_json(capsys, recorded_survey):
    code, out, _ = run(capsys, "status", str(recorded_survey), "--json")
    payload = json.loads(out)
    assert payload["observations"] > 20
    assert payload["calibrated"] is False
    assert payload["power_unit"] == "dBFS/Hz"


def test_analyze_reports_the_reconstruction_with_caveats(capsys, recorded_survey):
    code, out, _ = run(
        capsys, "analyze", str(recorded_survey), "-f", "433.92MHz", "--no-write"
    )
    assert code == 0
    assert "[ESTIMATED]" in out
    assert "Probable strongest-signal region" in out
    assert "not where a transmitter is" in out
    assert "masked, not drawn" in out
    assert "relative, uncalibrated" in out


def test_analyze_json_and_products(capsys, recorded_survey, tmp_path):
    code, out, _ = run(
        capsys, "analyze", str(recorded_survey), "-f", "433.92MHz",
        "--out", str(tmp_path / "products"), "--json",
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["band"]["method"] == "peak"
    assert payload["hotspot"]["evidence"] == "estimated"
    assert payload["field"]["provenance"]["algorithm"] == "inverse-distance-weighting"
    for path in payload["outputs"].values():
        assert path


def test_analyze_alias_spelling_works(capsys, recorded_survey):
    code, _, _ = run(
        capsys, "analyse", str(recorded_survey), "-f", "433.92MHz", "--no-write"
    )
    assert code == 0


def test_analyze_of_an_unmeasured_frequency_explains_the_coverage(capsys, recorded_survey):
    code, _, err = run(
        capsys, "analyze", str(recorded_survey), "-f", "868MHz", "--no-write"
    )
    assert code == 1
    assert "The survey measured" in err


@pytest.mark.parametrize(
    "hz, expected",
    [(433_920_000, "433.920000 MHz"), (1.2e9, "1.200000 GHz"), (12_500, "12.500 kHz")],
)
def test_frequency_formatting(hz, expected):
    assert format_hz(hz) == expected


def test_end_to_end_record_then_analyze(capsys, tmp_path):
    """The README workflow, run for real."""
    survey = tmp_path / "cli-run.smap"
    code, _, _ = run(
        capsys, "survey", "--driver", "mock", "-f", "433.92MHz", "-g", "24",
        "--sample-rate", "250kHz", "--offset-tuning", "50kHz", "--fft-size", "512",
        "-d", "20", "-o", str(survey),
    )
    assert code == 0
    assert survey.exists()

    code, out, _ = run(capsys, "analyze", str(survey), "-f", "433.92MHz")
    assert code == 0
    assert "Written" in out
    assert (survey / "processing").is_dir()
