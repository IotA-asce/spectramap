# Safety, ethics and legal responsibility

## What SpectraMap is

A **passive RF measurement instrument**. It receives, measures the distribution of
energy across frequency, attaches position and time, and draws maps.

It is intended for:

- RF engineering, education and experimentation
- spectrum diagnostics and interference hunting in environments you control
- antenna and propagation comparison
- locating your own test transmitters
- understanding the radio environment of a place

## What SpectraMap is not, and will not become

The project does **not** implement, and will not accept contributions implementing:

| | |
|---|---|
| ❌ Transmission of any kind | it is receive-only; there is no transmit path anywhere in the codebase |
| ❌ Interception of private communications | |
| ❌ Decryption, or defeating access controls | |
| ❌ Payload or content decoding | spectra are stored; message contents are not |
| ❌ Jamming or interference generation | |
| ❌ Signal injection or transmitter impersonation | |
| ❌ Wi-Fi credential analysis or capture | |
| ❌ Features designed to identify or track private individuals | |

This is architectural, not a policy note. There is no modulation, no demodulator, no
protocol parser and no transmit code path. The detection module finds *peaks in a
power spectrum*; it has no concept of a message.

## Privacy by construction

SpectraMap stores **spectra, not content**.

An observation is an array of power values across frequency. It records that energy
existed at a frequency, at a place, at a time. It does not record, and cannot
reconstruct, what was being communicated.

- No demodulation. No decoding. No payload storage.
- No cloud services, no telemetry, no accounts. Everything runs locally; a survey is a
  directory on your disk.
- The only network access in the entire project is optional map tiles from
  OpenStreetMap for the viewer's base layer, and the viewer works without them.

Any future protocol-analysis capability would have to be a separate, explicitly
installed module with its own legal and ethical boundaries — not part of this core.

## Legal responsibility

**You are responsible for complying with the law where you are.** SpectraMap cannot
know your jurisdiction and does not try to guess.

Radio law varies enormously. Depending on where you are, some or all of these may
apply.

### Reception

In many jurisdictions receiving is broadly permitted, but with real exceptions.
Frequently restricted: cellular bands, emergency services, aviation and maritime
traffic, satellite downlinks, and anything that is encrypted. Several countries
restrict receiving equipment itself. Others permit reception but prohibit *acting on*,
recording, or disclosing what you hear — the offence being the use, not the listening.

### Transmission

SpectraMap does not transmit. But the [validation scenario](v0-plan.md) involves
placing a **test transmitter**, and that absolutely is regulated. If you do this:

- use only equipment you are licensed or permitted to operate;
- stay within the power limits, frequency allocations and duty-cycle rules that apply;
- use licence-exempt ISM bands within their conditions, or transmit under an amateur
  licence within its terms;
- never transmit on emergency, aviation, maritime or cellular allocations;
- do not cause interference to anyone else.

If you are unsure whether you may transmit at all: **you may not**. Use the simulator,
which needs no transmitter and no licence.

### Location, access and privacy

Surveying involves walking around with recording equipment.

- Respect property boundaries. Get permission for private land.
- Airports, military installations, ports, and government sites frequently have
  restrictions on both RF equipment and photography or recording.
- Position data is personal data in many jurisdictions. A `.smap` file contains a
  detailed track of where you walked and when. Treat it accordingly before sharing it,
  and consider that publishing a survey publishes your route.
- Where a survey covers residential areas, remember that a map of RF sources can imply
  facts about the people who live there.

### Aviation and drones

Drone survey mode is explicitly **not** part of V0. If it arrives, flight regulation
is entirely separate from and additional to radio regulation, and SpectraMap will
never be a flight controller — it consumes telemetry, it does not fly anything.

## Ethical use of localisation

The strongest-signal estimator exists for legitimate purposes: finding **your own**
test beacon, tracking down interference in an environment you are responsible for,
antenna experiments, teaching, and spectrum diagnostics.

It is deliberately **not** built to identify private individuals or their devices, and
that shapes the design:

- it estimates a **region with an uncertainty radius**, never a coordinate;
- it grades its own confidence and forces `low` when the data does not support a
  conclusion;
- it reports a geometry score that exposes surveys too one-sided to constrain an
  answer;
- it does not, and will not, correlate signals with device identifiers, because it
  never decodes anything that could contain one.

Using RF measurements to track a person, or to infer things about a household without
consent, is outside the intended use of this project regardless of what the software
technically permits.

## Physical safety

Surveying means walking while looking at a screen.

- Watch where you are going. Traffic, kerbs, water, cyclists.
- Do not survey while driving. If you must survey from a vehicle, someone else drives.
- Consider how you look. A person walking slowly around a residential area with an
  antenna and a laptop attracts attention. Be prepared to explain what you are doing,
  carry identification, and consider telling people in advance. In some places, being
  able to explain yourself matters more than being technically permitted.
- RF exposure from receiving is zero — you are not transmitting. If you add a test
  transmitter, keep the usual distance from antennas.

## For contributors

Contributions are welcome within this scope. Not accepted:

- transmit capability of any kind;
- demodulation, decoding, or payload storage;
- features whose primary purpose is identifying individuals or their devices;
- anything designed to circumvent access controls or encryption;
- interference generation.

Contributions that *strengthen* the boundaries — better warnings, clearer uncertainty,
better documentation of legal context in specific jurisdictions — are especially
welcome.

## In short

> SpectraMap measures the radio environment. It does not listen to what is being said,
> and it does not transmit. What you may receive, where you may go, what you may
> transmit, and what you may do with the results are matters of local law and of
> judgement. Both are yours.
