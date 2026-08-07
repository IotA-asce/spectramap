"""RTL-SDR driver, built on pyrtlsdr.

Importing this module never requires the hardware library; the import is deferred to
:meth:`RTLSDRDevice.open` so that ``spectramap devices`` works, and CI runs, on a
machine with no ``librtlsdr`` at all.

Honesty about capabilities
--------------------------
``librtlsdr`` does not report a tuner's frequency limits. This driver therefore
derives them from the tuner chip it *can* read, and marks the result
:attr:`~spectramap.types.Evidence.ASSUMED`. The published limits are also nominal:
real coverage varies with the individual unit, the crystal, and temperature, and
the bottom of the range on a direct-sampling-less R820T is not usable at all.
Treat the numbers as a guard rail against nonsense requests, not as a specification.

Sample rate reality
-------------------
The RTL2832U will accept rates it cannot sustain over USB. 2.4 MS/s is generally
reliable; 2.56 MS/s and above drop samples on many hosts, and 3.2 MS/s essentially
always does. Dropped samples are detected and flagged rather than being allowed to
quietly corrupt the timeline.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np

from spectramap.sdr.base import (
    ConfigurationError,
    DeviceNotFoundError,
    IQBlock,
    SDRDevice,
    StreamError,
    estimate_block_start_mono_ns,
    now_mono_utc_ns,
)
from spectramap.types import (
    DeviceCapabilities,
    DeviceInfo,
    Evidence,
    FrequencyRange,
    ReceiverConfig,
)

# Nominal tuning ranges by tuner chip. ASSUMED, not measured. See module docstring.
TUNER_RANGES: dict[str, tuple[float, float]] = {
    "R820T": (24e6, 1_766e6),
    "R828D": (24e6, 1_766e6),
    "E4000": (52e6, 2_200e6),  # With a gap around 1100-1250 MHz on most units.
    "FC0012": (22e6, 948e6),
    "FC0013": (22e6, 1_100e6),
    "FC2580": (146e6, 924e6),
    "UNKNOWN": (24e6, 1_700e6),
}

# Rates the RTL2832U accepts. Anything above 2.4 MS/s is marked risky in the notes.
RTL_SAMPLE_RATES: tuple[float, ...] = (
    250e3, 1_024e3, 1_536e3, 1_792e3, 1_920e3, 2_048e3, 2_160e3, 2_400e3, 2_560e3,
)

RELIABLE_MAX_SAMPLE_RATE = 2_400e3


def _import_rtlsdr() -> Any:
    try:
        import rtlsdr  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - depends on host packages
        raise DeviceNotFoundError(
            "RTL-SDR support requires pyrtlsdr and librtlsdr.\n"
            "  pip install 'spectramap[rtlsdr]'\n"
            "  macOS:  brew install librtlsdr\n"
            "  Debian: apt install librtlsdr0 rtl-sdr\n"
            "Use --driver mock to run the full pipeline without hardware."
        ) from exc
    return rtlsdr


def enumerate_rtlsdr() -> list[DeviceInfo]:
    """List attached RTL-SDR devices. Returns an empty list if the library is absent."""
    try:
        rtlsdr = _import_rtlsdr()
    except DeviceNotFoundError:
        return []
    try:
        serials = rtlsdr.RtlSdr.get_device_serial_addresses()
    except Exception:  # pragma: no cover - librtlsdr raises varied errors
        return []
    return [
        DeviceInfo(driver="rtl-sdr", index=i, serial=str(s), manufacturer="Realtek")
        for i, s in enumerate(serials)
    ]


class RTLSDRDevice(SDRDevice):
    """An RTL2832U-based receiver.

    Delivers samples already normalised to full scale +/-1.0, matching the convention
    in :mod:`spectramap.sdr.base` (pyrtlsdr's ``read_samples`` does this by dividing
    the 8-bit unsigned pairs by 127.5 and offsetting by -1).
    """

    def __init__(
        self,
        index: int = 0,
        serial: str | None = None,
        freq_correction_ppm: int = 0,
    ) -> None:
        self._index = index
        self._serial = serial
        self._sdr: Any = None
        self._ppm = int(freq_correction_ppm)
        self._tuner = "UNKNOWN"
        self._sequence = 0
        self._agc = False
        self._last_end_mono_ns: int | None = None

    # -- Lifecycle ----------------------------------------------------------

    def open(self) -> None:
        if self._sdr is not None:
            return
        rtlsdr = _import_rtlsdr()
        try:
            if self._serial is not None:
                index = rtlsdr.RtlSdr.get_device_index_by_serial(self._serial)
            else:
                index = self._index
            self._sdr = rtlsdr.RtlSdr(device_index=index)
        except Exception as exc:
            raise DeviceNotFoundError(
                f"could not open RTL-SDR (index={self._index}, serial={self._serial}): {exc}"
            ) from exc
        if self._ppm:
            self._sdr.freq_correction = self._ppm
        self._tuner = self._read_tuner_name()

    def close(self) -> None:
        if self._sdr is None:
            return
        try:
            self._sdr.close()
        except Exception:  # pragma: no cover - closing a dead device
            pass
        finally:
            self._sdr = None

    @property
    def is_open(self) -> bool:
        return self._sdr is not None

    def _read_tuner_name(self) -> str:
        try:
            raw = self._sdr.get_tuner_type()
        except Exception:  # pragma: no cover
            return "UNKNOWN"
        # librtlsdr's enum; map the common values to chip names.
        mapping = {1: "E4000", 2: "FC0012", 3: "FC0013", 4: "FC2580", 5: "R820T", 6: "R828D"}
        return mapping.get(int(raw), "UNKNOWN")

    def _require(self) -> Any:
        if self._sdr is None:
            raise StreamError("RTL-SDR is not open")
        return self._sdr

    # -- Description --------------------------------------------------------

    def get_capabilities(self) -> DeviceCapabilities:
        low, high = TUNER_RANGES.get(self._tuner, TUNER_RANGES["UNKNOWN"])
        gains: tuple[float, ...] = ()
        if self._sdr is not None:
            try:
                gains = tuple(float(g) for g in self._sdr.valid_gains_db)
            except Exception:  # pragma: no cover
                gains = ()
        return DeviceCapabilities(
            driver="rtl-sdr",
            tuning_range=FrequencyRange(low, high),
            sample_rates_hz=RTL_SAMPLE_RATES,
            gains_db=gains,
            supports_agc=True,
            max_bandwidth_hz=RTL_SAMPLE_RATES[-1],
            full_scale_bits=8,
            # librtlsdr cannot report tuning limits, so these come from the tuner
            # model, not from the device.
            tuning_evidence=Evidence.ASSUMED,
            notes=(
                f"Tuner {self._tuner}. Range is nominal for that chip and varies by unit "
                f"and temperature. Rates above {RELIABLE_MAX_SAMPLE_RATE / 1e6:.1f} MS/s "
                "drop samples on many hosts."
            ),
        )

    def get_device_info(self) -> DeviceInfo:
        serial = self._serial or ""
        if self._sdr is not None and not serial:
            try:
                serial = str(self._sdr.get_device_serial_addresses()[self._index])
            except Exception:  # pragma: no cover
                serial = ""
        return DeviceInfo(
            driver="rtl-sdr",
            index=self._index,
            serial=serial,
            manufacturer="Realtek",
            product="RTL2832U",
            tuner=self._tuner,
        )

    # -- Configuration ------------------------------------------------------

    def set_center_frequency(self, hz: float) -> float:
        sdr = self._require()
        self._validate(hz, float(sdr.sample_rate))
        sdr.center_freq = hz
        return float(sdr.center_freq)

    def set_sample_rate(self, hz: float) -> float:
        sdr = self._require()
        if hz > RTL_SAMPLE_RATES[-1]:
            raise ConfigurationError(
                f"{hz / 1e6:.3f} MS/s exceeds what the RTL2832U accepts "
                f"({RTL_SAMPLE_RATES[-1] / 1e6:.3f} MS/s)"
            )
        sdr.sample_rate = hz
        return float(sdr.sample_rate)

    def set_gain(self, db: float | None) -> float | None:
        sdr = self._require()
        if db is None:
            sdr.gain = "auto"
            self._agc = True
            return None
        self._agc = False
        try:
            sdr.gain = float(db)
        except Exception as exc:
            raise ConfigurationError(
                f"gain {db} dB rejected; supported: {self.get_capabilities().gains_db}"
            ) from exc
        return float(sdr.gain)

    @property
    def config(self) -> ReceiverConfig:
        sdr = self._require()
        return ReceiverConfig(
            center_frequency_hz=float(sdr.center_freq),
            sample_rate_hz=float(sdr.sample_rate),
            gain_db=None if self._agc else float(sdr.gain),
            agc=self._agc,
            bandwidth_hz=float(sdr.sample_rate),
            freq_correction_ppm=float(self._ppm),
        )

    # -- Streaming ----------------------------------------------------------

    def read_iq(self, n_samples: int) -> IQBlock:
        sdr = self._require()
        seq = self._sequence
        self._sequence += 1
        try:
            samples = sdr.read_samples(n_samples)
        except Exception as exc:
            raise StreamError(f"RTL-SDR read failed at block {seq}: {exc}") from exc

        arrival_mono_ns, arrival_utc_ns = now_mono_utc_ns()
        sample_rate = float(sdr.sample_rate)
        n = int(np.asarray(samples).shape[0])
        start_mono_ns = estimate_block_start_mono_ns(arrival_mono_ns, n, sample_rate)

        # librtlsdr does not report overflows through this API, so infer a gap from
        # the timeline: if this block started noticeably after the previous one
        # ended, samples went missing. The tolerance absorbs scheduling jitter.
        dropped = 0
        if self._last_end_mono_ns is not None:
            gap_ns = start_mono_ns - self._last_end_mono_ns
            tolerance_ns = int(0.05 * n / sample_rate * 1e9)
            if gap_ns > tolerance_ns:
                dropped = int(round(gap_ns / 1e9 * sample_rate))
        self._last_end_mono_ns = start_mono_ns + int(round(n / sample_rate * 1e9))

        return IQBlock(
            samples=np.asarray(samples, dtype=np.complex64),
            t_start_mono_ns=start_mono_ns,
            t_start_utc_ns=arrival_utc_ns - (arrival_mono_ns - start_mono_ns),
            sample_rate_hz=sample_rate,
            center_frequency_hz=float(sdr.center_freq),
            sequence=seq,
            dropped_samples=dropped,
            metadata={"arrival_mono_ns": arrival_mono_ns, "host_read_s": time.monotonic()},
        )


__all__ = [
    "RELIABLE_MAX_SAMPLE_RATE",
    "RTL_SAMPLE_RATES",
    "RTLSDRDevice",
    "TUNER_RANGES",
    "enumerate_rtlsdr",
]
