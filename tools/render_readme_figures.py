#!/usr/bin/env python3
"""Regenerate the figures used in README.md.

These are **real output**, not mockups: a survey is recorded through the full
pipeline, reconstructed, and drawn using SpectraMap's own colour map and PNG encoder.
Re-running this after a change to the DSP or the interpolation will change the
pictures, which is the point.

    python tools/render_readme_figures.py

Writes docs/images/heatmap.png and docs/images/frequency-selector.png.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np

from spectramap.analysis.pipeline import BandSelection, reconstruct
from spectramap.interpolation.idw import InterpolatedField
from spectramap.simulation.world import compact_world
from spectramap.storage import SurveyReader
from spectramap.survey.config import SurveyConfig
from spectramap.survey.recorder import SurveyRecorder
from spectramap.survey.session import build_session
from spectramap.viz.colormap import apply_colormap
from spectramap.viz.png import write_png

BEACON_HZ = 433_920_000.0
CONTROL_HZ = 433_950_000.0
OUT = Path("docs/images")
SCALE = 6  # Pixels per grid cell.

# Palette for the overlays, chosen to stay legible on top of viridis.
BACKDROP = (14, 17, 23)  # Masked cells show this: "not measured", not page background.
ROUTE = (235, 240, 245)
POINT = (12, 14, 20)
ESTIMATE = (255, 255, 255)  # White: viridis has no white, so the ring always reads.
TRUTH = (255, 106, 96)


# ---------------------------------------------------------------------------
# Tiny raster helpers. Enough to draw a figure; not a graphics library.
# ---------------------------------------------------------------------------


def blend(rgba: np.ndarray, mask: np.ndarray, colour: tuple[int, int, int], alpha=1.0) -> None:
    """Alpha-blend a flat colour into the pixels selected by ``mask``, in place."""
    if not mask.any():
        return
    a = float(alpha)
    for channel, value in enumerate(colour):
        existing = rgba[..., channel][mask].astype(np.float64)
        rgba[..., channel][mask] = np.round(existing * (1 - a) + value * a).astype(np.uint8)
    rgba[..., 3][mask] = np.maximum(rgba[..., 3][mask], int(round(255 * a)))


def disc(shape, cx: float, cy: float, radius: float) -> np.ndarray:
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    return (xx - cx) ** 2 + (yy - cy) ** 2 <= radius**2


def ring(shape, cx: float, cy: float, radius: float, width: float) -> np.ndarray:
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    d = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    return (d >= radius - width / 2) & (d <= radius + width / 2)


def cross(shape, cx: float, cy: float, arm: float, width: float) -> np.ndarray:
    yy, xx = np.ogrid[: shape[0], : shape[1]]
    dx, dy = xx - cx, yy - cy
    diag1 = (np.abs(dx - dy) <= width) & (np.abs(dx) <= arm) & (np.abs(dy) <= arm)
    diag2 = (np.abs(dx + dy) <= width) & (np.abs(dx) <= arm) & (np.abs(dy) <= arm)
    return diag1 | diag2


def polyline(shape, points: list[tuple[float, float]], width: float) -> np.ndarray:
    """Rasterise a polyline by stamping discs densely along each segment."""
    mask = np.zeros(shape[:2], dtype=bool)
    for (x0, y0), (x1, y1) in zip(points[:-1], points[1:], strict=True):
        steps = max(2, int(np.hypot(x1 - x0, y1 - y0) / max(width * 0.4, 0.5)))
        for t in np.linspace(0.0, 1.0, steps):
            mask |= disc(shape, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, width / 2)
    return mask


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_field(field: InterpolatedField, vmin: float, vmax: float) -> np.ndarray:
    """The interpolated field as RGBA, north-up, upscaled, confidence bound to alpha.

    Composited over an opaque dark backdrop so that masked cells -- ground the survey
    never came close to -- read as holes in a panel rather than as the edge of the
    image. The holes are the honest part of the picture and should be visible.
    """
    values = np.flipud(field.power_db)
    alpha = np.flipud(field.confidence)
    alpha = np.where(np.isfinite(alpha), alpha, 0.0)
    # Lift the floor a little: at full range the weakest cells vanish entirely, and the
    # point is to show that they are thin, not that they are absent.
    alpha = np.where(alpha > 0, 0.40 + 0.60 * alpha, 0.0)
    rgba = apply_colormap(values, vmin=vmin, vmax=vmax, name="viridis", alpha=alpha)

    a = rgba[..., 3:4].astype(np.float64) / 255.0
    backdrop = np.array(BACKDROP, dtype=np.float64)
    composited = np.empty(rgba.shape, dtype=np.uint8)
    composited[..., :3] = np.round(rgba[..., :3] * a + backdrop * (1 - a)).astype(np.uint8)
    composited[..., 3] = 255
    return np.repeat(np.repeat(composited, SCALE, axis=0), SCALE, axis=1)


def projector(field: InterpolatedField):
    """Geodetic -> pixel, for the upscaled, north-up raster."""

    def to_px(lat, lon):
        east, north, _ = field.plane.to_enu(lat, lon)
        x = (float(east) - field.east_m[0]) / field.resolution_m * SCALE
        y = (field.north_m[-1] - float(north)) / field.resolution_m * SCALE
        return x, y

    return to_px


def draw_overlays(rgba, field, track, measurements, hotspot, truth) -> None:
    """Route, measurement points, the estimated region, and the true transmitter."""
    shape = rgba.shape
    to_px = projector(field)

    if track:
        pts = [to_px(lat, lon) for lat, lon in track]
        blend(rgba, polyline(shape, pts, width=2.0), ROUTE, 0.30)

    for lat, lon in zip(measurements["latitude"], measurements["longitude"], strict=True):
        x, y = to_px(lat, lon)
        blend(rgba, ring(shape, x, y, radius=2.6, width=1.6), POINT, 0.55)

    if hotspot is not None:
        x, y = to_px(hotspot.latitude, hotspot.longitude)
        radius_px = max(6.0, hotspot.radius_m / field.resolution_m * SCALE)
        blend(rgba, disc(shape, x, y, radius_px), ESTIMATE, 0.14)
        blend(rgba, ring(shape, x, y, radius_px, width=2.0), ESTIMATE, 0.95)
        blend(rgba, cross(shape, x, y, arm=5.0, width=1.2), ESTIMATE, 0.95)

    if truth is not None:
        x, y = to_px(truth["latitude"], truth["longitude"])
        blend(rgba, cross(shape, x, y, arm=7.0, width=1.6), TRUTH, 1.0)


def frame(rgba: np.ndarray, colour=(58, 66, 78)) -> np.ndarray:
    """A one-pixel border, so the figure reads as a panel on any page background."""
    out = rgba.copy()
    for edge in (out[0, :], out[-1, :], out[:, 0], out[:, -1]):
        edge[..., :3] = colour
        edge[..., 3] = 255
    return out


def side_by_side(left: np.ndarray, right: np.ndarray, gap: int = 14) -> np.ndarray:
    height = max(left.shape[0], right.shape[0])

    def pad(img):
        if img.shape[0] == height:
            return img
        extra = np.zeros((height - img.shape[0], img.shape[1], 4), dtype=np.uint8)
        return np.vstack([img, extra])

    spacer = np.zeros((height, gap, 4), dtype=np.uint8)
    return np.hstack([pad(left), spacer, pad(right)])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def record(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    config = SurveyConfig.from_dict(
        {
            "survey": {"name": "readme-demo", "mode": "fixed"},
            "receiver": {"driver": "mock", "gain": 24.0},
            "capture": {
                "center_frequency_hz": BEACON_HZ,
                "sample_rate_hz": 250_000,
                "offset_tuning_hz": 50_000,
                "block_samples": 8192,
            },
            "fft": {"size": 512, "window": "hann"},
            "aggregation": {"interval_ms": 400},
            "location": {"provider": "mock"},
        }
    )
    world = compact_world()
    session = build_session(config, world=world)
    recorder = SurveyRecorder(
        config, session.device, session.positions, path,
        extra_manifest=session.manifest_extras(),
    )
    return recorder.run(max_duration_s=world.route.duration_s)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    survey = record(Path("/tmp/spectramap-readme-demo.smap"))

    with SurveyReader(survey) as reader:
        track = [(f.latitude, f.longitude) for f in reader.positions()]
        truth = {
            t["tx_id"]: t
            for t in reader.manifest["simulation_ground_truth"]["transmitters"]
        }

        # Fine grid for a printable figure; the mask is kept tight so the honest
        # holes at the edges of the survey are visible.
        common = dict(resolution_m=1.5, max_distance_m=14.0, search_radius_m=40.0)
        beacon = reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"), **common)
        control = reconstruct(
            reader, BandSelection.around(CONTROL_HZ, 20_000.0, "peak"), **common
        )

    # --- Figure 1: the hero image -----------------------------------------
    finite = beacon.field.power_db[np.isfinite(beacon.field.power_db)]
    vmin, vmax = float(np.percentile(finite, 1)), float(np.percentile(finite, 99))
    hero = render_field(beacon.field, vmin, vmax)
    draw_overlays(
        hero, beacon.field, track, beacon.measurements, beacon.hotspot, truth["beacon-433"]
    )
    write_png(OUT / "heatmap.png", frame(hero))

    # --- Figure 2: the same survey at two frequencies ----------------------
    # Both panels get the SAME colour range in dB, each centred on its own median.
    #
    # Autoscaling each panel to its own min/max would be badly misleading here: the
    # control carrier varies by 0.4 dB, and stretching that across the full colour
    # range renders pure noise as a confident-looking gradient. Sharing the dB span
    # means a colour step is the same number of decibels in both panels, so the
    # control panel correctly appears flat -- which is the entire point of the figure.
    spans = []
    for recon in (beacon, control):
        f = recon.field.power_db[np.isfinite(recon.field.power_db)]
        spans.append(float(np.percentile(f, 99) - np.percentile(f, 1)))
    common_span = max(spans)

    panels = []
    for recon in (beacon, control):
        f = recon.field.power_db[np.isfinite(recon.field.power_db)]
        mid = float(np.percentile(f, 1) + np.percentile(f, 99)) / 2
        panel = render_field(recon.field, mid - common_span / 2, mid + common_span / 2)
        draw_overlays(panel, recon.field, track, recon.measurements, None, None)
        panels.append(frame(panel))
    write_png(OUT / "frequency-selector.png", side_by_side(*panels))
    print(f"  both panels share a {common_span:.0f} dB colour range")

    for recon, label in ((beacon, "433.92 MHz"), (control, "433.95 MHz")):
        powers = recon.measurements["power_db"]
        print(
            f"  {label}: span {np.ptp(powers):5.1f} dB · "
            f"coverage {recon.field.coverage_fraction * 100:3.0f}% · "
            f"confidence {recon.hotspot.confidence}"
        )
    for name in ("heatmap.png", "frequency-selector.png"):
        path = OUT / name
        print(f"  wrote {path} ({path.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
