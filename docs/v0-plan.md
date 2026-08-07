# V0 plan and status

**V0 goal.** Walk through an outdoor area carrying an SDR receiver and a GPS source,
record RF power measurements at known positions, and reconstruct an interactive map
showing the spatial intensity of a selected frequency or band.

**Status: the simulated vertical slice is complete and validated in CI. Physical
RTL-SDR integration has not yet been done.**

## Milestones

Each is independently verifiable. ✅ complete · ⬜ not started.

### M1 — Foundations ✅

Core types, evidence and unit model, quality flags, project skeleton, CI.

*Verified by:* `tests/test_config.py`, `tests/test_storage.py`.

### M2 — DSP ✅

Windows and their scaling constants, Welch PSD, both normalisations, absolute
frequency axis, noise-floor estimation, peak detection, overload detection.

*Verified by:* `tests/test_dsp.py` (42 tests). A full-scale tone reads 0.000000 dBFS
across four windows and three FFT sizes; a designed noise floor is recovered to
±0.15 dB independently of FFT size; a tone placed at bin *k* is measured at bin *k*.

### M3 — Device abstraction and the simulator ✅

`SDRDevice`, driver registry, `MockSDR`, virtual world with log-distance propagation,
frequency drift, duty cycling, shadowing, gain and AGC behaviour, fault injection.

*Verified by:* `tests/test_simulation.py`. Measured power matches the closed-form path
loss to ±0.2 dB; drift is checked against the integral of instantaneous frequency;
duty cycling reduces integrated power by exactly 3 dB; identical seeds produce
byte-identical samples.

### M4 — Positioning ✅

`PositionProvider`, NMEA 0183 parser (checksums, `ddmm.mmmm`, GGA/RMC/GSA, DOP →
accuracy), `PositionTrack` with interpolation rules, mock and static providers.

*Verified by:* `tests/test_positioning.py`. The NMEA path is exercised in CI with
canned sentences and no receiver.

### M5 — Acquisition ✅

The recorder: integration windows, the RF/position time join, quality flagging, event
logging, disk-space guard, clean Ctrl+C, sweep stepping.

*Verified by:* `tests/test_recorder.py`. A device failure mid-survey still yields a
readable survey marked incomplete; dropped samples are flagged and logged; a GPS
dropout produces flagged observations rather than missing ones.

### M6 — Storage ✅

The `.smap` container, band-power query over memory-mapped chunks, crash-truncation
handling, GeoJSON route export.

*Verified by:* `tests/test_storage.py`, plus a CI job that reads a survey with only
`json` and `numpy`.

### M7 — Reconstruction ✅

Local tangent plane, grid aggregation with spread statistics, IDW with masking and a
confidence field, hotspot estimation with bootstrap uncertainty and a geometry score.

*Verified by:* `tests/test_geospatial.py`. Projection scale is checked against
published WGS84 degree lengths; masked cells are `NaN`, never zero; IDW cannot
overshoot the measured range.

### M8 — Interfaces ✅

CLI (`devices`, `gps`, `survey`, `status`, `analyze`, `serve`), local JSON API, and a
self-contained browser viewer with a frequency selector, spectrum explorer,
measurement points and confidence-driven opacity.

*Verified by:* `tests/test_cli.py`, `tests/test_web.py`.

### M9 — Simulated validation ✅

A synthetic beacon at a known virtual coordinate, surveyed through the entire real
pipeline, reconstructed without the mapping code ever being told where it is.

*Verified by:* `tests/test_end_to_end.py`. See below.

### M10 — Physical RTL-SDR bring-up ⬜

The driver is written and the abstraction is in place, but nothing has been validated
against real hardware. Order of work:

1. `spectramap devices` against a real dongle; confirm the reported capabilities.
2. Verify the FFT against a known signal — a signal generator, or a legal low-power
   beacon at a measured frequency. Confirm the measured frequency matches within the
   stated uncertainty, and measure the dongle's ppm error.
3. Confirm dropped-sample detection is real: run at 2.4 MS/s and again at 3.2 MS/s and
   check `DROPPED_SAMPLES` appears at the higher rate.
4. Real GPS: `spectramap gps --provider nmea --port ...`, confirm fix quality and that
   the HDOP-derived accuracy is sane.
5. Combined bench recording, stationary, and check the timestamps line up.
6. The outdoor controlled survey ([validation scenario](#the-physical-validation)).

### M11 — Calibration hooks ⬜

Architecture supports it ([calibration.md](calibration.md)); no correction stage is
implemented. V0 does not require it.

## Acceptance criteria

From the charter, against the current state.

| | Criterion | Status |
|---|---|---|
| **SDR** | A supported SDR can be discovered | ✅ mock; ⬜ real |
| | Centre frequency configurable | ✅ |
| | Sample rate configurable | ✅ |
| | Gain configurable | ✅ incl. snapping to real steps |
| | IQ samples can be captured | ✅ mock; ⬜ real |
| **DSP** | IQ → power spectrum | ✅ |
| | Frequency bins correctly calculated | ✅ tested to the bin |
| | Known synthetic signals appear at expected frequencies | ✅ |
| | DSP pipeline unit tested | ✅ 42 tests |
| **Position** | GPS coordinates acquired | ✅ parser; ⬜ real receiver |
| | GPS accuracy recorded | ✅ (as `ASSUMED`, from HDOP) |
| | Mock positioning works | ✅ |
| **Acquisition** | Spectrum and location synchronised | ✅ |
| | Recording runs for an extended period | ✅ simulated 11 min; ⬜ real |
| | Device failures surfaced | ✅ |
| **Storage** | Surveys persist across restarts | ✅ |
| | Survey configuration preserved | ✅ verbatim |
| | Measurements can be reprocessed | ✅ |
| **Mapping** | Measurements appear at correct locations | ✅ |
| | A selected frequency rendered spatially | ✅ |
| | Actual measurement points displayable | ✅ |
| | Interpolation uncertainty represented | ✅ mask + confidence + coverage |
| **Spectrum explorer** | Inspect spectra by location | ✅ |
| **Simulation** | Entire pipeline works with mock SDR + mock GPS | ✅ |
| | CI requires no RF hardware | ✅ |
| **Validation** | Known simulated source reconstructs correctly | ✅ |
| | Physical test produces a reasonable spatial gradient | ⬜ **outstanding** |

## The simulated validation

`tests/test_end_to_end.py`, run on every CI push.

**Setup.** A synthetic beacon at 433.92 MHz sits inside an 80 m grid at a known
virtual coordinate. A second emitter at 433.95 MHz sits 2.2 km away: clearly
detectable, but spatially flat over the grid. A simulated receiver walks the grid with
3.5 m GPS noise, recording through acquisition → DSP → storage → band selection →
aggregation → interpolation.

**The algorithm is never given the beacon's position.** Ground truth lives under a
separate manifest key and is read only for scoring.

**Results** (compact scenario, ~1.4 s wall clock):

| | |
|---|---|
| Measured dynamic range | 28 dB |
| Correlation of power with log-distance | < −0.85 |
| Hotspot error vs truth | **~4.5 m** |
| Field peak error | ~8.7 m |
| Stated uncertainty radius | 3.5 m (1σ-equivalent) |

**The control case matters as much.** For the distant carrier, the pipeline must
*decline* to localise: measured span < 5 dB, confidence forced to `low`, and the
caveat `NOT USABLE as a location` emitted. A pipeline that "localises" a spatially
flat signal is finding structure in noise, and the test asserts SpectraMap does not.

Also asserted end to end: unmeasured ground is masked rather than invented; every
product carries its evidence tag; no output of an uncalibrated survey contains the
string `dBm`; two runs give identical results; changing interpolation parameters
changes the recorded provenance.

## The physical validation

The first real-world success criterion, still outstanding:

> Place a known legal test transmitter at a known location, walk around it with
> SpectraMap, and produce a map whose measured RF field visibly converges toward the
> transmitter region **without manually supplying the transmitter's position**.

Protocol:

1. Place a legal, low-power, user-owned RF source at a measured position. Confirm it
   is permitted in your jurisdiction, on your own property or with permission. See
   [safety-and-legal.md](safety-and-legal.md).
2. Record its coordinates. Do not put them anywhere SpectraMap can read.
3. Survey the surrounding area — a **grid or loop, not a single straight line**. A
   straight walk cannot distinguish a source to the left from one to the right, and
   the geometry score will say so.
4. `spectramap analyze <survey> -f <frequency>`.
5. Compare the estimated region against the truth.

Record for each attempt: localisation error, number of observations, survey geometry
and geometry score, frequency, receiver configuration, antenna, gain, and weather or
obstruction notes. That gives SpectraMap a measurable benchmark that can be tracked
across releases.

Expect worse results than the simulation. The simulator has no multipath, no
obstruction, no antenna pattern, no body shadowing and no receiver drift. Errors of
tens of metres are the realistic expectation, which is exactly why the estimator
returns a region with a confidence grade rather than a coordinate.

## Explicit non-goals for V0

Not implemented, deliberately: drone integration, autonomous flight, 3D volumetric
mapping, advanced source localisation, multi-receiver synchronisation, phased arrays,
direction-finding arrays, cellular interception, decryption, payload decoding, Wi-Fi
credential analysis, jamming, signal injection, transmitter impersonation, AI
classification, cloud SaaS, distributed sensor networks, advanced propagation
modelling.

Ship the spatial RF map first.

## Known limitations

- **Uncalibrated.** Relative receiver units only. [calibration.md](calibration.md).
- **Untested on hardware.** The RTL-SDR driver has never seen a real dongle.
- **No absolute timing guarantee.** Block timestamps carry an uncorrected few
  milliseconds of USB and kernel latency.
- **Hotspot uncertainty covers sampling scatter only.** Systematic error from
  multipath and obstruction is not in the radius, and the caveats say so.
- **Base map needs internet.** Tiles come from OpenStreetMap when reachable; the
  viewer works offline without them, and all analysis is fully offline.
- **Sweep mode is implemented but lightly exercised.** V1 focus.
