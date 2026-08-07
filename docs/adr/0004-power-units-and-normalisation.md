# ADR 0004 — Relative dBFS, with density normalisation by default

**Status:** Accepted · **Date:** 2026-08-07

## Context

An RTL-SDR is not a calibrated instrument. Its gain table is nominal, its frequency
response is not flat, its antenna is unknown, and none of it is traceable. But every
spectrum analyser display a user has ever seen is labelled in dBm, and it is very
tempting to print that.

Separately, the FFT scaling convention has to be chosen. Power-per-bin and
power-per-hertz differ by the equivalent noise bandwidth, and picking wrongly makes
two surveys silently incomparable.

## Decision (units)

**V0 produces `dBFS` and `dBFS/Hz` only. `dBm` is never printed.**

Enforced rather than documented:

- `PowerUnit.is_calibrated` is `False` for both, and gates any absolute display;
- `PowerUnit.label` renders as `dBFS/Hz (relative, uncalibrated)`, and that string
  reaches the CLI, the JSON API and the viewer legend;
- the manifest carries `"calibrated": false` plus a `reference` string spelling out
  what the numbers are relative to;
- an end-to-end test asserts no output of an uncalibrated survey contains `dBm`.

The reference is explicit: **dB relative to a full-scale complex sinusoid on the ADC,
at the recorded gain setting.**

Relative units still answer the V0 question. Comparing power *between places* within
one survey at fixed gain is exactly what a spatial map needs, and SNR is a ratio that
cancels the unknown reference entirely. What they cannot do — state field strength,
compare against a regulatory limit, compare across receivers — is out of V0 scope
anyway. See [calibration.md](../calibration.md).

## Decision (normalisation)

**Default to `DENSITY` (power spectral density, dBFS/Hz). Offer `SPECTRUM`
(power-per-bin, dBFS) for tone work.**

```
SPECTRUM:  P[k] = |X[k]|² / S1²        full-scale tone on a bin centre → 0.000000 dBFS
DENSITY:   P[k] = |X[k]|² / (fs · S2)  white noise reads the same at any FFT size

density_dB = spectrum_dB − 10·log10(ENBW_Hz)
```

Density is the default because comparing surveys taken with different FFT sizes is a
normal thing to want, and per-bin numbers make that comparison wrong by
`10·log10(N₂/N₁)` — an error with no visible symptom.

Both are tested against closed-form expectations: 0 dBFS for a full-scale tone across
four windows and three FFT sizes; a designed noise floor recovered to ±0.15 dB at three
FFT sizes; the two conventions differing by exactly the ENBW.

`PowerSpectrum` carries `enbw_hz` **and** `bin_width_hz` separately, because quoting
bin spacing as "resolution bandwidth" overstates what a Hann-windowed measurement can
distinguish by 50%.

## Consequences

**Good.** No user can mistake a receiver reading for field strength. Survey-to-survey
comparison is well defined. The calibration path is a correction stage at a single
seam, with `PowerUnit` already able to express the result.

**Bad.** "−95 dBFS/Hz" is less familiar than "−62 dBm", and the label is verbose. Users
coming from commercial analysers may find it pedantic.

**Accepted.** A number that invites invalid comparison is worse than one that requires
a sentence of explanation.

## What would change our mind

Nothing about the default. Adding calibrated `dBm` output once a correction chain and a
stated uncertainty exist is the planned evolution, not a reversal — the type system
already models it.
