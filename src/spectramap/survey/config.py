"""Survey configuration: the reproducibility contract.

A survey must be re-runnable from its configuration, and an archived survey must
carry enough configuration to explain how it was acquired. Both directions run
through this module: a :class:`SurveyConfig` is loaded from YAML to start a survey,
and is serialised verbatim into the survey manifest when one is recorded.

Validation happens here rather than deep in the recorder, so that a mistake costs a
message before the walk instead of a ruined dataset after it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

from spectramap.types import AntennaInfo, FFTConfig, FrequencyRange, Normalization


class SurveyMode(str, Enum):
    """What kind of survey this is. Recorded so a viewer cannot misrepresent it.

    ``FIXED`` and ``NARROWBAND`` stare at one centre frequency continuously.
    ``SWEEP`` steps the tuner through a sequence of windows, so different parts of
    the displayed span were measured at different times and (on foot) different
    places. The distinction is in the data model precisely so that a wideband sweep
    is never drawn as if it were an instantaneous wideband capture.
    ``STATIONARY`` observes change over time from one location.
    """

    FIXED = "fixed"
    NARROWBAND = "narrowband"
    SWEEP = "sweep"
    STATIONARY = "stationary"

    @property
    def is_sequential(self) -> bool:
        return self is SurveyMode.SWEEP


@dataclass(frozen=True, slots=True)
class ReceiverSettings:
    driver: str = "mock"
    index: int = 0
    serial: str | None = None
    # Fixed gain is the default on purpose. See docs/calibration.md: under AGC the
    # receiver rescales itself as you walk and spatial power comparison stops meaning
    # anything.
    gain_db: float | None = 24.0
    freq_correction_ppm: int = 0

    @property
    def agc(self) -> bool:
        return self.gain_db is None


@dataclass(frozen=True, slots=True)
class SweepSettings:
    """Sequential retuning across a span.

    ``dwell_ms`` is time spent per step. The full span is revisited every
    ``n_steps * dwell_ms``, which is the revisit period a viewer must show alongside
    any wideband picture.
    """

    start_hz: float
    stop_hz: float
    step_hz: float
    dwell_ms: float = 250.0

    def steps(self) -> list[float]:
        """Centre frequencies of each window, low to high."""
        if self.step_hz <= 0:
            raise ValueError("sweep step_hz must be positive")
        if self.stop_hz <= self.start_hz:
            raise ValueError("sweep stop_hz must exceed start_hz")
        out: list[float] = []
        f = self.start_hz + self.step_hz / 2.0
        while f - self.step_hz / 2.0 < self.stop_hz:
            out.append(f)
            f += self.step_hz
        return out


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """What to measure, and where to put the receiver's local oscillator.

    ``center_frequency_hz`` is the frequency of *interest* -- what the user wants to
    measure. ``offset_tuning_hz`` is how far away from it the receiver is actually
    tuned, and it defaults to a non-zero value on purpose.

    A zero-IF receiver cannot measure its own tuned frequency. DC offset in the ADC
    and local-oscillator self-mixing pile a large artefact into the centre bin, and
    the DC removal that suppresses that artefact removes any real signal sitting
    there along with it. Tuning exactly onto a target therefore reliably reports the
    one frequency you care about as empty. Offsetting the LO moves the target away
    from DC while keeping it inside the captured span; the frequency axis stays
    absolute, so the target still appears at its true frequency.
    """

    center_frequency_hz: float = 433_920_000.0
    sample_rate_hz: float = 2_400_000.0
    # Samples per driver read. Smaller means finer time resolution and more overhead;
    # at 2.4 MS/s, 262144 samples is ~109 ms.
    block_samples: int = 262_144
    # Default is ~10% of a 2.4 MS/s span: comfortably clear of the DC artefact, well
    # inside the analogue filter's flat region.
    offset_tuning_hz: float = 250_000.0
    sweep: SweepSettings | None = None

    @property
    def tuned_center_hz(self) -> float:
        """The frequency the local oscillator is actually set to."""
        return self.center_frequency_hz - self.offset_tuning_hz

    @property
    def span_hz(self) -> FrequencyRange:
        """Absolute frequency range the receiver can see, given the tuned LO."""
        half = self.sample_rate_hz / 2.0
        return FrequencyRange(self.tuned_center_hz - half, self.tuned_center_hz + half)


@dataclass(frozen=True, slots=True)
class AggregationSettings:
    """How many FFTs become one geotagged observation.

    ``interval_ms`` trades spatial resolution against noise. At 1.3 m/s, 500 ms is
    0.65 m of travel -- well inside GPS error, so nothing is lost spatially, while
    averaging several FFTs cuts the variance of the estimate.
    """

    interval_ms: float = 500.0
    # Optionally reduce stored bins by integer-factor binning, in the linear domain.
    # 1 keeps full resolution.
    bin_decimation: int = 1


@dataclass(frozen=True, slots=True)
class LocationSettings:
    provider: str = "mock"  # mock | nmea | static
    port: str | None = None
    baudrate: int = 9600
    latitude: float | None = None  # For the static provider.
    longitude: float | None = None
    altitude_m: float = 0.0
    update_rate_hz: float = 1.0
    # A fix older than this is not stretched to cover an observation.
    max_age_ms: float = 5_000.0
    # Above this reported accuracy, observations are flagged GPS_LOW_ACCURACY.
    accuracy_limit_m: float = 15.0


@dataclass(frozen=True, slots=True)
class QualitySettings:
    # Fraction of clipped samples above which SDR_OVERLOAD is raised.
    overload_fraction: float = 1e-4
    # Stop rather than fill the disk and produce a truncated survey silently.
    min_free_disk_mb: float = 200.0
    max_duration_s: float | None = None
    max_observations: int | None = None


@dataclass(frozen=True, slots=True)
class SimulationSettings:
    seed: int = 20260807
    scenario: str = "default"
    realtime: bool = False
    gps_dropout_windows: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class SurveyConfig:
    """The complete, validated description of how to record a survey."""

    name: str = "untitled-survey"
    mode: SurveyMode = SurveyMode.FIXED
    notes: str = ""
    receiver: ReceiverSettings = field(default_factory=ReceiverSettings)
    antenna: AntennaInfo = field(default_factory=AntennaInfo)
    capture: CaptureSettings = field(default_factory=CaptureSettings)
    fft: FFTConfig = field(default_factory=lambda: FFTConfig(size=4096))
    aggregation: AggregationSettings = field(default_factory=AggregationSettings)
    location: LocationSettings = field(default_factory=LocationSettings)
    quality: QualitySettings = field(default_factory=QualitySettings)
    simulation: SimulationSettings = field(default_factory=SimulationSettings)

    # -- Validation ---------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of warnings; raise on anything that makes data invalid.

        Warnings are for choices that are legal but will degrade the result, and they
        are surfaced to the user *and* written into the manifest so that a later
        reader sees the same caveats the surveyor did.
        """
        warnings: list[str] = []

        if self.capture.sample_rate_hz <= 0:
            raise ValueError("capture.sample_rate_hz must be positive")
        if self.capture.block_samples < self.fft.size:
            raise ValueError(
                f"capture.block_samples ({self.capture.block_samples}) must be at least "
                f"fft.size ({self.fft.size}); otherwise no FFT can be computed"
            )
        if self.aggregation.interval_ms <= 0:
            raise ValueError("aggregation.interval_ms must be positive")
        if self.aggregation.bin_decimation < 1:
            raise ValueError("aggregation.bin_decimation must be >= 1")

        block_ms = self.capture.block_samples / self.capture.sample_rate_hz * 1000.0
        if block_ms > self.aggregation.interval_ms:
            warnings.append(
                f"one IQ block spans {block_ms:.0f} ms but the aggregation interval is "
                f"{self.aggregation.interval_ms:.0f} ms; observations cannot be shorter "
                "than a block, so the effective interval will be longer than configured"
            )

        if self.receiver.agc:
            warnings.append(
                "automatic gain is enabled: the receiver will rescale itself as you move, "
                "so power CANNOT be compared between locations. Every observation will be "
                "flagged AGC_ACTIVE. Set receiver.gain_db to a fixed value for any survey "
                "whose purpose is spatial comparison"
            )

        if self.mode is SurveyMode.SWEEP and self.capture.sweep is None:
            raise ValueError("mode 'sweep' requires a capture.sweep block")
        if self.capture.sweep is not None:
            steps = self.capture.sweep.steps()
            revisit_s = len(steps) * self.capture.sweep.dwell_ms / 1000.0
            warnings.append(
                f"sweep mode: {len(steps)} windows, revisit period {revisit_s:.1f} s. "
                "Different parts of the span are measured at different times and places; "
                "they are not a simultaneous wideband capture"
            )
            if self.capture.sweep.step_hz > self.capture.sample_rate_hz:
                raise ValueError(
                    f"sweep step ({self.capture.sweep.step_hz / 1e6:.3f} MHz) is wider than "
                    f"the capture bandwidth ({self.capture.sample_rate_hz / 1e6:.3f} MHz); "
                    "this would leave unmeasured gaps between windows"
                )

        if self.location.provider == "nmea" and not self.location.port:
            raise ValueError("location.provider 'nmea' requires location.port")
        if self.location.provider == "static" and (
            self.location.latitude is None or self.location.longitude is None
        ):
            raise ValueError("location.provider 'static' requires latitude and longitude")
        if self.location.provider == "static" and self.mode is not SurveyMode.STATIONARY:
            warnings.append(
                "a static position provider with a moving survey mode will place every "
                "observation at the same coordinate; no spatial structure can be recovered"
            )

        if self.capture.sweep is None:
            span = self.capture.span_hz
            target = self.capture.center_frequency_hz
            offset = self.capture.offset_tuning_hz
            resolution = self.fft.resolution_hz(self.capture.sample_rate_hz)

            if not span.contains(target):
                raise ValueError(
                    f"the target frequency {target / 1e6:.5f} MHz falls outside the captured "
                    f"span {span.min_hz / 1e6:.5f}-{span.max_hz / 1e6:.5f} MHz. "
                    f"offset_tuning_hz ({offset / 1e3:.0f} kHz) is too large for a sample "
                    f"rate of {self.capture.sample_rate_hz / 1e6:.3f} MS/s"
                )
            if self.fft.remove_dc and abs(offset) < 4 * resolution:
                warnings.append(
                    f"offset_tuning_hz is only {offset / 1e3:.1f} kHz, within a few bins of "
                    f"DC ({resolution / 1e3:.2f} kHz each). The receiver's DC artefact and "
                    "the DC removal that suppresses it both sit on the target frequency, so "
                    "the target will read as empty. Increase capture.offset_tuning_hz"
                )
            if abs(offset) > 0.4 * self.capture.sample_rate_hz:
                warnings.append(
                    "the target sits near the edge of the captured span, where the receiver's "
                    "analogue filter rolls off; measured power there under-reads"
                )
            if 0.8 * self.capture.sample_rate_hz < 4 * resolution:
                warnings.append("very few usable bins across the span; consider a larger FFT")

        return warnings

    # -- Derived quantities -------------------------------------------------

    def observations_per_second(self) -> float:
        return 1000.0 / self.aggregation.interval_ms

    def stored_bins(self) -> int:
        return self.fft.size // self.aggregation.bin_decimation

    def bytes_per_observation(self) -> int:
        """Spectrum payload only, float32."""
        return self.stored_bins() * 4

    def estimated_bytes_per_hour(self) -> int:
        """Payload plus roughly 200 bytes of index row per observation."""
        per_obs = self.bytes_per_observation() + 200
        return int(per_obs * self.observations_per_second() * 3600)

    # -- Serialisation ------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "survey": {"name": self.name, "mode": self.mode.value, "notes": self.notes},
            "receiver": asdict(self.receiver),
            "antenna": self.antenna.to_dict(),
            "capture": {
                "center_frequency_hz": self.capture.center_frequency_hz,
                "sample_rate_hz": self.capture.sample_rate_hz,
                "block_samples": self.capture.block_samples,
                "offset_tuning_hz": self.capture.offset_tuning_hz,
                "tuned_center_hz": self.capture.tuned_center_hz,
                "sweep": asdict(self.capture.sweep) if self.capture.sweep else None,
            },
            "fft": self.fft.to_dict(),
            "aggregation": asdict(self.aggregation),
            "location": asdict(self.location),
            "quality": asdict(self.quality),
            "simulation": {
                "seed": self.simulation.seed,
                "scenario": self.simulation.scenario,
                "realtime": self.simulation.realtime,
                "gps_dropout_windows": [list(w) for w in self.simulation.gps_dropout_windows],
            },
        }
        return d

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SurveyConfig:
        data = data or {}
        survey = data.get("survey") or {}
        rx = data.get("receiver") or {}
        ant = data.get("antenna") or {}
        cap = data.get("capture") or {}
        fft = data.get("fft") or {}
        agg = data.get("aggregation") or {}
        loc = data.get("location") or {}
        qual = data.get("quality") or {}
        sim = data.get("simulation") or {}

        sweep_raw = cap.get("sweep")
        sweep = (
            SweepSettings(
                start_hz=_hz(sweep_raw["start_hz"]),
                stop_hz=_hz(sweep_raw["stop_hz"]),
                step_hz=_hz(sweep_raw["step_hz"]),
                dwell_ms=float(sweep_raw.get("dwell_ms", 250.0)),
            )
            if sweep_raw
            else None
        )

        antenna_range = ant.get("frequency_range")
        antenna = AntennaInfo(
            antenna_id=str(ant.get("id", ant.get("antenna_id", "unknown"))),
            type=str(ant.get("type", "unknown")),
            frequency_range=FrequencyRange(
                _hz(antenna_range["min_hz"]), _hz(antenna_range["max_hz"])
            )
            if antenna_range
            else None,
            polarization=str(ant.get("polarization", "unknown")),
            orientation_deg=_opt_float(ant.get("orientation_deg")),
            gain_dbi=_opt_float(ant.get("gain_dbi", ant.get("gain_if_known"))),
            notes=str(ant.get("notes", "")),
        )

        defaults = cls()
        return cls(
            name=str(survey.get("name", defaults.name)),
            mode=SurveyMode(str(survey.get("mode", defaults.mode.value)).lower()),
            notes=str(survey.get("notes", "")),
            receiver=ReceiverSettings(
                driver=str(rx.get("driver", "mock")),
                index=int(rx.get("index", 0)),
                serial=_opt_str(rx.get("serial")),
                gain_db=_parse_gain(rx.get("gain", rx.get("gain_db", 24.0))),
                freq_correction_ppm=int(rx.get("freq_correction_ppm", 0)),
            ),
            antenna=antenna,
            capture=CaptureSettings(
                center_frequency_hz=_hz(
                    cap.get("center_frequency_hz", defaults.capture.center_frequency_hz)
                ),
                sample_rate_hz=_hz(cap.get("sample_rate_hz", defaults.capture.sample_rate_hz)),
                block_samples=int(cap.get("block_samples", defaults.capture.block_samples)),
                offset_tuning_hz=_hz(
                    cap.get("offset_tuning_hz", defaults.capture.offset_tuning_hz)
                ),
                sweep=sweep,
            ),
            fft=FFTConfig(
                size=int(fft.get("size", 4096)),
                window=str(fft.get("window", "hann")),
                overlap=float(fft.get("overlap", 0.5)),
                normalization=Normalization(str(fft.get("normalization", "density")).lower()),
                remove_dc=bool(fft.get("remove_dc", True)),
            ),
            aggregation=AggregationSettings(
                interval_ms=float(agg.get("interval_ms", 500.0)),
                bin_decimation=int(agg.get("bin_decimation", 1)),
            ),
            location=LocationSettings(
                provider=str(loc.get("provider", "mock")).lower(),
                port=_opt_str(loc.get("port")),
                baudrate=int(loc.get("baudrate", 9600)),
                latitude=_opt_float(loc.get("latitude")),
                longitude=_opt_float(loc.get("longitude")),
                altitude_m=float(loc.get("altitude_m", 0.0)),
                update_rate_hz=float(loc.get("update_rate_hz", 1.0)),
                max_age_ms=float(loc.get("max_age_ms", 5000.0)),
                accuracy_limit_m=float(loc.get("accuracy_limit_m", 15.0)),
            ),
            quality=QualitySettings(
                overload_fraction=float(qual.get("overload_fraction", 1e-4)),
                min_free_disk_mb=float(qual.get("min_free_disk_mb", 200.0)),
                max_duration_s=_opt_float(qual.get("max_duration_s")),
                max_observations=_opt_int(qual.get("max_observations")),
            ),
            simulation=SimulationSettings(
                seed=int(sim.get("seed", 20260807)),
                scenario=str(sim.get("scenario", "default")),
                realtime=bool(sim.get("realtime", False)),
                gps_dropout_windows=tuple(
                    (float(a), float(b)) for a, b in sim.get("gps_dropout_windows", [])
                ),
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> SurveyConfig:
        text = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text) or {})

    def with_center_frequency(self, hz: float) -> SurveyConfig:
        """A copy retuned to ``hz``; used to walk sweep steps."""
        return replace(self, capture=replace(self.capture, center_frequency_hz=float(hz)))


# ---------------------------------------------------------------------------
# Small parsers so that human-written YAML can say "433.92MHz" instead of a
# nine-digit integer, without the ambiguity of guessing units from magnitude.
# ---------------------------------------------------------------------------

_SUFFIXES = {"ghz": 1e9, "mhz": 1e6, "khz": 1e3, "hz": 1.0, "g": 1e9, "m": 1e6, "k": 1e3}


def parse_frequency(value: str | float | int) -> float:
    """Parse ``433.92MHz``, ``433920 kHz`` or a bare number of hertz.

    A bare number is always hertz. Guessing units from magnitude would mean
    ``433.92`` silently becomes 433.92 Hz for one user and 433.92 MHz for another.
    """
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(" ", "").replace("_", "")
    for suffix, scale in sorted(_SUFFIXES.items(), key=lambda kv: -len(kv[0])):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * scale
    return float(text)


_hz = parse_frequency


def _parse_gain(value: Any) -> float | None:
    """``auto``/``null`` means AGC; anything else is a fixed gain in dB."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("auto", "agc", "none", "null"):
        return None
    return float(value)


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _opt_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "AggregationSettings",
    "CaptureSettings",
    "LocationSettings",
    "QualitySettings",
    "ReceiverSettings",
    "SimulationSettings",
    "SurveyConfig",
    "SurveyMode",
    "SweepSettings",
    "parse_frequency",
]
