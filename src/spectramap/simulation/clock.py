"""The simulated clock shared by the mock receiver and the mock position provider.

Simulated time is advanced by the receiver, because the receiver is the sample-rate
master: reading N samples at fs advances the world by exactly N/fs seconds. The
position provider only ever *reads* the clock. That gives two properties worth having:

* **Determinism.** A run produces byte-identical results regardless of how fast the
  host is, because nothing depends on wall-clock scheduling.
* **Realism of alignment.** RF and position are aligned through exactly the same
  monotonic-clock mechanism the real recorder uses, so the synchronisation code is
  genuinely exercised rather than bypassed.

In ``realtime`` mode the clock additionally sleeps so a demo unfolds at human speed;
simulated time stays exact either way.
"""

from __future__ import annotations

import time


class SimClock:
    """A monotonic simulated clock with a fixed mapping onto UTC.

    ``t_sim_s`` is seconds since the start of the simulation. ``mono_ns`` and
    ``utc_ns`` are derived from it, so a simulated survey carries plausible absolute
    timestamps while remaining perfectly reproducible.
    """

    __slots__ = ("_t_sim_s", "_t0_mono_ns", "_t0_utc_ns", "realtime", "_wall_start_ns")

    def __init__(
        self,
        start_utc_ns: int | None = None,
        realtime: bool = False,
    ) -> None:
        self._t_sim_s = 0.0
        self._t0_mono_ns = time.monotonic_ns()
        # A fixed default epoch keeps simulated surveys byte-reproducible.
        # 2026-01-01T00:00:00Z.
        self._t0_utc_ns = 1_767_225_600_000_000_000 if start_utc_ns is None else int(start_utc_ns)
        self.realtime = realtime
        self._wall_start_ns = time.monotonic_ns()

    @property
    def t_sim_s(self) -> float:
        return self._t_sim_s

    @property
    def mono_ns(self) -> int:
        return self._t0_mono_ns + int(round(self._t_sim_s * 1e9))

    @property
    def utc_ns(self) -> int:
        return self._t0_utc_ns + int(round(self._t_sim_s * 1e9))

    def mono_ns_at(self, t_sim_s: float) -> int:
        return self._t0_mono_ns + int(round(t_sim_s * 1e9))

    def utc_ns_at(self, t_sim_s: float) -> int:
        return self._t0_utc_ns + int(round(t_sim_s * 1e9))

    def sim_s_from_mono(self, mono_ns: int) -> float:
        return (mono_ns - self._t0_mono_ns) / 1e9

    def advance(self, seconds: float) -> float:
        """Advance simulated time; in realtime mode, wait for the wall clock to agree.

        Returns the new simulated time.
        """
        if seconds < 0:
            raise ValueError(f"cannot advance the clock backwards ({seconds} s)")
        self._t_sim_s += seconds
        if self.realtime:
            target_ns = self._wall_start_ns + int(round(self._t_sim_s * 1e9))
            lag_ns = target_ns - time.monotonic_ns()
            if lag_ns > 0:
                time.sleep(lag_ns / 1e9)
        return self._t_sim_s


__all__ = ["SimClock"]
