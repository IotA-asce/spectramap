"""A deterministic simulated receiver.

Synthesises complex baseband from a :class:`~spectramap.simulation.world.VirtualWorld`
so the entire pipeline runs, and is tested, without radio hardware.

What it models, and why each one earns its place:

* **Log-distance path loss** -- produces the spatial gradient the mapping stage exists
  to reconstruct.
* **Complex Gaussian noise at a declared PSD** -- lets a test assert that the measured
  noise floor equals the designed one, which validates the FFT normalisation.
* **Frequency drift and wander** -- an RTL-SDR's uncompensated crystal really does
  move by kilohertz, so detection must not assume a signal stays in one bin.
* **Duty cycling** -- an intermittent beacon integrates differently from a carrier,
  and averaging must handle that without silently under-reporting.
* **Gain, including AGC** -- gain scales signal and noise together and can clip. This
  is what makes the "never survey with AGC on" warning testable rather than folklore.
* **Band limits** -- a transmitter outside the captured span is simply not heard,
  because real hardware cannot see outside its instantaneous bandwidth.
"""

from __future__ import annotations

import numpy as np

from spectramap.sdr.base import ConfigurationError, IQBlock, SDRDevice
from spectramap.simulation.clock import SimClock
from spectramap.simulation.world import VirtualWorld
from spectramap.types import (
    DeviceCapabilities,
    DeviceInfo,
    Evidence,
    FrequencyRange,
    ReceiverConfig,
)

# Gain steps of a typical R820T tuner, in dB. Used so that the mock's AGC behaves
# like a real one: it snaps to discrete steps rather than gliding continuously.
R820T_GAINS = (
    0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9,
    25.4, 28.0, 29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5,
    48.0, 49.6,
)


class MockSDR(SDRDevice):
    """Simulated receiver driven by a :class:`VirtualWorld`.

    Advances the shared :class:`~spectramap.simulation.clock.SimClock` by exactly the
    duration of every block it produces, making it the time master of a simulated run.
    """

    def __init__(
        self,
        world: VirtualWorld,
        clock: SimClock | None = None,
        index: int = 0,
        serial: str = "MOCK0001",
        drop_blocks: frozenset[int] = frozenset(),
        fail_at_block: int | None = None,
    ) -> None:
        self.world = world
        self.clock = clock if clock is not None else SimClock()
        self._index = index
        self._serial = serial
        self._open = False
        self._sequence = 0
        self._center_hz = 433_920_000.0
        self._sample_rate_hz = 2_400_000.0
        self._gain_db: float | None = world.reference_gain_db
        self._agc = False
        # Fault injection, so recorder robustness is tested rather than assumed.
        self._drop_blocks = drop_blocks
        self._fail_at_block = fail_at_block

    # -- Lifecycle ----------------------------------------------------------

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    # -- Description --------------------------------------------------------

    def get_capabilities(self) -> DeviceCapabilities:
        return DeviceCapabilities(
            driver="mock",
            # Mirrors an R820T-based RTL-SDR so that configurations are portable
            # between the simulator and real hardware without edits.
            tuning_range=FrequencyRange(24e6, 1_766e6),
            sample_rates_hz=(250e3, 1_024e3, 1_536e3, 1_792e3, 1_920e3, 2_048e3, 2_400e3),
            gains_db=R820T_GAINS,
            supports_agc=True,
            max_bandwidth_hz=2_400e3,
            full_scale_bits=8,
            tuning_evidence=Evidence.ASSUMED,
            notes="Simulated device. Produces synthetic IQ from a VirtualWorld.",
        )

    def get_device_info(self) -> DeviceInfo:
        return DeviceInfo(
            driver="mock",
            index=self._index,
            serial=self._serial,
            manufacturer="SpectraMap",
            product="Mock SDR",
            tuner="simulated-R820T",
        )

    # -- Configuration ------------------------------------------------------

    def set_center_frequency(self, hz: float) -> float:
        self._validate(hz, self._sample_rate_hz)
        self._center_hz = float(hz)
        return self._center_hz

    def set_sample_rate(self, hz: float) -> float:
        caps = self.get_capabilities()
        if hz > caps.max_bandwidth_hz:
            raise ConfigurationError(
                f"sample rate {hz / 1e6:.3f} MS/s exceeds the device maximum "
                f"{caps.max_bandwidth_hz / 1e6:.3f} MS/s"
            )
        self._sample_rate_hz = float(hz)
        return self._sample_rate_hz

    def set_gain(self, db: float | None) -> float | None:
        if db is None:
            self._agc = True
            self._gain_db = None
            return None
        self._agc = False
        # Snap to the nearest supported step, exactly as a real tuner does, and
        # report what was actually applied rather than what was asked for.
        gains = np.asarray(self.get_capabilities().gains_db)
        self._gain_db = float(gains[int(np.argmin(np.abs(gains - db)))])
        return self._gain_db

    @property
    def config(self) -> ReceiverConfig:
        return ReceiverConfig(
            center_frequency_hz=self._center_hz,
            sample_rate_hz=self._sample_rate_hz,
            gain_db=self._gain_db,
            agc=self._agc,
            bandwidth_hz=self._sample_rate_hz,
        )

    # -- Streaming ----------------------------------------------------------

    def read_iq(self, n_samples: int) -> IQBlock:
        if not self._open:
            raise ConfigurationError("device is not open")
        if n_samples <= 0:
            raise ValueError(f"n_samples must be positive, got {n_samples}")

        seq = self._sequence
        self._sequence += 1
        if self._fail_at_block is not None and seq >= self._fail_at_block:
            from spectramap.sdr.base import StreamError

            raise StreamError(f"simulated device failure at block {seq}")

        t_start_s = self.clock.t_sim_s
        duration_s = n_samples / self._sample_rate_hz

        if seq in self._drop_blocks:
            # Model a USB overflow: the samples for this block never arrive, and the
            # world keeps turning while they do not.
            dropped = n_samples
            samples = np.zeros(0, dtype=np.complex64)
        else:
            dropped = 0
            samples = self._synthesise(t_start_s, n_samples, seq)

        self.clock.advance(duration_s)
        return IQBlock(
            samples=samples,
            t_start_mono_ns=self.clock.mono_ns_at(t_start_s),
            t_start_utc_ns=self.clock.utc_ns_at(t_start_s),
            sample_rate_hz=self._sample_rate_hz,
            center_frequency_hz=self._center_hz,
            sequence=seq,
            dropped_samples=dropped,
            metadata={"simulated": True, "t_sim_s": t_start_s},
        )

    # -- Synthesis ----------------------------------------------------------

    def _synthesise(self, t_start_s: float, n: int, seq: int) -> np.ndarray:
        """Generate one block of complex baseband at the reference gain, then apply gain."""
        fs = self._sample_rate_hz
        fc = self._center_hz
        t = t_start_s + np.arange(n, dtype=np.float64) / fs
        # Path loss is evaluated once per block at its midpoint. At walking pace a
        # 0.1 s block covers ~13 cm, far below any spatial structure this models.
        t_mid = t_start_s + 0.5 * n / fs

        rng = self.world.rng_for("iq", seq)

        # Complex Gaussian noise whose PSD equals the declared floor:
        # E|x|^2 = N0 * fs, split evenly between the real and imaginary parts.
        n0_linear = 10.0 ** (self.world.noise_floor_dbfs_per_hz / 10.0)
        sigma = np.sqrt(n0_linear * fs / 2.0)
        signal = rng.normal(0.0, sigma, n) + 1j * rng.normal(0.0, sigma, n)

        f_low, f_high = fc - fs / 2.0, fc + fs / 2.0
        for tx in self.world.transmitters:
            if not (f_low <= tx.frequency_hz < f_high):
                continue  # Outside the instantaneous bandwidth: genuinely inaudible.

            power_db = self.world.received_power_dbfs(tx, t_mid)
            amplitude = 10.0 ** (power_db / 20.0)
            if self.world.band_edge_rolloff > 0:
                amplitude *= self._edge_response(tx.frequency_hz, fc, fs)

            phase = 2.0 * np.pi * (tx.frequency_hz - fc) * t + tx.phase_at(t)
            # A per-transmitter constant phase, stable across blocks.
            phase += float(self.world.rng_for(f"phase:{tx.tx_id}", 0).uniform(0, 2 * np.pi))

            if tx.bandwidth_hz > 0:
                component = amplitude * self._band_limited(tx, n, fs, seq) * np.exp(1j * phase)
            else:
                component = amplitude * np.exp(1j * phase)

            if tx.duty_cycle < 1.0:
                # Gate per sample, so an observation straddling an on/off edge
                # integrates a genuinely partial burst.
                component = component * ((t % tx.period_s) < tx.duty_cycle * tx.period_s)

            signal += component

        signal *= self._gain_scale()
        # Clip at full scale. This is what receiver overload actually is, and it makes
        # the resulting power an under-estimate rather than an error.
        np.clip(signal.real, -1.0, 1.0, out=signal.real)
        np.clip(signal.imag, -1.0, 1.0, out=signal.imag)
        return signal.astype(np.complex64)

    def _gain_scale(self) -> float:
        """Amplitude scaling from the current gain, relative to the world's reference."""
        if self._agc:
            # A crude but representative AGC: pick a step that keeps the strongest
            # component comfortably inside full scale. It changes as the receiver
            # moves, which is precisely why AGC ruins spatial comparison.
            headroom_db = -6.0 - self._peak_estimate_db()
            gains = np.asarray(self.get_capabilities().gains_db)
            target = self.world.reference_gain_db + headroom_db
            applied = float(gains[int(np.argmin(np.abs(gains - target)))])
            return 10.0 ** ((applied - self.world.reference_gain_db) / 20.0)
        gain = self.world.reference_gain_db if self._gain_db is None else self._gain_db
        return 10.0 ** ((gain - self.world.reference_gain_db) / 20.0)

    def _peak_estimate_db(self) -> float:
        """Strongest in-band component right now, for the simulated AGC."""
        fs, fc = self._sample_rate_hz, self._center_hz
        t = self.clock.t_sim_s
        powers = [
            self.world.received_power_dbfs(tx, t)
            for tx in self.world.transmitters
            if fc - fs / 2.0 <= tx.frequency_hz < fc + fs / 2.0
        ]
        return max(powers) if powers else -100.0

    def _band_limited(self, tx, n: int, fs: float, seq: int) -> np.ndarray:
        """Unit-power complex noise limited to ``tx.bandwidth_hz``.

        Generated per block, so there is a phase discontinuity at block boundaries.
        That is acceptable for a nuisance signal and is documented rather than hidden;
        pure carriers (``bandwidth_hz == 0``) have no such artefact and are what the
        validation tests use.
        """
        rng = self.world.rng_for(f"bl:{tx.tx_id}", seq)
        noise = rng.normal(0, 1, n) + 1j * rng.normal(0, 1, n)
        spectrum = np.fft.fft(noise)
        freqs = np.fft.fftfreq(n, d=1.0 / fs)
        spectrum[np.abs(freqs) > tx.bandwidth_hz / 2.0] = 0.0
        shaped = np.fft.ifft(spectrum)
        rms = np.sqrt(np.mean(np.abs(shaped) ** 2))
        return shaped / rms if rms > 0 else shaped

    @staticmethod
    def _edge_response(f_hz: float, fc: float, fs: float) -> float:
        """Raised-cosine roll-off near the edges of the captured span."""
        offset = abs(f_hz - fc) / (fs / 2.0)
        flat = 0.9
        if offset <= flat:
            return 1.0
        x = min(1.0, (offset - flat) / (1.0 - flat))
        return float(0.5 * (1.0 + np.cos(np.pi * x)))


__all__ = ["R820T_GAINS", "MockSDR"]
