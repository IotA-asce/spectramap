"""Inverse distance weighting, with an explicit confidence layer.

Interpolation is not measurement. A heatmap makes every pixel look equally real, and
the pixel 80 metres from the nearest sample is not real at all. Three mechanisms keep
that honest here:

1. **A hard mask.** Cells whose nearest sample is further than ``max_distance_m``
   get NaN and are not drawn. The map has holes where the survey had holes.
2. **A confidence field.** For every drawn cell, a 0-1 number derived from the
   distance to the nearest sample and how many samples are in range. The renderer
   binds it to opacity, so thin evidence looks thin.
3. **Provenance.** The algorithm and every parameter are recorded, so a map that
   changes because ``power`` went from 2 to 3 can be explained rather than argued
   about.

Interpolation happens on **linear power**, not on decibels. Interpolating dB averages
logarithms, which biases the field low between a strong and a weak sample.

IDW is the V0 choice because it has no fitted parameters to over-trust, cannot
overshoot beyond the range of the data, and degrades obviously rather than subtly
when the data is sparse. Kriging and Gaussian processes give better fields *and*
principled variance, and are the natural V1 upgrade; they also invite the user to
believe a smooth, confident-looking surface derived from a handful of points.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np

from spectramap.geospatial.projection import LocalTangentPlane
from spectramap.types import Evidence, Provenance, db_to_linear, linear_to_db
from spectramap.version import FORMAT_VERSION, __version__


@dataclass(frozen=True, slots=True)
class InterpolatedField:
    """A regular raster of interpolated power, plus the evidence behind each cell.

    Arrays are indexed ``[row, col]`` with row 0 at the *south* edge, so that
    ``north_m[row]`` increases with the row index. ``power_db`` is NaN wherever the
    field was masked for lack of nearby measurements.
    """

    east_m: np.ndarray  # 1-D, cell centres
    north_m: np.ndarray  # 1-D, cell centres
    power_db: np.ndarray  # 2-D (n_north, n_east), NaN where unmeasured
    confidence: np.ndarray  # 2-D, 0-1, NaN where masked
    distance_m: np.ndarray  # 2-D, distance to the nearest sample
    n_within_radius: np.ndarray  # 2-D, samples inside the search radius
    plane: LocalTangentPlane
    resolution_m: float
    provenance: Provenance

    @property
    def shape(self) -> tuple[int, int]:
        return self.power_db.shape  # type: ignore[return-value]

    @property
    def coverage_fraction(self) -> float:
        """Fraction of the raster that survived the distance mask."""
        return float(np.count_nonzero(np.isfinite(self.power_db)) / self.power_db.size)

    def bounds(self) -> tuple[float, float, float, float]:
        """(south, west, north, east) in degrees, for a map viewport."""
        half = self.resolution_m / 2.0
        lat_s, lon_w, _ = self.plane.to_geodetic(self.east_m[0] - half, self.north_m[0] - half)
        lat_n, lon_e, _ = self.plane.to_geodetic(self.east_m[-1] + half, self.north_m[-1] + half)
        return float(lat_s), float(lon_w), float(lat_n), float(lon_e)

    def cell_centers_geodetic(self) -> tuple[np.ndarray, np.ndarray]:
        """Latitude and longitude grids for every cell centre."""
        ee, nn = np.meshgrid(self.east_m, self.north_m)
        lat, lon, _ = self.plane.to_geodetic(ee, nn)
        return lat, lon

    def value_at(self, latitude: float, longitude: float) -> float:
        """Interpolated value at a point, or NaN if that point is masked."""
        east, north, _ = self.plane.to_enu(latitude, longitude)
        col = int(np.argmin(np.abs(self.east_m - float(east))))
        row = int(np.argmin(np.abs(self.north_m - float(north))))
        return float(self.power_db[row, col])

    def argmax_geodetic(self) -> tuple[float, float, float]:
        """(latitude, longitude, power_db) of the strongest interpolated cell."""
        if not np.any(np.isfinite(self.power_db)):
            raise ValueError("field is entirely masked; nothing was measured closely enough")
        flat = np.nanargmax(np.where(np.isfinite(self.power_db), self.power_db, -np.inf))
        row, col = np.unravel_index(flat, self.power_db.shape)
        lat, lon, _ = self.plane.to_geodetic(self.east_m[col], self.north_m[row])
        return float(lat), float(lon), float(self.power_db[row, col])


def idw_grid(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    power_db: np.ndarray,
    resolution_m: float = 5.0,
    power: float = 2.0,
    search_radius_m: float = 60.0,
    max_distance_m: float = 40.0,
    min_neighbours: int = 1,
    smoothing_m: float = 1.0,
    margin_m: float = 20.0,
    plane: LocalTangentPlane | None = None,
    survey_id: str = "unknown",
) -> InterpolatedField:
    """Interpolate scattered power measurements onto a regular grid.

    Parameters
    ----------
    resolution_m:
        Grid cell size. Finer than the GPS accuracy buys apparent detail and no
        information.
    power:
        IDW exponent. 2 is the conventional default; larger values make the field
        cling to the nearest sample, smaller values smooth more.
    search_radius_m:
        Samples further than this contribute nothing to a cell.
    max_distance_m:
        If the *nearest* sample is further than this, the cell is masked entirely.
        This is the honesty control: it is what puts holes in the map.
    min_neighbours:
        Cells with fewer samples in range than this are masked too.
    smoothing_m:
        Added to every distance before weighting, which prevents an infinite weight
        at a sample location and reflects that a fix is not a point but a
        few-metre blob.
    margin_m:
        How far the raster extends beyond the measured extent. Kept small, because
        every metre of margin is extrapolation waiting to be masked.
    """
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    values_db = np.asarray(power_db, dtype=np.float64)
    if not (lat.shape == lon.shape == values_db.shape):
        raise ValueError("latitude, longitude and power arrays must have the same shape")
    if lat.size == 0:
        raise ValueError("cannot interpolate an empty set of observations")
    if resolution_m <= 0:
        raise ValueError(f"resolution_m must be positive, got {resolution_m}")
    if search_radius_m <= 0:
        raise ValueError(f"search_radius_m must be positive, got {search_radius_m}")

    finite = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(values_db)
    lat, lon, values_db = lat[finite], lon[finite], values_db[finite]
    if lat.size == 0:
        raise ValueError("every observation was non-finite; nothing to interpolate")

    if plane is None:
        plane = LocalTangentPlane.from_points(lat, lon)
    sx, sy, _ = plane.to_enu(lat, lon)
    sx = np.asarray(sx, dtype=np.float64)
    sy = np.asarray(sy, dtype=np.float64)

    east_axis = _axis(sx, resolution_m, margin_m)
    north_axis = _axis(sy, resolution_m, margin_m)

    values_linear = np.asarray(db_to_linear(values_db), dtype=np.float64)

    n_north, n_east = north_axis.size, east_axis.size
    out = np.full((n_north, n_east), np.nan, dtype=np.float64)
    nearest = np.full((n_north, n_east), np.inf, dtype=np.float64)
    in_range = np.zeros((n_north, n_east), dtype=np.int32)

    # Row at a time: the full pairwise matrix would be n_cells x n_samples, which for
    # a 5 m grid over a 3 km walk is large enough to matter on a Raspberry Pi.
    dx = east_axis[:, None] - sx[None, :]  # (n_east, n_samples); independent of row.
    for row in range(n_north):
        dy = north_axis[row] - sy
        dist = np.sqrt(dx * dx + dy[None, :] * dy[None, :])
        nearest[row] = dist.min(axis=1)
        within = dist <= search_radius_m
        in_range[row] = within.sum(axis=1)

        weights = np.where(within, 1.0 / np.power(dist + smoothing_m, power), 0.0)
        totals = weights.sum(axis=1)
        usable = totals > 0
        estimate = np.full(n_east, np.nan)
        estimate[usable] = (weights[usable] @ values_linear) / totals[usable]
        out[row] = estimate

    mask = (nearest > max_distance_m) | (in_range < min_neighbours) | ~np.isfinite(out)
    with np.errstate(invalid="ignore"):
        result_db = np.where(mask, np.nan, linear_to_db(np.where(mask, 1.0, out)))

    confidence = _confidence(nearest, in_range, max_distance_m, min_neighbours)
    confidence = np.where(mask, np.nan, confidence)

    provenance = Provenance(
        product="interpolated_field",
        algorithm="inverse-distance-weighting",
        parameters={
            "power": power,
            "search_radius_m": search_radius_m,
            "max_distance_m": max_distance_m,
            "min_neighbours": min_neighbours,
            "smoothing_m": smoothing_m,
            "resolution_m": resolution_m,
            "margin_m": margin_m,
            "interpolation_domain": "linear power",
        },
        source_survey_id=survey_id,
        software_version=__version__,
        format_version=FORMAT_VERSION,
        created_utc_ns=time.time_ns(),
        evidence=Evidence.INTERPOLATED,
        inputs={"n_observations": int(lat.size)},
        notes=(
            "Values between measurement points are computed, not measured. Cells whose "
            "nearest measurement exceeds max_distance_m are masked and must not be drawn."
        ),
    )

    return InterpolatedField(
        east_m=east_axis,
        north_m=north_axis,
        power_db=result_db,
        confidence=confidence,
        distance_m=nearest,
        n_within_radius=in_range,
        plane=plane,
        resolution_m=float(resolution_m),
        provenance=provenance,
    )


def _axis(values: np.ndarray, resolution_m: float, margin_m: float) -> np.ndarray:
    lo = float(values.min()) - margin_m
    hi = float(values.max()) + margin_m
    n = max(2, int(np.ceil((hi - lo) / resolution_m)) + 1)
    return lo + np.arange(n, dtype=np.float64) * resolution_m


def _confidence(
    nearest_m: np.ndarray,
    n_within: np.ndarray,
    max_distance_m: float,
    min_neighbours: int,
) -> np.ndarray:
    """A 0-1 confidence combining proximity and support.

    Proximity falls linearly from 1 at a sample to 0 at ``max_distance_m``. Support
    saturates once a cell has several samples nearby, because the fourth neighbour
    adds much less than the second. Deliberately simple and monotonic: this number
    drives an opacity, and it must not be mistaken for a calibrated error bar.
    """
    proximity = np.clip(1.0 - nearest_m / max(max_distance_m, 1e-9), 0.0, 1.0)
    support = np.clip(n_within / max(4.0 * max(min_neighbours, 1), 1.0), 0.0, 1.0)
    return np.sqrt(proximity * np.maximum(support, 0.05))


__all__ = ["InterpolatedField", "idw_grid"]
