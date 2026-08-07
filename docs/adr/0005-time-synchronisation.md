# ADR 0005 — Monotonic internal timeline, with buffered position resolution

**Status:** Accepted · **Date:** 2026-08-07

## Context

Two independent streams must be joined:

- **RF** arrives in buffers, at whatever rate USB delivers them, timestamped by the
  host when the buffer completes.
- **Position** arrives at ~1 Hz from an unrelated device, with its own idea of time.

An observation integrates over 500 ms and needs a position for its **midpoint**. Getting
this wrong smears every measurement along the direction of travel — the map still looks
plausible, it is just displaced.

## Decision

### 1. Monotonic time is the internal timeline

Everything — IQ block starts, position fixes, integration windows — is keyed on
`time.monotonic_ns()`. UTC (`time.time_ns()`) is captured alongside, as close together
as possible, and carried for human consumption and archival.

Wall clock can step. NTP corrections and GPS-driven clock sets happen mid-survey, and a
wall-clock-keyed track can silently reorder itself when they do. A monotonic clock
cannot.

### 2. Block timestamps are corrected to the first sample

A driver hands over a buffer *after* it fills, so the arrival timestamp belongs to the
last sample:

```
t_first_sample ≈ t_arrival − n_samples / sample_rate
```

USB transfer and kernel scheduling latency — a few milliseconds — remain uncorrected.
That is millimetres at walking pace, and it is recorded as a known limitation rather
than pretended away.

### 3. Observations wait for their bracketing fix

The important one, and it came out of testing.

Resolving an observation's position the moment its window closes means that with a 1 Hz
GPS and 500 ms observations, **roughly half of all observations close before the fix
that follows their midpoint has arrived**. They would be extrapolated from the last
known fix. The first implementation did exactly this, and the recorded survey came back
with 74% of observations flagged `POSITION_EXTRAPOLATED`.

Instead, closed observations wait in a small buffer until a fix later than their
midpoint arrives, then interpolate between two real fixes.

Cost: about one second of write latency. Benefit: on that same survey,
`POSITION_EXTRAPOLATED` went from 74% to 0% — every position now sits between two real
fixes. Only a tail after the final fix can be extrapolated, and it is flagged honestly
when it is.

### 4. Resolution rules are explicit and flagged

| Situation | Result | Evidence | Flag |
|---|---|---|---|
| Exact hit | that fix | `MEASURED` | — |
| Between two fixes | linear interpolation | `DERIVED` | `POSITION_INTERPOLATED` |
| Outside the track, within `max_age_ms` | nearest fix held | `ESTIMATED` | `POSITION_EXTRAPOLATED` |
| Older than `max_age_ms` | **no position** | — | `NO_LOCATION`, `GPS_STALE` |

Interpolated accuracy takes the **worse** endpoint, never a blend. An observation
beyond `max_age_ms` gets no position at all rather than a stale guess — it is still
stored, because it is still real RF data, but it must not be placed on a map.

### 5. Elapsed time comes from the sample timeline

Duration limits and sweep dwells are measured from IQ block timestamps, not the host
wall clock. This keeps them correct when the simulator runs faster than real time: an
80-second simulated survey completes in ~1.4 s of wall clock and still records exactly
80 seconds of observations.

## Consequences

**Good.** Immune to clock steps. Nearly all positions are interpolated between real
fixes rather than extrapolated. The simulator exercises the same synchronisation code as
real hardware — no bypass — so the join is genuinely tested. Every position says how it
was obtained.

**Bad.** Writes lag acquisition by up to one fix interval, so a crash can lose the
buffered tail. Observation counts against `max_observations` had to be taken from
*emitted* rather than *written* observations, which is a subtlety in the recorder.

**Accepted.** The buffer is drained unconditionally on shutdown, including on device
failure. A test asserts the buffer is not silently dropped when the accumulator happens
to be empty — a real data-loss bug found and fixed during development.

## What would change our mind

- A position source at 10 Hz or better, which would make the buffering unnecessary.
- Vehicle-speed surveys, where the uncorrected USB latency starts to matter and
  hardware timestamps become worth pursuing.
- External telemetry sources (drone, IMU) with their own clocks, needing an explicit
  clock-offset model rather than a single monotonic timeline.
