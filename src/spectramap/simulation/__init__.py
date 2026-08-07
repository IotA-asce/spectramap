"""Deterministic RF simulation.

The simulator exists so that the entire pipeline -- acquisition, DSP, geotagging,
storage, aggregation, interpolation, mapping -- can be exercised in CI with no radio
hardware, and so that reconstruction algorithms can be scored against a ground truth
that the algorithm was never told.

The one honest coupling here: :class:`~spectramap.simulation.world.VirtualWorld` is
shared by the mock receiver and the mock position provider, because a simulated
receiver's signal strength genuinely depends on where the simulated receiver is. That
coupling lives entirely inside this package. No production code path touches it.
"""

from spectramap.simulation.world import (
    VirtualRoute,
    VirtualTransmitter,
    VirtualWorld,
    Waypoint,
)

__all__ = ["VirtualRoute", "VirtualTransmitter", "VirtualWorld", "Waypoint"]
