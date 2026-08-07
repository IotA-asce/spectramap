# Architecture

## The shape of the problem

SpectraMap samples a five-dimensional field:

```
RF(latitude, longitude, altitude, frequency, time)
```

Everything in the system is either acquiring samples of that field, storing them
without loss, or reconstructing parts of it for a human to look at. The architecture
follows from three properties of the problem:

1. **The data rate collapses early.** An RTL-SDR at 2.4 MS/s produces ~19 MB/s of IQ.
   After the FFT and integration, a survey produces ~32 KB/s of spectra. The design
   must throw away the high-rate stream deliberately and early, and be explicit that
   it has done so.
2. **Two clocks must be joined.** RF arrives in buffers at whatever rate USB delivers
   them; position arrives at 1 Hz from an unrelated device. Joining these correctly
   is the difference between a map and a smear.
3. **Reconstruction is not measurement.** Most of what a user sees on a heatmap was
   never measured. The system's job is to keep that distinction visible at every
   layer, not just in the UI.

## Layers

```
                        Frontend
              map · spectrum explorer · surveys
                            |
                     Analysis API
         ┌──────────────────┼──────────────────┐
         │                  │                  │
    Geospatial             DSP            Hotspot est.
   project · grid    window · FFT · PSD    weighted centroid
         │                  │                  │
         └──────────────────┼──────────────────┘
                            │
                      Survey Store
                        (.smap)
                            ▲
                            │
                   Acquisition Engine
                     ┌──────┴──────┐
                     │             │
                 SDRDevice   PositionProvider
```

No microservices, no server database, no cloud. A survey is a directory; the
application is a Python package; the viewer is a static page served by
`http.server`. A user can copy a `.smap` directory to a USB stick and it still means
something on another machine, with or without SpectraMap installed.

## Module map

| Module | Responsibility | Depends on |
|---|---|---|
| `spectramap.types` | The shared vocabulary: `RFObservation`, `Position`, `PowerUnit`, `Evidence`, `QualityFlag`, `Provenance` | numpy only |
| `spectramap.dsp` | IQ → power spectra. Windows, FFT scaling, ENBW, noise floor, peaks | types |
| `spectramap.sdr` | `SDRDevice` abstraction + `mock` and `rtl-sdr` drivers | types |
| `spectramap.positioning` | `PositionProvider` abstraction, NMEA parsing, and the RF/position time join | types |
| `spectramap.simulation` | Virtual world, routes, deterministic clock | geospatial |
| `spectramap.survey` | Configuration, session wiring, the acquisition loop | all of the above |
| `spectramap.storage` | The `.smap` container: write, read, band query | types |
| `spectramap.geospatial` | Local tangent plane, distances, spatial aggregation | types |
| `spectramap.interpolation` | IDW with masking and a confidence field | geospatial |
| `spectramap.analysis` | Summaries, band selection, hotspot estimation, the reconstruction pipeline | everything |
| `spectramap.viz` | Colour maps, PNG encoding, GeoJSON/CSV export | interpolation |
| `spectramap.web` | Local JSON API + the single-page viewer | analysis |
| `spectramap.cli` | The command line | everything |

The dependency graph is acyclic and points downward. `dsp` knows nothing about
position, storage or maps. `storage` knows nothing about hardware. Nothing outside
`spectramap.sdr` imports a vendor SDR library, which is what makes the receiver
replaceable.

One deliberate exception is documented in the code: `spectramap.simulation` couples
the mock receiver to the mock position provider through a shared `VirtualWorld`,
because a simulated receiver's signal strength genuinely depends on where the
simulated receiver is. That coupling lives entirely inside the simulation package
and no production path touches it.

## The acquisition loop

Single threaded and pull-based:

```
read IQ block ──► overload check ──► Welch PSD ──► accumulate (linear power)
       │                                                    │
       ├──► drain position fixes into PositionTrack         │
       │                                                    ▼
       │                                     integration window full?
       │                                                    │
       └──────────────────────────────────────► emit pending observation
                                                            │
                              position fix later than its midpoint arrived?
                                                            │
                                                    resolve + write
```

The only thread in the system belongs to the serial GPS reader, which hands fixes
over through a lock so that a stalled or unplugged receiver cannot wedge acquisition.

### Why observations are buffered before being written

An observation's position is resolved at the **midpoint** of its integration window.
With a 1 Hz GPS and 500 ms observations, roughly half of all observations close
*before* the fix that follows their midpoint has arrived. Resolving immediately would
mean extrapolating from the last known fix for half the survey.

Instead, closed observations wait in a small buffer until a fix later than their
midpoint arrives, then interpolate between two real fixes. This costs about one
second of write latency and nothing in accuracy. Measured on a recorded 11-minute survey (1 Hz fixes, 500 ms observations), this took
`POSITION_EXTRAPOLATED` from 74% of observations to 0% — every position now sits
between two real fixes. Only a tail after the final fix can be extrapolated, and it is
flagged when it is.

## Time synchronisation

Two clocks, kept explicitly:

- **Monotonic** (`time.monotonic_ns`) — the internal timeline. Cannot step, cannot go
  backwards. Everything is aligned on it: IQ block starts, position fixes,
  integration windows.
- **UTC** (`time.time_ns`) — carried alongside for human consumption and archival.

Both are captured as close together as possible (`now_mono_utc_ns`) so their offset
is known. Position fixes are keyed on monotonic time, never on wall clock: an NTP
correction or a GPS-driven clock set mid-survey would otherwise silently reorder the
track.

### Block timestamps

A driver hands over a buffer *after* it has filled, so the arrival timestamp belongs
to the **last** sample, not the first. `estimate_block_start_mono_ns` subtracts the
block duration:

```
t_first_sample ≈ t_arrival − n_samples / sample_rate
```

This does not correct for USB transfer and kernel scheduling latency, which remain
an unmeasured bias of order a few milliseconds. At walking pace that is millimetres,
far below GPS error. It matters more for vehicle-speed surveys, and it is recorded as
a known limitation in [technical-risks.md](technical-risks.md) rather than pretended
away.

### Elapsed time comes from the sample timeline

Duration limits and sweep dwells are measured from IQ block timestamps, not from the
host wall clock. That keeps them correct when the simulator runs the world faster
than real time — an 80-second simulated survey completes in about 1.4 seconds of wall
clock and still records exactly 80 seconds of observations.

## Data flow and rates

```
IQ stream            19.2 MB/s   transient, never stored
  ↓ window + FFT + |·|²
periodogram          per segment, linear power
  ↓ Welch average across segments
block spectrum       ~9/s at 109 ms blocks
  ↓ accumulate over the integration interval (linear domain)
RFObservation        2/s, 4096 float32 bins = 16 KiB
  ↓
.smap                ~120 MB/hour at full resolution
```

A 28-minute survey at 4096 bins and 2 obs/s is ~56 MB. `spectramap survey` prints the
projected rate before recording, and the recorder stops cleanly rather than filling
the disk.

Raw IQ is never persisted in V0. Short-interval IQ capture is a plausible later
addition; it would be a separate, explicitly requested artefact, not a default.

## Storage

See [survey-format.md](survey-format.md) for the full schema. The split:

- **SQLite** — small, queryable: the observation index, the raw position track, the
  event log. Answers "which observations, where, when, with what settings" without
  touching a byte of spectrum.
- **Flat `float32` chunks** — large, regular: the spectra themselves. Memory-mapped,
  so extracting one frequency across a whole survey is a strided column read.

Putting 4096-bin arrays in SQLite BLOBs would make the most common question — "what
was the power near 433.92 MHz along the route?" — cost a full read of every FFT ever
recorded.

## Evidence, everywhere

Every value carries how it came to exist:

| Evidence | Example | Where it appears |
|---|---|---|
| `MEASURED` | receiver power at a point | observations, GeoJSON points |
| `DERIVED` | band peak power, interpolated position, grid cell mean | band queries, aggregation |
| `INTERPOLATED` | heatmap cells between samples | the field, its GeoJSON and PNG |
| `ESTIMATED` | probable strongest-signal region | hotspot output |
| `ASSUMED` | antenna gain, tuner frequency range, GPS accuracy from HDOP | manifest, capabilities |

This is not decoration. `PowerUnit.is_calibrated` gates whether `dBm` may ever be
printed; the interpolated field is *masked* rather than extrapolated; the hotspot
estimator refuses to grade itself above "low" when the measurements show no gradient.
An end-to-end test asserts that no output of an uncalibrated survey contains the
string `dBm`.

## Extension points

| Want to add | Do this | Nothing else changes |
|---|---|---|
| A HackRF / Airspy / SoapySDR | Implement `SDRDevice`, add one entry to `sdr/registry.py` | ✓ |
| A GPSD or phone position source | Implement `PositionProvider` | ✓ |
| Kriging instead of IDW | New module in `interpolation/`, returning an `InterpolatedField` | ✓ |
| Calibration | A correction stage between the DSP and the observation; `PowerUnit` becomes `DBM` | ✓ |
| A new export format | A function in `viz/export.py` | ✓ |

Related decisions: [ADR 0001 (language)](adr/0001-implementation-language.md),
[ADR 0002 (SDR abstraction)](adr/0002-sdr-abstraction.md),
[ADR 0003 (storage)](adr/0003-storage-model.md),
[ADR 0004 (units)](adr/0004-power-units-and-normalisation.md),
[ADR 0005 (time)](adr/0005-time-synchronisation.md),
[ADR 0006 (offset tuning)](adr/0006-offset-tuning.md).
