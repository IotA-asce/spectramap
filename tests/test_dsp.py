"""DSP correctness.

These are the tests that decide whether any number in SpectraMap means anything. A
spectrum that is off by a scale factor, or whose frequency axis is shifted by half a
bin, produces maps that look completely convincing and are wrong.
"""

from __future__ import annotations

import numpy as np
import pytest

from spectramap.dsp.detect import band_occupancy, detect_peaks, estimate_noise_floor
from spectramap.dsp.spectrum import SpectrumAccumulator, SpectrumEstimator, frequency_axis
from spectramap.dsp.window import get_window, window_stats
from spectramap.types import FFTConfig, Normalization, PowerUnit
from tests.conftest import complex_noise, tone

FS = 2_400_000.0
FC = 433_920_000.0


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------


def test_window_enbw_matches_published_values():
    """ENBW is the number that converts per-bin to per-Hz; textbook values."""
    expected = {"rectangular": 1.0, "hann": 1.5, "hamming": 1.3628, "blackman-harris": 2.0044}
    for name, enbw in expected.items():
        assert window_stats(name, 4096).enbw_bins == pytest.approx(enbw, rel=1e-3)


def test_hann_window_is_periodic_not_symmetric():
    """The DFT-even definition is required for spectral analysis.

    The symmetric variant (used for FIR design) has a different ENBW and would put a
    small, silent scale error into every measurement.
    """
    w = get_window("hann", 8)
    assert w[0] == pytest.approx(0.0)
    assert w[4] == pytest.approx(1.0)  # Peak lands exactly on a sample.
    assert w[1] == pytest.approx(w[7])


def test_scalloping_loss_is_worst_for_rectangular():
    rect = window_stats("rectangular", 1024).scalloping_loss_db
    hann = window_stats("hann", 1024).scalloping_loss_db
    assert rect == pytest.approx(-3.92, abs=0.05)
    assert hann == pytest.approx(-1.42, abs=0.05)
    assert hann > rect  # Less loss with a smoother window.


def test_unknown_window_is_rejected_not_substituted():
    with pytest.raises(ValueError, match="unknown window"):
        get_window("kaiser-ish", 256)


# ---------------------------------------------------------------------------
# Frequency axis
# ---------------------------------------------------------------------------


def test_frequency_axis_is_ascending_and_centred():
    axis = frequency_axis(FC, FS, 8)
    assert np.all(np.diff(axis) > 0)
    assert axis[4] == pytest.approx(FC)  # Bin N/2 is exactly the tuned frequency.
    assert axis[0] == pytest.approx(FC - FS / 2)
    # Even-length FFTs are asymmetric: the top bin is one step short of +fs/2.
    assert axis[-1] == pytest.approx(FC + FS / 2 - FS / 8)


@pytest.mark.parametrize("offset_bins", [-701, -1, 0, 1, 37, 700])
def test_tone_lands_in_the_expected_bin(offset_bins):
    """A tone placed at bin k must be measured at bin k, with no half-bin drift."""
    config = FFTConfig(size=4096, window="hann", normalization=Normalization.SPECTRUM,
                       remove_dc=False)
    est = SpectrumEstimator(config, FS, FC)
    target = FC + offset_bins * FS / 4096
    spectrum = est.estimate(tone(target, FC, FS, 4096 * 4))
    peak = int(np.argmax(spectrum.power_db))
    assert peak == 2048 + offset_bins
    assert spectrum.frequencies_hz[peak] == pytest.approx(target)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", ["rectangular", "hann", "hamming", "blackman-harris"])
@pytest.mark.parametrize("size", [256, 1024, 4096])
def test_full_scale_tone_reads_zero_dbfs(window, size):
    """Spectrum normalisation is defined so a full-scale tone reads 0 dBFS exactly.

    That must hold for every window and every FFT size, or powers from surveys with
    different settings cannot be compared.
    """
    config = FFTConfig(size=size, window=window, normalization=Normalization.SPECTRUM,
                       remove_dc=False)
    est = SpectrumEstimator(config, FS, FC)
    target = FC + 40 * FS / size
    spectrum = est.estimate(tone(target, FC, FS, size * 4))
    assert spectrum.power_db.max() == pytest.approx(0.0, abs=1e-6)
    assert spectrum.unit is PowerUnit.DBFS


def test_half_amplitude_tone_reads_minus_six_db():
    config = FFTConfig(size=1024, window="hann", normalization=Normalization.SPECTRUM,
                       remove_dc=False)
    est = SpectrumEstimator(config, FS, FC)
    spectrum = est.estimate(tone(FC + 100 * FS / 1024, FC, FS, 4096, amplitude=0.5))
    assert spectrum.power_db.max() == pytest.approx(-6.0206, abs=1e-4)


@pytest.mark.parametrize("size", [512, 2048, 8192])
def test_noise_psd_is_independent_of_fft_size(size):
    """Density normalisation must give the same noise floor at any FFT size.

    This is precisely why DENSITY is the default: per-bin numbers would shift by
    10*log10(size) between two otherwise identical surveys.
    """
    designed = -125.0
    samples = complex_noise(designed, FS, 2**19, seed=3)
    config = FFTConfig(size=size, window="hann", normalization=Normalization.DENSITY,
                       remove_dc=False)
    est = SpectrumEstimator(config, FS, FC)
    spectrum = est.estimate(samples)
    mean_psd_db = 10 * np.log10(np.mean(10 ** (spectrum.power_db / 10)))
    assert mean_psd_db == pytest.approx(designed, abs=0.15)
    assert spectrum.unit is PowerUnit.DBFS_PER_HZ


def test_spectrum_and_density_differ_by_exactly_enbw():
    samples = complex_noise(-120.0, FS, 2**17, seed=11)
    common = dict(size=1024, window="hann", remove_dc=False)
    per_bin = SpectrumEstimator(
        FFTConfig(**common, normalization=Normalization.SPECTRUM), FS, FC
    ).estimate(samples)
    density = SpectrumEstimator(
        FFTConfig(**common, normalization=Normalization.DENSITY), FS, FC
    ).estimate(samples)
    delta = per_bin.power_db - density.power_db
    assert np.allclose(delta, 10 * np.log10(per_bin.enbw_hz))


def test_enbw_exceeds_bin_width_for_a_real_window():
    """Bin spacing is not resolution bandwidth, and the code must not conflate them."""
    est = SpectrumEstimator(FFTConfig(size=4096, window="hann"), FS, FC)
    assert est.enbw_hz == pytest.approx(1.5 * est.bin_width_hz)
    assert est.enbw_hz > est.bin_width_hz


# ---------------------------------------------------------------------------
# DC handling
# ---------------------------------------------------------------------------


def test_dc_removal_suppresses_a_dc_offset():
    config = FFTConfig(size=1024, window="hann", normalization=Normalization.SPECTRUM,
                       remove_dc=True)
    est = SpectrumEstimator(config, FS, FC)
    samples = complex_noise(-120.0, FS, 8192, seed=5) + np.complex64(0.3 + 0.3j)
    spectrum = est.estimate(samples)
    assert spectrum.power_db[512] < -80  # The DC spike is gone.


def test_dc_removal_also_destroys_a_signal_tuned_exactly_to_centre():
    """The reason offset tuning is a first-class configuration setting.

    A zero-IF receiver cannot measure its own tuned frequency: the DC-blocking that
    suppresses the receiver's own artefact removes any real signal there too. This
    test pins that behaviour so nobody "fixes" the DC removal and silently
    reintroduces a fake carrier at the centre of every survey.
    """
    config = FFTConfig(size=1024, window="hann", normalization=Normalization.SPECTRUM,
                       remove_dc=True)
    est = SpectrumEstimator(config, FS, FC)
    at_dc = est.estimate(tone(FC, FC, FS, 8192, amplitude=0.5))
    # On a bin centre, so the comparison is not confused by scalloping loss.
    offset = est.estimate(tone(FC + 21 * FS / 1024, FC, FS, 8192, amplitude=0.5))
    assert at_dc.power_db.max() < -40      # Annihilated.
    assert offset.power_db.max() == pytest.approx(-6.02, abs=0.1)  # Measured correctly.


# ---------------------------------------------------------------------------
# Averaging and accumulation
# ---------------------------------------------------------------------------


def test_averaging_happens_in_the_linear_domain():
    """Averaging dB would give a geometric mean and under-report bursty signals."""
    accumulator = SpectrumAccumulator(3)
    accumulator.add(np.array([1.0, 1.0, 1.0]))
    accumulator.add(np.array([100.0, 1.0, 1.0]))
    mean = accumulator.mean_linear()
    assert mean[0] == pytest.approx(50.5)     # Arithmetic mean of powers.
    assert mean[0] != pytest.approx(10.0)     # Geometric mean would give 10.


def test_accumulator_tracks_spread():
    accumulator = SpectrumAccumulator(2)
    accumulator.add(np.array([1e-6, 1e-3]))
    accumulator.add(np.array([1e-4, 1e-3]))
    assert accumulator.count == 2
    assert accumulator.min_db()[0] == pytest.approx(-60.0)
    assert accumulator.max_db()[0] == pytest.approx(-40.0)


def test_welch_averaging_reduces_variance():
    """More segments -> a tighter noise-floor estimate. Sanity check on the averaging."""
    samples = complex_noise(-120.0, FS, 2**18, seed=17)
    config = FFTConfig(size=1024, window="hann", remove_dc=False)
    est = SpectrumEstimator(config, FS, FC)
    one = est.estimate(samples[:1024])
    many = est.estimate(samples)
    assert many.n_averages > 100
    assert np.std(many.power_db) < np.std(one.power_db) / 3


def test_short_block_is_rejected_rather_than_zero_padded():
    est = SpectrumEstimator(FFTConfig(size=4096), FS, FC)
    with pytest.raises(ValueError, match="shorter than FFT size"):
        est.linear_power(np.zeros(100, dtype=np.complex64))


def test_real_input_is_rejected():
    est = SpectrumEstimator(FFTConfig(size=64), FS, FC)
    with pytest.raises(TypeError, match="complex"):
        est.linear_power(np.zeros(1024, dtype=np.float64))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_noise_floor_estimate_is_unbiased():
    """The median-to-mean correction must actually remove the bias.

    Without it the floor reads ~1.6 dB low at one average, which would be reported as
    free SNR on every peak in the survey.
    """
    designed = -118.0
    samples = complex_noise(designed, FS, 2**18, seed=23)
    est = SpectrumEstimator(FFTConfig(size=1024, window="hann", remove_dc=False), FS, FC)
    spectrum = est.estimate(samples)
    assert float(estimate_noise_floor(spectrum)) == pytest.approx(designed, abs=0.3)


def test_noise_floor_ignores_a_strong_carrier():
    est = SpectrumEstimator(FFTConfig(size=1024, window="hann", remove_dc=False), FS, FC)
    noise = complex_noise(-118.0, FS, 2**17, seed=29)
    with_signal = noise + tone(FC + 200 * FS / 1024, FC, FS, noise.size, amplitude=0.05)
    assert float(estimate_noise_floor(est.estimate(with_signal))) == pytest.approx(-118.0, abs=0.4)


def test_detect_peaks_finds_a_known_carrier():
    est = SpectrumEstimator(FFTConfig(size=2048, window="hann", remove_dc=False), FS, FC)
    target = FC + 137.5 * FS / 2048  # Deliberately between bin centres.
    samples = complex_noise(-120.0, FS, 2**17, seed=31) + tone(
        target, FC, FS, 2**17, amplitude=0.01
    )
    peaks = detect_peaks(est.estimate(samples), threshold_db=10.0)
    assert peaks, "a 40 dB carrier must be detected"
    best = peaks[0]
    assert best.frequency_hz == pytest.approx(target, abs=best.frequency_uncertainty_hz)
    assert best.snr_db > 30
    # Uncertainty is never claimed to be better than half a bin.
    assert best.frequency_uncertainty_hz == pytest.approx(est.bin_width_hz / 2)


def test_detect_peaks_finds_nothing_in_pure_noise():
    est = SpectrumEstimator(FFTConfig(size=1024, window="hann", remove_dc=False), FS, FC)
    spectrum = est.estimate(complex_noise(-120.0, FS, 2**18, seed=37))
    assert detect_peaks(spectrum, threshold_db=10.0) == []


def test_close_carriers_are_not_reported_as_a_family_of_peaks():
    est = SpectrumEstimator(FFTConfig(size=2048, window="hann", remove_dc=False), FS, FC)
    samples = complex_noise(-125.0, FS, 2**17, seed=41)
    samples = samples + tone(FC + 300 * FS / 2048, FC, FS, samples.size, amplitude=0.05)
    peaks = detect_peaks(est.estimate(samples), threshold_db=10.0)
    assert len(peaks) == 1, "window skirts must not be counted as separate signals"


def test_band_occupancy_reflects_activity():
    est = SpectrumEstimator(FFTConfig(size=1024, window="hann", remove_dc=False), FS, FC)
    quiet = est.estimate(complex_noise(-120.0, FS, 2**17, seed=43))
    assert band_occupancy(quiet, threshold_db=6.0) < 0.02
