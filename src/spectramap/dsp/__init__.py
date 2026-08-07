"""Signal processing: IQ samples in, power spectra out.

The whole pipeline is documented in docs/dsp-pipeline.md. Nothing in here knows
about hardware, position, storage or maps.
"""

from spectramap.dsp.detect import Peak, detect_peaks, estimate_noise_floor
from spectramap.dsp.spectrum import (
    SpectrumAccumulator,
    SpectrumEstimator,
    frequency_axis,
    overload_fraction,
)
from spectramap.dsp.window import WINDOWS, WindowStats, get_window, window_stats

__all__ = [
    "Peak",
    "SpectrumAccumulator",
    "SpectrumEstimator",
    "WINDOWS",
    "WindowStats",
    "detect_peaks",
    "estimate_noise_floor",
    "frequency_axis",
    "get_window",
    "overload_fraction",
    "window_stats",
]
