"""Welch power-spectrum estimation for complex baseband IQ.

Reference convention
--------------------
IQ samples are complex, normalised so that a full-scale complex sinusoid has
magnitude 1.0. Every driver is responsible for delivering that convention; see
``spectramap.sdr.base``. With that in place:

* ``SPECTRUM`` normalisation reads **0 dBFS** for a full-scale tone sitting exactly
  on a bin centre, at any FFT size and with any window.
* ``DENSITY`` normalisation reads the power spectral density in dBFS/Hz, so a white
  noise floor reads the same number regardless of FFT size. This is the default,
  because comparing surveys taken with different FFT sizes is a normal thing to
  want and per-bin numbers make that comparison silently wrong.

The two differ by exactly the equivalent noise bandwidth::

    density_dB = spectrum_dB - 10*log10(enbw_hz)

Neither is calibrated. Both are :class:`~spectramap.types.PowerUnit.DBFS` family
units and must be labelled as relative.
"""

from __future__ import annotations

import numpy as np

from spectramap.dsp.window import WindowStats, get_window, window_stats
from spectramap.types import (
    Evidence,
    FFTConfig,
    Normalization,
    PowerSpectrum,
    PowerUnit,
    linear_to_db,
)

# Cap on segments transformed per FFT call. Bounds peak memory to roughly
# MAX_SEGMENT_BATCH * fft_size * 16 bytes regardless of how long an IQ block is.
MAX_SEGMENT_BATCH = 64


def frequency_axis(center_hz: float, sample_rate_hz: float, n: int) -> np.ndarray:
    """Absolute, ascending frequency axis for an ``n``-point complex FFT.

    Bin ``k`` (after ``fftshift``) is centred at ``center_hz + (k - n//2) * fs/n``.
    For even ``n`` the axis is asymmetric: it runs from ``-fs/2`` up to
    ``+fs/2 - fs/n``, because the Nyquist bin belongs to the negative side. This is
    the single most common place to introduce a half-bin frequency error, so it is
    implemented once, here, and unit tested.
    """
    if n < 2:
        raise ValueError(f"need at least 2 bins, got {n}")
    return center_hz + np.fft.fftshift(np.fft.fftfreq(n, d=1.0 / sample_rate_hz))


def overload_fraction(iq: np.ndarray, threshold: float = 0.99) -> float:
    """Fraction of samples whose I or Q component is at/near full scale.

    A non-trivial value means the front end is probably compressing and the measured
    power is an under-estimate of the true field, not a valid reading. Callers set
    :attr:`~spectramap.types.QualityFlag.SDR_OVERLOAD` from this.
    """
    iq = np.asarray(iq)
    if iq.size == 0:
        return 0.0
    clipped = (np.abs(iq.real) >= threshold) | (np.abs(iq.imag) >= threshold)
    return float(np.count_nonzero(clipped) / iq.size)


class SpectrumEstimator:
    """Turns IQ blocks into power spectra using a fixed, declared configuration.

    Constructed once per survey (or per sweep step) so the window and its scaling
    constants are computed once rather than per FFT.
    """

    __slots__ = (
        "config",
        "sample_rate_hz",
        "center_frequency_hz",
        "_window",
        "_stats",
        "_frequencies",
    )

    def __init__(
        self,
        config: FFTConfig,
        sample_rate_hz: float,
        center_frequency_hz: float,
    ) -> None:
        if sample_rate_hz <= 0:
            raise ValueError(f"sample rate must be positive, got {sample_rate_hz}")
        self.config = config
        self.sample_rate_hz = float(sample_rate_hz)
        self.center_frequency_hz = float(center_frequency_hz)
        self._window = get_window(config.window, config.size)
        self._stats = window_stats(config.window, config.size)
        self._frequencies = frequency_axis(center_frequency_hz, sample_rate_hz, config.size)

    def __repr__(self) -> str:
        return (
            f"SpectrumEstimator(fc={self.center_frequency_hz:.6g} Hz, "
            f"fs={self.sample_rate_hz:.6g} Hz, N={self.config.size}, "
            f"window={self.config.window!r}, unit={self.unit.value})"
        )

    # -- Derived quantities -------------------------------------------------

    @property
    def frequencies_hz(self) -> np.ndarray:
        """Absolute frequency of each bin centre, ascending."""
        return self._frequencies

    @property
    def bin_width_hz(self) -> float:
        """Bin spacing, ``fs/N``. Not the resolution bandwidth."""
        return self.sample_rate_hz / self.config.size

    @property
    def enbw_hz(self) -> float:
        """Equivalent noise bandwidth of one bin: the true resolution bandwidth."""
        return self._stats.enbw_hz(self.sample_rate_hz)

    @property
    def window_stats(self) -> WindowStats:
        return self._stats

    @property
    def n_bins(self) -> int:
        return self.config.size

    @property
    def unit(self) -> PowerUnit:
        """V0 always produces relative units. Calibration changes this, not the DSP."""
        return (
            PowerUnit.DBFS_PER_HZ
            if self.config.normalization is Normalization.DENSITY
            else PowerUnit.DBFS
        )

    @property
    def segment_step(self) -> int:
        """Hop between Welch segments, in samples."""
        return max(1, int(round(self.config.size * (1.0 - self.config.overlap))))

    def segments_in(self, n_samples: int) -> int:
        """How many full segments fit in ``n_samples``."""
        if n_samples < self.config.size:
            return 0
        return 1 + (n_samples - self.config.size) // self.segment_step

    def samples_for(self, n_segments: int) -> int:
        """Samples needed to yield exactly ``n_segments`` segments."""
        return self.config.size + max(0, n_segments - 1) * self.segment_step

    # -- The pipeline -------------------------------------------------------

    def linear_power(self, iq: np.ndarray) -> np.ndarray:
        """IQ block -> averaged linear power per bin, in ascending frequency order.

        Stages, in order: optional DC removal, segmentation with overlap, windowing,
        FFT, magnitude squared, normalisation, average across segments.

        Averaging happens in the linear domain. Averaging dB would compute a
        geometric mean and systematically under-report bursty signals.
        """
        iq = np.asarray(iq)
        if iq.ndim != 1:
            raise ValueError(f"expected a 1-D IQ block, got shape {iq.shape}")
        if not np.iscomplexobj(iq):
            raise TypeError("IQ block must be complex; real input is not baseband IQ")

        n_seg = self.segments_in(iq.size)
        if n_seg == 0:
            raise ValueError(
                f"block of {iq.size} samples is shorter than FFT size {self.config.size}"
            )

        work = np.ascontiguousarray(iq, dtype=np.complex128)
        if self.config.remove_dc:
            # Removes the receiver's DC spike (LO self-mixing plus ADC offset), which
            # otherwise appears as a large fake signal exactly at the tuned frequency.
            work = work - work.mean()

        size = self.config.size
        step = self.segment_step
        # Strided view over the block: no copy, and one FFT call per batch.
        segments = np.lib.stride_tricks.as_strided(
            work,
            shape=(n_seg, size),
            strides=(work.strides[0] * step, work.strides[0]),
            writeable=False,
        )

        accum = np.zeros(size, dtype=np.float64)
        for start in range(0, n_seg, MAX_SEGMENT_BATCH):
            batch = segments[start : start + MAX_SEGMENT_BATCH]
            transformed = np.fft.fft(batch * self._window, axis=-1)
            accum += np.einsum("ij,ij->j", transformed.real, transformed.real)
            accum += np.einsum("ij,ij->j", transformed.imag, transformed.imag)
        accum /= n_seg
        power = np.fft.fftshift(accum)

        if self.config.normalization is Normalization.SPECTRUM:
            # |X|^2 / S1^2 -> a full-scale tone on a bin centre reads 1.0 (0 dBFS).
            power /= self._stats.s1**2
        else:
            # |X|^2 / (fs * S2) -> power per Hz; independent of FFT size for noise.
            power /= self.sample_rate_hz * self._stats.s2
        return power

    def estimate(self, iq: np.ndarray) -> PowerSpectrum:
        """IQ block -> a fully described :class:`PowerSpectrum`."""
        power = self.linear_power(iq)
        return self.to_spectrum(power, n_averages=self.segments_in(np.asarray(iq).size))

    def to_spectrum(self, linear_power: np.ndarray, n_averages: int) -> PowerSpectrum:
        """Wrap an averaged linear-power array in its full description."""
        return PowerSpectrum(
            frequencies_hz=self._frequencies,
            power_db=np.asarray(linear_to_db(linear_power), dtype=np.float64),
            unit=self.unit,
            normalization=self.config.normalization,
            enbw_hz=self.enbw_hz,
            bin_width_hz=self.bin_width_hz,
            n_averages=int(n_averages),
            evidence=Evidence.MEASURED,
        )

    def describe(self) -> dict[str, object]:
        """Everything a reader needs in order to interpret these numbers."""
        return {
            "fft_size": self.config.size,
            "window": self.config.window,
            "overlap": self.config.overlap,
            "normalization": self.config.normalization.value,
            "remove_dc": self.config.remove_dc,
            "sample_rate_hz": self.sample_rate_hz,
            "center_frequency_hz": self.center_frequency_hz,
            "bin_width_hz": self.bin_width_hz,
            "enbw_hz": self.enbw_hz,
            "enbw_bins": self._stats.enbw_bins,
            "scalloping_loss_db": self._stats.scalloping_loss_db,
            "unit": self.unit.value,
            "calibrated": False,
        }


class SpectrumAccumulator:
    """Accumulates spectra over an integration window into one observation.

    Keeps a running mean in the linear domain, plus per-bin min/max and a count, so
    that aggregation can report variation instead of hiding it behind one number.
    """

    __slots__ = ("_sum", "_min", "_max", "_count", "_n_bins")

    def __init__(self, n_bins: int) -> None:
        self._n_bins = int(n_bins)
        self._sum = np.zeros(self._n_bins, dtype=np.float64)
        self._min = np.full(self._n_bins, np.inf, dtype=np.float64)
        self._max = np.zeros(self._n_bins, dtype=np.float64)
        self._count = 0

    def add(self, linear_power: np.ndarray, weight: int = 1) -> None:
        """Add one (already averaged) linear power spectrum, weighted by FFT count."""
        arr = np.asarray(linear_power, dtype=np.float64)
        if arr.shape != (self._n_bins,):
            raise ValueError(f"expected {self._n_bins} bins, got {arr.shape}")
        if weight <= 0:
            raise ValueError(f"weight must be positive, got {weight}")
        self._sum += arr * weight
        np.minimum(self._min, arr, out=self._min)
        np.maximum(self._max, arr, out=self._max)
        self._count += weight

    @property
    def count(self) -> int:
        """Number of FFTs folded in so far."""
        return self._count

    @property
    def is_empty(self) -> bool:
        return self._count == 0

    def mean_linear(self) -> np.ndarray:
        if self._count == 0:
            raise ValueError("no spectra accumulated")
        return self._sum / self._count

    def min_db(self) -> np.ndarray:
        return np.asarray(linear_to_db(self._min), dtype=np.float64)

    def max_db(self) -> np.ndarray:
        return np.asarray(linear_to_db(self._max), dtype=np.float64)

    def reset(self) -> None:
        self._sum.fill(0.0)
        self._min.fill(np.inf)
        self._max.fill(0.0)
        self._count = 0


__all__ = [
    "MAX_SEGMENT_BATCH",
    "SpectrumAccumulator",
    "SpectrumEstimator",
    "frequency_axis",
    "overload_fraction",
]
