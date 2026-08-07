# ADR 0006 — Offset tuning is on by default

**Status:** Accepted · **Date:** 2026-08-07

## Context

Found by testing, on the first end-to-end run of the pipeline.

The simulated beacon was placed at 433.92 MHz and the receiver was tuned to
433.92 MHz — the obvious thing to do. The reconstruction produced a dynamic range of
**0.45 dB** across the whole survey and a localisation error of 80 m. The beacon was
40 dB above the noise floor and completely invisible.

The cause is not a bug in the simulator. It is real receiver physics:

- a zero-IF receiver has a large artefact at exactly its tuned frequency — ADC offset
  plus local-oscillator self-mixing;
- the standard fix is DC removal: subtract the mean of the block;
- **that also removes any real signal sitting at DC**, which is to say, at the tuned
  frequency.

So tuning exactly onto a target reliably reports that target as empty. The failure mode
is indistinguishable from a true negative, and it strikes precisely the one frequency
the user cares most about.

## Options considered

**Document it and move on.** The user is expected to know to offset-tune. This is what
most SDR tooling does, and it is why the mistake is so common.

**Disable DC removal by default.** Trades a deleted signal for a large fake one at the
same frequency. Worse.

**Notch only the single DC bin.** Still destroys a target sitting in that bin, and adds
a hole in the spectrum at an arbitrary place.

**Make offset tuning a first-class setting with a non-zero default.**

## Decision

**Separate "the frequency you care about" from "where the local oscillator sits", and
offset them by default.**

```yaml
capture:
  center_frequency_hz: 433.92MHz    # what you want to measure
  offset_tuning_hz: 250kHz          # how far below it the LO actually sits
```

- The recorder tunes the hardware to `center_frequency_hz − offset_tuning_hz`.
- The observation records the **actual tuned LO** in `receiver_config`.
- Frequency axes are absolute throughout, so 433.92 MHz still appears at 433.92 MHz and
  nothing downstream knows or cares.
- The default is 250 kHz — about 10% of a 2.4 MS/s span: clear of DC, well inside the
  analogue filter's flat region.

Validation, because a default alone is not enough:

- **warns** when the offset is within a few FFT bins of DC while `remove_dc` is on,
  explaining that the target will read as empty;
- **errors** when the offset would push the target outside the captured span — the
  250 kHz default is wrong for a 250 kS/s capture, and that must fail loudly rather
  than measure nothing;
- **warns** when the target lands near the span edge, where the analogue filter rolls
  off and power under-reads.

The DSP behaviour is pinned by
`test_dc_removal_also_destroys_a_signal_tuned_exactly_to_centre`, so nobody can "fix"
the DC removal and silently reintroduce a fake carrier at the centre of every survey.

## Consequences

**Good.** The default configuration measures the thing the user asked for. The failure
mode is impossible to hit accidentally. `spectramap survey` displays both the target
and the tuned LO, so the mechanism is visible rather than magic.

**Bad.** One more concept in the configuration, and two frequencies that a reader must
not confuse. The observation's `center_frequency_hz` is the LO, not the target, which
is correct but needs saying — it is said, in
[survey-format.md](../survey-format.md).

Offset tuning also costs a little usable bandwidth on one side of the span, and pushes
the target toward the filter skirts if set too large. The validator warns about the
second.

## What would change our mind

Nothing, for zero-IF receivers. A receiver with a real IF, or with hardware DC
correction good enough to leave the centre bin usable, would not need this — at which
point the default becomes per-driver rather than global.
