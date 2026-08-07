# The `.smap` survey format

**Format version 0.1.**

A survey is a self-contained directory. The design goal is that another developer can
read the measurements without running SpectraMap, without installing anything exotic,
and without reverse-engineering anything.

CI enforces that promise: a job reads a freshly recorded survey with nothing but
`json` and `numpy` and checks the result.

## Layout

```
<name>.smap/
├── manifest.json          format version, configuration, device, antenna, units, summary
├── survey.sqlite          observation index, raw position track, event log
├── spectra/
│   ├── chunk_000000.f32   flat little-endian float32, C order, (n_rows, n_bins)
│   └── chunk_000001.f32
├── route.geojson          GPS track + measurement points, for any GIS tool
├── calibration/           correction tables (empty in V0)
└── processing/            derived products, each with a provenance record
    └── 433_92000MHz-w20_0kHz-peak/
        ├── observations.geojson
        ├── observations.csv
        ├── heatmap.geojson
        ├── heatmap.png
        ├── heatmap.json          georeferencing sidecar
        ├── provenance.json
        └── reconstruction.json
```

## Reading the spectra

The chunk format is deliberately the dullest thing that could work: a raw C-order
`float32` array, little-endian, no header. Its shape lives in `manifest.json` and in
the `chunks` table.

```python
import json, numpy as np, pathlib

survey = pathlib.Path("backyard-test.smap")
manifest = json.loads((survey / "manifest.json").read_text())

for chunk in manifest["spectra"]["chunks"]:
    spectra = np.memmap(
        survey / chunk["path"], dtype="<f4", mode="r"
    ).reshape(-1, chunk["n_bins"])                       # (n_observations, n_bins)

    freqs = chunk["f_start_hz"] + np.arange(chunk["n_bins"]) * chunk["f_step_hz"]

    k = int(np.argmin(abs(freqs - 433.92e6)))
    print(f"433.92 MHz across the survey: {spectra[:, k]}  # {manifest['units']['power']}")
```

Row *i* of chunk *c* corresponds to the observation whose `(chunk_id, chunk_row)` is
`(c, i)` in the `observations` table.

### Why a flat array rather than Parquet or SQLite BLOBs

The most common question a survey is asked is "what was the power near one frequency,
everywhere along the route?" That is a **column** slice across every observation.

- **SQLite BLOBs** would require reading and decoding every 16 KiB array to answer it.
- **Parquet** would answer it well, but adds a substantial dependency to a project
  whose core is otherwise numpy and PyYAML, and is harder to read from a language
  without an Arrow binding.
- **A memory-mapped flat array** answers it with a strided read that touches only the
  pages that column lives on, needs no library beyond numpy, and is trivially
  readable from C, Rust, MATLAB or Julia.

Parquet export remains available as an optional extra for interchange with
dataframe-oriented tooling. See [ADR 0003](adr/0003-storage-model.md).

### Chunk homogeneity

A chunk is homogeneous: every row shares one `(n_bins, f_start_hz, f_step_hz)`. A
retune — a sweep step — starts a new chunk, so a chunk never mixes windows measured at
different frequencies. Chunks also roll at 4096 rows to keep individual files
manageable (4096 × 4096 bins × 4 bytes = 64 MiB).

### Truncated surveys

Readers take the row count from the **file size**, not from the recorded `n_rows`:

```
n_rows = filesize // (n_bins × 4)
```

A survey killed by a power cut may have written more rows than the last commit knew
about, and may have a partially written final row. Deriving the count from the file
means the complete rows are recovered and the partial one is simply absent, rather
than being read as garbage powers at a real location.
`test_truncated_final_row_is_ignored_rather_than_read_as_data` pins this.

## `manifest.json`

```json
{
  "format": "spectramap-survey",
  "format_version": "0.1",
  "software_version": "0.1.0.dev0",
  "survey_id": "backyard-test-1786128233",
  "name": "backyard-test",
  "mode": "fixed",
  "notes": "...",
  "started_utc_ns": 1786128233000000000,
  "ended_utc_ns": 1786128879000000000,
  "duration_s": 646.0,
  "n_observations": 1262,

  "receiver":     { "driver": "rtl-sdr", "index": 0, "serial": "...", "tuner": "R820T",
                    "receiver_id": "rtl-sdr:00000001" },
  "capabilities": { "tuning_range": {...}, "sample_rates_hz": [...], "gains_db": [...],
                    "tuning_evidence": "assumed", "notes": "..." },
  "antenna":      { "antenna_id": "whip-433", "type": "quarter-wave whip",
                    "polarization": "vertical", "gain_dbi": 2.1, "evidence": "assumed" },

  "configuration": { "survey": {...}, "receiver": {...}, "capture": {...},
                     "fft": {...}, "aggregation": {...}, "location": {...},
                     "quality": {...}, "simulation": {...} },

  "units": {
    "power": "dBFS/Hz",
    "calibrated": false,
    "reference": "dB relative to a full-scale complex sinusoid at the recorded gain setting. NOT absolute field strength and NOT dBm."
  },

  "spectra": {
    "encoding": "raw little-endian float32, C order, no header",
    "read_recipe": "np.memmap(path, dtype='<f4', mode='r').reshape(-1, n_bins); frequency of bin k is f_start_hz + k * f_step_hz",
    "chunks": [ { "chunk_id": 0, "path": "spectra/chunk_000000.f32",
                  "n_bins": 4096, "n_rows": 1262, "dtype": "<f4",
                  "layout": "C-order (n_rows, n_bins)",
                  "f_start_hz": 432470000.0, "f_step_hz": 585.9375 } ]
  },

  "warnings": ["..."],
  "summary":  { "observations": 1262, "complete": true, "dsp": {...}, "...": "..." }
}
```

`configuration` is the complete, validated `SurveyConfig`. A survey can be re-run from
it: `spectramap survey -c <extracted config>`.

`simulation_ground_truth` appears **only** for simulated surveys, under its own
top-level key, holding transmitter positions and route parameters. It exists so tests
can score a reconstruction against a truth the algorithm never saw. It is never an
input to any estimate, and a test asserts it does not leak into `configuration`.

## `survey.sqlite`

Plain SQLite, no extensions. Inspectable with the `sqlite3` CLI.

### `observations`

| Column | Type | Notes |
|---|---|---|
| `observation_id` | INTEGER PK | |
| `t_start_utc_ns`, `t_end_utc_ns` | INTEGER | integration window, wall clock |
| `t_start_mono_ns` | INTEGER | internal timeline |
| `latitude`, `longitude`, `altitude_m` | REAL | NULL when `NO_LOCATION` |
| `h_accuracy_m`, `v_accuracy_m` | REAL | 1-sigma, may be NULL |
| `speed_mps` | REAL | |
| `position_evidence` | TEXT | `measured` / `derived` / `estimated` |
| `position_age_ms` | REAL | time from the nearest real fix |
| `center_frequency_hz` | REAL | the **tuned LO**, not the target |
| `sample_rate_hz` | REAL | |
| `gain_db` | REAL | NULL under AGC |
| `agc` | INTEGER | |
| `n_ffts`, `n_bins` | INTEGER | |
| `sweep_step` | INTEGER | 0 for fixed frequency |
| `flags` | INTEGER | `QualityFlag` bitmask |
| `chunk_id`, `chunk_row` | INTEGER | where the spectrum lives |
| `noise_floor_db`, `peak_power_db`, `peak_frequency_hz` | REAL | precomputed summaries |

The summary columns exist so maps, status views and route rendering never touch a
spectra file.

Indexed on time, centre frequency, `(latitude, longitude)` and `(chunk_id, chunk_row)`.

### `positions`

The raw track, every fix exactly as received, kept **separately** from the resolved
per-observation positions. The originals survive independently of how they were
interpolated, so a later reader can redo the join differently.

### `events`

`(t_utc_ns, t_mono_ns, level, kind, message, detail)`. Device failures, dropped
samples, gain changes, retunes, disk pressure, configuration warnings. A survey with
gaps says so.

### `chunks` and `meta`

Chunk geometry, and small key/value metadata (`survey_id`, `format_version`,
`software_version`, `started_utc_ns`).

## `route.geojson`

Two layers in one FeatureCollection:

- `gps_track` — a LineString of the raw fixes.
- `observation` — a Point per positioned observation, with `observation_id`,
  timestamp, flags, `position_evidence` and `peak_power_db`.

Both are exported because seeing them disagree is often the first sign of a timing or
interpolation problem.

## `processing/`

One directory per reconstruction, named for its band and method
(`433_92000MHz-w20_0kHz-peak`), because **the band and the aggregation method are part
of the identity of the result** — peak power and mean power over the same band answer
different questions and produce different maps.

Every directory carries `provenance.json`:

```json
{
  "product": "interpolated_field",
  "algorithm": "inverse-distance-weighting",
  "parameters": { "power": 2.0, "search_radius_m": 60.0, "max_distance_m": 40.0,
                  "min_neighbours": 1, "smoothing_m": 1.0, "resolution_m": 5.0,
                  "interpolation_domain": "linear power" },
  "source_survey_id": "backyard-test-1786128233",
  "software_version": "0.1.0.dev0",
  "format_version": "0.1",
  "created_utc_ns": 1786129000000000000,
  "evidence": "interpolated",
  "inputs": { "n_observations": 1262 },
  "notes": "Values between measurement points are computed, not measured. ..."
}
```

If a heatmap changes because interpolation settings changed, this is what makes that
obvious rather than mysterious.

`heatmap.png` is accompanied by `heatmap.json` carrying bounds, value range, colour
map, what alpha encodes, coverage fraction and the same provenance — because a PNG
alone is not a dataset and is not treated as one.

## Compatibility

`format_version` changes only when the container layout changes in a way an older
reader could not handle. `software_version` is stamped on every survey and every
derived product, so any artefact traces back to the code that produced it.

Readers should:

- check `format` is `spectramap-survey`;
- check `units.calibrated` before displaying anything as absolute power;
- derive chunk row counts from file size;
- treat unknown `flags` bits as "some quality concern" rather than ignoring them.
