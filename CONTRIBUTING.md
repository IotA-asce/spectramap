# Contributing to SpectraMap

Contributions are welcome. The most useful ones right now are **physical hardware
validation** — the RTL-SDR driver has never seen a real dongle.

## Getting started

```bash
git clone <repo> && cd spectramap
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

pytest          # 227 tests, no radio hardware needed
ruff check .
python examples/simulated_survey.py
```

The entire pipeline runs against the simulator. If you need hardware to run a test,
that test is in the wrong place.

Keep `tests/__init__.py`: tests share helpers through `tests.conftest`. The package
marker makes those imports work with the `pytest` console command used by CI as
well as `python -m pytest`; the latter alone can hide a missing package marker.
See [pytest's import-path explanation](https://docs.pytest.org/en/stable/explanation/pythonpath.html).

## Engineering priorities

In order. When two conflict, the higher one wins.

1. measurement integrity
2. reproducibility
3. correct DSP
4. raw-data preservation
5. transparent uncertainty
6. hardware abstraction
7. testability
8. performance
9. visualisation
10. advanced features

**Never prioritise a beautiful heatmap over correct measurements.**

## Rules that are not negotiable

These are enforced by tests, and a change that breaks one is a bug, not a trade-off.

- **Evidence is never erased.** Every value carries `MEASURED` / `DERIVED` /
  `INTERPOLATED` / `ESTIMATED` / `ASSUMED`, all the way to the UI.
- **Uncalibrated numbers are labelled uncalibrated.** No code path may print `dBm` from
  a survey whose `PowerUnit.is_calibrated` is `False`.
- **Unmeasured ground is masked, not invented.** Interpolated cells beyond
  `max_distance_m` are `NaN` and are not drawn.
- **Bad data is flagged, not deleted.** Filtering is an analysis-time decision.
- **Averaging happens in the linear power domain.** Averaging dB is a geometric mean
  and under-reports bursty signals.
- **No false precision.** Estimates return a region and an uncertainty, with the
  uncertainty floored by the position accuracy.
- **Derived products carry provenance.** Algorithm, parameters, source survey, version.

## Scope

Out of scope, permanently — see [docs/safety-and-legal.md](docs/safety-and-legal.md):
transmission of any kind, demodulation, payload decoding or storage, decryption,
jamming, signal injection, transmitter impersonation, and anything whose primary
purpose is identifying individuals or their devices.

## Testing

Every behavioural change needs a test. What a good one looks like here:

- **Assert against closed-form expectations, not against the code's own output.** The
  DSP tests check a full-scale tone reads exactly 0 dBFS and that a designed noise floor
  is recovered — not that the answer matches a golden file.
- **Test the failure modes.** Device dies mid-survey, GPS drops out, samples are lost,
  the disk fills, the band is empty. The simulator has fault injection for all of these.
- **Name what the test protects.** `test_dc_removal_also_destroys_a_signal_tuned_exactly_to_centre`
  exists so nobody removes the offset-tuning default.

New DSP or geodesy code should be checked against published values where they exist —
window ENBW figures, WGS84 degree lengths — rather than against another function in
this repository.

## Adding a receiver

1. Implement `SDRDevice` in `spectramap/sdr/`.
2. Honour the two conventions in `sdr/base.py`: full-scale normalisation at magnitude
   1.0, and honest `tuning_evidence` in the capabilities.
3. Add one entry to `DRIVERS` in `sdr/registry.py`.
4. Guard the vendor import so `spectramap devices` still works without the library.

Nothing else changes.

## Adding an interpolation method

Return an `InterpolatedField` with `power_db`, `confidence`, `distance_m` and a
`Provenance`. Mask cells that are too far from a measurement. A method that produces a
smoother, more confident-looking surface must not produce a *more confident* one.

## Reporting hardware results

Especially valuable. Please include: dongle model and tuner, host, sample rate, gain,
antenna, the output of `spectramap status`, and — if you ran the physical validation —
the localisation error against your known transmitter position, plus the survey
geometry. See [docs/v0-plan.md](docs/v0-plan.md#the-physical-validation).

## Style

`ruff check .` must pass. Comments explain *why*, not *what* — the codebase leans on
this heavily for the parts where the obvious implementation is subtly wrong. Match the
density and idiom of the surrounding code.
