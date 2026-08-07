"""Exports to standard formats.

Every export carries its provenance: the algorithm, its parameters, the source
survey, the software version, and whether the values are MEASURED or INTERPOLATED.
A GeoJSON that omits that is a picture, not a dataset.

No proprietary formats are invented. GeoJSON for vector layers, CSV for tables, PNG
for rasters, plus a sidecar JSON world file so the PNG can be placed on a map.
GeoTIFF is the obvious later addition and needs a GDAL-class dependency, so it is
deliberately out of V0.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import numpy as np

from spectramap.interpolation.idw import InterpolatedField
from spectramap.storage.store import SurveyReader
from spectramap.types import Evidence, Provenance, QualityFlag
from spectramap.viz.colormap import apply_colormap
from spectramap.viz.png import write_png


def write_provenance(path: str | Path, provenance: Provenance) -> Path:
    """Write a provenance record next to the product it explains."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(provenance.to_dict(), indent=2), encoding="utf-8")
    return out


def export_observations_geojson(
    reader: SurveyReader,
    path: str | Path,
    band: dict[str, np.ndarray] | None = None,
    band_description: dict[str, Any] | None = None,
) -> Path:
    """Export measurement points as GeoJSON.

    These are MEASURED. Every feature carries its quality flags and how its position
    was obtained, so a consumer can filter on evidence rather than trusting the dots.
    """
    features: list[dict[str, Any]] = []
    if band is not None:
        by_id = {
            int(i): float(p)
            for i, p in zip(band["observation_id"], band["power_db"], strict=True)
        }
    else:
        by_id = {}

    for row in reader.observation_rows(with_position_only=True):
        oid = int(row["observation_id"])
        props: dict[str, Any] = {
            "observation_id": oid,
            "t_utc_ns": row["t_start_utc_ns"],
            "position_accuracy_m": row["h_accuracy_m"],
            "position_evidence": row["position_evidence"],
            "center_frequency_hz": row["center_frequency_hz"],
            "gain_db": row["gain_db"],
            "agc": bool(row["agc"]),
            "n_ffts": row["n_ffts"],
            "flags": QualityFlag.describe(row["flags"]),
            "noise_floor_db": row["noise_floor_db"],
            "peak_power_db": row["peak_power_db"],
            "peak_frequency_hz": row["peak_frequency_hz"],
            "evidence": Evidence.MEASURED.value,
            "unit": reader.power_unit.value,
            "calibrated": reader.is_calibrated,
        }
        if oid in by_id:
            props["band_power_db"] = by_id[oid]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [row["longitude"], row["latitude"]],
                },
                "properties": props,
            }
        )

    collection = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "survey_id": reader.survey_id,
            "name": reader.name,
            "layer": "observations",
            "evidence": Evidence.MEASURED.value,
            "unit": reader.power_unit.value,
            "unit_label": reader.power_unit.label,
            "calibrated": reader.is_calibrated,
            "band": band_description or {},
        },
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(collection), encoding="utf-8")
    return out


def export_field_geojson(
    field: InterpolatedField,
    path: str | Path,
    min_confidence: float = 0.0,
) -> Path:
    """Export an interpolated field as a GeoJSON grid of squares.

    Masked cells are simply absent from the output. Writing them as zero, or as the
    minimum value, would turn "we did not go there" into "it was quiet there".
    """
    half = field.resolution_m / 2.0
    features: list[dict[str, Any]] = []
    for row in range(field.north_m.size):
        for col in range(field.east_m.size):
            value = field.power_db[row, col]
            if not np.isfinite(value):
                continue
            conf = float(field.confidence[row, col])
            if conf < min_confidence:
                continue
            e, n = field.east_m[col], field.north_m[row]
            lat0, lon0, _ = field.plane.to_geodetic(e - half, n - half)
            lat1, lon1, _ = field.plane.to_geodetic(e + half, n + half)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [float(lon0), float(lat0)],
                                [float(lon1), float(lat0)],
                                [float(lon1), float(lat1)],
                                [float(lon0), float(lat1)],
                                [float(lon0), float(lat0)],
                            ]
                        ],
                    },
                    "properties": {
                        "power_db": float(value),
                        "confidence": conf,
                        "distance_to_nearest_sample_m": float(field.distance_m[row, col]),
                        "samples_within_radius": int(field.n_within_radius[row, col]),
                        "evidence": Evidence.INTERPOLATED.value,
                    },
                }
            )

    collection = {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "layer": "interpolated_field",
            "evidence": Evidence.INTERPOLATED.value,
            "coverage_fraction": field.coverage_fraction,
            "provenance": field.provenance.to_dict(),
            "note": (
                "Values are computed between measurements, not measured. Cells beyond "
                "max_distance_m from any measurement are absent from this file."
            ),
        },
    }
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(collection), encoding="utf-8")
    return out


def export_field_png(
    field: InterpolatedField,
    path: str | Path,
    colormap: str = "viridis",
    use_confidence_alpha: bool = True,
    vmin: float | None = None,
    vmax: float | None = None,
) -> Path:
    """Render an interpolated field to a PNG, with a JSON sidecar for georeferencing.

    Confidence is bound to alpha by default, so the image itself shows where the map
    is thin. The sidecar carries the bounds, the value range and the full provenance;
    a PNG alone is not a dataset and is not treated as one.
    """
    out = Path(path)
    # Row 0 of the field is the south edge; PNG row 0 is the top of the image.
    values = np.flipud(field.power_db)
    alpha = np.flipud(field.confidence) if use_confidence_alpha else 1.0
    if use_confidence_alpha:
        alpha = np.where(np.isfinite(alpha), alpha, 0.0)

    finite = field.power_db[np.isfinite(field.power_db)]
    if vmin is None:
        vmin = float(np.percentile(finite, 2)) if finite.size else 0.0
    if vmax is None:
        vmax = float(np.percentile(finite, 98)) if finite.size else 1.0

    rgba = apply_colormap(values, vmin=vmin, vmax=vmax, name=colormap, alpha=alpha)
    write_png(out, rgba)

    south, west, north, east = field.bounds()
    sidecar = {
        "image": out.name,
        "width": int(field.east_m.size),
        "height": int(field.north_m.size),
        "bounds": {"south": south, "west": west, "north": north, "east": east},
        "value_range_db": [vmin, vmax],
        "colormap": colormap,
        "alpha": "interpolation confidence" if use_confidence_alpha else "opaque",
        "evidence": Evidence.INTERPOLATED.value,
        "coverage_fraction": field.coverage_fraction,
        "provenance": field.provenance.to_dict(),
    }
    out.with_suffix(".json").write_text(json.dumps(sidecar, indent=2), encoding="utf-8")
    return out


def export_observations_csv(
    reader: SurveyReader,
    path: str | Path,
    band: dict[str, np.ndarray] | None = None,
) -> Path:
    """Export the observation index as CSV, one row per observation.

    Spectra are not included: a CSV with 4096 power columns per row is not a useful
    artefact. The ``.f32`` chunk files are the interchange path for full spectra and
    are documented in docs/survey-format.md.
    """
    by_id = (
        {int(i): float(p) for i, p in zip(band["observation_id"], band["power_db"], strict=True)}
        if band is not None
        else {}
    )
    columns = [
        "observation_id",
        "t_start_utc_ns",
        "t_end_utc_ns",
        "latitude",
        "longitude",
        "altitude_m",
        "position_accuracy_m",
        "position_evidence",
        "center_frequency_hz",
        "sample_rate_hz",
        "gain_db",
        "agc",
        "n_ffts",
        "sweep_step",
        "noise_floor_db",
        "peak_power_db",
        "peak_frequency_hz",
        "band_power_db",
        "flags",
        "power_unit",
        "calibrated",
    ]
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in reader.observation_rows():
            oid = int(row["observation_id"])
            writer.writerow(
                [
                    oid,
                    row["t_start_utc_ns"],
                    row["t_end_utc_ns"],
                    row["latitude"],
                    row["longitude"],
                    row["altitude_m"],
                    row["h_accuracy_m"],
                    row["position_evidence"],
                    row["center_frequency_hz"],
                    row["sample_rate_hz"],
                    row["gain_db"],
                    int(row["agc"]),
                    row["n_ffts"],
                    row["sweep_step"],
                    row["noise_floor_db"],
                    row["peak_power_db"],
                    row["peak_frequency_hz"],
                    by_id.get(oid, ""),
                    "|".join(QualityFlag.describe(row["flags"])),
                    reader.power_unit.value,
                    int(reader.is_calibrated),
                ]
            )
    return out


__all__ = [
    "export_field_geojson",
    "export_field_png",
    "export_observations_csv",
    "export_observations_geojson",
    "write_provenance",
]
