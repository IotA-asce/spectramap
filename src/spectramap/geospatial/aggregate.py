"""Spatial aggregation of observations into cells.

Several observations usually fall inside one square metre or two of ground -- a
surveyor pauses, or the GPS wanders. Collapsing them to a single number is necessary
but is also where information gets destroyed, so every cell keeps the full set of
summary statistics rather than just a mean.

Averaging is done in the **linear power domain**. Averaging decibels computes a
geometric mean, which systematically under-reports a cell containing one strong
sample among weak ones -- exactly the cell a user is looking for.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from spectramap.geospatial.projection import LocalTangentPlane
from spectramap.types import Evidence, db_to_linear, linear_to_db


@dataclass(frozen=True, slots=True)
class GridCell:
    """One aggregated cell. ``spread_db`` is what stops a mean from lying."""

    ix: int
    iy: int
    east_m: float
    north_m: float
    latitude: float
    longitude: float
    mean_db: float
    median_db: float
    min_db: float
    max_db: float
    std_db: float
    count: int

    @property
    def spread_db(self) -> float:
        """Max minus min: the visible range of measurements inside this cell."""
        return self.max_db - self.min_db

    def to_dict(self) -> dict[str, object]:
        return {
            "ix": self.ix,
            "iy": self.iy,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "mean_db": self.mean_db,
            "median_db": self.median_db,
            "min_db": self.min_db,
            "max_db": self.max_db,
            "std_db": self.std_db,
            "spread_db": self.spread_db,
            "count": self.count,
            "evidence": Evidence.DERIVED.value,
        }


@dataclass(frozen=True, slots=True)
class GridStats:
    """The result of aggregating a survey's observations onto a regular grid."""

    cells: list[GridCell]
    cell_size_m: float
    plane: LocalTangentPlane
    n_observations: int

    @property
    def n_cells(self) -> int:
        return len(self.cells)

    def as_arrays(self) -> dict[str, np.ndarray]:
        return {
            "latitude": np.array([c.latitude for c in self.cells]),
            "longitude": np.array([c.longitude for c in self.cells]),
            "east_m": np.array([c.east_m for c in self.cells]),
            "north_m": np.array([c.north_m for c in self.cells]),
            "mean_db": np.array([c.mean_db for c in self.cells]),
            "max_db": np.array([c.max_db for c in self.cells]),
            "std_db": np.array([c.std_db for c in self.cells]),
            "count": np.array([c.count for c in self.cells]),
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_size_m": self.cell_size_m,
            "n_cells": self.n_cells,
            "n_observations": self.n_observations,
            "plane": self.plane.to_dict(),
            "evidence": Evidence.DERIVED.value,
        }


def aggregate_to_grid(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    power_db: np.ndarray,
    cell_size_m: float = 10.0,
    plane: LocalTangentPlane | None = None,
) -> GridStats:
    """Bin observations into square cells and summarise each one.

    ``cell_size_m`` should not be smaller than the GPS accuracy. A 1-metre grid built
    from fixes with 3-metre error produces detail that is pure noise, and it looks
    convincing, which makes it worse than a coarse grid that looks uncertain.
    """
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    power = np.asarray(power_db, dtype=np.float64)
    if not (lat.shape == lon.shape == power.shape):
        raise ValueError(
            f"latitude {lat.shape}, longitude {lon.shape} and power {power.shape} "
            "must have the same shape"
        )
    if cell_size_m <= 0:
        raise ValueError(f"cell_size_m must be positive, got {cell_size_m}")

    if lat.size == 0:
        # No measurements is a legitimate outcome (everything filtered out), not an
        # error -- but there is no centroid to anchor a plane on, so check first.
        return GridStats([], cell_size_m, plane or LocalTangentPlane(0.0, 0.0), 0)
    if plane is None:
        plane = LocalTangentPlane.from_points(lat, lon)

    east, north, _ = plane.to_enu(lat, lon)
    ix = np.floor(east / cell_size_m).astype(np.int64)
    iy = np.floor(north / cell_size_m).astype(np.int64)

    linear = np.asarray(db_to_linear(power), dtype=np.float64)
    cells: list[GridCell] = []
    # Group by cell index using a lexicographic sort: no dict of lists, no O(n^2).
    order = np.lexsort((iy, ix))
    ix_s, iy_s = ix[order], iy[order]
    boundaries = np.flatnonzero((np.diff(ix_s) != 0) | (np.diff(iy_s) != 0)) + 1
    groups = np.split(order, boundaries)

    for group in groups:
        if group.size == 0:
            continue
        cx, cy = int(ix[group[0]]), int(iy[group[0]])
        values_linear = linear[group]
        values_db = power[group]
        mean_db = float(linear_to_db(values_linear.mean()))
        centre_e = (cx + 0.5) * cell_size_m
        centre_n = (cy + 0.5) * cell_size_m
        clat, clon, _ = plane.to_geodetic(centre_e, centre_n)
        cells.append(
            GridCell(
                ix=cx,
                iy=cy,
                east_m=centre_e,
                north_m=centre_n,
                latitude=float(clat),
                longitude=float(clon),
                mean_db=mean_db,
                median_db=float(np.median(values_db)),
                min_db=float(values_db.min()),
                max_db=float(values_db.max()),
                # Spread is reported in dB of the dB values: a plain description of
                # how much the readings in this cell disagreed.
                std_db=float(values_db.std(ddof=1)) if values_db.size > 1 else 0.0,
                count=int(group.size),
            )
        )

    return GridStats(cells, float(cell_size_m), plane, int(lat.size))


__all__ = ["GridCell", "GridStats", "aggregate_to_grid"]
