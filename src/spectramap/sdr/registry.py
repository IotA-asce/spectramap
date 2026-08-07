"""Driver registry: the one place that maps a driver name to an implementation.

Adding a HackRF, Airspy or SoapySDR backend means adding a module and one entry
here. No other part of SpectraMap changes.

On SoapySDR: it is the obvious long-term answer for "one API, many radios", and the
:class:`~spectramap.sdr.base.SDRDevice` interface is deliberately shaped so a
``SoapySDRDevice`` slots in as just another entry below. It is not a V0 dependency,
because pulling in a large native stack to support hardware nobody is testing with
would buy complexity now against a benefit later. See docs/adr/0002-sdr-abstraction.md.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from spectramap.sdr.base import DeviceNotFoundError, SDRDevice
from spectramap.types import DeviceInfo


def _open_mock(**kwargs: Any) -> SDRDevice:
    from spectramap.sdr.mock import MockSDR
    from spectramap.simulation.world import compact_world

    world = kwargs.pop("world", None)
    if world is None:
        # The compact scenario, not the full walking one: its 80-second route
        # completes within a demo-length survey, so `spectramap survey --driver mock`
        # produces a survey with real spatial contrast rather than a partial walk
        # that (correctly, but unhelpfully) reports no usable gradient.
        world = compact_world(seed=int(kwargs.pop("seed", 20260807)))
    kwargs.pop("seed", None)
    return MockSDR(world=world, **kwargs)


def _open_rtlsdr(**kwargs: Any) -> SDRDevice:
    from spectramap.sdr.rtlsdr import RTLSDRDevice

    kwargs.pop("world", None)
    kwargs.pop("seed", None)
    return RTLSDRDevice(**kwargs)


def _enumerate_mock() -> list[DeviceInfo]:
    return [
        DeviceInfo(
            driver="mock",
            index=0,
            serial="MOCK0001",
            manufacturer="SpectraMap",
            product="Mock SDR",
            tuner="simulated-R820T",
        )
    ]


def _enumerate_rtlsdr() -> list[DeviceInfo]:
    from spectramap.sdr.rtlsdr import enumerate_rtlsdr

    return enumerate_rtlsdr()


DRIVERS: dict[str, dict[str, Any]] = {
    "mock": {
        "open": _open_mock,
        "enumerate": _enumerate_mock,
        "description": "Deterministic simulator. No hardware required.",
        "requires": (),
    },
    "rtl-sdr": {
        "open": _open_rtlsdr,
        "enumerate": _enumerate_rtlsdr,
        "description": "RTL2832U-based receivers via pyrtlsdr.",
        "requires": ("pyrtlsdr", "librtlsdr"),
    },
}

# Aliases people will actually type.
_ALIASES = {"rtlsdr": "rtl-sdr", "rtl": "rtl-sdr", "sim": "mock", "simulator": "mock"}


def resolve_driver(name: str) -> str:
    key = name.strip().lower()
    key = _ALIASES.get(key, key)
    if key not in DRIVERS:
        raise DeviceNotFoundError(
            f"unknown driver {name!r}; available: {', '.join(sorted(DRIVERS))}"
        )
    return key


def open_device(driver: str, **kwargs: Any) -> SDRDevice:
    """Construct (but do not open) a device for ``driver``."""
    key = resolve_driver(driver)
    factory: Callable[..., SDRDevice] = DRIVERS[key]["open"]
    return factory(**kwargs)


def enumerate_devices(driver: str | None = None) -> list[DeviceInfo]:
    """List available devices, for one driver or for all of them."""
    keys = [resolve_driver(driver)] if driver else list(DRIVERS)
    found: list[DeviceInfo] = []
    for key in keys:
        try:
            found.extend(DRIVERS[key]["enumerate"]())
        except Exception:
            # A driver whose native library is missing must not stop the others
            # from being listed.
            continue
    return found


def describe_drivers() -> list[dict[str, Any]]:
    """Driver names, descriptions and their native requirements, for the CLI."""
    return [
        {"driver": name, "description": spec["description"], "requires": list(spec["requires"])}
        for name, spec in sorted(DRIVERS.items())
    ]


__all__ = ["DRIVERS", "describe_drivers", "enumerate_devices", "open_device", "resolve_driver"]
