"""Where is the signal strongest? -- with an uncertainty that is not decoration.

Scope
-----
This is **not** source localisation, which is a V3 milestone. It answers a smaller
and more defensible question: given these measurements, which region of the surveyed
area carries the most energy at this frequency, and how well constrained is that
region?

The distinction matters because received signal strength is a bad ranging
observable. Multipath, obstruction, antenna orientation, body shadowing, terrain and
receiver gain all move RSSI by more than distance does over short baselines. A
pipeline that reports a transmitter's coordinates to six decimal places from RSSI is
reporting arithmetic, not evidence.

Method
------
1. Keep the strongest ``top_fraction`` of positioned measurements.
2. Take their power-weighted centroid, weighting on *linear* power so that one strong
   sample counts as much as its energy warrants.
3. Bootstrap-resample those measurements many times, recomputing the centroid each
   time. The spread of the resampled centroids is the uncertainty radius.

The bootstrap radius captures uncertainty from sampling and measurement scatter. It
does **not** capture systematic error -- a reflector that makes the signal look
strongest on the wrong side of a building biases every resample identically. That is
why the result is labelled ESTIMATED and why the survey geometry is reported
alongside: an estimate from a single straight walk is not constrained across-track,
whatever the radius says.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from spectramap.geospatial.projection import LocalTangentPlane
from spectramap.types import Evidence, Provenance, db_to_linear
from spectramap.version import FORMAT_VERSION, __version__


@dataclass(frozen=True, slots=True)
class HotspotEstimate:
    """A probable strongest-signal region. Never a transmitter coordinate."""

    latitude: float
    longitude: float
    radius_m: float
    confidence: str  # "low" | "moderate" | "good"
    n_observations: int
    n_used: int
    peak_power_db: float
    power_range_db: float
    geometry_score: float  # 0-1: how well the route surrounds the estimate.
    provenance: Provenance
    caveats: list[str]
    evidence: Evidence = Evidence.ESTIMATED

    def describe(self) -> str:
        """A deliberately hedged one-line rendering, used by the CLI and the UI."""
        return (
            f"Probable strongest-signal region near {self.latitude:.5f}, "
            f"{self.longitude:.5f} (estimated radius {self.radius_m:.0f} m, "
            f"confidence: {self.confidence})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_m": self.radius_m,
            "confidence": self.confidence,
            "n_observations": self.n_observations,
            "n_used": self.n_used,
            "peak_power_db": self.peak_power_db,
            "power_range_db": self.power_range_db,
            "geometry_score": self.geometry_score,
            "evidence": self.evidence.value,
            "caveats": self.caveats,
            "provenance": self.provenance.to_dict(),
            "description": self.describe(),
        }


def estimate_hotspot(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    power_db: np.ndarray,
    top_fraction: float = 0.2,
    n_bootstrap: int = 400,
    seed: int = 12345,
    survey_id: str = "unknown",
    weight_exponent: float = 1.0,
    position_accuracy_m: float | None = None,
) -> HotspotEstimate:
    """Estimate the region of strongest measured signal, with an uncertainty radius.

    Parameters
    ----------
    top_fraction:
        Fraction of the strongest measurements to use. Using everything drags the
        centroid towards wherever the surveyor happened to spend time; using too few
        makes the estimate a single noisy sample.
    weight_exponent:
        Exponent applied to linear power when weighting. 1.0 weights by energy.
        Higher values concentrate the estimate on the strongest few points.
    position_accuracy_m:
        Typical positional uncertainty of the measurements. The returned radius is
        floored at this value: an estimate cannot be more certain about where a
        signal was than the survey was about where *it* was.
    """
    lat = np.asarray(latitudes, dtype=np.float64)
    lon = np.asarray(longitudes, dtype=np.float64)
    power = np.asarray(power_db, dtype=np.float64)
    finite = np.isfinite(lat) & np.isfinite(lon) & np.isfinite(power)
    lat, lon, power = lat[finite], lon[finite], power[finite]
    if lat.size < 3:
        raise ValueError(
            f"need at least 3 positioned observations to estimate a region, got {lat.size}"
        )
    if not 0.0 < top_fraction <= 1.0:
        raise ValueError(f"top_fraction must be in (0, 1], got {top_fraction}")

    plane = LocalTangentPlane.from_points(lat, lon)
    east, north, _ = plane.to_enu(lat, lon)
    east = np.asarray(east, dtype=np.float64)
    north = np.asarray(north, dtype=np.float64)

    n_used = max(3, int(round(lat.size * top_fraction)))
    order = np.argsort(power)[::-1][:n_used]
    ex, ny, pw = east[order], north[order], power[order]

    # Weight on power above the selection floor, so the weighting responds to
    # contrast within the selected set rather than to the arbitrary absolute level.
    relative_db = pw - pw.min()
    weights = np.power(np.asarray(db_to_linear(relative_db), dtype=np.float64), weight_exponent)

    cx, cy = _weighted_centroid(ex, ny, weights)

    rng = np.random.default_rng(seed)
    samples = np.empty((n_bootstrap, 2), dtype=np.float64)
    for i in range(n_bootstrap):
        pick = rng.integers(0, n_used, n_used)
        samples[i] = _weighted_centroid(ex[pick], ny[pick], weights[pick])
    # 68th percentile of the resampled displacement: a 1-sigma-equivalent radius that
    # makes no assumption of a Gaussian.
    displacement = np.hypot(samples[:, 0] - cx, samples[:, 1] - cy)
    radius_m = float(np.percentile(displacement, 68.0))

    geometry = _geometry_score(east - cx, north - cy)
    power_range = float(power.max() - power.min())

    # The bootstrap measures only how much the answer moves when the *sample* is
    # resampled. It is blind to two floors that are always present, and reporting a
    # radius below either of them is precision the data cannot support:
    #   - measurements no closer together than their own spacing cannot resolve
    #     structure finer than that spacing;
    #   - and nothing can be located more accurately than the positions were known.
    bootstrap_radius_m = radius_m
    spacing_floor_m = _median_spacing(east, north) / 2.0
    accuracy_floor_m = float(position_accuracy_m or 0.0)
    radius_m = max(radius_m, spacing_floor_m, accuracy_floor_m)

    confidence, caveats = _grade(
        geometry_score=geometry,
        power_range_db=power_range,
        n_used=n_used,
        radius_m=radius_m,
    )

    lat_c, lon_c, _ = plane.to_geodetic(cx, cy)
    provenance = Provenance(
        product="hotspot_estimate",
        algorithm="power-weighted-centroid-with-bootstrap",
        parameters={
            "top_fraction": top_fraction,
            "n_bootstrap": n_bootstrap,
            "weight_exponent": weight_exponent,
            "seed": seed,
            "radius_definition": (
                "max of: 68th percentile of bootstrap centroid displacement, half the "
                "median measurement spacing, and the median position accuracy"
            ),
            "bootstrap_radius_m": bootstrap_radius_m,
            "spacing_floor_m": spacing_floor_m,
            "position_accuracy_floor_m": accuracy_floor_m,
        },
        source_survey_id=survey_id,
        software_version=__version__,
        format_version=FORMAT_VERSION,
        created_utc_ns=time.time_ns(),
        evidence=Evidence.ESTIMATED,
        inputs={"n_observations": int(lat.size), "n_used": n_used},
        notes=(
            "Region of strongest measured signal. NOT a transmitter position. RSSI-based "
            "inference is biased by multipath, obstruction and antenna pattern; the radius "
            "reflects sampling scatter only, not systematic error."
        ),
    )

    return HotspotEstimate(
        latitude=float(lat_c),
        longitude=float(lon_c),
        radius_m=radius_m,
        confidence=confidence,
        n_observations=int(lat.size),
        n_used=n_used,
        peak_power_db=float(power.max()),
        power_range_db=power_range,
        geometry_score=geometry,
        provenance=provenance,
        caveats=caveats,
    )


def _weighted_centroid(x: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    total = float(w.sum())
    if total <= 0:
        return float(x.mean()), float(y.mean())
    return float((x * w).sum() / total), float((y * w).sum() / total)


def _median_spacing(east: np.ndarray, north: np.ndarray, cap: int = 400) -> float:
    """Median nearest-neighbour distance between measurements, in metres."""
    n = east.size
    if n < 2:
        return 0.0
    if n > cap:
        idx = np.linspace(0, n - 1, cap).astype(int)
        east, north = east[idx], north[idx]
    dx = east[:, None] - east[None, :]
    dy = north[:, None] - north[None, :]
    dist = np.hypot(dx, dy)
    np.fill_diagonal(dist, np.inf)
    return float(np.median(dist.min(axis=1)))


def _geometry_score(dx: np.ndarray, dy: np.ndarray) -> float:
    """How well the measurements surround the estimate, from 0 to 1.

    Computed as one minus the length of the mean unit bearing vector. Measurements
    spread evenly around a point give ~1; measurements all on one side (a single
    straight walk past the source) give ~0, correctly flagging that the estimate is
    unconstrained in the across-track direction even if the bootstrap looks tight.
    """
    r = np.hypot(dx, dy)
    usable = r > 1e-6
    if np.count_nonzero(usable) < 3:
        return 0.0
    ux = dx[usable] / r[usable]
    uy = dy[usable] / r[usable]
    resultant = np.hypot(ux.mean(), uy.mean())
    return float(np.clip(1.0 - resultant, 0.0, 1.0))


def _grade(
    geometry_score: float, power_range_db: float, n_used: int, radius_m: float
) -> tuple[str, list[str]]:
    """Turn the diagnostics into a coarse grade plus the reasons behind it."""
    caveats: list[str] = [
        "Estimated from received signal strength only. Multipath, obstructions and "
        "antenna orientation can move the apparent strongest region by tens of metres.",
        "This marks where the signal was strongest, not where a transmitter is.",
    ]
    score = 0
    if geometry_score >= 0.6:
        score += 1
    else:
        caveats.append(
            f"Survey geometry is one-sided (score {geometry_score:.2f}); the estimate is "
            "poorly constrained across the direction of travel. Walk a loop or a grid "
            "around the area to improve it."
        )
    if power_range_db >= 10.0:
        score += 1
    else:
        caveats.append(
            f"Only {power_range_db:.1f} dB of variation across the survey. Without a clear "
            "spatial gradient there is little to localise; the source may be far outside "
            "the surveyed area, or absent."
        )
    if n_used >= 20:
        score += 1
    else:
        caveats.append(f"Only {n_used} measurements contributed to the estimate.")

    confidence = ("low", "low", "moderate", "good")[score]

    # Dynamic range is a gate, not one vote among three. With no spatial gradient the
    # centroid is just the centre of wherever the surveyor walked, and a tight
    # bootstrap radius around that number is precision without accuracy -- the exact
    # false confidence this whole module exists to avoid.
    if power_range_db < 6.0:
        confidence = "low"
        caveats.insert(
            0,
            f"NOT USABLE as a location: the measurements span only {power_range_db:.1f} dB, "
            "which is consistent with no detectable spatial gradient at all. This point is "
            "close to the centroid of the survey route, not of a signal.",
        )
    # A radius far smaller than the localisation baseline is not credible for RSSI.
    if radius_m < 5.0 and confidence != "good":
        caveats.append(
            f"The {radius_m:.0f} m radius reflects sampling scatter only. Systematic errors "
            "from multipath and obstruction are typically tens of metres and are not "
            "included in it."
        )
    return confidence, caveats


__all__ = ["HotspotEstimate", "estimate_hotspot"]
