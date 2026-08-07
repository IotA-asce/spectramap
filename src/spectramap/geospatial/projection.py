"""Local tangent-plane projection and geodetic distance, in pure numpy.

A survey covers hundreds of metres to a few kilometres. Over that extent a local
East-North-Up tangent plane anchored at the survey origin is accurate to well under
a metre, needs no projection database, and keeps every downstream algorithm --
gridding, interpolation, hotspot estimation -- working in honest metres instead of
degrees. Working in raw degrees is the classic way to end up with a heatmap stretched
by ``1/cos(latitude)``.

The scale factors use the true WGS84 radii of curvature at the origin latitude
rather than a single spherical radius, which removes the ~0.3% error a spherical
approximation would leave.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WGS84_A = 6378137.0  # Semi-major axis, metres.
WGS84_F = 1.0 / 298.257223563  # Flattening.
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)  # First eccentricity squared.


def _radii_of_curvature(latitude_deg: float) -> tuple[float, float]:
    """Meridional (M) and prime-vertical (N) radii of curvature at a latitude."""
    phi = np.radians(latitude_deg)
    s = np.sin(phi)
    w = np.sqrt(1.0 - WGS84_E2 * s * s)
    m = WGS84_A * (1.0 - WGS84_E2) / (w**3)
    n = WGS84_A / w
    return float(m), float(n)


@dataclass(frozen=True, slots=True)
class LocalTangentPlane:
    """Converts between WGS84 geodetic coordinates and local metres.

    East is +x, North is +y, Up is +z, all in metres from the origin. Valid for
    extents of order tens of kilometres; beyond that, use a real projection library.
    """

    origin_lat: float
    origin_lon: float
    origin_alt_m: float = 0.0

    @classmethod
    def from_points(
        cls, latitudes: np.ndarray, longitudes: np.ndarray, altitudes: np.ndarray | None = None
    ) -> LocalTangentPlane:
        """Anchor a plane at the centroid of a set of points."""
        lat = np.asarray(latitudes, dtype=np.float64)
        lon = np.asarray(longitudes, dtype=np.float64)
        if lat.size == 0:
            raise ValueError("cannot build a tangent plane from zero points")
        alt = 0.0 if altitudes is None else float(np.nanmean(np.asarray(altitudes, np.float64)))
        if not np.isfinite(alt):
            alt = 0.0
        return cls(float(np.mean(lat)), float(np.mean(lon)), alt)

    @property
    def _scales(self) -> tuple[float, float]:
        """Metres per radian of latitude, and metres per radian of longitude."""
        m, n = _radii_of_curvature(self.origin_lat)
        return m, n * float(np.cos(np.radians(self.origin_lat)))

    def to_enu(
        self,
        latitude: np.ndarray | float,
        longitude: np.ndarray | float,
        altitude_m: np.ndarray | float | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Geodetic -> local metres (east, north, up)."""
        lat = np.asarray(latitude, dtype=np.float64)
        lon = np.asarray(longitude, dtype=np.float64)
        m_per_rad_lat, m_per_rad_lon = self._scales
        east = np.radians(lon - self.origin_lon) * m_per_rad_lon
        north = np.radians(lat - self.origin_lat) * m_per_rad_lat
        if altitude_m is None:
            up = np.zeros_like(north)
        else:
            up = np.asarray(altitude_m, dtype=np.float64) - self.origin_alt_m
        return east, north, up

    def to_geodetic(
        self,
        east_m: np.ndarray | float,
        north_m: np.ndarray | float,
        up_m: np.ndarray | float | None = None,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Local metres -> geodetic (latitude, longitude, altitude)."""
        east = np.asarray(east_m, dtype=np.float64)
        north = np.asarray(north_m, dtype=np.float64)
        m_per_rad_lat, m_per_rad_lon = self._scales
        lat = self.origin_lat + np.degrees(north / m_per_rad_lat)
        lon = self.origin_lon + np.degrees(east / m_per_rad_lon)
        alt = (
            np.full_like(lat, self.origin_alt_m)
            if up_m is None
            else np.asarray(up_m, dtype=np.float64) + self.origin_alt_m
        )
        return lat, lon, alt

    def to_dict(self) -> dict[str, float]:
        return {
            "origin_latitude": self.origin_lat,
            "origin_longitude": self.origin_lon,
            "origin_altitude_m": self.origin_alt_m,
        }


def haversine_m(
    lat1: np.ndarray | float,
    lon1: np.ndarray | float,
    lat2: np.ndarray | float,
    lon2: np.ndarray | float,
) -> np.ndarray | float:
    """Great-circle distance in metres on a sphere of the WGS84 mean radius.

    Good to about 0.5% worldwide, which is far better than GPS accuracy at survey
    scale. Used for route lengths and coarse neighbourhood tests; anything doing
    real geometry should project with :class:`LocalTangentPlane` first.
    """
    r = 6371008.8  # Mean radius, metres.
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dphi = p2 - p1
    dlam = np.radians(np.asarray(lon2, np.float64) - np.asarray(lon1, np.float64))
    a = np.sin(dphi / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dlam / 2) ** 2
    return 2.0 * r * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def initial_bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dlam = np.radians(lon2 - lon1)
    y = np.sin(dlam) * np.cos(p2)
    x = np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dlam)
    return float((np.degrees(np.arctan2(y, x)) + 360.0) % 360.0)


def destination(
    latitude: float, longitude: float, bearing_deg: float, distance_m: float
) -> tuple[float, float]:
    """Point reached by travelling ``distance_m`` along ``bearing_deg``."""
    r = 6371008.8
    p1 = np.radians(latitude)
    lam1 = np.radians(longitude)
    theta = np.radians(bearing_deg)
    delta = distance_m / r
    p2 = np.arcsin(np.sin(p1) * np.cos(delta) + np.cos(p1) * np.sin(delta) * np.cos(theta))
    lam2 = lam1 + np.arctan2(
        np.sin(theta) * np.sin(delta) * np.cos(p1),
        np.cos(delta) - np.sin(p1) * np.sin(p2),
    )
    return float(np.degrees(p2)), float((np.degrees(lam2) + 540.0) % 360.0 - 180.0)


def path_length_m(
    latitudes: np.ndarray, longitudes: np.ndarray, smooth_window: int = 1
) -> float:
    """Total along-track distance of a polyline, in metres.

    ``smooth_window`` applies a centred moving average to the coordinates first, and
    it is not cosmetic. Summing raw fix-to-fix distances counts positional noise as
    travel: with 2 m of per-fix scatter at 1 Hz, a walk at 1.3 m/s accumulates more
    error per step than actual movement, and the reported distance can come out two
    to three times the truth. Averaging over a few fixes suppresses the noise, which
    is uncorrelated between fixes, while preserving the motion, which is not.

    A window of 1 disables smoothing and returns the raw track length.
    """
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    if lat.size < 2:
        return 0.0
    if smooth_window > 1 and lat.size > smooth_window:
        kernel = np.ones(int(smooth_window)) / float(smooth_window)
        lat = np.convolve(lat, kernel, mode="valid")
        lon = np.convolve(lon, kernel, mode="valid")
    return float(np.sum(haversine_m(lat[:-1], lon[:-1], lat[1:], lon[1:])))


__all__ = [
    "LocalTangentPlane",
    "WGS84_A",
    "WGS84_E2",
    "WGS84_F",
    "destination",
    "haversine_m",
    "initial_bearing_deg",
    "path_length_m",
]
