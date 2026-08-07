# The RF measurement model

## What an observation is

The atom of SpectraMap is a single geotagged, time-stamped spectrum measurement: one
sample of `RF(x, y, z, f, t)`.

```
RFObservation
├── observation_id
├── t_start_utc_ns, t_end_utc_ns      when (wall clock)
├── t_start_mono_ns                   when (internal timeline)
├── position: ResolvedPosition
│     ├── position: Position | None   lat, lon, alt, accuracies, fix quality
│     ├── evidence                    MEASURED | DERIVED | ESTIMATED
│     ├── age_ms                      distance in time from the nearest real fix
│     └── flags
├── receiver_id, antenna_id           what heard it
├── receiver_config                   centre freq, sample rate, gain, AGC, ppm
├── fft_config                        size, window, overlap, normalisation, DC removal
├── spectrum: PowerSpectrum
│     ├── frequencies_hz              absolute, ascending
│     ├── power_db                    in `unit`
│     ├── unit                        dBFS | dBFS/Hz | dBm | dBm/Hz
│     ├── normalization               spectrum | density
│     ├── enbw_hz, bin_width_hz       true resolution BW, and bin spacing
│     └── n_averages                  FFTs folded in
├── n_ffts
├── flags: QualityFlag
└── sweep_step                        0 for fixed frequency
```

The observation carries its own acquisition context. That is the point: it remains
interpretable in isolation, years later, without the survey it came from. **Two
measurements are only scientifically comparable if the relevant receiver
configuration is known**, so the configuration travels with the measurement rather
than living only in a header.

## Epistemic status

Every number in SpectraMap carries an `Evidence` tag.

| | Meaning | Example |
|---|---|---|
| `MEASURED` | Came off the receiver | power at a bin, a GPS fix |
| `DERIVED` | Deterministic function of measurements | band peak power, cell mean, an interpolated position between two fixes |
| `INTERPOLATED` | Filled in between measurements by an algorithm | a heatmap cell |
| `ESTIMATED` | Inferred by a model with real uncertainty | the probable strongest-signal region |
| `ASSUMED` | Supplied by a user or a default; not verified | antenna gain, tuner frequency range, GPS accuracy from HDOP |

These distinctions are enforced, not merely documented:

- The interpolated field is **masked** beyond `max_distance_m`, so unmeasured ground
  is `NaN` and is not drawn.
- Exported GeoJSON tags every feature and every collection with its evidence.
- `PowerUnit.is_calibrated` gates whether `dBm` may be printed at all.
- The hotspot estimator will not grade itself above "low" when the measurements show
  no spatial gradient.

## Units

V0 produces **relative receiver units**:

| Unit | Meaning |
|---|---|
| `dBFS` | dB relative to a full-scale complex sinusoid, per bin, at the recorded gain |
| `dBFS/Hz` | the same, per hertz (default) |
| `dBm`, `dBm/Hz` | absolute. **V0 never produces these.** |

`PowerUnit.label` renders uncalibrated units as `dBFS/Hz (relative, uncalibrated)`,
and that string reaches the CLI, the JSON API and the viewer's legend.

What relative units are good for:

- comparing power **between places** within one survey, at fixed gain — which is
  exactly what a spatial map needs;
- comparing **before and after** at the same location with the same hardware;
- measuring **SNR**, which is a ratio and so cancels the unknown reference entirely.

What they cannot do: state field strength, be compared against a regulatory limit, or
be compared between two different receivers or antennas. See
[calibration.md](calibration.md).

## Position

```
Position
├── t_utc_ns, t_mono_ns
├── latitude, longitude, altitude_m
├── horizontal_accuracy_m, vertical_accuracy_m    1-sigma estimates, or None
├── speed_mps, heading_deg
├── fix_quality      none | estimated | gps | dgps | rtk_float | rtk_fixed | simulated
├── satellites
└── source
```

`None` accuracy means "the source did not tell us", which is different from zero.
NMEA does not report accuracy at all — it reports *dilution of precision*, a geometry
factor — so the accuracy attached to a real GPS fix is `HDOP × UERE` with an assumed
UERE of 4 m. That makes it `ASSUMED`, derived from a measured DOP, and it is labelled
so wherever it surfaces.

### Attaching position to RF

A fix almost never lands exactly at the midpoint of an integration window, so the
midpoint is interpolated between the bracketing fixes. The rules, implemented in
`PositionTrack.resolve`:

| Situation | Result | Evidence | Flag |
|---|---|---|---|
| Exact hit on a fix | that fix | `MEASURED` | — |
| Between two fixes | linear interpolation | `DERIVED` | `POSITION_INTERPOLATED` |
| Outside the track, within `max_age_ms` | nearest fix held | `ESTIMATED` | `POSITION_EXTRAPOLATED` |
| Older than `max_age_ms` | **no position** | — | `NO_LOCATION`, `GPS_STALE` |
| Accuracy worse than the limit | position kept | — | `GPS_LOW_ACCURACY` |

Interpolated accuracy takes the **worse** of the two endpoints, never a blend:
interpolation cannot be more certain than its inputs.

An observation with no trustworthy location is still stored. It is real RF data and
belongs to the spectrum record; it simply must not be placed on a map.

## Quality flags

Flags mark data. They never delete it.

| Flag | Raised when |
|---|---|
| `NO_LOCATION` | no usable fix covered the window |
| `GPS_LOW_ACCURACY` | reported accuracy worse than `accuracy_limit_m` |
| `GPS_STALE` | nearest fix older than `max_age_ms` |
| `POSITION_INTERPOLATED` | position interpolated between two fixes |
| `POSITION_EXTRAPOLATED` | held from a single bracketing fix |
| `SDR_OVERLOAD` | IQ clipping; power **under-reads** the true field |
| `DROPPED_SAMPLES` | driver reported, or the timeline implied, a gap |
| `GAIN_CHANGED` | gain differs from the previous observation |
| `AGC_ACTIVE` | automatic gain: powers are not spatially comparable |
| `DEVICE_RECONNECTED` | receiver reopened mid-survey |
| `PARTIAL_INTEGRATION` | fewer FFTs averaged than configured |
| `CLOCK_UNSYNCED` | host UTC not corroborated by GPS time |
| `RETUNED` | window straddled a sweep step |

Stored as one integer column, so filtering is a bitmask test in SQL. Spatial products
exclude `NO_LOCATION | SDR_OVERLOAD | GPS_LOW_ACCURACY | DROPPED_SAMPLES` by default,
report how many they set aside, and `--include-flagged` turns the filter off. The raw
record is untouched either way.

## The survey

```
Survey
├── survey_id, name, mode, notes
├── started_at, ended_at, duration
├── receiver (DeviceInfo) + capabilities
├── antenna (AntennaInfo)
├── configuration            the complete SurveyConfig, verbatim
├── route                    the raw position track, every fix as received
├── observations
├── events                   device faults, retunes, GPS loss, disk pressure
├── warnings                 configuration caveats, recorded at record time
└── summary                  counts, flags, and whether the survey completed
```

Warnings are captured **into the survey**, not just printed. A reader five years later
sees the same caveats the surveyor did.

## Survey modes

The distinction between staring and sweeping is in the data model, because a wideband
sweep drawn as an instantaneous wideband capture is a lie about what the hardware did.

| Mode | What it does |
|---|---|
| `fixed` | one centre frequency, continuously |
| `narrowband` | as above, over a defined range within one span |
| `sweep` | steps the tuner through several windows sequentially |
| `stationary` | one location, observing change over time |

An RTL-SDR does **not** measure its whole tuning range at once. It sees `sample_rate`
of instantaneous bandwidth — 2.4 MHz at best. Wider coverage means retuning, and then
different parts of the displayed span were measured at different times and, on foot,
different places.

For sweep surveys SpectraMap records `sweep_step` on every observation, starts a new
storage chunk at each retune so a chunk never mixes frequency axes, logs a `retune`
event, computes the revisit period, and writes a manifest warning that the viewer
displays. V0 prioritises fixed and narrowband; sweep is implemented and tested but is
a V1 focus.

## Antenna metadata

RF measurements are close to meaningless without knowing the antenna, and two surveys
are only comparable if it was the same. SpectraMap records what the user knows — type,
polarization, orientation, claimed gain, frequency range, notes — and marks the whole
structure `ASSUMED`. Professional characterisation is not required; discarding what
the user does know is not acceptable.

## Extensibility

The observation model reserves room for later milestones without requiring them now:
`altitude_m` is carried end to end (V6 volumetric mapping), `Position` already has
`heading_deg` (V4 directional antennas), and `RFObservation.extras` is a free-form
dictionary for attaching IMU or telemetry data (V4/V5). None of these are populated in
V0, and none of them gate it.
