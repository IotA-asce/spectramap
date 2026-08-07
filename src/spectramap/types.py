"""Core value types shared by every layer of SpectraMap.

Design rules encoded here:

* A power number is never bare. It always travels with a :class:`PowerUnit` so that
  uncalibrated receiver units can never be silently rendered as absolute dBm.
* A quantity is never presented without saying how it came to exist. That is what
  :class:`Evidence` is for: MEASURED, DERIVED, INTERPOLATED, ESTIMATED, ASSUMED.
* Acquisition metadata is not optional decoration. Two observations are comparable
  only if the receiver configuration behind them is known, so the configuration
  travels with the observation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from enum import Enum, IntFlag
from typing import Any

import numpy as np

# ---------------------------------------------------------------------------
# Epistemics
# ---------------------------------------------------------------------------


class Evidence(str, Enum):
    """How a number came to exist. Never hide this from the user.

    MEASURED      Came off the receiver (possibly with calibration applied).
    DERIVED       Deterministic function of measurements (e.g. band peak power).
    INTERPOLATED  Filled in between measurements by an interpolation algorithm.
    ESTIMATED     Inferred by a model with real uncertainty (e.g. a source region).
    ASSUMED       Supplied by the user or a default; not verified by this software.
    """

    MEASURED = "measured"
    DERIVED = "derived"
    INTERPOLATED = "interpolated"
    ESTIMATED = "estimated"
    ASSUMED = "assumed"


class PowerUnit(str, Enum):
    """Units a power value may carry.

    ``DBFS`` and ``DBFS_PER_HZ`` are *relative* receiver units: dB with respect to a
    full-scale complex sinusoid on the ADC, given the gain setting recorded alongside
    them. They are comparable across observations of one survey at fixed gain. They
    are **not** absolute field strength and must never be displayed as ``dBm``.

    ``DBM`` and ``DBM_PER_HZ`` are only legal once a calibration with a traceable
    reference has been applied. V0 does not produce them.
    """

    DBFS = "dBFS"
    DBFS_PER_HZ = "dBFS/Hz"
    DBM = "dBm"
    DBM_PER_HZ = "dBm/Hz"

    @property
    def is_calibrated(self) -> bool:
        """True only for units that claim absolute physical meaning."""
        return self in (PowerUnit.DBM, PowerUnit.DBM_PER_HZ)

    @property
    def is_density(self) -> bool:
        """True for per-Hz (spectral density) units."""
        return self in (PowerUnit.DBFS_PER_HZ, PowerUnit.DBM_PER_HZ)

    @property
    def label(self) -> str:
        """Human-facing label that keeps uncalibrated units honest."""
        if self.is_calibrated:
            return self.value
        return f"{self.value} (relative, uncalibrated)"


class Normalization(str, Enum):
    """FFT scaling convention. See docs/dsp-pipeline.md.

    ``SPECTRUM``  Power-per-bin, scaled so a full-scale complex tone at a bin centre
                  reads 0 dBFS. Correct for reading discrete carriers.
    ``DENSITY``   Power spectral density, per Hz. Correct for reading noise floors
                  and for comparing measurements taken at different FFT sizes.
    """

    SPECTRUM = "spectrum"
    DENSITY = "density"


class QualityFlag(IntFlag):
    """Per-observation quality annotations.

    Flags mark data; they never delete it. Analysis may filter on them, but the raw
    record always survives. Stored as a single integer column.
    """

    NONE = 0
    NO_LOCATION = 1 << 0  # No usable fix covered this observation.
    GPS_LOW_ACCURACY = 1 << 1  # Horizontal accuracy worse than configured limit.
    GPS_STALE = 1 << 2  # Nearest fix older than configured limit.
    POSITION_INTERPOLATED = 1 << 3  # Position interpolated between two fixes.
    POSITION_EXTRAPOLATED = 1 << 4  # Position held from a single bracketing fix.
    SDR_OVERLOAD = 1 << 5  # IQ near or at full scale; front end likely saturated.
    DROPPED_SAMPLES = 1 << 6  # Driver reported or timing implied a gap.
    GAIN_CHANGED = 1 << 7  # Gain differs from the previous observation.
    AGC_ACTIVE = 1 << 8  # Automatic gain: powers not spatially comparable.
    DEVICE_RECONNECTED = 1 << 9  # Receiver was reopened mid-survey.
    PARTIAL_INTEGRATION = 1 << 10  # Fewer FFTs averaged than configured.
    CLOCK_UNSYNCED = 1 << 11  # Host UTC not corroborated by GPS time.
    RETUNED = 1 << 12  # Centre frequency changed (sweep step boundary).

    @staticmethod
    def describe(value: QualityFlag | int) -> list[str]:
        """Return the names of the set flags, for display and export."""
        flags = QualityFlag(int(value))
        return [f.name for f in QualityFlag if f is not QualityFlag.NONE and f & flags]


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


class FixQuality(str, Enum):
    """Coarse classification of a position fix, mapped from the position source."""

    NONE = "none"
    ESTIMATED = "estimated"  # Dead reckoning.
    GPS = "gps"  # Standard single-point fix.
    DGPS = "dgps"
    RTK_FLOAT = "rtk_float"
    RTK_FIXED = "rtk_fixed"
    SIMULATED = "simulated"


@dataclass(frozen=True, slots=True)
class Position:
    """A single position fix.

    ``t_utc_ns`` is wall-clock UTC in nanoseconds since the Unix epoch.
    ``t_mono_ns`` is the host monotonic clock reading for the same instant; it is the
    clock used for aligning RF and position, because wall clock can step. See
    docs/architecture.md#time-synchronisation.

    Accuracies are 1-sigma estimates in metres as reported by the source. ``None``
    means "the source did not tell us", which is different from "zero".
    """

    t_utc_ns: int
    t_mono_ns: int
    latitude: float
    longitude: float
    altitude_m: float | None = None
    horizontal_accuracy_m: float | None = None
    vertical_accuracy_m: float | None = None
    speed_mps: float | None = None
    heading_deg: float | None = None
    fix_quality: FixQuality = FixQuality.GPS
    satellites: int | None = None
    source: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["fix_quality"] = self.fix_quality.value
        return d


@dataclass(frozen=True, slots=True)
class ResolvedPosition:
    """A position attached to an observation, plus the story of how it got there.

    The recorder rarely has a fix at exactly the midpoint of an integration window,
    so it interpolates. That fact is recorded rather than smoothed over.
    """

    position: Position | None
    evidence: Evidence
    age_ms: float | None  # Time from the nearest real fix to the requested instant.
    flags: QualityFlag = QualityFlag.NONE


# ---------------------------------------------------------------------------
# Hardware description
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrequencyRange:
    min_hz: float
    max_hz: float

    def contains(self, hz: float) -> bool:
        return self.min_hz <= hz <= self.max_hz

    def to_dict(self) -> dict[str, float]:
        return {"min_hz": self.min_hz, "max_hz": self.max_hz}


@dataclass(frozen=True, slots=True)
class DeviceCapabilities:
    """What a receiver can actually do.

    The UI must not offer spectrum this structure does not cover. ``tuning_evidence``
    records whether the range was read from the hardware or assumed from a tuner
    model, because for RTL-SDR it is nearly always the latter.
    """

    driver: str
    tuning_range: FrequencyRange
    sample_rates_hz: tuple[float, ...]
    gains_db: tuple[float, ...]
    supports_agc: bool
    max_bandwidth_hz: float
    full_scale_bits: int
    tuning_evidence: Evidence = Evidence.ASSUMED
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "driver": self.driver,
            "tuning_range": self.tuning_range.to_dict(),
            "sample_rates_hz": list(self.sample_rates_hz),
            "gains_db": list(self.gains_db),
            "supports_agc": self.supports_agc,
            "max_bandwidth_hz": self.max_bandwidth_hz,
            "full_scale_bits": self.full_scale_bits,
            "tuning_evidence": self.tuning_evidence.value,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Identity of a physical (or simulated) receiver."""

    driver: str
    index: int
    serial: str
    manufacturer: str = ""
    product: str = ""
    tuner: str = ""

    @property
    def receiver_id(self) -> str:
        """Stable identifier written into every observation."""
        return f"{self.driver}:{self.serial or self.index}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["receiver_id"] = self.receiver_id
        return d


@dataclass(frozen=True, slots=True)
class AntennaInfo:
    """What the user knows about the antenna.

    Professional characterisation is not required. Recording what is known is,
    because an RF power number without an antenna description is close to meaningless
    and definitely not comparable across surveys.

    Every field here is :attr:`Evidence.ASSUMED` unless the user measured it.
    """

    antenna_id: str = "unknown"
    type: str = "unknown"  # e.g. "quarter-wave whip", "dipole", "yagi"
    frequency_range: FrequencyRange | None = None
    polarization: str = "unknown"  # "vertical", "horizontal", "circular", "unknown"
    orientation_deg: float | None = None  # Azimuth, if fixed and known.
    gain_dbi: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.frequency_range is not None:
            d["frequency_range"] = self.frequency_range.to_dict()
        d["evidence"] = Evidence.ASSUMED.value
        return d


@dataclass(frozen=True, slots=True)
class ReceiverConfig:
    """The receiver state that produced a block of observations.

    Two observations are comparable only if these agree (in particular ``gain_db``
    and ``agc``). Changes are recorded, not averaged away.
    """

    center_frequency_hz: float
    sample_rate_hz: float
    gain_db: float | None  # None means AGC was in control.
    agc: bool
    bandwidth_hz: float | None = None
    freq_correction_ppm: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class FFTConfig:
    """DSP parameters. Everything needed to reproduce a spectrum from IQ."""

    size: int
    window: str = "hann"
    overlap: float = 0.5
    normalization: Normalization = Normalization.DENSITY
    remove_dc: bool = True

    def __post_init__(self) -> None:
        if self.size < 8 or (self.size & (self.size - 1)) != 0:
            raise ValueError(f"fft size must be a power of two >= 8, got {self.size}")
        if not 0.0 <= self.overlap < 1.0:
            raise ValueError(f"fft overlap must be in [0, 1), got {self.overlap}")

    def resolution_hz(self, sample_rate_hz: float) -> float:
        """Raw bin spacing. Note this is *not* the resolution bandwidth."""
        return sample_rate_hz / self.size

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["normalization"] = self.normalization.value
        return d


# ---------------------------------------------------------------------------
# Spectra and observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PowerSpectrum:
    """A power spectrum on an explicit absolute frequency axis.

    ``frequencies_hz`` are absolute RF frequencies (centre frequency already added),
    ascending. ``power_db`` is in ``unit``. ``enbw_hz`` is the equivalent noise
    bandwidth of one bin, which is what you actually need to convert between
    per-bin and per-Hz readings; it is larger than the bin spacing for any real
    window.
    """

    frequencies_hz: np.ndarray
    power_db: np.ndarray
    unit: PowerUnit
    normalization: Normalization
    enbw_hz: float
    bin_width_hz: float
    n_averages: int
    evidence: Evidence = Evidence.MEASURED

    def __post_init__(self) -> None:
        if self.frequencies_hz.shape != self.power_db.shape:
            raise ValueError(
                f"frequency axis {self.frequencies_hz.shape} does not match "
                f"power axis {self.power_db.shape}"
            )

    @property
    def n_bins(self) -> int:
        return int(self.power_db.shape[0])

    @property
    def span_hz(self) -> tuple[float, float]:
        return float(self.frequencies_hz[0]), float(self.frequencies_hz[-1])

    def bin_index(self, frequency_hz: float) -> int:
        """Index of the bin whose centre is nearest ``frequency_hz``."""
        return int(np.argmin(np.abs(self.frequencies_hz - frequency_hz)))


@dataclass(frozen=True, slots=True)
class RFObservation:
    """One geotagged, time-stamped spectrum measurement: a sample of RF(x, y, z, f, t).

    This is the atom of the whole system. It carries its own acquisition context so
    that it remains interpretable in isolation, years later, without the survey it
    came from.
    """

    observation_id: int
    t_start_utc_ns: int
    t_end_utc_ns: int
    t_start_mono_ns: int
    position: ResolvedPosition
    receiver_id: str
    antenna_id: str
    receiver_config: ReceiverConfig
    fft_config: FFTConfig
    spectrum: PowerSpectrum
    n_ffts: int
    flags: QualityFlag = QualityFlag.NONE
    sweep_step: int = 0  # Which window of a band sweep; 0 for fixed-frequency.
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def t_mid_utc_ns(self) -> int:
        return (self.t_start_utc_ns + self.t_end_utc_ns) // 2

    @property
    def duration_s(self) -> float:
        return (self.t_end_utc_ns - self.t_start_utc_ns) / 1e9

    @property
    def has_position(self) -> bool:
        return self.position.position is not None

    def with_flags(self, extra: QualityFlag) -> RFObservation:
        return replace(self, flags=self.flags | extra)


# ---------------------------------------------------------------------------
# Derived-product provenance
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Provenance:
    """Why a derived product looks the way it does.

    Attached to every heatmap, grid, export and estimate. If interpolation settings
    change and the map changes, this is what makes that obvious.
    """

    product: str
    algorithm: str
    parameters: dict[str, Any]
    source_survey_id: str
    software_version: str
    format_version: str
    created_utc_ns: int
    evidence: Evidence
    inputs: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["evidence"] = self.evidence.value
        return d


# ---------------------------------------------------------------------------
# Small numeric helpers used across layers
# ---------------------------------------------------------------------------

# Powers below this (linear) are treated as the numerical floor so that log10 never
# sees a zero. -300 dB is far below anything an 8-bit receiver can represent.
LINEAR_FLOOR = 1e-30
DB_FLOOR = 10.0 * math.log10(LINEAR_FLOOR)


def db_to_linear(db: np.ndarray | float) -> np.ndarray | float:
    """Convert dB power to linear power."""
    return np.power(10.0, np.asarray(db, dtype=np.float64) / 10.0)


def linear_to_db(linear: np.ndarray | float) -> np.ndarray | float:
    """Convert linear power to dB, clamping at :data:`LINEAR_FLOOR`."""
    arr = np.maximum(np.asarray(linear, dtype=np.float64), LINEAR_FLOOR)
    return 10.0 * np.log10(arr)


def mean_power_db(db_values: np.ndarray, axis: int | None = None) -> np.ndarray | float:
    """Average powers correctly: in the linear domain, not the log domain.

    Averaging dB values directly computes a geometric mean of powers, which
    understates the contribution of strong samples. Every aggregation in SpectraMap
    goes through here.
    """
    return linear_to_db(np.mean(db_to_linear(db_values), axis=axis))


__all__ = [
    "AntennaInfo",
    "DB_FLOOR",
    "DeviceCapabilities",
    "DeviceInfo",
    "Evidence",
    "FFTConfig",
    "FixQuality",
    "FrequencyRange",
    "LINEAR_FLOOR",
    "Normalization",
    "Position",
    "PowerSpectrum",
    "PowerUnit",
    "Provenance",
    "QualityFlag",
    "RFObservation",
    "ReceiverConfig",
    "ResolvedPosition",
    "db_to_linear",
    "linear_to_db",
    "mean_power_db",
]
