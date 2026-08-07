"""Survey summaries: what was recorded, how good it is, and what to distrust.

A summary that only reports counts and a peak is a marketing document. This one leads
with the things that would invalidate a conclusion: how much of the survey is flagged,
whether gain was held fixed, how accurate the positions were, and how much of the
route actually carries usable data.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from spectramap.geospatial.projection import path_length_m
from spectramap.storage.store import SurveyReader
from spectramap.types import PowerUnit, QualityFlag, mean_power_db


@dataclass(frozen=True, slots=True)
class SurveySummary:
    name: str
    survey_id: str
    n_observations: int
    n_positioned: int
    duration_s: float
    distance_m: float
    frequency_spans_hz: list[tuple[float, float]]
    power_unit: PowerUnit
    calibrated: bool
    flag_counts: dict[str, int]
    median_accuracy_m: float | None
    worst_accuracy_m: float | None
    gain_db: float | None
    agc: bool
    noise_floor_db: float | None
    peak_power_db: float | None
    peak_frequency_hz: float | None
    warnings: list[str] = field(default_factory=list)
    events: dict[str, int] = field(default_factory=dict)

    @property
    def positioned_fraction(self) -> float:
        return self.n_positioned / self.n_observations if self.n_observations else 0.0

    @property
    def flagged_fraction(self) -> float:
        flagged = sum(self.flag_counts.values())
        return min(1.0, flagged / self.n_observations) if self.n_observations else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "survey_id": self.survey_id,
            "observations": self.n_observations,
            "observations_with_position": self.n_positioned,
            "positioned_fraction": self.positioned_fraction,
            "duration_s": self.duration_s,
            "distance_m": self.distance_m,
            "frequency_spans_hz": [list(s) for s in self.frequency_spans_hz],
            "power_unit": self.power_unit.value,
            "power_unit_label": self.power_unit.label,
            "calibrated": self.calibrated,
            "flag_counts": self.flag_counts,
            "median_position_accuracy_m": self.median_accuracy_m,
            "worst_position_accuracy_m": self.worst_accuracy_m,
            "gain_db": self.gain_db,
            "agc": self.agc,
            "noise_floor_db": self.noise_floor_db,
            "peak_power_db": self.peak_power_db,
            "peak_frequency_hz": self.peak_frequency_hz,
            "warnings": self.warnings,
            "events": self.events,
        }


def summarise(reader: SurveyReader) -> SurveySummary:
    """Compute a summary from the observation index alone; spectra are not read."""
    rows = reader.observation_rows()
    n = len(rows)

    positioned = [r for r in rows if r["latitude"] is not None]
    accuracies = [
        r["h_accuracy_m"] for r in positioned if r["h_accuracy_m"] is not None
    ]

    flag_counts: Counter[str] = Counter()
    for r in rows:
        for name in QualityFlag.describe(r["flags"]):
            flag_counts[name] += 1

    duration_s = 0.0
    if n:
        duration_s = (rows[-1]["t_end_utc_ns"] - rows[0]["t_start_utc_ns"]) / 1e9

    fixes = reader.positions()
    distance_m = 0.0
    if len(fixes) >= 2:
        # Smoothed: a raw fix-to-fix sum counts GPS scatter as distance travelled.
        distance_m = path_length_m(
            np.array([f.latitude for f in fixes]),
            np.array([f.longitude for f in fixes]),
            smooth_window=5,
        )

    noise_floors = [r["noise_floor_db"] for r in rows if r["noise_floor_db"] is not None]
    peaks = [(r["peak_power_db"], r["peak_frequency_hz"]) for r in rows if r["peak_power_db"]]
    best = max(peaks, default=(None, None))

    gains = {r["gain_db"] for r in rows}
    agc = any(r["agc"] for r in rows)

    event_counts: Counter[str] = Counter()
    for event in reader.events():
        event_counts[f"{event['level']}:{event['kind']}"] += 1

    return SurveySummary(
        name=reader.name,
        survey_id=reader.survey_id,
        n_observations=n,
        n_positioned=len(positioned),
        duration_s=duration_s,
        distance_m=distance_m,
        frequency_spans_hz=reader.frequency_coverage(),
        power_unit=reader.power_unit,
        calibrated=reader.is_calibrated,
        flag_counts=dict(flag_counts),
        median_accuracy_m=float(np.median(accuracies)) if accuracies else None,
        worst_accuracy_m=float(np.max(accuracies)) if accuracies else None,
        # A single gain means the survey is internally comparable; more than one
        # means it is not, and the summary must say so rather than pick the mode.
        gain_db=next(iter(gains)) if len(gains) == 1 else None,
        agc=agc,
        noise_floor_db=float(mean_power_db(np.array(noise_floors))) if noise_floors else None,
        peak_power_db=best[0],
        peak_frequency_hz=best[1],
        warnings=reader.warnings,
        events=dict(event_counts),
    )


def spectrum_at_observation(reader: SurveyReader, observation_id: int) -> dict[str, Any]:
    """One observation's spectrum plus everything needed to interpret it.

    Backs the spectrum explorer: click a point on the map, see what was measured
    there, with the units, the receiver settings and the quality flags attached.
    """
    row = next(
        (r for r in reader.observation_rows() if r["observation_id"] == observation_id), None
    )
    if row is None:
        raise KeyError(f"no observation {observation_id}")
    spectrum = reader.spectrum(observation_id)
    return {
        "observation_id": observation_id,
        "t_utc_ns": row["t_start_utc_ns"],
        "latitude": row["latitude"],
        "longitude": row["longitude"],
        "altitude_m": row["altitude_m"],
        "position_accuracy_m": row["h_accuracy_m"],
        "position_evidence": row["position_evidence"],
        "center_frequency_hz": row["center_frequency_hz"],
        "sample_rate_hz": row["sample_rate_hz"],
        "gain_db": row["gain_db"],
        "agc": bool(row["agc"]),
        "n_ffts": row["n_ffts"],
        "sweep_step": row["sweep_step"],
        "flags": QualityFlag.describe(row["flags"]),
        "unit": spectrum.unit.value,
        "unit_label": spectrum.unit.label,
        "calibrated": spectrum.unit.is_calibrated,
        "enbw_hz": spectrum.enbw_hz,
        "bin_width_hz": spectrum.bin_width_hz,
        "frequencies_hz": spectrum.frequencies_hz.tolist(),
        "power_db": spectrum.power_db.tolist(),
    }


__all__ = ["SurveySummary", "spectrum_at_observation", "summarise"]
