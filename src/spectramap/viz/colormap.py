"""Colour maps for RF power rasters.

Only perceptually-ordered maps are offered. A rainbow map invents boundaries where
the data is smooth and hides real steps elsewhere, which on an RF heatmap looks
exactly like structure that is not there. ``viridis`` and ``magma`` are reproduced
here as anchor points with linear interpolation between them -- close enough for a
map legend, and free of a matplotlib dependency.

Masked cells (NaN) are returned fully transparent, never as a colour. An unmeasured
region must read as absent, not as "cold".
"""

from __future__ import annotations

import numpy as np

# Anchors sampled from the matplotlib colormaps of the same names (BSD licensed,
# themselves derived from public-domain work).
_VIRIDIS = [
    (0.267, 0.005, 0.329), (0.283, 0.141, 0.458), (0.254, 0.265, 0.530),
    (0.207, 0.372, 0.553), (0.164, 0.471, 0.558), (0.128, 0.567, 0.551),
    (0.135, 0.659, 0.518), (0.267, 0.749, 0.441), (0.478, 0.821, 0.318),
    (0.741, 0.873, 0.150), (0.993, 0.906, 0.144),
]
_MAGMA = [
    (0.001, 0.000, 0.014), (0.116, 0.066, 0.238), (0.288, 0.101, 0.427),
    (0.446, 0.148, 0.463), (0.605, 0.196, 0.451), (0.767, 0.245, 0.398),
    (0.898, 0.334, 0.320), (0.968, 0.494, 0.371), (0.995, 0.660, 0.505),
    (0.996, 0.816, 0.663), (0.987, 0.991, 0.750),
]
_GREY = [(0.05, 0.05, 0.06), (0.95, 0.95, 0.96)]

COLORMAPS: dict[str, list[tuple[float, float, float]]] = {
    "viridis": _VIRIDIS,
    "magma": _MAGMA,
    "grey": _GREY,
    "gray": _GREY,
}


def apply_colormap(
    values: np.ndarray,
    vmin: float | None = None,
    vmax: float | None = None,
    name: str = "viridis",
    alpha: np.ndarray | float = 1.0,
) -> np.ndarray:
    """Map a 2-D float array to RGBA uint8.

    NaN inputs -- masked, unmeasured cells -- come back with zero alpha.

    ``alpha`` may be a scalar or an array of the same shape, which is how an
    interpolation-confidence field is bound to opacity: weakly supported cells are
    drawn faintly rather than being drawn as confidently as measured ones.
    """
    arr = np.asarray(values, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError(f"expected a 2-D array, got shape {arr.shape}")
    anchors = COLORMAPS.get(name.lower())
    if anchors is None:
        raise ValueError(f"unknown colormap {name!r}; available: {sorted(COLORMAPS)}")

    valid = np.isfinite(arr)
    if vmin is None:
        vmin = float(np.nanmin(arr)) if np.any(valid) else 0.0
    if vmax is None:
        vmax = float(np.nanmax(arr)) if np.any(valid) else 1.0
    if vmax <= vmin:
        vmax = vmin + 1.0

    normalised = np.clip((np.where(valid, arr, vmin) - vmin) / (vmax - vmin), 0.0, 1.0)

    table = np.asarray(anchors, dtype=np.float64)
    positions = np.linspace(0.0, 1.0, table.shape[0])
    rgb = np.stack(
        [np.interp(normalised, positions, table[:, c]) for c in range(3)], axis=-1
    )

    alpha_arr = np.broadcast_to(np.asarray(alpha, dtype=np.float64), arr.shape)
    alpha_arr = np.where(valid & np.isfinite(alpha_arr), np.clip(alpha_arr, 0.0, 1.0), 0.0)

    rgba = np.empty((*arr.shape, 4), dtype=np.uint8)
    rgba[..., :3] = np.round(rgb * 255.0).astype(np.uint8)
    rgba[..., 3] = np.round(alpha_arr * 255.0).astype(np.uint8)
    return rgba


__all__ = ["COLORMAPS", "apply_colormap"]
