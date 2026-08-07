"""Window functions and their scaling factors.

A window changes what a spectrum *means*, so its gain factors are computed here once
and carried through every normalisation. For a window ``w`` of length ``N``:

* Coherent gain      ``S1 = sum(w)``          -- scales a discrete tone.
* Incoherent gain    ``S2 = sum(w**2)``       -- scales broadband noise.
* Equivalent noise bandwidth (in bins) ``ENBW = N * S2 / S1**2``.

ENBW is the width of the ideal rectangular filter that would pass the same noise
power as one real bin. It is strictly greater than 1 bin for any window other than
rectangular, which is exactly why the raw bin spacing ``fs/N`` must never be called
"resolution bandwidth".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np


def _rectangular(n: int) -> np.ndarray:
    return np.ones(n, dtype=np.float64)


def _hann(n: int) -> np.ndarray:
    # Periodic (DFT-even) definition: correct for spectral analysis, unlike the
    # symmetric definition used for FIR filter design.
    k = np.arange(n, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * k / n)


def _hamming(n: int) -> np.ndarray:
    k = np.arange(n, dtype=np.float64)
    return 0.54 - 0.46 * np.cos(2.0 * np.pi * k / n)


def _blackman_harris(n: int) -> np.ndarray:
    # 4-term Blackman-Harris, -92 dB sidelobes. Useful when a strong carrier would
    # otherwise smear across the band and hide weak neighbours.
    a = (0.35875, 0.48829, 0.14128, 0.01168)
    k = np.arange(n, dtype=np.float64)
    x = 2.0 * np.pi * k / n
    return a[0] - a[1] * np.cos(x) + a[2] * np.cos(2 * x) - a[3] * np.cos(3 * x)


WINDOWS: dict[str, Callable[[int], np.ndarray]] = {
    "rectangular": _rectangular,
    "boxcar": _rectangular,
    "hann": _hann,
    "hanning": _hann,
    "hamming": _hamming,
    "blackman-harris": _blackman_harris,
    "blackmanharris": _blackman_harris,
}


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Scaling constants derived from a window.

    ``enbw_bins`` multiplied by the bin spacing gives the equivalent noise bandwidth
    in Hz, which is the number to quote as the true resolution bandwidth.
    """

    name: str
    length: int
    s1: float  # sum(w): coherent gain
    s2: float  # sum(w**2): incoherent gain
    enbw_bins: float
    scalloping_loss_db: float  # Worst-case loss for a tone halfway between bins.

    def enbw_hz(self, sample_rate_hz: float) -> float:
        return self.enbw_bins * sample_rate_hz / self.length


def get_window(name: str, length: int) -> np.ndarray:
    """Return a window of ``length`` samples.

    Raises ValueError for unknown names rather than silently falling back, because a
    silently substituted window would corrupt every power number downstream.
    """
    key = name.strip().lower()
    if key not in WINDOWS:
        raise ValueError(f"unknown window {name!r}; known windows: {sorted(set(WINDOWS))}")
    if length < 2:
        raise ValueError(f"window length must be >= 2, got {length}")
    return WINDOWS[key](length)


def window_stats(name: str, length: int) -> WindowStats:
    """Compute the scaling constants for a window."""
    w = get_window(name, length)
    s1 = float(np.sum(w))
    s2 = float(np.sum(w * w))
    enbw_bins = length * s2 / (s1 * s1)
    return WindowStats(
        name=name.strip().lower(),
        length=length,
        s1=s1,
        s2=s2,
        enbw_bins=enbw_bins,
        scalloping_loss_db=_scalloping_loss_db(w),
    )


def _scalloping_loss_db(w: np.ndarray) -> float:
    """Loss for a tone exactly halfway between two bin centres.

    This is the systematic under-read that afflicts any peak power taken from a
    single bin; SpectraMap reports it so a user can bound their own error.
    """
    n = w.shape[0]
    k = np.arange(n, dtype=np.float64)
    # Response of the windowed DFT at a half-bin offset.
    half_bin = np.abs(np.sum(w * np.exp(-1j * np.pi * k / n)))
    return float(20.0 * np.log10(half_bin / np.sum(w)))


__all__ = ["WINDOWS", "WindowStats", "get_window", "window_stats"]
