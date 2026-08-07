# ADR 0001 — Python for everything in V0

**Status:** Accepted · **Date:** 2026-08-07

## Context

The charter explicitly asks for the host stack to be evaluated rather than assumed.
The candidates were Python (scientific and geospatial ecosystem), Rust
(high-performance acquisition), and TypeScript (UI and application layers), or some
combination.

The real constraint is what V0 actually has to do:

- transform ~19 MB/s of IQ into ~32 KB/s of spectra, in real time, on hardware as
  modest as a Raspberry Pi;
- do correct DSP, correct geodesy and correct interpolation;
- persist and query a moderate volume of structured data;
- render a map in a browser;
- run its entire test suite in CI with no hardware.

## Options considered

**Rust for acquisition + Python for analysis.** Genuinely faster and a better fit for
a real-time sample pump. But it makes the project two-language from day one: two build
systems, two test frameworks, an FFI boundary, and a much steeper contribution curve —
in exchange for performance headroom V0 does not need.

**TypeScript throughout, including DSP.** One language across backend and UI, and good
packaging. But the scientific ecosystem is thin: no numpy, no mature FFT ecosystem, no
geospatial libraries of the same depth. It would mean reimplementing well-understood
numerics, which is exactly where subtle errors hide.

**Python core + a web frontend only where needed.** numpy for DSP, SQLite for the
index, plain HTML/JS for the viewer.

## Decision

**Python 3.10+ for acquisition, DSP, storage, analysis and the CLI. A dependency-free
browser front end, served by the standard library, only where a map is genuinely
better than text.**

Reasons, in order of weight:

1. **Correctness leverage.** numpy's FFT is well-tested and fast. The subtle parts of
   this project are the scaling conventions, the frequency axis, the time join and the
   uncertainty accounting — not the inner loop. Python keeps that code short enough to
   read and check.
2. **Performance is sufficient.** At 2.4 MS/s with 4096-point FFTs, the DSP is a
   handful of numpy calls per 109 ms block; the heavy lifting is already in C. Measured:
   the compact CI scenario simulates 80 s of surveying — synthesis, DSP, storage,
   reconstruction — in about 1.4 s of wall clock.
3. **One language, one test suite, one install.** `pip install -e '.[dev]'` and
   `pytest`. A contributor who can write numpy can contribute to any layer.
4. **The ecosystem is there when needed.** pyrtlsdr, pyserial, pyarrow, and the whole
   geospatial stack are one optional extra away.

### Dependency discipline

The core is **numpy and PyYAML**. Everything else is an extra: `rtlsdr`, `gps`,
`parquet`, `dev`. Consequences:

- CI installs two dependencies and runs the entire simulated pipeline;
- a Raspberry Pi in a field does not need a plotting stack — PNG encoding is done with
  `zlib` and `struct` from the standard library;
- the browser viewer has no build step, no bundler, and no CDN.

## Consequences

**Good.** Fast to develop and read. Trivial CI. Genuinely portable — laptop, Pi, macOS.
Deep numerics available when needed.

**Bad.** A single-core Python loop will not sustain very high sample rates; multi-SDR
synchronisation or 20 MS/s capture would need a different acquisition tier. The GIL
means the acquisition loop and the viewer cannot share a process efficiently at scale.
Python packaging remains Python packaging.

**Accepted.** Neither limitation is in V0 scope, and the module boundaries are drawn so
a faster acquisition tier could be swapped in behind `SDRDevice` without touching
anything above it.

## What would change our mind

- Sustained capture above ~5 MS/s becoming a requirement.
- Multi-receiver synchronisation (V-later) needing tight timing across devices.
- Profiling showing the DSP, rather than I/O, as the bottleneck on target hardware.

In any of those cases the natural move is a Rust or C acquisition process behind the
existing `SDRDevice` interface, feeding the same `.smap` container — not a rewrite.
