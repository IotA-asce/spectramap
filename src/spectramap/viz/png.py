"""Minimal PNG writer built on the standard library.

Roughly forty lines of zlib and CRC gets a correct RGBA PNG, which is a better trade
than making matplotlib a hard dependency of a tool meant to run on a Raspberry Pi in
a field. Anyone who wants publication figures can read the exported GeoJSON or CSV
with whatever they like.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(rgba: np.ndarray, compression: int = 6) -> bytes:
    """Encode an ``(height, width, 4)`` uint8 RGBA array as PNG bytes.

    Row 0 of the array becomes the top row of the image. Callers holding a raster
    whose first row is the *south* edge -- which is how every field in SpectraMap is
    stored -- must flip it first, or north will come out at the bottom.
    """
    arr = np.asarray(rgba)
    if arr.ndim != 3 or arr.shape[2] != 4:
        raise ValueError(f"expected an (h, w, 4) RGBA array, got shape {arr.shape}")
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)

    height, width = arr.shape[0], arr.shape[1]
    # Each scanline is prefixed with filter type 0 (None). Filtering would compress
    # better; it would also be the only subtle part of this file.
    raw = np.hstack(
        [np.zeros((height, 1), dtype=np.uint8), arr.reshape(height, width * 4)]
    ).tobytes()

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit, RGBA
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw, compression))
        + _chunk(b"IEND", b"")
    )


def write_png(path: str | Path, rgba: np.ndarray, compression: int = 6) -> Path:
    """Encode and write an RGBA array to ``path``."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(encode_png(rgba, compression))
    return out


__all__ = ["encode_png", "write_png"]
