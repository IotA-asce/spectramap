"""A virtual RF environment: transmitters in space, a receiver moving through them.

The world is a function of simulated time. Given ``t`` it can say where the receiver
is and what complex baseband it would hear. It is fully deterministic: the same seed
and the same sequence of requests always produce the same samples, which is what
makes CI assertions on reconstructed maps meaningful.

Propagation model
-----------------
A log-distance path loss model, which is the standard first-order model for terrestrial
links::

    P_rx(d) = P_ref - 10 * n * log10(d / d_ref)

``n`` is the path-loss exponent: 2.0 is free space, 2.7-3.5 is typical for suburban
and urban ground-level paths, and 4+ occurs with heavy obstruction. Optional
log-normal shadowing adds spatially correlated fading.

This is deliberately a *simple* model. It is here to generate a known spatial
gradient for testing reconstruction, not to predict real coverage. Nothing in
SpectraMap's analysis path uses it, so an algorithm validated against this world is
not being marked by its own homework -- but it is being tested against a friendlier
world than reality, which is exactly why the physical benchmark in
docs/v0-plan.md matters.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from spectramap.geospatial.projection import LocalTangentPlane, haversine_m, path_length_m

# Distances below this are clamped, because log-distance path loss diverges at zero
# and a receiver cannot occupy the same point as an antenna anyway.
MIN_DISTANCE_M = 0.5


@dataclass(frozen=True, slots=True)
class VirtualTransmitter:
    """A simulated emitter.

    ``reference_power_dbfs`` is the power the receiver would measure at
    ``reference_distance_m``, expressed in the same relative full-scale units the DSP
    produces at ``reference_gain_db``. Choosing it in receiver units rather than
    watts keeps the simulator honest about the fact that V0 is uncalibrated: there is
    no claim here about EIRP or field strength.
    """

    tx_id: str
    latitude: float
    longitude: float
    frequency_hz: float
    reference_power_dbfs: float = -35.0
    altitude_m: float = 0.0
    reference_distance_m: float = 10.0
    path_loss_exponent: float = 2.6
    bandwidth_hz: float = 0.0  # 0 -> pure carrier; >0 -> band-limited noise.
    duty_cycle: float = 1.0  # Fraction of each period the emitter is on.
    period_s: float = 1.0  # Cycle length for an intermittent emitter.
    drift_hz_per_s: float = 0.0  # Linear frequency drift (e.g. crystal warm-up).
    wander_hz: float = 0.0  # Amplitude of sinusoidal frequency wander.
    wander_period_s: float = 60.0
    shadowing_db: float = 0.0  # Std-dev of log-normal shadowing.
    shadowing_scale_m: float = 25.0  # Correlation distance of that shadowing.
    notes: str = ""

    def __post_init__(self) -> None:
        if not 0.0 < self.duty_cycle <= 1.0:
            raise ValueError(f"duty_cycle must be in (0, 1], got {self.duty_cycle}")
        if self.period_s <= 0:
            raise ValueError(f"period_s must be positive, got {self.period_s}")
        if self.reference_distance_m <= 0:
            raise ValueError("reference_distance_m must be positive")

    def power_dbfs_at(self, distance_m: float | np.ndarray) -> np.ndarray | float:
        """Deterministic (unshadowed) received power at a distance, in relative dB."""
        d = np.maximum(np.asarray(distance_m, dtype=np.float64), MIN_DISTANCE_M)
        return self.reference_power_dbfs - 10.0 * self.path_loss_exponent * np.log10(
            d / self.reference_distance_m
        )

    def is_on(self, t_s: float) -> bool:
        """Whether the emitter is transmitting at simulated time ``t_s``."""
        if self.duty_cycle >= 1.0:
            return True
        return (t_s % self.period_s) < (self.duty_cycle * self.period_s)

    def frequency_offset_hz(self, t_s: float) -> float:
        """Instantaneous frequency offset from the nominal, at time ``t_s``."""
        offset = self.drift_hz_per_s * t_s
        if self.wander_hz:
            offset += self.wander_hz * math.sin(2.0 * math.pi * t_s / self.wander_period_s)
        return offset

    def phase_at(self, t_s: np.ndarray) -> np.ndarray:
        """Phase accumulated by the drifting carrier, relative to its nominal frequency.

        This is the integral of the instantaneous frequency offset, not the offset
        multiplied by time. Using ``2*pi*f(t)*t`` -- the tempting shortcut -- produces
        a signal whose actual instantaneous frequency is wrong by a factor of two for
        linear drift, and the simulator would then be validating the DSP against a
        signal that is not where it claims to be.
        """
        phase = np.zeros_like(t_s)
        if self.drift_hz_per_s:
            phase += 2.0 * np.pi * self.drift_hz_per_s * t_s**2 / 2.0
        if self.wander_hz:
            w = 2.0 * np.pi / self.wander_period_s
            # Integral of a*sin(w t) dt = (a/w)*(1 - cos(w t)).
            phase += 2.0 * np.pi * (self.wander_hz / w) * (1.0 - np.cos(w * t_s))
        return phase

    def to_dict(self) -> dict[str, object]:
        return {
            "tx_id": self.tx_id,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "altitude_m": self.altitude_m,
            "frequency_hz": self.frequency_hz,
            "reference_power_dbfs": self.reference_power_dbfs,
            "reference_distance_m": self.reference_distance_m,
            "path_loss_exponent": self.path_loss_exponent,
            "bandwidth_hz": self.bandwidth_hz,
            "duty_cycle": self.duty_cycle,
            "period_s": self.period_s,
            "drift_hz_per_s": self.drift_hz_per_s,
            "wander_hz": self.wander_hz,
            "shadowing_db": self.shadowing_db,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class Waypoint:
    latitude: float
    longitude: float
    altitude_m: float = 0.0
    # Seconds to remain here before moving on. Models a surveyor stopping to
    # integrate at a point, which is a genuinely different measurement regime.
    dwell_s: float = 0.0


class VirtualRoute:
    """A piecewise-linear path walked at constant speed, with optional dwells.

    Positions between waypoints are interpolated in the local tangent plane, so the
    metre-scale geometry the propagation model sees is correct.
    """

    __slots__ = ("waypoints", "speed_mps", "loop", "_plane", "_enu", "_legs", "_schedule")

    def __init__(
        self,
        waypoints: list[Waypoint],
        speed_mps: float = 1.3,
        loop: bool = False,
    ) -> None:
        if len(waypoints) < 1:
            raise ValueError("a route needs at least one waypoint")
        if speed_mps <= 0:
            raise ValueError(f"speed must be positive, got {speed_mps}")
        pts = list(waypoints)
        if loop and len(pts) > 1:
            pts.append(pts[0])
        self.waypoints = pts
        self.speed_mps = float(speed_mps)
        self.loop = loop

        lats = np.array([w.latitude for w in pts])
        lons = np.array([w.longitude for w in pts])
        self._plane = LocalTangentPlane.from_points(lats, lons)
        east, north, _ = self._plane.to_enu(lats, lons)
        self._enu = np.column_stack([east, north, [w.altitude_m for w in pts]])

        # Build a timeline of (t_start, t_end, kind, index) segments.
        schedule: list[tuple[float, float, str, int]] = []
        t = 0.0
        for i, wp in enumerate(pts):
            if wp.dwell_s > 0:
                schedule.append((t, t + wp.dwell_s, "dwell", i))
                t += wp.dwell_s
            if i + 1 < len(pts):
                leg = float(np.linalg.norm(self._enu[i + 1, :2] - self._enu[i, :2]))
                travel = leg / self.speed_mps if leg > 0 else 0.0
                if travel > 0:
                    schedule.append((t, t + travel, "move", i))
                    t += travel
        if not schedule:  # Single stationary waypoint.
            schedule.append((0.0, math.inf, "dwell", 0))
        self._schedule = schedule
        self._legs = t

    @property
    def duration_s(self) -> float:
        """Time to walk the route once. ``inf`` for a single stationary point."""
        return self._legs if self._legs > 0 else math.inf

    @property
    def length_m(self) -> float:
        lats = np.array([w.latitude for w in self.waypoints])
        lons = np.array([w.longitude for w in self.waypoints])
        return path_length_m(lats, lons)

    def position_at(self, t_s: float) -> tuple[float, float, float]:
        """(latitude, longitude, altitude) at simulated time ``t_s``.

        Time before the start clamps to the first waypoint; time past the end clamps
        to the last, unless the route loops, in which case it wraps.
        """
        if t_s <= 0:
            wp = self.waypoints[0]
            return wp.latitude, wp.longitude, wp.altitude_m
        if self.loop and math.isfinite(self.duration_s) and self.duration_s > 0:
            t_s = t_s % self.duration_s

        for t0, t1, kind, i in self._schedule:
            if t_s < t1 or not math.isfinite(t1):
                if kind == "dwell":
                    wp = self.waypoints[i]
                    return wp.latitude, wp.longitude, wp.altitude_m
                frac = (t_s - t0) / (t1 - t0)
                p = self._enu[i] + frac * (self._enu[i + 1] - self._enu[i])
                lat, lon, _ = self._plane.to_geodetic(p[0], p[1])
                return float(lat), float(lon), float(p[2])

        wp = self.waypoints[-1]
        return wp.latitude, wp.longitude, wp.altitude_m

    def speed_at(self, t_s: float) -> float:
        """Ground speed at ``t_s``; zero while dwelling."""
        if self.loop and math.isfinite(self.duration_s) and self.duration_s > 0:
            t_s = t_s % self.duration_s
        for _t0, t1, kind, _ in self._schedule:
            if t_s < t1 or not math.isfinite(t1):
                return 0.0 if kind == "dwell" else self.speed_mps
        return 0.0

    @classmethod
    def grid(
        cls,
        center_lat: float,
        center_lon: float,
        size_m: float = 120.0,
        rows: int = 6,
        speed_mps: float = 1.3,
        altitude_m: float = 0.0,
    ) -> VirtualRoute:
        """A boustrophedon ("lawnmower") grid centred on a point.

        Even coverage of an area is what makes a reconstruction test meaningful; a
        single straight line through a field cannot distinguish a source to the left
        from one to the right.
        """
        if rows < 2:
            raise ValueError("a grid needs at least 2 rows")
        plane = LocalTangentPlane(center_lat, center_lon)
        half = size_m / 2.0
        northings = np.linspace(-half, half, rows)
        pts: list[Waypoint] = []
        for r, n in enumerate(northings):
            eastings = [-half, half] if r % 2 == 0 else [half, -half]
            for e in eastings:
                lat, lon, _ = plane.to_geodetic(e, n)
                pts.append(Waypoint(float(lat), float(lon), altitude_m))
        return cls(pts, speed_mps=speed_mps)


class VirtualWorld:
    """Transmitters, a route, a noise floor, and a seed.

    Shared by :class:`~spectramap.sdr.mock.MockSDR` and
    :class:`~spectramap.positioning.mock.MockPositionProvider` so both agree on where
    the receiver is at any simulated instant.
    """

    __slots__ = (
        "transmitters",
        "route",
        "noise_floor_dbfs_per_hz",
        "seed",
        "reference_gain_db",
        "gps_accuracy_m",
        "gps_noise_m",
        "band_edge_rolloff",
        "_plane",
    )

    def __init__(
        self,
        transmitters: list[VirtualTransmitter],
        route: VirtualRoute,
        noise_floor_dbfs_per_hz: float = -130.0,
        seed: int = 20260807,
        # An actual R820T gain step, not a round number. Transmitter powers are
        # declared "at the reference gain", so the reference has to be a setting the
        # simulated tuner can really take -- otherwise asking for 24 dB snaps to the
        # nearest step and every declared power comes out about 1 dB low.
        reference_gain_db: float = 22.9,
        gps_accuracy_m: float = 3.5,
        gps_noise_m: float = 2.0,
        band_edge_rolloff: float = 0.0,
    ) -> None:
        self.transmitters = list(transmitters)
        self.route = route
        self.noise_floor_dbfs_per_hz = float(noise_floor_dbfs_per_hz)
        self.seed = int(seed)
        self.reference_gain_db = float(reference_gain_db)
        self.gps_accuracy_m = float(gps_accuracy_m)
        self.gps_noise_m = float(gps_noise_m)
        # Fraction of the captured span at each edge over which response rolls off,
        # modelling the receiver's analogue filter skirts. Zero by default so that
        # validation tests see a flat, exactly predictable response.
        self.band_edge_rolloff = float(band_edge_rolloff)
        self._plane = LocalTangentPlane(route.waypoints[0].latitude, route.waypoints[0].longitude)

    # -- Geometry -----------------------------------------------------------

    def position_at(self, t_s: float) -> tuple[float, float, float]:
        return self.route.position_at(t_s)

    def distance_to(self, tx: VirtualTransmitter, t_s: float) -> float:
        """Slant range from the receiver to a transmitter at time ``t_s``."""
        lat, lon, alt = self.position_at(t_s)
        ground = float(haversine_m(lat, lon, tx.latitude, tx.longitude))
        dz = tx.altitude_m - alt
        return float(math.hypot(ground, dz))

    def received_power_dbfs(self, tx: VirtualTransmitter, t_s: float) -> float:
        """Received power at time ``t_s`` including shadowing, in relative dB.

        Ground truth. Available to tests for scoring, never to the analysis code.
        """
        d = self.distance_to(tx, t_s)
        power = float(tx.power_dbfs_at(d))
        if tx.shadowing_db > 0:
            power += self._shadowing_db(tx, t_s)
        return power

    def _shadowing_db(self, tx: VirtualTransmitter, t_s: float) -> float:
        """Spatially correlated log-normal shadowing.

        Keyed on quantised position rather than on time, so that walking back over
        the same ground reproduces the same fade -- which is what makes shadowing a
        *spatial* nuisance rather than white noise that averaging trivially removes.
        """
        lat, lon, _ = self.position_at(t_s)
        east, north, _ = self._plane.to_enu(lat, lon)
        cell = (
            int(np.floor(float(east) / tx.shadowing_scale_m)),
            int(np.floor(float(north) / tx.shadowing_scale_m)),
        )
        rng = np.random.default_rng([self.seed, abs(hash(tx.tx_id)) % (2**31), *map(abs, cell)])
        return float(rng.normal(0.0, tx.shadowing_db))

    # -- Reproducibility ----------------------------------------------------

    def rng_for(self, stream: str, index: int) -> np.random.Generator:
        """An independent, reproducible generator for one (stream, index) pair.

        Seeding per block rather than threading one generator through the run means
        block *N* contains the same noise no matter how many blocks were read before
        it, so a test can read any part of the simulated survey in isolation.
        """
        return np.random.default_rng([self.seed, abs(hash(stream)) % (2**31), index])

    def to_dict(self) -> dict[str, object]:
        """Ground truth, written into the survey manifest for simulated runs only."""
        return {
            "simulated": True,
            "seed": self.seed,
            "noise_floor_dbfs_per_hz": self.noise_floor_dbfs_per_hz,
            "reference_gain_db": self.reference_gain_db,
            "gps_accuracy_m": self.gps_accuracy_m,
            "transmitters": [tx.to_dict() for tx in self.transmitters],
            "route": {
                "waypoints": [
                    {"latitude": w.latitude, "longitude": w.longitude, "altitude_m": w.altitude_m}
                    for w in self.route.waypoints
                ],
                "speed_mps": self.route.speed_mps,
                "length_m": self.route.length_m,
                "duration_s": self.route.duration_s,
            },
        }


def default_world(seed: int = 20260807) -> VirtualWorld:
    """The reference scenario used by the examples and the CI validation test.

    One strong beacon at 433.92 MHz sitting inside a surveyed grid, plus a weak
    constant carrier at 434.20 MHz to prove frequency selection actually selects.
    Coordinates are in the middle of the South Atlantic so that no real location is
    implied by the test fixtures.
    """
    center_lat, center_lon = 0.0, -25.0
    beacon = VirtualTransmitter(
        tx_id="beacon-433",
        latitude=center_lat + 0.00027,  # ~30 m north of centre.
        longitude=center_lon + 0.00018,  # ~20 m east of centre.
        frequency_hz=433_920_000.0,
        reference_power_dbfs=-38.0,
        reference_distance_m=10.0,
        path_loss_exponent=2.6,
        notes="Simulated low-power test beacon; the target of the localisation benchmark.",
    )
    background = VirtualTransmitter(
        tx_id="carrier-434",
        latitude=center_lat + 0.02,  # ~2.2 km away: effectively uniform over the grid.
        longitude=center_lon,
        frequency_hz=434_200_000.0,
        # Chosen to sit well above the noise floor but, being far away, to vary by
        # under a decibel across the whole grid. That is the control case: clearly
        # detectable, yet carrying no usable spatial information.
        reference_power_dbfs=-25.0,
        path_loss_exponent=2.0,
        notes="Distant constant carrier; detectable but with no usable spatial gradient.",
    )
    route = VirtualRoute.grid(center_lat, center_lon, size_m=120.0, rows=6, speed_mps=1.3)
    return VirtualWorld([beacon, background], route, seed=seed)


def compact_world(seed: int = 20260807) -> VirtualWorld:
    """A smaller, faster version of :func:`default_world` for CI and quick demos.

    Same structure -- a beacon inside a surveyed grid plus a spatially flat control
    carrier -- but a tighter grid walked at cycling pace, so the whole survey
    simulates in a couple of seconds. Both emitters sit within +/-125 kHz of the
    tuned centre so that a 250 kS/s capture can see them.
    """
    center_lat, center_lon = 0.0, -25.0
    beacon = VirtualTransmitter(
        tx_id="beacon-433",
        latitude=center_lat + 0.00018,  # ~20 m north of centre.
        longitude=center_lon + 0.00013,  # ~14 m east of centre.
        frequency_hz=433_920_000.0,
        reference_power_dbfs=-38.0,
        reference_distance_m=10.0,
        path_loss_exponent=2.6,
        notes="Simulated test beacon; the target of the CI reconstruction check.",
    )
    control = VirtualTransmitter(
        tx_id="carrier-433-95",
        latitude=center_lat + 0.02,  # ~2.2 km away: flat across the grid.
        longitude=center_lon,
        frequency_hz=433_950_000.0,
        reference_power_dbfs=-25.0,
        path_loss_exponent=2.0,
        notes="Distant carrier: detectable, but with no usable spatial gradient.",
    )
    route = VirtualRoute.grid(center_lat, center_lon, size_m=80.0, rows=4, speed_mps=5.0)
    return VirtualWorld([beacon, control], route, seed=seed)


__all__ = [
    "MIN_DISTANCE_M",
    "VirtualRoute",
    "VirtualTransmitter",
    "VirtualWorld",
    "Waypoint",
    "compact_world",
    "default_world",
]
