# ADR 0002 — A minimal own abstraction, not SoapySDR

**Status:** Accepted · **Date:** 2026-08-07

## Context

SpectraMap must not depend on a particular RTL-SDR dongle model, and should be able to
grow to HackRF, Airspy, LimeSDR and others. The charter asks specifically whether
SoapySDR is the right long-term abstraction, while warning against complexity added
purely for theoretical future support.

## Options considered

**SoapySDR as the only backend.** One API, many radios, already solves the problem.
But it is a native library plus per-device modules, installed outside pip. It would
make "clone and run the tests" require a native stack on every developer machine and
CI runner, for hardware nobody is currently testing against. It also imposes its own
model of gains, streams and channels, which SpectraMap would then have to translate
back into something honest about, for example, whether a tuning range was *read* or
*assumed*.

**No abstraction; call pyrtlsdr directly.** Simplest today, and wrong tomorrow: driver
calls spread through acquisition, and the simulator has nowhere to live.

**A small own interface, with drivers behind a registry.**

## Decision

**Define `SDRDevice` — a minimal ABC — and register concrete drivers in one place.**

```
open / close / is_open
get_capabilities / get_device_info
set_center_frequency / set_sample_rate / set_gain
config
read_iq / stream
```

Two conventions every driver must honour, and they are the real content of the
abstraction:

1. **Full-scale normalisation.** Samples are complex with full scale at magnitude 1.0.
   A driver that ignores this silently shifts an entire survey's power scale.
2. **Honest capabilities.** `DeviceCapabilities` carries `tuning_evidence`, which says
   whether the frequency range was read from the device or assumed from a model
   number. For RTL-SDR it is always the latter, because `librtlsdr` cannot report it —
   and `spectramap devices` says so on screen.

SoapySDR is explicitly **not** a dependency, but the interface is shaped so a
`SoapySDRDevice` is just another registry entry when someone has hardware to test it
with.

## Consequences

**Good.** Core install is numpy + PyYAML; hardware libraries are optional extras. CI
runs the full pipeline with no native stack. The mock receiver is a first-class driver,
not a test double bolted on — the simulator goes through exactly the same interface as
real hardware, so the acquisition code is genuinely exercised. Adding a receiver is one
module and one registry line.

**Bad.** Every new backend needs a small adapter rather than coming free with Soapy.
The interface may prove too narrow for radios with features RTL-SDR lacks — multiple
channels, full duplex, hardware timestamps.

**Accepted.** The adapters are small, and a too-narrow interface discovered against
real hardware is a better problem than a heavyweight dependency adopted speculatively.

## What would change our mind

- Two or three non-RTL backends actually being maintained, at which point the adapter
  cost exceeds the dependency cost.
- A contributor with the hardware to test a SoapySDR backend properly.
- Needing hardware timestamps or multi-channel coherent capture, which the current
  interface does not model.
