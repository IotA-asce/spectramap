"""Position source abstraction.

Position arrives on its own timeline, at its own rate (typically 1 Hz for a consumer
GPS), and is never sampled at exactly the instant an RF integration window is
centred. The recorder therefore does not ask "where am I now?" -- it collects fixes
into a :class:`~spectramap.positioning.track.PositionTrack` and asks "where was I at
this monotonic timestamp?", getting back an answer that says how it was obtained.

Providers are polled, not callback-driven. Polling keeps the recorder single
threaded, makes the simulated provider exactly reproducible, and means a provider
that stalls cannot wedge acquisition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from spectramap.types import Position


class PositionError(RuntimeError):
    """Base class for position-source failures."""


class PositionUnavailable(PositionError):
    """The source is present but has no usable fix (no satellites, no lock)."""


class PositionProvider(ABC):
    """A source of timestamped position fixes."""

    @abstractmethod
    def start(self) -> None:
        """Begin acquiring. Idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop acquiring and release resources. Idempotent."""

    @property
    @abstractmethod
    def is_running(self) -> bool: ...

    @abstractmethod
    def poll(self) -> list[Position]:
        """Return fixes that have become available since the last call.

        Must not block. An empty list is a normal, expected result: a 1 Hz GPS says
        nothing at all for most of any given second.
        """

    @abstractmethod
    def describe(self) -> dict[str, object]:
        """Provider identity and settings, for the survey manifest."""

    def get_position(self) -> Position | None:
        """Most recent fix, if any. Convenience for status displays."""
        fixes = self.poll()
        return fixes[-1] if fixes else None

    def __enter__(self) -> PositionProvider:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


__all__ = ["PositionError", "PositionProvider", "PositionUnavailable"]
