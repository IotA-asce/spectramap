"""Simulated position source driven by the shared virtual world.

Emits fixes at a fixed rate along the world's route, with configurable positional
noise and reported accuracy. Because it reads the same
:class:`~spectramap.simulation.clock.SimClock` the mock receiver advances, RF and
position stay aligned through the real synchronisation code rather than by
construction.

The noise matters. A provider that returned exact truth would let a reconstruction
test pass while the real pipeline fell apart on 3-metre GPS error, which is the
error every actual survey has.
"""

from __future__ import annotations

from spectramap.geospatial.projection import LocalTangentPlane
from spectramap.positioning.base import PositionProvider
from spectramap.simulation.clock import SimClock
from spectramap.simulation.world import VirtualWorld
from spectramap.types import FixQuality, Position


class MockPositionProvider(PositionProvider):
    """Generates fixes along a :class:`VirtualWorld` route at ``update_rate_hz``.

    Parameters
    ----------
    dropout_windows:
        ``(start_s, end_s)`` intervals of simulated time during which no fix is
        produced, modelling a walk under tree cover or between buildings. Used to
        test that observations survive a position gap and get flagged rather than
        being discarded or silently placed at the last known point.
    """

    def __init__(
        self,
        world: VirtualWorld,
        clock: SimClock,
        update_rate_hz: float = 1.0,
        dropout_windows: tuple[tuple[float, float], ...] = (),
        report_accuracy_m: float | None = None,
    ) -> None:
        if update_rate_hz <= 0:
            raise ValueError(f"update rate must be positive, got {update_rate_hz}")
        self.world = world
        self.clock = clock
        self.update_rate_hz = float(update_rate_hz)
        self.dropout_windows = tuple(dropout_windows)
        self.report_accuracy_m = (
            world.gps_accuracy_m if report_accuracy_m is None else float(report_accuracy_m)
        )
        self._running = False
        self._next_index = 0
        self._plane = LocalTangentPlane(
            world.route.waypoints[0].latitude, world.route.waypoints[0].longitude
        )

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def poll(self) -> list[Position]:
        """Emit every scheduled fix whose time has passed since the last poll."""
        if not self._running:
            return []
        period = 1.0 / self.update_rate_hz
        now_s = self.clock.t_sim_s
        out: list[Position] = []
        while self._next_index * period <= now_s:
            t_s = self._next_index * period
            index = self._next_index
            self._next_index += 1
            if any(lo <= t_s <= hi for lo, hi in self.dropout_windows):
                continue
            out.append(self._fix_at(t_s, index))
        return out

    def _fix_at(self, t_s: float, index: int) -> Position:
        lat, lon, alt = self.world.position_at(t_s)
        rng = self.world.rng_for("gps", index)
        if self.world.gps_noise_m > 0:
            east, north, _ = self._plane.to_enu(lat, lon)
            east = float(east) + rng.normal(0.0, self.world.gps_noise_m)
            north = float(north) + rng.normal(0.0, self.world.gps_noise_m)
            lat_a, lon_a, _ = self._plane.to_geodetic(east, north)
            lat, lon = float(lat_a), float(lon_a)
            # Vertical error on consumer GNSS is roughly 1.5-2x the horizontal.
            alt = alt + float(rng.normal(0.0, self.world.gps_noise_m * 1.6))

        return Position(
            t_utc_ns=self.clock.utc_ns_at(t_s),
            t_mono_ns=self.clock.mono_ns_at(t_s),
            latitude=lat,
            longitude=lon,
            altitude_m=alt,
            horizontal_accuracy_m=self.report_accuracy_m,
            vertical_accuracy_m=self.report_accuracy_m * 1.6,
            speed_mps=self.world.route.speed_at(t_s),
            heading_deg=None,
            fix_quality=FixQuality.SIMULATED,
            satellites=9,
            source="mock",
        )

    def describe(self) -> dict[str, object]:
        return {
            "provider": "mock",
            "update_rate_hz": self.update_rate_hz,
            "reported_accuracy_m": self.report_accuracy_m,
            "position_noise_sigma_m": self.world.gps_noise_m,
            "dropout_windows_s": [list(w) for w in self.dropout_windows],
            "simulated": True,
        }


class StaticPositionProvider(PositionProvider):
    """A fixed position, for stationary monitoring and bench work.

    Honest about what it is: the fix quality is
    :attr:`~spectramap.types.FixQuality.ESTIMATED` and the accuracy is whatever the
    user claims, because a coordinate typed by a human is an assumption, not a
    measurement.
    """

    def __init__(
        self,
        latitude: float,
        longitude: float,
        altitude_m: float = 0.0,
        accuracy_m: float = 10.0,
        clock: SimClock | None = None,
        update_rate_hz: float = 1.0,
    ) -> None:
        self.latitude = latitude
        self.longitude = longitude
        self.altitude_m = altitude_m
        self.accuracy_m = accuracy_m
        self.clock = clock
        self.update_rate_hz = update_rate_hz
        self._running = False
        self._next_index = 0

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def poll(self) -> list[Position]:
        if not self._running:
            return []
        if self.clock is not None:
            period = 1.0 / self.update_rate_hz
            out = []
            while self._next_index * period <= self.clock.t_sim_s:
                t_s = self._next_index * period
                self._next_index += 1
                out.append(
                    self._make(self.clock.utc_ns_at(t_s), self.clock.mono_ns_at(t_s))
                )
            return out
        from spectramap.sdr.base import now_mono_utc_ns

        mono, utc = now_mono_utc_ns()
        return [self._make(utc, mono)]

    def _make(self, utc_ns: int, mono_ns: int) -> Position:
        return Position(
            t_utc_ns=utc_ns,
            t_mono_ns=mono_ns,
            latitude=self.latitude,
            longitude=self.longitude,
            altitude_m=self.altitude_m,
            horizontal_accuracy_m=self.accuracy_m,
            vertical_accuracy_m=self.accuracy_m * 2,
            speed_mps=0.0,
            fix_quality=FixQuality.ESTIMATED,
            source="static",
        )

    def describe(self) -> dict[str, object]:
        return {
            "provider": "static",
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "claimed_accuracy_m": self.accuracy_m,
            "evidence": "user-supplied coordinate; not a measurement",
        }


__all__ = ["MockPositionProvider", "StaticPositionProvider"]
