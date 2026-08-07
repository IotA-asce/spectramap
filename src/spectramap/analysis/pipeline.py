"""Survey -> spatial reconstruction, as one reproducible call.

Everything this produces is written under ``processing/`` inside the survey, next to
a provenance record. Re-running with different interpolation settings produces a
different product and a different record, and the difference is explicable rather
than mysterious.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from spectramap.analysis.hotspot import HotspotEstimate, estimate_hotspot
from spectramap.analysis.summary import SurveySummary, summarise
from spectramap.geospatial.aggregate import GridStats, aggregate_to_grid
from spectramap.interpolation.idw import InterpolatedField, idw_grid
from spectramap.storage.store import SurveyReader
from spectramap.types import Evidence, Provenance, QualityFlag
from spectramap.version import FORMAT_VERSION, __version__
from spectramap.viz.export import (
    export_field_geojson,
    export_field_png,
    export_observations_csv,
    export_observations_geojson,
    write_provenance,
)


@dataclass(frozen=True, slots=True)
class BandSelection:
    """The frequency band a reconstruction was made from.

    ``method`` is part of the identity of the result, not a footnote: peak power and
    mean power over the same band produce different maps that answer different
    questions.
    """

    f_low_hz: float
    f_high_hz: float
    method: str

    @property
    def center_hz(self) -> float:
        return (self.f_low_hz + self.f_high_hz) / 2.0

    @property
    def width_hz(self) -> float:
        return self.f_high_hz - self.f_low_hz

    @property
    def label(self) -> str:
        if self.width_hz <= 0:
            return f"{self.center_hz / 1e6:.5f} MHz ({self.method})"
        return (
            f"{self.f_low_hz / 1e6:.5f}-{self.f_high_hz / 1e6:.5f} MHz ({self.method})"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "f_low_hz": self.f_low_hz,
            "f_high_hz": self.f_high_hz,
            "center_hz": self.center_hz,
            "width_hz": self.width_hz,
            "method": self.method,
            "label": self.label,
        }

    @classmethod
    def around(cls, center_hz: float, width_hz: float, method: str = "peak") -> BandSelection:
        half = width_hz / 2.0
        return cls(center_hz - half, center_hz + half, method)


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """A complete spatial reconstruction of one band, plus its evidence trail."""

    summary: SurveySummary
    band: BandSelection
    measurements: dict[str, np.ndarray]
    grid: GridStats
    field: InterpolatedField
    hotspot: HotspotEstimate | None
    provenance: Provenance
    excluded_observations: int
    notes: list[str]

    @property
    def n_measurements(self) -> int:
        return int(self.measurements["power_db"].size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "survey": self.summary.to_dict(),
            "band": self.band.to_dict(),
            "n_measurements": self.n_measurements,
            "excluded_observations": self.excluded_observations,
            "measured_power_db": {
                "min": float(self.measurements["power_db"].min()) if self.n_measurements else None,
                "max": float(self.measurements["power_db"].max()) if self.n_measurements else None,
                "range": float(np.ptp(self.measurements["power_db"]))
                if self.n_measurements
                else None,
            },
            "grid": self.grid.to_dict(),
            "field": {
                "resolution_m": self.field.resolution_m,
                "shape": list(self.field.shape),
                "coverage_fraction": self.field.coverage_fraction,
                "provenance": self.field.provenance.to_dict(),
            },
            "hotspot": self.hotspot.to_dict() if self.hotspot else None,
            "provenance": self.provenance.to_dict(),
            "notes": self.notes,
        }


# Observations carrying any of these are excluded from spatial products by default.
# They are never deleted; the raw record keeps them, and the exclusion count is
# reported so the user can see what was set aside and why.
DEFAULT_EXCLUDE = (
    QualityFlag.NO_LOCATION
    | QualityFlag.SDR_OVERLOAD
    | QualityFlag.GPS_LOW_ACCURACY
    | QualityFlag.DROPPED_SAMPLES
)


def reconstruct(
    reader: SurveyReader,
    band: BandSelection,
    resolution_m: float = 5.0,
    cell_size_m: float = 10.0,
    idw_power: float = 2.0,
    search_radius_m: float = 60.0,
    max_distance_m: float = 40.0,
    min_neighbours: int = 1,
    exclude_flags: QualityFlag = DEFAULT_EXCLUDE,
    estimate_source_region: bool = True,
    sweep_step: int | None = None,
) -> Reconstruction:
    """Select a band, aggregate it spatially, interpolate it, and grade the result."""
    summary = summarise(reader)
    total_positioned = len(reader.observation_rows(with_position_only=True))

    measurements = reader.band_power(
        band.f_low_hz,
        band.f_high_hz,
        method=band.method,
        with_position_only=True,
        exclude_flags=exclude_flags,
    )
    n = int(measurements["power_db"].size)
    if n == 0:
        raise ValueError(
            f"no usable observations cover {band.label}. The survey measured "
            f"{_describe_coverage(reader)}; check the requested frequency, or relax "
            "the quality filter with --include-flagged."
        )

    notes: list[str] = []
    excluded = total_positioned - n
    if excluded > 0:
        notes.append(
            f"{excluded} positioned observation(s) excluded by quality filter "
            f"({'|'.join(QualityFlag.describe(exclude_flags))}); they remain in the survey."
        )
    if summary.agc:
        notes.append(
            "This survey used automatic gain. Power values are NOT comparable between "
            "locations and no spatial conclusion should be drawn from them."
        )
    if summary.gain_db is None and not summary.agc:
        notes.append(
            "Gain was not constant across the survey; power comparison between "
            "locations spanning a gain change is invalid."
        )

    grid = aggregate_to_grid(
        measurements["latitude"], measurements["longitude"], measurements["power_db"], cell_size_m
    )

    field = idw_grid(
        measurements["latitude"],
        measurements["longitude"],
        measurements["power_db"],
        resolution_m=resolution_m,
        power=idw_power,
        search_radius_m=search_radius_m,
        max_distance_m=max_distance_m,
        min_neighbours=min_neighbours,
        survey_id=reader.survey_id,
    )

    hotspot: HotspotEstimate | None = None
    if estimate_source_region and n >= 3:
        hotspot = estimate_hotspot(
            measurements["latitude"],
            measurements["longitude"],
            measurements["power_db"],
            survey_id=reader.survey_id,
            position_accuracy_m=summary.median_accuracy_m,
        )

    provenance = Provenance(
        product="reconstruction",
        algorithm="band-select -> grid-aggregate -> idw",
        parameters={
            "band": band.to_dict(),
            "cell_size_m": cell_size_m,
            "resolution_m": resolution_m,
            "idw_power": idw_power,
            "search_radius_m": search_radius_m,
            "max_distance_m": max_distance_m,
            "min_neighbours": min_neighbours,
            "excluded_flags": QualityFlag.describe(exclude_flags),
            "sweep_step": sweep_step,
        },
        source_survey_id=reader.survey_id,
        software_version=__version__,
        format_version=FORMAT_VERSION,
        created_utc_ns=time.time_ns(),
        evidence=Evidence.DERIVED,
        inputs={"n_observations_used": n, "n_observations_excluded": excluded},
    )

    return Reconstruction(
        summary=summary,
        band=band,
        measurements=measurements,
        grid=grid,
        field=field,
        hotspot=hotspot,
        provenance=provenance,
        excluded_observations=excluded,
        notes=notes,
    )


def write_products(
    reader: SurveyReader,
    reconstruction: Reconstruction,
    out_dir: str | Path | None = None,
    colormap: str = "viridis",
) -> dict[str, Path]:
    """Write every product for a reconstruction, plus its provenance.

    Defaults to ``<survey>/processing/<band-slug>/`` so products travel with the
    survey they came from and never lose their link to it.
    """
    slug = _slug(reconstruction.band)
    directory = Path(out_dir) if out_dir else reader.path / "processing" / slug
    directory.mkdir(parents=True, exist_ok=True)

    outputs = {
        "observations_geojson": export_observations_geojson(
            reader,
            directory / "observations.geojson",
            band=reconstruction.measurements,
            band_description=reconstruction.band.to_dict(),
        ),
        "observations_csv": export_observations_csv(
            reader, directory / "observations.csv", band=reconstruction.measurements
        ),
        "field_geojson": export_field_geojson(
            reconstruction.field, directory / "heatmap.geojson"
        ),
        "field_png": export_field_png(
            reconstruction.field, directory / "heatmap.png", colormap=colormap
        ),
        "provenance": write_provenance(
            directory / "provenance.json", reconstruction.provenance
        ),
    }

    (directory / "reconstruction.json").write_text(
        json.dumps(reconstruction.to_dict(), indent=2), encoding="utf-8"
    )
    outputs["reconstruction"] = directory / "reconstruction.json"
    return outputs


def _slug(band: BandSelection) -> str:
    return (
        f"{band.center_hz / 1e6:.5f}MHz".replace(".", "_")
        + f"-w{band.width_hz / 1e3:.1f}kHz".replace(".", "_")
        + f"-{band.method}"
    )


def _describe_coverage(reader: SurveyReader) -> str:
    spans = reader.frequency_coverage()
    if not spans:
        return "nothing"
    return ", ".join(f"{lo / 1e6:.4f}-{hi / 1e6:.4f} MHz" for lo, hi in spans)


__all__ = [
    "DEFAULT_EXCLUDE",
    "BandSelection",
    "Reconstruction",
    "reconstruct",
    "write_products",
]
