"""Geospatial support: local projection, gridding, spatial aggregation."""

from spectramap.geospatial.aggregate import GridCell, GridStats, aggregate_to_grid
from spectramap.geospatial.projection import (
    WGS84_A,
    WGS84_F,
    LocalTangentPlane,
    destination,
    haversine_m,
    initial_bearing_deg,
    path_length_m,
)

__all__ = [
    "GridCell",
    "GridStats",
    "LocalTangentPlane",
    "WGS84_A",
    "WGS84_F",
    "aggregate_to_grid",
    "destination",
    "haversine_m",
    "initial_bearing_deg",
    "path_length_m",
]
