"""The buffer that joins the position timeline to the RF timeline.

This is where "an RF observation has a location" actually happens, and it is the
place where an unconsidered implementation quietly invents data. The rules here:

* Interpolate between two bracketing fixes. Say so, via
  :attr:`~spectramap.types.QualityFlag.POSITION_INTERPOLATED`.
* Never extrapolate silently past the end of the track. If only one side is
  available, hold the nearest fix, mark it
  :attr:`~spectramap.types.QualityFlag.POSITION_EXTRAPOLATED`, downgrade the evidence
  to ESTIMATED, and record the age.
* Beyond ``max_age_ms``, return no position at all and flag
  :attr:`~spectramap.types.QualityFlag.NO_LOCATION`. An observation without a
  trustworthy location is still stored -- it is real RF data, and it still belongs to
  the spectrum record -- it simply must not be placed on a map.
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Iterable, Iterator

from spectramap.types import Evidence, FixQuality, Position, QualityFlag, ResolvedPosition


class PositionTrack:
    """An ordered buffer of fixes supporting time-indexed lookup.

    Fixes are keyed on the host monotonic clock, never on wall clock, because NTP or
    a GPS-driven clock correction can step wall time mid-survey and silently reorder
    a track.
    """

    __slots__ = ("_fixes", "_keys", "_max_fixes")

    def __init__(self, max_fixes: int = 100_000) -> None:
        self._fixes: deque[Position] = deque(maxlen=max_fixes)
        self._keys: deque[int] = deque(maxlen=max_fixes)
        self._max_fixes = max_fixes

    def __len__(self) -> int:
        return len(self._fixes)

    def __iter__(self) -> Iterator[Position]:
        return iter(self._fixes)

    @property
    def fixes(self) -> list[Position]:
        return list(self._fixes)

    @property
    def latest(self) -> Position | None:
        return self._fixes[-1] if self._fixes else None

    def add(self, fix: Position) -> None:
        """Append a fix. Out-of-order fixes are dropped rather than corrupting order."""
        if self._keys and fix.t_mono_ns < self._keys[-1]:
            return
        self._fixes.append(fix)
        self._keys.append(fix.t_mono_ns)

    def extend(self, fixes: Iterable[Position]) -> None:
        for fix in fixes:
            self.add(fix)

    def resolve(
        self,
        t_mono_ns: int,
        max_age_ms: float = 5_000.0,
        accuracy_limit_m: float | None = 15.0,
    ) -> ResolvedPosition:
        """Position at ``t_mono_ns``, with the evidence and flags that go with it."""
        if not self._fixes:
            return ResolvedPosition(None, Evidence.MEASURED, None, QualityFlag.NO_LOCATION)

        keys = list(self._keys)
        idx = bisect.bisect_left(keys, t_mono_ns)

        if 0 < idx < len(keys):
            before, after = self._fixes[idx - 1], self._fixes[idx]
            gap_ns = after.t_mono_ns - before.t_mono_ns
            age_ms = min(t_mono_ns - before.t_mono_ns, after.t_mono_ns - t_mono_ns) / 1e6
            if gap_ns <= 0:
                fix, flags = after, QualityFlag.NONE
                evidence = Evidence.MEASURED
            else:
                frac = (t_mono_ns - before.t_mono_ns) / gap_ns
                fix = _interpolate(before, after, frac, t_mono_ns)
                flags = QualityFlag.POSITION_INTERPOLATED
                evidence = Evidence.DERIVED
        else:
            # Before the first fix or after the last one.
            fix = self._fixes[0] if idx == 0 else self._fixes[-1]
            age_ms = abs(t_mono_ns - fix.t_mono_ns) / 1e6
            exact = age_ms == 0.0
            flags = QualityFlag.NONE if exact else QualityFlag.POSITION_EXTRAPOLATED
            evidence = Evidence.MEASURED if exact else Evidence.ESTIMATED

        if age_ms > max_age_ms:
            return ResolvedPosition(
                None, Evidence.MEASURED, age_ms, QualityFlag.NO_LOCATION | QualityFlag.GPS_STALE
            )

        if fix.fix_quality is FixQuality.NONE:
            flags |= QualityFlag.NO_LOCATION
        if (
            accuracy_limit_m is not None
            and fix.horizontal_accuracy_m is not None
            and fix.horizontal_accuracy_m > accuracy_limit_m
        ):
            flags |= QualityFlag.GPS_LOW_ACCURACY

        return ResolvedPosition(fix, evidence, age_ms, flags)


def _interpolate(a: Position, b: Position, frac: float, t_mono_ns: int) -> Position:
    """Linear interpolation between two fixes.

    Linear is right here: consumer GPS fixes are a second apart and a surveyor walks
    roughly a metre in that time, well inside the fix's own uncertainty. The
    interpolated accuracy takes the *worse* of the two endpoints rather than blending
    them, because interpolation cannot be more certain than its inputs.
    """

    def lerp(x: float | None, y: float | None) -> float | None:
        if x is None or y is None:
            return x if y is None else y
        return x + (y - x) * frac

    def worse(x: float | None, y: float | None) -> float | None:
        if x is None:
            return y
        if y is None:
            return x
        return max(x, y)

    return Position(
        t_utc_ns=int(a.t_utc_ns + (b.t_utc_ns - a.t_utc_ns) * frac),
        t_mono_ns=t_mono_ns,
        latitude=a.latitude + (b.latitude - a.latitude) * frac,
        longitude=a.longitude + (b.longitude - a.longitude) * frac,
        altitude_m=lerp(a.altitude_m, b.altitude_m),
        horizontal_accuracy_m=worse(a.horizontal_accuracy_m, b.horizontal_accuracy_m),
        vertical_accuracy_m=worse(a.vertical_accuracy_m, b.vertical_accuracy_m),
        speed_mps=lerp(a.speed_mps, b.speed_mps),
        heading_deg=b.heading_deg,
        # An interpolated point is never better than the worse of its neighbours.
        fix_quality=a.fix_quality if a.fix_quality == b.fix_quality else FixQuality.ESTIMATED,
        satellites=min(x for x in (a.satellites, b.satellites) if x is not None)
        if (a.satellites is not None or b.satellites is not None)
        else None,
        source=a.source,
    )


__all__ = ["PositionTrack"]
