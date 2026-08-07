# Calibration

## The short version

**V0 is not calibrated.** It produces `dBFS` and `dBFS/Hz` — decibels relative to a
full-scale signal on the receiver's ADC, at the gain setting recorded alongside them.

SpectraMap will not print `dBm`. `PowerUnit.is_calibrated` is `False` for every unit
V0 can produce, the manifest says `"calibrated": false`, the CLI and the viewer render
the unit as `dBFS/Hz (relative, uncalibrated)`, and an end-to-end test asserts that no
output of an uncalibrated survey contains the string `dBm`.

This is not modesty. A number labelled `−62 dBm` invites comparison against regulatory
limits, link budgets and other people's measurements. An uncalibrated RTL-SDR reading
supports none of that.

## What relative units are actually good for

Quite a lot, as it happens:

| Question | Answerable? |
|---|---|
| Where along my route is 433.92 MHz strongest? | ✅ at fixed gain |
| Is the signal 20 dB above the local noise floor? | ✅ SNR is a ratio; the reference cancels |
| Is the interference worse today than last week? | ✅ same receiver, same antenna, same gain |
| Which of these two antennas hears the beacon better? | ✅ same receiver, back to back |
| Is this transmitter within its licensed EIRP? | ❌ |
| Is the field here above a safety limit? | ❌ |
| Does my reading agree with someone else's dongle? | ❌ |

The entire V0 goal — mapping *relative* spatial structure at one frequency — sits in
the first group.

## The conditions relative units depend on

Comparability within a survey holds only while these are true. Each has a
corresponding quality flag or warning.

| Condition | Enforcement |
|---|---|
| Gain is fixed | fixed gain is the default; AGC produces a loud warning and flags every observation `AGC_ACTIVE` |
| Gain does not change mid-survey | `GAIN_CHANGED` flag + a logged event |
| The front end is not compressing | `SDR_OVERLOAD` from clipped-sample fraction |
| Same antenna, same orientation, same height | recorded in `AntennaInfo`; not enforceable in software |
| Same frequency region | the receiver's response varies with frequency; see below |
| Similar temperature | not tracked in V0 |

## What would need to change

Correction stages, in the order they would bite:

```
raw power (dBFS)
  + receiver gain response       ← the actual gain at the requested setting
  + frequency response           ← the receiver is not flat across its range
  + antenna response             ← gain and pattern at this frequency and angle
  + cable and connector loss
  + reference offset             ← tie to a known source
= corrected power (dBm)
```

### 1. Receiver gain response

An RTL-SDR's gain table is nominal. The actual gain at a given setting varies between
units, with frequency, and with temperature, by several decibels.

*Measurement:* inject a known level at one frequency, step through every gain setting,
record measured versus applied. Produces a per-unit gain correction table.

### 2. Frequency response

Even at fixed gain, the response varies across the tuning range — several decibels
across a few hundred megahertz, plus roll-off near the edges of each captured span
from the analogue filter.

*Measurement:* sweep a known-flat source, or use a calibrated noise source, and record
measured versus frequency.

This one has a spatial consequence: within a single 2.4 MHz span the variation is
small, so a **fixed-frequency** survey is barely affected. A **wideband sweep**
compares readings taken at different frequencies, and there the response shape becomes
a fake spatial pattern if uncorrected.

### 3. Antenna response

The hardest, and the one most likely to be wrong. Gain varies with frequency and with
angle in both azimuth and elevation, and is affected by the ground plane, nearby
metal, and the operator's body.

*Measurement:* an anechoic chamber, or a careful outdoor range with a reference
antenna. Out of reach for most users, which is why SpectraMap records what the user
*claims* and marks the whole `AntennaInfo` structure `ASSUMED`.

### 4. Reference offset

Finally, tie the chain to something known: a calibrated signal generator, a
laboratory-characterised noise source, or a transfer standard measured against a
calibrated spectrum analyser.

## A practical middle path

Full traceable calibration is a laboratory exercise. A **relative** calibration is
achievable at home and buys most of the practical benefit:

1. **Characterise the noise floor.** Terminate the input with 50 Ω, record at each
   gain setting across the bands you use. Gives the receiver's own noise contribution
   and reveals internal spurs — birdies that are trivially mistaken for real signals.
2. **Flatten the frequency response.** With a broadband noise source (even a cheap
   one), record the response across the span. It will not be traceable, but it removes
   the *shape*, which is what corrupts sweep maps.
3. **One-point transfer.** If you can borrow a calibrated analyser once, measure one
   known signal on both instruments and record the offset. That single number converts
   the whole relative scale into an approximate absolute one, with an honest
   uncertainty of a few decibels.

Stage 3 is the point at which `PowerUnit.DBM` would become defensible — with the
uncertainty stated, and only for the frequency, gain and antenna it was measured at.

## Architecture

The correction stage does not exist yet, but nothing blocks it:

- `PowerUnit` already distinguishes calibrated from relative, and `is_calibrated`
  already gates display.
- The `.smap` container has a `calibration/` directory reserved.
- `Provenance` can record which correction tables were applied to a product.
- Corrections would apply in `spectramap.dsp` between the PSD and the `PowerSpectrum`,
  a single well-defined seam.

**The raw measurement is always retained.** Calibration is a transformation recorded
in provenance, never a replacement. If a correction table is later found to be wrong,
every survey can be reprocessed — which is only possible because nothing is discarded.

## Related sources of error

Some things calibration does not fix. See [technical-risks.md](technical-risks.md):

- **Frequency error.** An uncompensated dongle can be 20–60 ppm off — 9–26 kHz at
  433 MHz. `receiver.freq_correction_ppm` applies a per-unit correction; measure it
  once against a known reference.
- **Multipath.** Constructive and destructive interference produces 20 dB swings over
  a few centimetres. No amplitude calibration touches this; only spatial averaging
  helps.
- **Temperature drift.** Both gain and frequency move as the dongle warms. Let it
  stabilise before surveying.
