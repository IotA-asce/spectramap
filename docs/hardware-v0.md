# Hardware for V0

You need three things: a receiver, a position source, and an antenna. None of them
need to be expensive. **You need none of them to try SpectraMap** — the simulator
runs the entire pipeline.

> The RTL-SDR driver is written against pyrtlsdr and librtlsdr but has **not yet been
> validated against a physical device**. Expect to find rough edges, and please report
> them. See [v0-plan.md](v0-plan.md) milestone M10.

## Receiver

### The V0 target: an RTL-SDR

A DVB-T dongle repurposed as a wideband receiver. Around 25–40 USD.

| | |
|---|---|
| Tuner | R820T2 or R860 (avoid E4000 and FC00xx unless you have a reason) |
| Nominal range | 24 MHz – 1.766 GHz for R820T-family |
| Instantaneous bandwidth | up to 2.4 MS/s reliably |
| ADC | 8 bit |
| Frequency stability | 1 ppm TCXO on better units, 20–60 ppm on cheap ones |

Prefer a unit with a **TCXO** and a metal case. The temperature-compensated oscillator
matters more than anything else on the spec sheet: an uncompensated dongle drifts by
kilohertz at 433 MHz as it warms up, which smears a narrow carrier across bins and
makes repeat surveys disagree with each other.

Bias-tee support is useful if you later add a low-noise amplifier.

### What "24 MHz to 1.766 GHz" does not mean

Three caveats that the software encodes:

1. **Not all at once.** The receiver sees `sample_rate` of instantaneous bandwidth —
   2.4 MHz at best. Everything else requires retuning. SpectraMap distinguishes fixed
   observation from sweeping in its data model precisely so a sequential sweep is
   never drawn as a simultaneous wideband capture.
2. **The range is assumed, not read.** `librtlsdr` cannot report a tuner's frequency
   limits. SpectraMap derives them from the tuner chip it *can* read and marks the
   result `ASSUMED` — `spectramap devices` says so on screen. Real coverage varies by
   unit and temperature.
3. **The ends are not usable.** Sensitivity falls off badly near the extremes, and the
   bottom of the range needs direct sampling or an upconverter that most dongles do
   not have.

### Sample rate

2.4 MS/s is the highest rate most hosts sustain over USB. The hardware will happily
*accept* 2.56 or 3.2 MS/s and then silently drop samples. SpectraMap infers gaps from
the block timeline and raises `DROPPED_SAMPLES`, but the honest answer is to stay at
2.4 MS/s or below.

### Later receivers

The `SDRDevice` abstraction is shaped so these slot in without touching anything else:
HackRF One (wider range, 8-bit, half-duplex), Airspy (12-bit, better dynamic range),
LimeSDR, or anything behind SoapySDR. See
[ADR 0002](adr/0002-sdr-abstraction.md).

## Host computer

| Host | Notes |
|---|---|
| Laptop (Linux/macOS) | Easiest. Plenty of headroom at 2.4 MS/s and 4096-point FFTs. |
| Raspberry Pi 4/5 | Works. Use a good power supply — brownouts show up as dropped samples. Prefer an SSD or a fast A2 card. |
| Raspberry Pi Zero 2 W | Marginal. Reduce sample rate or FFT size, or raise `bin_decimation`. |

USB is where surveys go wrong. Use a short, good-quality cable, avoid unpowered hubs,
and keep the dongle away from the host's own USB 3 ports — USB 3 radiates broadband
noise right across the bands you care about.

**Power budget.** A dongle draws 250–350 mA. A Pi 4 with an SDR wants a solid 3 A
supply; a power bank that sags will produce dropped samples that look like RF effects.

**Storage.** ~120 MB/hour at 4096 bins and 2 observations/second. `spectramap survey`
prints the projected rate before it starts and stops cleanly rather than filling the
card (`quality.min_free_disk_mb`, default 200 MB).

## Position source

### USB GPS dongle — the easy option

A u-blox 7/8-based receiver (~15–25 USD) enumerates as a serial port and emits NMEA at
9600 or 38400 baud.

```bash
ls /dev/tty*                       # Linux: /dev/ttyUSB0 or /dev/ttyACM0
                                   # macOS: /dev/tty.usbserial-*
spectramap gps --provider nmea --port /dev/ttyUSB0
```

### GPS HAT / serial module

On a Pi, a HAT on the UART (`/dev/serial0`) works well. Disable the serial console
first (`raspi-config` → Interface Options → Serial → login shell *no*, hardware
*yes*).

### What to expect

| Condition | Typical horizontal accuracy |
|---|---|
| Open sky, warm start | 2–4 m |
| Suburban, some obstruction | 4–10 m |
| Urban canyon | 10–50 m, with multipath |
| Under dense tree cover | frequently no fix at all |

A **cold start can take several minutes**. Get a fix before you start recording, not
after.

Accuracy is not reported by NMEA at all. It reports HDOP — a geometry factor —
and SpectraMap estimates accuracy as `HDOP × 4 m`, marked `ASSUMED`.

### Grid resolution follows GPS accuracy

Do not build a 1 m grid from 3 m fixes. The detail is pure noise, and it looks
convincing, which makes it worse than an honestly coarse grid. Keep `--cell-size` and
`--resolution` at or above the median accuracy reported by `spectramap status`.

## Antenna

The antenna determines what you can hear and, more importantly for mapping, **how
your measurement depends on which way you are facing**.

### For V0: a vertical quarter-wave whip

Roughly omnidirectional in azimuth, which is what you want when the point is comparing
power between *places* rather than *directions*. A quarter wave at 433 MHz is about
17 cm; at 868 MHz about 8.6 cm. A magnetic base on a small ground plane is fine.

Match the polarization of what you are measuring — most ISM-band devices are
vertical.

### Things that will confuse your map

- **Body shadowing.** Your own body attenuates by 5–15 dB. Turning around changes the
  reading. Carry the antenna consistently: same side, same height, same orientation,
  and where practical walk the route in a consistent direction.
- **Height.** Chest height and ground level give materially different results. Pick
  one and record it in `antenna.notes`.
- **Orientation.** Even a "omnidirectional" whip is not omnidirectional in elevation.
- **Nearby metal.** Railings, cars, and your laptop all detune and reflect.

### Directional antennas

A Yagi will find a source faster, but the measurement then depends on heading, and V0
records no heading. Directional work is V4, with IMU/compass integration. If you use
one now, record the orientation manually in `antenna.orientation_deg` and treat every
map from it with suspicion.

### Record what you know

SpectraMap does not require antenna characterisation, but it does record what you tell
it — type, polarization, claimed gain, frequency range, notes — because an RF power
number without an antenna description is close to meaningless, and two surveys are
only comparable if the antenna was the same. All of it is marked `ASSUMED`.

## Optional extras

| | Why |
|---|---|
| Band-pass filter | Keeps strong out-of-band signals (FM broadcast, cellular) from compressing the front end. If you see `SDR_OVERLOAD`, this or less gain is the answer. |
| LNA | Improves sensitivity on weak signals. Adds its own noise and another uncalibrated gain stage — record it. |
| Ferrite chokes | Cheap mitigation for USB-conducted noise. |
| Power bank | 10000 mAh runs a Pi + dongle for several hours. |

## Setup checklist

```bash
spectramap devices                             # receiver detected, capabilities sane
spectramap gps --provider nmea --port /dev/ttyUSB0   # fix acquired, accuracy < 10 m

# Dry run: validates configuration and prints the data rate, records nothing.
spectramap survey -c examples/433mhz-neighbourhood.yaml --dry-run
```

Then, before the real walk:

1. **Fix the gain.** Start around 24–30 dB. Record a short survey standing still,
   check `spectramap status` for `SDR_OVERLOAD`, and reduce gain if it appears. Do not
   change gain again for the rest of the survey — SpectraMap flags `GAIN_CHANGED` and
   the powers across that boundary are not comparable.
2. **Never use AGC** for a survey whose point is spatial comparison. The receiver
   rescales itself as you walk. SpectraMap warns, flags every observation
   `AGC_ACTIVE`, and refuses to draw conclusions from it — but it cannot repair it.
3. **Let the dongle warm up** for a few minutes before starting. Crystal drift is
   worst in the first minutes.
4. **Check the offset tuning.** The default puts the LO 250 kHz below the target so
   the target does not land in the DC bin. Do not set it to zero.
5. **Walk a grid or a loop**, not a straight line, if you want to localise anything.
