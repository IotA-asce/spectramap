"""Deterministic spectral detection: noise floor, peaks, band occupancy.

This is spectral analysis, not demodulation. Nothing here inspects, decodes or
stores the content of a transmission; it only measures how energy is distributed
across frequency. See docs/safety-and-legal.md.

Everything produced here is :attr:`~spectramap.types.Evidence.DERIVED`: a
deterministic function of measured spectra, carrying the uncertainty of its inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from spectramap.types import Evidence, PowerSpectrum, PowerUnit, db_to_linear, linear_to_db


def _chi2_median_ratio(n_averages: int) -> float:
    """median/mean of a chi-square periodogram averaged over ``n_averages`` FFTs.

    A periodogram bin of complex Gaussian noise is exponentially distributed; the
    median of an exponential sits ``ln 2`` (about -1.6 dB) below its mean. Averaging
    ``M`` segments gives a chi-square with ``2M`` degrees of freedom, whose median
    creeps back towards the mean. Using the Wilson-Hilferty approximation::

        median / mean ~= (1 - 2 / (9k))**3,   k = 2M degrees of freedom

    Without this correction a median-based noise floor reads about 1.6 dB low at
    M=1, which would then be reported as extra SNR on every peak.
    """
    k = 2.0 * max(1, int(n_averages))
    return float((1.0 - 2.0 / (9.0 * k)) ** 3)


def estimate_noise_floor(
    spectrum: PowerSpectrum,
    exclude_fraction: float = 0.0,
    local_bins: int | None = None,
) -> np.ndarray | float:
    """Estimate the noise floor of a spectrum, in the spectrum's own units.

    Uses the median rather than the mean because the median is insensitive to the
    minority of bins that actually contain signals -- which is the entire point of a
    noise floor. The median-to-mean bias is then corrected using the known
    chi-square statistics of the estimator.

    Parameters
    ----------
    exclude_fraction:
        Drop this fraction of the strongest bins before estimating. Useful when a
        band is heavily occupied and even the median sits on signal.
    local_bins:
        If given, estimate a *sliding* floor over windows of this many bins and
        return an array, so that a receiver with a tilted or shaped response does
        not get a single wrong global number. If ``None``, return a scalar.
    """
    linear = np.asarray(db_to_linear(spectrum.power_db), dtype=np.float64)
    correction = _chi2_median_ratio(spectrum.n_averages)

    if local_bins is None:
        values = linear
        if exclude_fraction > 0.0:
            keep = max(1, int(round(values.size * (1.0 - exclude_fraction))))
            values = np.sort(values)[:keep]
        return float(linear_to_db(float(np.median(values)) / correction))

    if local_bins < 3:
        raise ValueError(f"local_bins must be >= 3, got {local_bins}")
    half = local_bins // 2
    padded = np.pad(linear, half, mode="edge")
    # Sliding median via a strided view; local_bins is small so this stays cheap.
    windows = np.lib.stride_tricks.sliding_window_view(padded, local_bins)[: linear.size]
    floor_linear = np.median(windows, axis=-1) / correction
    return np.asarray(linear_to_db(floor_linear), dtype=np.float64)


@dataclass(frozen=True, slots=True)
class Peak:
    """A spectral peak.

    ``frequency_hz`` is refined below bin spacing by parabolic interpolation, but
    ``frequency_uncertainty_hz`` is reported alongside it and is never smaller than
    half a bin. A peak is a measurement of energy at a frequency; it is not an
    identification of a transmitter or a service.
    """

    frequency_hz: float
    frequency_uncertainty_hz: float
    power_db: float
    noise_floor_db: float
    snr_db: float
    bin_index: int
    unit: PowerUnit
    evidence: Evidence = Evidence.DERIVED

    def to_dict(self) -> dict[str, object]:
        return {
            "frequency_hz": self.frequency_hz,
            "frequency_uncertainty_hz": self.frequency_uncertainty_hz,
            "power_db": self.power_db,
            "noise_floor_db": self.noise_floor_db,
            "snr_db": self.snr_db,
            "bin_index": self.bin_index,
            "unit": self.unit.value,
            "calibrated": self.unit.is_calibrated,
            "evidence": self.evidence.value,
        }


def detect_peaks(
    spectrum: PowerSpectrum,
    threshold_db: float = 10.0,
    max_peaks: int = 16,
    min_separation_hz: float | None = None,
    local_floor_bins: int | None = None,
) -> list[Peak]:
    """Find local maxima standing at least ``threshold_db`` above the noise floor.

    Parameters
    ----------
    threshold_db:
        Required excess over the estimated noise floor. 10 dB is a conservative
        default that keeps false positives rare on a single observation.
    min_separation_hz:
        Minimum spacing between reported peaks. Defaults to twice the equivalent
        noise bandwidth, which stops one carrier's window skirts being reported as
        a family of separate signals.
    local_floor_bins:
        Estimate the floor locally over this many bins instead of globally. Use
        when the receiver response is not flat across the captured span.
    """
    power_db = np.asarray(spectrum.power_db, dtype=np.float64)
    if power_db.size < 3:
        return []

    floor = estimate_noise_floor(spectrum, local_bins=local_floor_bins)
    floor_arr = np.full_like(power_db, floor) if np.isscalar(floor) else np.asarray(floor)

    excess = power_db - floor_arr
    interior = slice(1, -1)
    is_local_max = np.zeros_like(power_db, dtype=bool)
    is_local_max[interior] = (power_db[1:-1] >= power_db[:-2]) & (power_db[1:-1] > power_db[2:])
    candidates = np.flatnonzero(is_local_max & (excess >= threshold_db))
    if candidates.size == 0:
        return []

    # Strongest first, then greedily drop anything too close to an accepted peak.
    order = candidates[np.argsort(excess[candidates])[::-1]]
    separation = (
        2.0 * spectrum.enbw_hz if min_separation_hz is None else float(min_separation_hz)
    )
    min_gap_bins = max(1, int(round(separation / spectrum.bin_width_hz)))

    accepted: list[int] = []
    for idx in order:
        if len(accepted) >= max_peaks:
            break
        if all(abs(int(idx) - a) >= min_gap_bins for a in accepted):
            accepted.append(int(idx))

    peaks: list[Peak] = []
    for idx in sorted(accepted):
        offset_bins = _parabolic_offset(power_db, idx)
        frequency = spectrum.frequencies_hz[idx] + offset_bins * spectrum.bin_width_hz
        peaks.append(
            Peak(
                frequency_hz=float(frequency),
                # Half a bin from discretisation, and never better than that however
                # good the parabolic fit looks.
                frequency_uncertainty_hz=float(spectrum.bin_width_hz / 2.0),
                power_db=float(power_db[idx]),
                noise_floor_db=float(floor_arr[idx]),
                snr_db=float(excess[idx]),
                bin_index=idx,
                unit=spectrum.unit,
            )
        )
    peaks.sort(key=lambda p: p.snr_db, reverse=True)
    return peaks


def _parabolic_offset(power_db: np.ndarray, idx: int) -> float:
    """Sub-bin peak offset from a 3-point parabola through the dB values.

    Returns 0.0 at the array edges, where there is no parabola to fit. The result is
    clamped to +/-0.5 bins: a fit that wants to place the peak outside its own bin is
    telling you the data is too noisy for interpolation, not that the peak moved.
    """
    if idx <= 0 or idx >= power_db.size - 1:
        return 0.0
    left, mid, right = power_db[idx - 1], power_db[idx], power_db[idx + 1]
    denom = left - 2.0 * mid + right
    if denom == 0.0:
        return 0.0
    offset = 0.5 * (left - right) / denom
    return float(np.clip(offset, -0.5, 0.5))


def band_occupancy(
    spectrum: PowerSpectrum,
    threshold_db: float = 6.0,
    f_low_hz: float | None = None,
    f_high_hz: float | None = None,
) -> float:
    """Fraction of bins in a band that sit ``threshold_db`` above the noise floor.

    A crude but useful summary of how busy a band was during one observation.
    """
    freqs = spectrum.frequencies_hz
    mask = np.ones(freqs.shape, dtype=bool)
    if f_low_hz is not None:
        mask &= freqs >= f_low_hz
    if f_high_hz is not None:
        mask &= freqs <= f_high_hz
    if not np.any(mask):
        return 0.0
    floor = estimate_noise_floor(spectrum)
    above = spectrum.power_db[mask] >= (float(floor) + threshold_db)
    return float(np.count_nonzero(above) / above.size)


__all__ = ["Peak", "band_occupancy", "detect_peaks", "estimate_noise_floor"]
