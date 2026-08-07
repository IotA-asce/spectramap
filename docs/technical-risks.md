# Technical risks

Everything below can make a SpectraMap measurement wrong or misleading. Each entry
says what the effect is, what SpectraMap does about it, and what remains the user's
problem. Nothing here is hypothetical: several were found by testing during
development.

Severity is about how easily the problem produces a **convincing but wrong map**.

---

## Receiver

### 🔴 Automatic gain control

**Effect.** Under AGC the receiver rescales itself as you move. Two locations with
identical fields report different powers, and the resulting map is a picture of the
AGC's decisions rather than of the RF environment. This is the single most damaging
mistake available.

**Mitigation.** Fixed gain is the default. AGC produces a loud configuration warning,
recorded into the manifest; every affected observation is flagged `AGC_ACTIVE`; the
reconstruction adds a note that no spatial conclusion should be drawn. The simulator
models AGC behaviour, and a test confirms the noise floor moves as the receiver walks,
so the warning stays true.

**Residual.** SpectraMap cannot repair an AGC survey. Do not record one.

### 🔴 DC artefact at the tuned frequency

**Effect.** A zero-IF receiver has a large artefact at exactly its tuned frequency
(ADC offset plus LO self-mixing). The DC removal that suppresses it also **deletes any
real signal sitting there**, so a survey tuned exactly onto its target reports that
target as empty — indistinguishable from a genuine absence.

**Found by testing during development**, on the very first end-to-end run.

**Mitigation.** Offset tuning is a first-class setting with a non-zero default
(250 kHz). The validator warns when the offset is within a few bins of DC and errors
when it would push the target outside the span. A DSP test pins the behaviour so it
cannot be "fixed" back into existence. See [ADR 0006](adr/0006-offset-tuning.md).

### 🟠 Front-end overload

**Effect.** A strong nearby signal compresses the front end. Measured power becomes an
**under-estimate**, and looks like a plausible reading rather than an error — so a
transmitter can appear *weaker* as you approach it.

**Mitigation.** Clipped-sample fraction raises `SDR_OVERLOAD`; affected observations
are excluded from spatial products by default and the count is reported.

**Residual.** Compression begins before hard clipping, so mild overload is not caught.
Use less gain, or a band-pass filter.

### 🟠 Frequency error and drift

**Effect.** An uncompensated dongle sits 20–60 ppm off — 9–26 kHz at 433 MHz — and
drifts as it warms. A narrow carrier moves between bins during a survey, so a fixed
band selection loses it partway through.

**Mitigation.** `receiver.freq_correction_ppm`. The simulator models both linear drift
and sinusoidal wander so detection is tested against a moving target.

**Residual.** Not auto-detected or auto-corrected. Buy a TCXO unit, let it warm up,
and use a band wide enough to contain the drift.

### 🟠 Gain response is nominal

**Effect.** The gain table is not the actual gain, which varies by unit, frequency and
temperature by several decibels.

**Mitigation.** The applied gain — after snapping to a real step — is recorded, not the
requested one. Units are labelled relative.

**Residual.** Absolute levels are unreliable; ratios within one survey are fine.

### 🟡 Dropped samples

**Effect.** USB cannot always sustain the requested rate. Lost samples create silent
time gaps, so an observation is attributed to the wrong stretch of route.

**Mitigation.** Gaps are inferred from the block timeline and raise `DROPPED_SAMPLES`
plus a logged event. Fault injection in the simulator tests the path.

**Residual.** Detection is timeline-based and heuristic. Stay at ≤ 2.4 MS/s, use a
short good cable, avoid unpowered hubs.

### 🟡 Internal spurs and USB noise

**Effect.** Dongles generate their own birdies, and USB 3 ports radiate broadband
noise. Both look like real, stationary signals — and being *stationary in the
receiver* they appear at constant strength everywhere, which is a recognisable
signature.

**Mitigation.** None automatic in V0. A spatially flat "signal" across a whole survey
should be suspected. [calibration.md](calibration.md) describes terminated-input
characterisation.

### 🟡 8-bit dynamic range

**Effect.** ~48 dB of instantaneous range before dithering. A strong signal and a weak
one in the same span cannot both be measured well.

**Mitigation.** Welch averaging recovers some effective range below the quantisation
floor. Overload detection catches the top end.

---

## Position

### 🟠 GPS accuracy limits everything spatial

**Effect.** 3–10 m of error is normal, worse in urban canyons. A grid finer than the
fix error produces detail that is pure noise and looks convincing.

**Mitigation.** Accuracy is stored per observation; `GPS_LOW_ACCURACY` above a
configurable limit; the hotspot radius is **floored at the median position accuracy**,
so no estimate can claim to know where a signal was better than the survey knew where
it was.

**Residual.** Nothing stops a user setting `--resolution 1`. The docs say not to.

### 🟠 The RF/position time join

**Effect.** Attaching a spectrum to the wrong point on the route smears the map along
the direction of travel.

**Mitigation.** A single monotonic timeline; block timestamps corrected from arrival to
first-sample; observations buffered until a fix *later* than their midpoint arrives, so
positions are interpolated between two real fixes rather than extrapolated. Measured on a
recorded 11-minute survey, this took `POSITION_EXTRAPOLATED` from 74% of observations
to 0%.

**Residual.** USB and kernel latency — a few milliseconds — is uncorrected. Millimetres
at walking pace; more at vehicle speed.

### 🟡 GPS dropout

**Effect.** Under tree cover or between buildings, fixes stop.

**Mitigation.** Beyond `max_age_ms`, no position is attached at all: the observation is
stored and flagged `NO_LOCATION` rather than being placed at the last known point.

### 🟡 Accuracy is not measured

**Effect.** NMEA reports HDOP, a geometry factor, not accuracy. SpectraMap estimates
`HDOP × 4 m`.

**Mitigation.** Marked `ASSUMED` everywhere it surfaces, with the assumed UERE
recorded.

### 🟡 GPS-derived clock steps

**Effect.** A clock correction mid-survey can reorder a wall-clock-keyed track.

**Mitigation.** Everything internal is keyed on the monotonic clock, which cannot step.
UTC is carried alongside for humans.

---

## Propagation and the physical world

### 🔴 Multipath

**Effect.** Reflections interfere constructively and destructively, producing **20 dB
swings over a few centimetres** — comparable to the spatial gradient the whole project
is trying to measure. This is the largest single source of error in RSSI-based spatial
work, and no amplitude calibration touches it.

**Mitigation.** Spatial aggregation into cells averages several observations; cell
spread statistics (min/max/std) are preserved so variation is visible rather than
hidden; the hotspot estimator's caveats name it explicitly.

**Residual.** Fundamental. Larger cells and repeat passes help; nothing eliminates it.

### 🟠 Obstruction and body shadowing

**Effect.** The operator's own body attenuates 5–15 dB. Turning around changes the
reading, so a route walked in two directions gives two different maps.

**Mitigation.** None automatic. Carry the antenna consistently — same side, same
height, same orientation.

### 🟠 Antenna pattern

**Effect.** No antenna is truly omnidirectional. Measured power depends on heading,
which V0 does not record.

**Mitigation.** Antenna metadata is recorded and marked `ASSUMED`. Heading capture is
V4.

### 🟡 Moving versus stationary

**Effect.** A moving receiver averages over a stretch of ground and over fading; a
stationary one integrates one multipath realisation deeply. The two are not the same
measurement.

**Mitigation.** `speed_mps` is stored per observation, so analysis can filter. The
simulator supports dwells at waypoints.

---

## Analysis and presentation

### 🔴 A polished heatmap implies every pixel was measured

**Effect.** The most dangerous failure mode in the whole project. A smooth, confident
surface derived from a thin scatter of points reads as data.

**Mitigation.** Multiple, layered:
- cells beyond `max_distance_m` from any measurement are **masked** — `NaN`, absent
  from GeoJSON, transparent in the PNG. The map has holes where the survey had holes;
- a confidence field from proximity and support drives opacity;
- coverage fraction is reported by the CLI and the viewer;
- measurement points and the raw GPS track are toggleable overlays;
- every product is tagged `INTERPOLATED` with full provenance.

### 🔴 False precision in localisation

**Effect.** Reporting "transmitter at 22.123456, 88.123456" from RSSI is reporting
arithmetic, not evidence.

**Mitigation.** The estimator returns a **region**, never a point claim; the radius is
the maximum of the bootstrap spread, half the measurement spacing, and the median
position accuracy; a **geometry score** flags one-sided surveys that cannot constrain
the across-track direction; confidence is **forced to `low`** when the measurements
span less than 6 dB, with the caveat `NOT USABLE as a location`. A CI test asserts the
distant control carrier is refused.

**Residual.** The radius covers sampling scatter, not systematic error. The caveats say
so in every output.

### 🟠 Averaging in the wrong domain

**Effect.** Averaging decibels computes a geometric mean and under-reports bursty
signals — averaging `[1, 100]` gives 10 instead of 50.5, a 7 dB error on exactly the
signal a user is hunting.

**Mitigation.** Every aggregation in the project — spectra, cells, band power, noise
floors, interpolation — works in the linear domain, routed through `mean_power_db`. A
test pins it.

### 🟠 Interpolation artefacts

**Effect.** IDW produces bullseyes around isolated samples and flat plateaus between
distant ones.

**Mitigation.** IDW cannot overshoot the measured range (tested). Smoothing distance
prevents infinite weight at a sample. Confidence and the point overlay let a user see
what is driving a feature.

**Residual.** Kriging would do better and is a V1 candidate — with the caveat that it
also produces smoother, more confident-looking surfaces from the same thin data.

### 🟡 Band aggregation method changes the answer

**Effect.** Peak and mean power over the same band produce different maps. Peak is
biased high by noise where nothing is transmitting.

**Mitigation.** The method is part of the product's identity — in the directory name,
the provenance, the CLI output and the viewer.

### 🟡 Sweep maps imply simultaneity

**Effect.** A wideband picture assembled from sequential windows was measured at
different times and, on foot, different places.

**Mitigation.** `sweep_step` on every observation; a new storage chunk per retune;
revisit period computed; a manifest warning the viewer displays.

### 🟡 Projection error

**Effect.** Working in degrees stretches a map by `1/cos(latitude)`.

**Mitigation.** A local tangent plane using true WGS84 radii of curvature, verified
against published degree lengths. Everything spatial works in metres.

---

## Data and operations

### 🟠 Storage volume

**Effect.** ~120 MB/hour at full resolution. A long survey fills a card, and a
half-written survey is worse than none.

**Mitigation.** Projected rate printed before recording; `bin_decimation` to trade
resolution for size; the recorder stops cleanly below `min_free_disk_mb`; readers
derive row counts from file size so a truncated survey loses only its final partial
row.

### 🟠 Silent partial surveys

**Effect.** A survey that lost its receiver, its GPS, or its disk halfway, but looks
complete.

**Mitigation.** Device failures are caught and recorded; the manifest carries
`summary.complete`; every fault becomes an event; the CLI exits non-zero. A test kills
the device mid-survey and asserts the result is readable and marked incomplete.

### 🟡 Reproducibility of derived products

**Effect.** A map that changed for unknown reasons is not evidence.

**Mitigation.** Every derived product carries algorithm, parameters, source survey,
software version and timestamp. Tests assert two runs agree and that changing a
parameter changes the recorded provenance.

---

## Regulatory

### 🔴 User responsibility

**Effect.** Reception, recording, and the use of results are governed by radio,
privacy, aviation and property law, and they differ everywhere.

**Mitigation.** [safety-and-legal.md](safety-and-legal.md). SpectraMap is receive-only
by design: no transmission, no decoding, no payload storage.

**Residual.** Entirely the user's responsibility. The software cannot know your
jurisdiction.
