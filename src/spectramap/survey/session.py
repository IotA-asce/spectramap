"""Wiring: turn a :class:`SurveyConfig` into a receiver and a position source.

Kept apart from the recorder so the recorder depends only on the two abstractions and
never on driver names. This is the single place that knows ``"mock"`` means a
simulator and ``"nmea"`` means a serial port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from spectramap.positioning.base import PositionProvider
from spectramap.positioning.mock import MockPositionProvider, StaticPositionProvider
from spectramap.positioning.nmea import SerialNMEAProvider
from spectramap.sdr.base import SDRDevice
from spectramap.sdr.registry import open_device, resolve_driver
from spectramap.simulation.clock import SimClock
from spectramap.simulation.world import VirtualWorld, compact_world, default_world
from spectramap.survey.config import SurveyConfig


@dataclass(frozen=True, slots=True)
class Session:
    """Everything the recorder needs, plus the simulation handles when simulated."""

    device: SDRDevice
    positions: PositionProvider
    clock: SimClock | None
    world: VirtualWorld | None

    @property
    def is_simulated(self) -> bool:
        return self.world is not None

    def manifest_extras(self) -> dict[str, Any]:
        """Simulation ground truth, recorded only for simulated surveys.

        Written under its own key and clearly marked so that no analysis can mistake
        it for a measurement, while still letting a test score a reconstruction
        against the truth the algorithm never saw.
        """
        if self.world is None:
            return {}
        return {"simulation_ground_truth": self.world.to_dict()}


def build_session(
    config: SurveyConfig,
    world: VirtualWorld | None = None,
    realtime: bool | None = None,
) -> Session:
    """Construct the receiver and position provider described by ``config``."""
    driver = resolve_driver(config.receiver.driver)
    use_realtime = config.simulation.realtime if realtime is None else realtime

    if driver == "mock":
        sim_world = world if world is not None else _default_scenario(config)
        clock = SimClock(realtime=use_realtime)
        device = open_device(
            "mock",
            world=sim_world,
            clock=clock,
            index=config.receiver.index,
        )
        positions = _build_simulated_positions(config, sim_world, clock)
        return Session(device=device, positions=positions, clock=clock, world=sim_world)

    device = open_device(
        driver,
        index=config.receiver.index,
        serial=config.receiver.serial,
        freq_correction_ppm=config.receiver.freq_correction_ppm,
    )
    return Session(
        device=device,
        positions=_build_real_positions(config),
        clock=None,
        world=None,
    )


# Named simulation scenarios, selected by `simulation.scenario` in the configuration.
SCENARIOS = {
    # 400 m grid walked at 5 m/s: completes in 80 s, so a demo-length survey covers the
    # whole route and shows real spatial contrast.
    "compact": compact_world,
    # 840 m grid at walking pace: ~11 minutes, closer to a real outdoor survey.
    "full": default_world,
    "default": compact_world,
}


def _default_scenario(config: SurveyConfig) -> VirtualWorld:
    """Build the world named by `simulation.scenario`.

    Defaults to the compact scenario rather than the full walking one. A survey that
    stops part-way through its route measures only part of the field, and then
    correctly -- but unhelpfully -- reports no usable spatial gradient.
    """
    name = config.simulation.scenario.strip().lower()
    factory = SCENARIOS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown simulation scenario {name!r}; available: {', '.join(sorted(SCENARIOS))}"
        )
    return factory(seed=config.simulation.seed)


def _build_simulated_positions(
    config: SurveyConfig, world: VirtualWorld, clock: SimClock
) -> PositionProvider:
    """A simulated receiver may still be paired with a static or real position source."""
    provider = config.location.provider
    if provider == "static":
        return StaticPositionProvider(
            latitude=config.location.latitude or world.route.waypoints[0].latitude,
            longitude=config.location.longitude or world.route.waypoints[0].longitude,
            altitude_m=config.location.altitude_m,
            clock=clock,
            update_rate_hz=config.location.update_rate_hz,
        )
    if provider == "nmea":
        return _build_real_positions(config)
    return MockPositionProvider(
        world=world,
        clock=clock,
        update_rate_hz=config.location.update_rate_hz,
        dropout_windows=config.simulation.gps_dropout_windows,
    )


def _build_real_positions(config: SurveyConfig) -> PositionProvider:
    provider = config.location.provider
    if provider == "nmea":
        if not config.location.port:
            raise ValueError("location.provider 'nmea' requires location.port")
        return SerialNMEAProvider(port=config.location.port, baudrate=config.location.baudrate)
    if provider == "static":
        if config.location.latitude is None or config.location.longitude is None:
            raise ValueError("location.provider 'static' requires latitude and longitude")
        return StaticPositionProvider(
            latitude=config.location.latitude,
            longitude=config.location.longitude,
            altitude_m=config.location.altitude_m,
            accuracy_m=config.location.accuracy_limit_m,
        )
    if provider == "mock":
        raise ValueError(
            "location.provider 'mock' needs a simulated receiver. Either set "
            "receiver.driver to 'mock', or choose a real position provider "
            "('nmea' or 'static')."
        )
    raise ValueError(f"unknown location provider {provider!r}")


__all__ = ["Session", "build_session"]
