# SpectraMap

**Map the radio-frequency environment across physical space.**

A traditional spectrum analyser answers *what frequencies exist here*. SpectraMap
answers a bigger question:

> What frequencies exist **where**, **when**, and with what **spatial structure**?

Carry an SDR receiver and a GPS source through an area. SpectraMap records power
spectra tied to position and time, stores them in an open, portable format, and
reconstructs the invisible radio landscape as a map you can explore by frequency.

The central object is not `Spectrum(frequency)` but:

```
RF(latitude, longitude, altitude, frequency, time)
```

SpectraMap is a **passive measurement instrument**. It receives, measures and maps.
It does not transmit, decode payloads, defeat access controls, or interfere with
communications. See [docs/safety-and-legal.md](docs/safety-and-legal.md).

---

## Status

**V0, simulated vertical slice — working end to end, no radio hardware required.**

The complete pipeline runs today against a deterministic simulator:

```
virtual transmitter -> MockSDR -> synthetic IQ -> DSP -> spectrum observation
     -> MockGPS route -> geotagged observations -> persistent survey
     -> spatial aggregation -> interpolation -> RF heatmap
```

CI reconstructs a synthetic transmitter's region from a simulated walk **without
telling the mapping code where the transmitter is**, and asserts that the
reconstruction lands near the truth. Physical RTL-SDR integration is the next
milestone; the driver is written and the abstraction is in place, but it has not yet
been validated against real hardware.

See [docs/v0-plan.md](docs/v0-plan.md) for the milestone breakdown and what is and
is not yet proven.

---

## Try it in sixty seconds

No SDR, no GPS, no network.

```bash
pip install -e '.[dev]'

spectramap devices                       # the simulator shows up as a device
spectramap survey --driver mock \
    --frequency 433.92MHz --gain 24 \
    --duration 80 --name backyard-test \
    --output surveys/backyard-test.smap
spectramap analyze surveys/backyard-test.smap --frequency 433.92MHz
spectramap serve surveys/backyard-test.smap
```

80 seconds covers the simulated route once. It records ~147 observations spanning
28 dB, and the analysis recovers the beacon's region to within a few metres — without
the mapping code ever being given its position.

`analyze` prints a summary, writes a heatmap, and reports the probable
strongest-signal region. `serve` opens a local map with the route, the individual
measurement points, an RF intensity layer with a frequency selector, and a spectrum
viewer for any point you click.

Or run the whole slice as one script:

```bash
python examples/simulated_survey.py
```

---

## What it records

Every observation keeps the context needed to interpret it years later:

| | |
|---|---|
| **Where** | latitude, longitude, altitude, reported accuracy, and how the position was obtained (measured / interpolated / extrapolated) |
| **When** | UTC and host-monotonic timestamps for both the RF and the position timelines |
| **What** | the full power spectrum, its frequency axis, units, FFT size, window, and equivalent noise bandwidth |
| **How** | receiver, antenna, centre frequency, sample rate, gain, AGC state |
| **How well** | quality flags: `SDR_OVERLOAD`, `GPS_LOW_ACCURACY`, `DROPPED_SAMPLES`, `GAIN_CHANGED`, `AGC_ACTIVE`, … |

Poor measurements are **flagged, never deleted**. Filtering is an analysis decision,
and the analyst gets to make it.

---

## Design commitments

These are the rules the code is written to keep, not aspirations.

**Measurement and interpolation are different things.** Every value carries an
`Evidence` tag — `MEASURED`, `DERIVED`, `INTERPOLATED`, `ESTIMATED`, `ASSUMED`. The
heatmap is masked wherever the nearest real measurement is too far away, so the map
has holes exactly where the survey had holes. Interpolation confidence drives opacity.
You can always reveal the measurement points underneath.

**Uncalibrated numbers are labelled uncalibrated.** V0 produces relative
`dBFS`/`dBFS/Hz` referenced to receiver full scale, never bare `dBm`. Calibration is
a first-class concept with a place in the architecture — and V0 has not done it.
See [docs/calibration.md](docs/calibration.md).

**No false precision.** The strongest-signal estimator returns a region and a radius
with a confidence grade and explicit caveats, plus a geometry score that tells you
when your survey shape could not constrain the answer. It never returns a
transmitter's coordinates.

**Fixed gain by default.** Under AGC the receiver quietly rescales itself as you
walk, and spatial power comparison stops meaning anything. SpectraMap defaults to
fixed gain, warns loudly if you don't, and flags every affected observation.

**Hardware is replaceable and CI needs none of it.** Everything above
`spectramap.sdr` talks to one `SDRDevice` interface. The full test suite runs with
no radio attached.

---

## Architecture

A modular local application. No microservices, no cloud, no server database.

```
                Frontend  (map, spectrum, surveys)
                    |
               Analysis API
       ------------------------------
       |            |               |
   Geospatial      DSP        Localisation
       ------------------------------
                    |
               Survey Store   (.smap)
                    ^
             Acquisition Engine
                 /       \
               SDR       GPS
```

Python throughout: numpy for DSP, SQLite plus flat `float32` arrays for storage, and
a dependency-free browser front end. Core install is two dependencies (`numpy`,
`PyYAML`); drivers and exporters are extras.
See [docs/adr/0001-implementation-language.md](docs/adr/0001-implementation-language.md).

---

## The survey format

A survey is a self-contained, portable directory. Another developer can read the
measurements without running SpectraMap:

```
backyard-test.smap/
├── manifest.json        format version, full configuration, device, antenna, units
├── survey.sqlite        observation index, raw GPS track, event log
├── spectra/
│   └── chunk_000000.f32 flat little-endian float32, C order, (n_rows, n_bins)
├── route.geojson        survey path and measurement points
├── calibration/
└── processing/          derived products, each with a provenance record
```

```python
import numpy as np
spectra = np.memmap("spectra/chunk_000000.f32", dtype="<f4", mode="r").reshape(-1, 4096)
# frequency of bin k = f_start_hz + k * f_step_hz, both in manifest.json
```

Full schema: [docs/survey-format.md](docs/survey-format.md).

---

## Documentation

| Document | What it covers |
|---|---|
| [architecture.md](docs/architecture.md) | System structure, data flow, time synchronisation |
| [v0-plan.md](docs/v0-plan.md) | Milestones, acceptance criteria, current status |
| [rf-measurement-model.md](docs/rf-measurement-model.md) | The observation model and what a measurement means |
| [dsp-pipeline.md](docs/dsp-pipeline.md) | FFT scaling, windows, ENBW, units — with the maths |
| [survey-format.md](docs/survey-format.md) | The open `.smap` format, field by field |
| [hardware-v0.md](docs/hardware-v0.md) | Receivers, GPS sources, antennas, what to buy |
| [calibration.md](docs/calibration.md) | Why V0 is uncalibrated and how to change that |
| [technical-risks.md](docs/technical-risks.md) | Everything that can make a measurement wrong |
| [safety-and-legal.md](docs/safety-and-legal.md) | Scope, ethics, and your responsibilities |
| [adr/](docs/adr/) | Architecture decision records |

---

## Roadmap

| | |
|---|---|
| **V0** | Spatial spectrum mapper — SDR + GPS → measurements → heatmap |
| V1 | Wideband sequential sweeps |
| V2 | Temporal analysis and survey differencing ("RF time machine") |
| V3 | Probabilistic source-region localisation |
| V4 | Directional antennas and IMU orientation |
| V5 | Drone telemetry consumption |
| V6 | Volumetric `RF(x, y, z)` reconstruction |
| V7 | Link and interference analysis |
| V8 | Optional signal-pattern classification |

Explicitly **not** in V0: drone integration, 3D mapping, advanced localisation,
multi-receiver synchronisation, direction finding, any form of decoding or
interception, jamming, transmitter impersonation, AI classification, cloud services.

---

## Contributing

Contributions are welcome. The engineering priorities, in order:

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

Never prioritise a beautiful heatmap over correct measurements.

```bash
pytest              # full suite, no hardware needed
ruff check .
```

## License

MIT. See [LICENSE](LICENSE).
