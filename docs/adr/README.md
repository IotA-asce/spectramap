# Architecture Decision Records

Short records of decisions that were not obvious, kept so that a future reader can see
what was considered and what would justify revisiting.

| | Decision | Status |
|---|---|---|
| [0001](0001-implementation-language.md) | Python for everything in V0 | Accepted |
| [0002](0002-sdr-abstraction.md) | A minimal own abstraction, not SoapySDR | Accepted |
| [0003](0003-storage-model.md) | SQLite index + flat float32 chunks | Accepted |
| [0004](0004-power-units-and-normalisation.md) | Relative dBFS, density normalisation by default | Accepted |
| [0005](0005-time-synchronisation.md) | Monotonic internal timeline, buffered position resolution | Accepted |
| [0006](0006-offset-tuning.md) | Offset tuning on by default | Accepted |

Format: context, options considered, decision, consequences, and what would change our
mind.
