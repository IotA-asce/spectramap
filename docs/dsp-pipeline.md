# The DSP pipeline

Everything a SpectraMap map shows rests on the numbers this stage produces. A
spectrum that is off by a scale factor, or whose frequency axis is shifted by half a
bin, produces heatmaps that look completely convincing and are wrong. This document
states the conventions precisely enough that anyone can check them.

Implementation: `spectramap/dsp/`. Tests: `tests/test_dsp.py`.

## The chain

```
complex IQ samples  (full scale = 1.0)
        │
        ├─ overload check ─────────► SDR_OVERLOAD flag
        │
        ▼
   DC removal (optional, default on)
        │
        ▼
   segmentation into N-sample windows, 50% overlap
        │
        ▼
   × window function (default Hann)
        │
        ▼
   FFT (complex, N-point)
        │
        ▼
   |X|²  → normalisation → linear power per bin
        │
        ▼
   average across segments (Welch), in the LINEAR domain
        │
        ▼
   fftshift → ascending absolute frequency axis
        │
        ▼
   PowerSpectrum(frequencies_hz, power_db, unit, enbw_hz, n_averages)
```

## Reference level

Samples are complex, normalised so a **full-scale complex sinusoid has magnitude
1.0**. Every driver is responsible for delivering that convention; pyrtlsdr already
does, by mapping the RTL2832U's 8-bit unsigned pairs to ±1.

With that fixed, all power is dB relative to full scale — **dBFS** — at the gain
setting recorded alongside it. This is a receiver-relative unit. It is not dBm, and
[calibration.md](calibration.md) explains what it would take to make it one.

## Window functions

For a window `w` of length `N`:

| Quantity | Definition | Meaning |
|---|---|---|
| Coherent gain | `S1 = Σ w` | scales a discrete tone |
| Incoherent gain | `S2 = Σ w²` | scales broadband noise |
| ENBW (bins) | `N · S2 / S1²` | width of the ideal rectangular filter passing the same noise power |
| Scalloping loss | response at a half-bin offset | worst-case under-read of a single-bin peak |

Computed values (verified in `test_window_enbw_matches_published_values`):

| Window | ENBW (bins) | Scalloping loss | Use |
|---|---|---|---|
| rectangular | 1.0000 | −3.92 dB | never, for survey work |
| **hann** | **1.5000** | **−1.42 dB** | default: good sidelobes, cheap |
| hamming | 1.3628 | −1.75 dB | slightly narrower main lobe |
| blackman-harris | 2.0044 | −0.83 dB | when a strong carrier would otherwise smear over weak neighbours |

Hann is the **periodic** (DFT-even) definition, `w[k] = 0.5 − 0.5·cos(2πk/N)`, not
the symmetric variant used for FIR filter design. The symmetric version has a
different ENBW and would put a small, silent scale error into every measurement.

An unknown window name raises rather than falling back to a default: a silently
substituted window corrupts every power number downstream.

### Bin spacing is not resolution bandwidth

```
bin spacing        Δf = fs / N
resolution BW    ENBW = Δf × ENBW_bins
```

At 2.4 MS/s with N = 4096 and a Hann window: bins are 586 Hz apart, but the true
resolution bandwidth is **879 Hz**. `PowerSpectrum` carries both, and the spectrum
explorer displays the ENBW, because quoting the bin spacing as "resolution" overstates
what the measurement can distinguish by 50%.

## Normalisation

Two conventions, both offered, one chosen per survey and recorded in the manifest.

### `SPECTRUM` — power per bin

```
P[k] = |X[k]|² / S1²
```

A full-scale tone sitting on a bin centre reads **exactly 0 dBFS**, for any window
and any FFT size. Unit: `dBFS`. Correct for reading discrete carriers.

### `DENSITY` — power spectral density (default)

```
P[k] = |X[k]|² / (fs · S2)
```

Units of power per hertz: `dBFS/Hz`. A white noise floor reads the **same number
regardless of FFT size**, which is why this is the default — comparing two surveys
taken with different FFT sizes is a normal thing to want, and per-bin numbers make
that comparison silently wrong by `10·log10(N₂/N₁)`.

### The exact relationship

```
density_dB = spectrum_dB − 10·log10(ENBW_Hz)
```

Verified to floating-point equality in `test_spectrum_and_density_differ_by_exactly_enbw`.

### What the tests pin

- A full-scale tone reads 0.000000 dBFS across `{rectangular, hann, hamming,
  blackman-harris} × {256, 1024, 4096}`.
- A half-amplitude tone reads −6.0206 dB.
- Designed noise at −125 dBFS/Hz measures −125 ± 0.15 dB at N ∈ {512, 2048, 8192}.
- A tone placed at bin *k* is measured at bin *k*, for offsets from −701 to +700.

## Frequency axis

For an N-point complex FFT after `fftshift`, bin *k* is centred at:

```
f[k] = f_center + (k − N/2) · fs / N
```

The axis is **asymmetric** for even N: it runs from `−fs/2` up to `+fs/2 − fs/N`,
because the Nyquist bin belongs to the negative side. This is the single most common
place to introduce a half-bin frequency error, so it is implemented once
(`frequency_axis`) and tested directly.

Frequencies are stored and exchanged as **absolute** values, never as offsets. Offset
tuning, retuning and sweep steps are therefore invisible to everything downstream.

## DC removal, and why offset tuning exists

A zero-IF receiver has a large artefact at exactly its tuned frequency: ADC offset
plus local-oscillator self-mixing. `remove_dc` subtracts the block mean, which
suppresses it.

**It also deletes any real signal sitting there.** A survey tuned exactly onto its
target reliably reports that target as empty — a failure that looks exactly like "no
signal present" and is easy to mistake for a true negative. This was found by testing
during development, and it is pinned by
`test_dc_removal_also_destroys_a_signal_tuned_exactly_to_centre` so that nobody
"fixes" the DC removal and reintroduces a fake carrier at the centre of every survey.

The fix is **offset tuning**: put the local oscillator a few hundred kilohertz away
from the target, keeping the target inside the captured span but away from DC. It is
a first-class configuration setting with a non-zero default, the configuration
validator warns when the offset is within a few bins of DC, and it errors outright
when the offset would push the target outside the span. See
[ADR 0006](adr/0006-offset-tuning.md).

## Averaging

Two levels, both in the **linear power domain**:

1. **Welch** — overlapping segments within one IQ block, averaged.
2. **Integration** — all segments across the configured interval (default 500 ms),
   accumulated in `SpectrumAccumulator`.

Averaging decibels computes a *geometric* mean of powers. For one strong sample among
weak ones the difference is large: averaging `[1, 100]` linearly gives 50.5, in dB it
gives 10 — a 7 dB under-report of exactly the bursty signal a user is hunting.
`test_averaging_happens_in_the_linear_domain` pins this, and every aggregation in the
project — spectra, grid cells, band power, noise floors — routes through
`mean_power_db`.

The accumulator also keeps per-bin min and max, so aggregation can report variation
rather than hiding it.

## Detection

### Noise floor

Median-based, because the median is insensitive to the minority of bins that contain
signals — which is the whole point of a noise floor. But a median is a *biased*
estimator of the mean of a chi-square distribution, so the bias is corrected using
the Wilson–Hilferty approximation:

```
median / mean ≈ (1 − 2/(9k))³,    k = 2M degrees of freedom, M = number of averages
```

Without this correction the floor reads ~1.6 dB low at M = 1, and that error would be
reported as free SNR on every peak in the survey.
`test_noise_floor_estimate_is_unbiased` checks the estimate against a designed floor
to ±0.3 dB, and `test_noise_floor_ignores_a_strong_carrier` confirms a 40 dB carrier
does not move it.

A sliding local floor (`local_bins`) is available for receivers whose response is not
flat across the span.

### Peaks

Local maxima at least `threshold_db` above the floor, greedily thinned so that peaks
closer than twice the ENBW are not reported separately — otherwise one carrier's
window skirts appear as a family of signals.

Sub-bin frequency refinement uses a 3-point parabola through the dB values, clamped
to ±0.5 bins: a fit that wants to place the peak outside its own bin is telling you
the data is too noisy for interpolation, not that the peak moved.

**Reported frequency uncertainty is never better than half a bin**, however tight the
parabolic fit looks.

This is spectral analysis, not demodulation. Nothing here inspects, decodes or stores
the content of a transmission. See [safety-and-legal.md](safety-and-legal.md).

## Overload

`overload_fraction` counts samples whose I or Q component is at or near full scale. A
non-trivial value means the front end is compressing, and the measured power is an
**under-estimate of the true field, not a valid reading**. It raises `SDR_OVERLOAD`,
and such observations are excluded from spatial products by default while remaining
in the survey.

## Performance

At 2.4 MS/s with N = 4096 and 50% overlap, a 109 ms block yields ~127 segments. Those
are transformed in batches of 64 through a strided no-copy view, bounding peak memory
at roughly `64 × N × 16` bytes regardless of block length. Measured throughput on the
development machine is comfortably faster than real time on a laptop; the compact CI
scenario simulates 80 seconds of surveying in about 1.4 seconds of wall clock,
including synthesis, DSP, storage and reconstruction.
