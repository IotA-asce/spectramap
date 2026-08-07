"""The simulator: determinism, physics, and the receiver behaviours it must reproduce.

If the simulator is wrong, every reconstruction test is validating against fiction.
These tests check it against closed-form expectations rather than against itself.
"""

from __future__ import annotations

import numpy as np
import pytest

from spectramap.dsp.detect import estimate_noise_floor
from spectramap.dsp.spectrum import SpectrumEstimator, overload_fraction
from spectramap.geospatial.projection import haversine_m
from spectramap.sdr.base import StreamError
from spectramap.sdr.mock import MockSDR
from spectramap.simulation.clock import SimClock
from spectramap.simulation.world import (
    VirtualRoute,
    VirtualTransmitter,
    VirtualWorld,
    Waypoint,
    compact_world,
)
from spectramap.types import FFTConfig, Normalization

FS = 250_000.0


def _world(**kwargs):
    tx = VirtualTransmitter(
        tx_id="t", latitude=0.0, longitude=0.0, frequency_hz=433_950_000.0, **kwargs
    )
    route = VirtualRoute([Waypoint(0.0, 0.0)], speed_mps=1.0)
    return VirtualWorld([tx], route, noise_floor_dbfs_per_hz=-130.0)


def _device(world, fc=433_920_000.0, gain=24.0):
    clock = SimClock()
    device = MockSDR(world, clock)
    device.open()
    device.set_sample_rate(FS)
    device.set_center_frequency(fc)
    device.set_gain(gain)
    return device, clock


def _estimator(fc=433_920_000.0, size=1024, normalization=Normalization.SPECTRUM):
    return SpectrumEstimator(
        FFTConfig(size=size, window="hann", normalization=normalization, remove_dc=False), FS, fc
    )


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_seed_produces_identical_samples():
    a, _ = _device(_world())
    b, _ = _device(_world())
    assert np.array_equal(a.read_iq(4096).samples, b.read_iq(4096).samples)


def test_blocks_are_independently_reproducible():
    """Block N contains the same noise regardless of what was read before it."""
    a, _ = _device(_world())
    for _ in range(3):
        a.read_iq(4096)
    fourth = a.read_iq(4096).samples

    b, _ = _device(_world())
    for _ in range(3):
        b.read_iq(4096)
    assert np.array_equal(fourth, b.read_iq(4096).samples)


def test_different_seeds_differ():
    w1 = _world()
    w2 = _world()
    w2.seed = 999
    a, _ = _device(w1)
    b, _ = _device(w2)
    assert not np.array_equal(a.read_iq(2048).samples, b.read_iq(2048).samples)


# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------


def test_noise_floor_matches_the_designed_psd():
    world = _world(reference_power_dbfs=-200.0)  # Effectively signal-free.
    device, _ = _device(world)
    spectrum = _estimator(normalization=Normalization.DENSITY).estimate(
        device.read_iq(1 << 17).samples
    )
    assert float(estimate_noise_floor(spectrum)) == pytest.approx(-130.0, abs=0.5)


def test_transmitter_power_follows_the_declared_path_loss():
    """Measured power at a distance must match the log-distance model in closed form."""
    # Exactly on a bin centre, so scalloping loss cannot blur the comparison.
    tone_hz = 433_920_000.0 + 245 * FS / 2048
    for distance_m, expected_db in [(10.0, -38.0), (20.0, -38.0 - 26.0 * np.log10(2.0))]:
        tx = VirtualTransmitter(
            tx_id="t",
            latitude=0.0,
            longitude=0.0,
            frequency_hz=tone_hz,
            reference_power_dbfs=-38.0,
            reference_distance_m=10.0,
            path_loss_exponent=2.6,
        )
        # Place the receiver `distance_m` north of the transmitter.
        offset_deg = distance_m / 110_574.0
        route = VirtualRoute([Waypoint(offset_deg, 0.0)], speed_mps=1.0)
        world = VirtualWorld([tx], route, noise_floor_dbfs_per_hz=-150.0)
        device, _ = _device(world)
        spectrum = _estimator(size=2048).estimate(device.read_iq(1 << 16).samples)
        assert spectrum.power_db.max() == pytest.approx(expected_db, abs=0.2)


def test_power_increases_as_the_receiver_approaches():
    world = compact_world()
    clock = SimClock()
    device = MockSDR(world, clock)
    device.open()
    device.set_sample_rate(FS)
    device.set_center_frequency(433_870_000.0)
    device.set_gain(24.0)
    beacon = world.transmitters[0]

    samples = []
    for _ in range(60):
        t = clock.t_sim_s
        block = device.read_iq(16384)
        spectrum = _estimator(fc=433_870_000.0, size=1024).estimate(block.samples)
        distance = world.distance_to(beacon, t)
        samples.append((distance, float(spectrum.power_db.max())))

    distances = np.array([d for d, _ in samples])
    powers = np.array([p for _, p in samples])
    # Strong negative correlation between distance and measured power.
    assert np.corrcoef(distances, powers)[0, 1] < -0.9


def test_transmitter_outside_the_captured_span_is_inaudible():
    """Real hardware cannot see outside its instantaneous bandwidth, so nor may this."""
    world = _world(reference_power_dbfs=0.0)
    world.transmitters[0] = VirtualTransmitter(
        tx_id="far", latitude=0.0, longitude=0.0, frequency_hz=440_000_000.0,
        reference_power_dbfs=0.0,
    )
    device, _ = _device(world)
    spectrum = _estimator(normalization=Normalization.DENSITY).estimate(
        device.read_iq(1 << 16).samples
    )
    assert float(estimate_noise_floor(spectrum)) == pytest.approx(-130.0, abs=0.5)
    assert spectrum.power_db.max() < -100


def test_frequency_drift_moves_the_carrier_the_right_way():
    """Phase is the integral of frequency; a naive implementation is off by 2x."""
    tx = VirtualTransmitter(
        tx_id="drift",
        latitude=0.0,
        longitude=0.0,
        frequency_hz=433_950_000.0,
        reference_power_dbfs=-30.0,
        reference_distance_m=1.0,
        drift_hz_per_s=2000.0,
    )
    world = VirtualWorld(
        [tx], VirtualRoute([Waypoint(0.0, 0.0)], 1.0), noise_floor_dbfs_per_hz=-150.0
    )
    device, clock = _device(world)
    est = _estimator(size=1024)

    first = est.estimate(device.read_iq(8192).samples)
    f0 = est.frequencies_hz[int(np.argmax(first.power_db))]
    elapsed_before = clock.t_sim_s
    for _ in range(120):
        device.read_iq(8192)
    later = est.estimate(device.read_iq(8192).samples)
    f1 = est.frequencies_hz[int(np.argmax(later.power_db))]

    elapsed = clock.t_sim_s - elapsed_before
    assert (f1 - f0) == pytest.approx(2000.0 * elapsed, rel=0.1)


def test_duty_cycled_emitter_integrates_to_less_power():
    """A 50%-duty beacon delivers half the energy of a continuous one: 3 dB down.

    Compared on *total integrated band power*, because that is the quantity duty
    cycling scales. The peak bin alone falls further, since gating also splits energy
    into sidebands either side of the carrier -- a real effect, and a good reason not
    to read a single bin as "the signal strength" of a bursty emitter.
    """
    common = dict(
        latitude=0.0, longitude=0.0, frequency_hz=433_950_000.0,
        reference_power_dbfs=-40.0, reference_distance_m=1.0,
    )
    results = {}
    for label, duty in (("continuous", 1.0), ("half", 0.5)):
        tx = VirtualTransmitter(tx_id=label, duty_cycle=duty, period_s=0.01, **common)
        world = VirtualWorld(
            [tx], VirtualRoute([Waypoint(0.0, 0.0)], 1.0), noise_floor_dbfs_per_hz=-150.0
        )
        device, _ = _device(world)
        est = _estimator(size=1024)
        total = est.linear_power(device.read_iq(1 << 16).samples).sum()
        results[label] = 10 * np.log10(total)
    assert results["continuous"] - results["half"] == pytest.approx(3.0, abs=0.5)


# ---------------------------------------------------------------------------
# Receiver behaviour
# ---------------------------------------------------------------------------


def test_gain_scales_signal_and_noise_together():
    world = _world(reference_power_dbfs=-60.0, reference_distance_m=1.0)
    low, _ = _device(world, gain=14.4)
    high, _ = _device(world, gain=25.4)
    est = _estimator(size=1024)
    a = est.estimate(low.read_iq(1 << 15).samples)
    b = est.estimate(high.read_iq(1 << 15).samples)
    delta = b.power_db.max() - a.power_db.max()
    assert delta == pytest.approx(25.4 - 14.4, abs=0.5)


def test_gain_snaps_to_a_supported_step_and_reports_what_was_applied():
    """A real tuner has discrete steps; the survey must record the applied value."""
    device, _ = _device(_world())
    applied = device.set_gain(24.0)
    assert applied != 24.0
    assert applied in device.get_capabilities().gains_db
    assert device.config.gain_db == applied


def test_strong_signal_at_high_gain_causes_overload():
    """Clipping is what receiver overload is; it must be detectable, not silent."""
    world = _world(reference_power_dbfs=0.0, reference_distance_m=1.0)
    device, _ = _device(world, gain=49.6)
    assert overload_fraction(device.read_iq(8192).samples) > 0.01


def test_agc_changes_gain_as_the_receiver_moves():
    """The reason AGC invalidates spatial comparison; pinned so the warning stays true."""
    # Strong enough that the simulated AGC is not pinned at maximum gain for the
    # whole route: it has to actually track, which is the behaviour under test.
    beacon = VirtualTransmitter(
        tx_id="beacon", latitude=0.00018, longitude=-24.99987,
        frequency_hz=433_920_000.0, reference_power_dbfs=-10.0, path_loss_exponent=2.6,
    )
    world = VirtualWorld(
        [beacon], VirtualRoute.grid(0.0, -25.0, size_m=80.0, rows=4, speed_mps=5.0)
    )
    clock = SimClock()
    device = MockSDR(world, clock)
    device.open()
    device.set_sample_rate(FS)
    device.set_center_frequency(433_870_000.0)
    device.set_gain(None)
    assert device.config.agc is True
    assert device.config.gain_db is None

    est = _estimator(fc=433_870_000.0, size=1024)
    floors = []
    for _ in range(150):  # ~40 s of the route, so the receiver really moves.
        block = device.read_iq(65536)
        floors.append(float(estimate_noise_floor(est.estimate(block.samples))))
    # Under AGC the *noise floor* moves as the receiver rescales itself, which is
    # exactly what makes two locations incomparable.
    assert np.ptp(floors) > 3.0


def test_tuning_outside_the_capability_range_is_refused():
    from spectramap.sdr.base import ConfigurationError

    device, _ = _device(_world())
    with pytest.raises(ConfigurationError, match="outside the tuning range"):
        device.set_center_frequency(10e9)


def test_sample_rate_above_the_maximum_is_refused():
    from spectramap.sdr.base import ConfigurationError

    device, _ = _device(_world())
    with pytest.raises(ConfigurationError, match="exceeds"):
        device.set_sample_rate(20e6)


# ---------------------------------------------------------------------------
# Fault injection
# ---------------------------------------------------------------------------


def test_dropped_block_is_reported_not_hidden():
    device = MockSDR(_world(), SimClock(), drop_blocks=frozenset({2}))
    device.open()
    device.set_sample_rate(FS)
    blocks = [device.read_iq(4096) for _ in range(4)]
    assert blocks[2].dropped_samples == 4096
    assert blocks[2].n_samples == 0
    assert all(b.dropped_samples == 0 for i, b in enumerate(blocks) if i != 2)


def test_device_failure_raises_a_stream_error():
    device = MockSDR(_world(), SimClock(), fail_at_block=3)
    device.open()
    device.set_sample_rate(FS)
    for _ in range(3):
        device.read_iq(1024)
    with pytest.raises(StreamError, match="simulated device failure"):
        device.read_iq(1024)


# ---------------------------------------------------------------------------
# Routes and the clock
# ---------------------------------------------------------------------------


def test_route_interpolates_between_waypoints():
    route = VirtualRoute([Waypoint(0.0, 0.0), Waypoint(0.001, 0.0)], speed_mps=10.0)
    start = route.position_at(0.0)
    mid = route.position_at(route.duration_s / 2)
    end = route.position_at(route.duration_s)
    assert start[0] == pytest.approx(0.0)
    assert mid[0] == pytest.approx(0.0005, abs=1e-5)
    assert end[0] == pytest.approx(0.001, abs=1e-6)


def test_route_dwell_holds_position():
    route = VirtualRoute(
        [Waypoint(0.0, 0.0, dwell_s=30.0), Waypoint(0.001, 0.0)], speed_mps=10.0
    )
    assert route.position_at(15.0)[0] == pytest.approx(0.0)
    assert route.speed_at(15.0) == 0.0
    assert route.speed_at(40.0) == 10.0


def test_grid_route_covers_an_area():
    route = VirtualRoute.grid(0.0, -25.0, size_m=100.0, rows=4)
    lats = [w.latitude for w in route.waypoints]
    lons = [w.longitude for w in route.waypoints]
    span_m = haversine_m(min(lats), min(lons), max(lats), max(lons))
    assert float(span_m) == pytest.approx(np.hypot(100, 100), rel=0.05)
    assert route.length_m > 400


def test_clock_advances_by_exactly_the_block_duration():
    device, clock = _device(_world())
    device.read_iq(25_000)
    assert clock.t_sim_s == pytest.approx(0.1)
    device.read_iq(25_000)
    assert clock.t_sim_s == pytest.approx(0.2)


def test_shadowing_is_spatially_correlated_not_random_per_call():
    """Walking back over the same ground must reproduce the same fade."""
    tx = VirtualTransmitter(
        tx_id="s", latitude=0.0, longitude=0.0, frequency_hz=433_950_000.0,
        shadowing_db=6.0, shadowing_scale_m=25.0,
    )
    route = VirtualRoute([Waypoint(0.0005, 0.0)], speed_mps=1.0)
    world = VirtualWorld([tx], route)
    assert world.received_power_dbfs(tx, 1.0) == world.received_power_dbfs(tx, 5.0)
