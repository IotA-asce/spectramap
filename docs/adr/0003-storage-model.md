# ADR 0003 — SQLite index plus flat float32 chunks

**Status:** Accepted · **Date:** 2026-08-07

## Context

A survey produces two very different kinds of data:

- **small and queryable** — one index row per observation: time, position, receiver
  settings, quality flags. A few hundred bytes each.
- **large and regular** — the spectra: 4096 float32 values per observation, 16 KiB
  each, ~120 MB/hour.

The question the storage layer is asked most often is: *"what was the power near
433.92 MHz, everywhere along the route?"* That is a **column** slice across every
observation.

Requirements: the survey must be portable, readable by another developer without
SpectraMap, survive a crash mid-write, and answer the column query without loading
every FFT ever recorded.

## Options considered

**Everything in SQLite, spectra as BLOBs.** One file, transactional, familiar. But the
column query requires reading and decoding every 16 KiB blob — hundreds of megabytes to
extract a few kilobytes of answer.

**Parquet for observations + spectra.** Columnar, compressed, well supported by
dataframe tooling, and genuinely good at this query. But pyarrow is a large dependency
for a project whose core is otherwise numpy and PyYAML, appending row-groups
incrementally during a live survey is awkward, and reading Parquet from C, Rust or
MATLAB needs an Arrow binding.

**HDF5.** Designed for exactly this. Same dependency-weight objection, plus a long
history of partial-write fragility.

**SQLite index + flat binary chunks.**

## Decision

**SQLite for the index, the raw position track and the event log. Spectra in flat,
little-endian, C-order `float32` chunk files, memory-mapped.**

```python
spectra = np.memmap(path, dtype="<f4", mode="r").reshape(-1, n_bins)
power_at_433_92 = spectra[:, k]     # a strided read, not a full scan
```

Supporting details:

- A chunk is **homogeneous**: one `(n_bins, f_start_hz, f_step_hz)` per file. A retune
  starts a new chunk, so a chunk never mixes frequency axes.
- Chunks roll at 4096 rows (64 MiB at 4096 bins).
- Readers derive the row count from **file size**, not from the recorded `n_rows`, so a
  survey killed by a power cut recovers every complete row and simply lacks the partial
  final one.
- SQLite runs in WAL mode and commits every 64 observations; spectra are flushed on the
  same cadence.
- float32 gives ~7 significant digits — far finer than the ~0.01 dB repeatability of any
  RTL-SDR — at half the size of float64.

Parquet export remains available as an optional extra for interchange.

## Consequences

**Good.** The column query touches only the pages it needs. No dependency beyond numpy.
Readable in one line from any language with mmap and IEEE floats — CI enforces this by
reading a fresh survey with only `json` and `numpy`. Crash behaviour is a truncated
final row rather than a corrupt file. A survey is a directory: copy it, zip it, move it.

**Bad.** No compression — spectra are largely noise, so gains would be modest, but
they would not be zero. Two storage mechanisms to keep consistent. A `chunk_id`/
`chunk_row` pointer that a reader must follow.

**Accepted.** `bin_decimation` addresses size where it matters. The consistency risk is
contained in one module and covered by tests.

## What would change our mind

- Surveys routinely reaching tens of gigabytes, making compression worth a dependency.
- Users wanting to load surveys directly into pandas or Polars often enough that
  Parquet should become the primary rather than an export.
- A need for concurrent writers, which this design does not support and does not need.
