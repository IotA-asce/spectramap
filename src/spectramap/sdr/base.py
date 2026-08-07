"""Receiver abstraction.

Nothing outside ``spectramap.sdr`` may import a vendor SDR library. Everything above
this layer talks to :class:`SDRDevice`, so swapping an RTL-SDR for a HackRF, an
Airspy, a SoapySDR device or the simulator changes one line of configuration.

Two conventions every driver must honour
----------------------------------------
1. **Full-scale normalisation.** Samples are complex, with a full-scale signal
   having magnitude 1.0. An 8-bit RTL-SDR sample of 255 maps to +1.0, not to 255.
   Every power number in SpectraMap is dB relative to that full scale, so a driver
   that ignores this silently shifts an entire survey.
2. **Honest capabilities.** :meth:`SDRDevice.get_capabilities` describes what the
   hardware can really do, and says via ``tuning_evidence`` whether the range was
   read from the device or assumed from a model number. The UI refuses to offer
   spectrum outside it.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spectramap.types import DeviceCapabilities, DeviceInfo, ReceiverConfig


class SDRError(RuntimeError):
    """Base class for receiver failures."""


class DeviceNotFoundError(SDRError):
    """No receiver matched the requested driver/index/serial."""


class ConfigurationError(SDRError):
    """A requested configuration is outside the device's capabilities."""


class StreamError(SDRError):
    """Sample delivery failed or the device disappeared mid-survey."""


@dataclass(frozen=True, slots=True)
class IQBlock:
    """A contiguous run of complex baseband samples, with its timing.

    ``t_start_mono_ns`` is the host monotonic clock at the instant the *first sample
    in this block* was captured. Drivers that timestamp on buffer arrival must
    subtract the block duration themselves; see
    :func:`estimate_block_start_mono_ns`. Getting this wrong smears every
    measurement along the survey route by up to one buffer length.

    ``dropped_samples`` is the driver's count of samples lost immediately *before*
    this block. Nonzero values become
    :attr:`~spectramap.types.QualityFlag.DROPPED_SAMPLES` on the affected
    observation; they are never quietly ignored.
    """

    samples: np.ndarray
    t_start_mono_ns: int
    t_start_utc_ns: int
    sample_rate_hz: float
    center_frequency_hz: float
    sequence: int = 0
    dropped_samples: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def n_samples(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_s(self) -> float:
        return self.n_samples / self.sample_rate_hz

    @property
    def t_end_mono_ns(self) -> int:
        return self.t_start_mono_ns + int(round(self.duration_s * 1e9))

    @property
    def t_mid_mono_ns(self) -> int:
        return self.t_start_mono_ns + int(round(self.duration_s * 5e8))


def estimate_block_start_mono_ns(
    arrival_mono_ns: int, n_samples: int, sample_rate_hz: float
) -> int:
    """Convert a buffer-arrival timestamp into a first-sample timestamp.

    A driver hands you a buffer *after* it has filled. The first sample in it is
    therefore about one buffer duration old. This does not correct for USB transfer
    and kernel latency, which remain an unmeasured bias of order a few
    milliseconds -- documented in docs/technical-risks.md rather than pretended away.
    """
    return int(arrival_mono_ns - round(n_samples / sample_rate_hz * 1e9))


def now_mono_utc_ns() -> tuple[int, int]:
    """Read the monotonic and wall clocks as close together as possible.

    Returns ``(mono_ns, utc_ns)``. The pair is what lets the recorder align RF and
    GPS on a clock that cannot step, while still writing real UTC timestamps.
    """
    mono = time.monotonic_ns()
    utc = time.time_ns()
    return mono, utc


class SDRDevice(ABC):
    """A tunable receiver that can deliver complex baseband samples.

    Lifecycle::

        with SomeDevice(index=0) as dev:
            dev.set_sample_rate(2_400_000)
            dev.set_center_frequency(433_920_000)
            dev.set_gain(24.0)
            for block in dev.stream(block_samples=262_144):
                ...
    """

    # -- Lifecycle ----------------------------------------------------------

    @abstractmethod
    def open(self) -> None:
        """Acquire the device. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Release the device. Idempotent, and safe to call after a failure."""

    @property
    @abstractmethod
    def is_open(self) -> bool: ...

    def __enter__(self) -> SDRDevice:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- Description --------------------------------------------------------

    @abstractmethod
    def get_capabilities(self) -> DeviceCapabilities: ...

    @abstractmethod
    def get_device_info(self) -> DeviceInfo: ...

    # -- Configuration ------------------------------------------------------

    @abstractmethod
    def set_center_frequency(self, hz: float) -> float:
        """Tune. Returns the frequency actually achieved, which may differ."""

    @abstractmethod
    def set_sample_rate(self, hz: float) -> float:
        """Set sample rate. Returns the rate actually achieved."""

    @abstractmethod
    def set_gain(self, db: float | None) -> float | None:
        """Set gain in dB, or ``None`` to hand control to the device's AGC.

        Returns the gain actually applied, or ``None`` under AGC.

        Fixed gain is strongly preferred for any survey whose point is comparing
        power across space. Under AGC the receiver quietly rescales itself as you
        walk, so two locations with identical fields can report different powers.
        Callers that permit AGC must set
        :attr:`~spectramap.types.QualityFlag.AGC_ACTIVE`.
        """

    @property
    @abstractmethod
    def config(self) -> ReceiverConfig:
        """The configuration currently in force, as it will be recorded."""

    # -- Streaming ----------------------------------------------------------

    @abstractmethod
    def read_iq(self, n_samples: int) -> IQBlock:
        """Blocking read of exactly ``n_samples`` complex samples."""

    def stream(self, block_samples: int, max_blocks: int | None = None) -> Iterator[IQBlock]:
        """Yield IQ blocks until stopped.

        The default implementation is a loop over :meth:`read_iq`, which is correct
        but not the lowest-latency option; drivers with an async callback API should
        override it.
        """
        emitted = 0
        while max_blocks is None or emitted < max_blocks:
            yield self.read_iq(block_samples)
            emitted += 1

    # -- Validation helper shared by drivers --------------------------------

    def _validate(self, center_hz: float, sample_rate_hz: float) -> None:
        caps = self.get_capabilities()
        if not caps.tuning_range.contains(center_hz):
            raise ConfigurationError(
                f"{center_hz / 1e6:.6f} MHz is outside the tuning range of "
                f"{caps.driver} ({caps.tuning_range.min_hz / 1e6:.3f}-"
                f"{caps.tuning_range.max_hz / 1e6:.3f} MHz)"
            )
        if sample_rate_hz > caps.max_bandwidth_hz:
            raise ConfigurationError(
                f"sample rate {sample_rate_hz / 1e6:.3f} MS/s exceeds the maximum "
                f"usable bandwidth of {caps.max_bandwidth_hz / 1e6:.3f} MS/s"
            )


__all__ = [
    "ConfigurationError",
    "DeviceNotFoundError",
    "IQBlock",
    "SDRDevice",
    "SDRError",
    "StreamError",
    "estimate_block_start_mono_ns",
    "now_mono_utc_ns",
]
