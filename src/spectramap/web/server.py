"""A local HTTP server for browsing one survey.

Deliberately small: ``http.server`` from the standard library, bound to localhost by
default, serving one static page and a handful of JSON endpoints. There is no
framework, no build step, no database and no account. A survey is a directory, and
this is a viewer for it.

Endpoints
---------
``GET /``                    the viewer
``GET /api/survey``          manifest, summary, measured spans, quality flags
``GET /api/band``            band power per observation + the GPS track
``GET /api/field``           interpolated field metadata (bounds, range, provenance)
``GET /api/field.png``       the interpolated field as an RGBA PNG
``GET /api/spectrum/<id>``   one observation's full spectrum
``GET /api/hotspot``         strongest-signal estimate for a band

Results are cached per (band, method, grid settings) so that dragging the frequency
selector does not re-interpolate the same field repeatedly.
"""

from __future__ import annotations

import json
import socketserver
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np

from spectramap.analysis.pipeline import DEFAULT_EXCLUDE, BandSelection, reconstruct
from spectramap.analysis.summary import spectrum_at_observation, summarise
from spectramap.storage import SurveyReader
from spectramap.types import QualityFlag
from spectramap.version import __version__
from spectramap.viz.colormap import apply_colormap
from spectramap.viz.png import encode_png

STATIC_DIR = Path(__file__).parent / "static"
# Bounded so a long session cannot grow without limit; each entry holds one raster.
MAX_CACHE_ENTRIES = 24


class SurveyService:
    """Everything the viewer needs from one survey, with a small result cache."""

    def __init__(self, path: str | Path) -> None:
        self.reader = SurveyReader(path)
        self.summary = summarise(self.reader)
        self._cache: dict[tuple, Any] = {}
        self._lock = threading.Lock()

    # -- Static description -------------------------------------------------

    def survey_info(self) -> dict[str, Any]:
        spans = self.reader.frequency_coverage()
        manifest = self.reader.manifest
        rows = self.reader.observation_rows(with_position_only=True)
        lats = [r["latitude"] for r in rows]
        lons = [r["longitude"] for r in rows]
        return {
            "software_version": __version__,
            "summary": self.summary.to_dict(),
            "spans_hz": [list(s) for s in spans],
            "mode": manifest.get("mode", "fixed"),
            "sequential_capture": manifest.get("mode") == "sweep",
            "units": manifest.get("units", {}),
            "dsp": manifest.get("summary", {}).get("dsp", {}),
            "warnings": self.reader.warnings,
            "bounds": {
                "south": min(lats) if lats else 0.0,
                "north": max(lats) if lats else 0.0,
                "west": min(lons) if lons else 0.0,
                "east": max(lons) if lons else 0.0,
            },
            "has_positions": bool(lats),
            "n_observations": self.summary.n_observations,
            # Simulation ground truth is exposed so the demo can show the true
            # transmitter next to the estimate. It is absent for real surveys, and it
            # is never an input to any estimate.
            "simulation_ground_truth": manifest.get("simulation_ground_truth"),
        }

    def track(self) -> list[list[float]]:
        return [[f.latitude, f.longitude] for f in self.reader.positions()]

    # -- Band selection -----------------------------------------------------

    def band(self, f_low: float, f_high: float, method: str, include_flagged: bool) -> dict:
        key = ("band", round(f_low, 1), round(f_high, 1), method, include_flagged)
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit

        data = self.reader.band_power(
            f_low,
            f_high,
            method=method,
            with_position_only=True,
            exclude_flags=QualityFlag.NONE if include_flagged else DEFAULT_EXCLUDE,
        )
        powers = data["power_db"]
        result = {
            "observation_id": data["observation_id"].tolist(),
            "latitude": data["latitude"].tolist(),
            "longitude": data["longitude"].tolist(),
            "power_db": powers.tolist(),
            "t_utc_ns": data["t_utc_ns"].tolist(),
            "flags": [QualityFlag.describe(int(f)) for f in data["flags"]],
            "n": int(powers.size),
            "min_db": float(powers.min()) if powers.size else None,
            "max_db": float(powers.max()) if powers.size else None,
            "range_db": float(np.ptp(powers)) if powers.size else None,
            "method": method,
            "unit": self.reader.power_unit.value,
            "unit_label": self.reader.power_unit.label,
            "calibrated": self.reader.is_calibrated,
        }
        self._store(key, result)
        return result

    def reconstruction(
        self,
        f_low: float,
        f_high: float,
        method: str,
        resolution_m: float,
        max_distance_m: float,
        include_flagged: bool,
    ):
        key = (
            "recon",
            round(f_low, 1),
            round(f_high, 1),
            method,
            resolution_m,
            max_distance_m,
            include_flagged,
        )
        with self._lock:
            hit = self._cache.get(key)
        if hit is not None:
            return hit
        band = BandSelection(f_low, f_high, method)
        result = reconstruct(
            self.reader,
            band,
            resolution_m=resolution_m,
            max_distance_m=max_distance_m,
            search_radius_m=max(max_distance_m * 1.5, 30.0),
            exclude_flags=QualityFlag.NONE if include_flagged else DEFAULT_EXCLUDE,
        )
        self._store(key, result)
        return result

    def _store(self, key: tuple, value: Any) -> None:
        with self._lock:
            if len(self._cache) >= MAX_CACHE_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = value

    def spectrum(self, observation_id: int) -> dict[str, Any]:
        return spectrum_at_observation(self.reader, observation_id)

    def close(self) -> None:
        self.reader.close()


def _field_png_bytes(field, colormap: str, use_confidence: bool) -> tuple[bytes, dict]:
    """Render an interpolated field to PNG bytes plus placement metadata."""
    values = np.flipud(field.power_db)
    alpha = np.flipud(field.confidence) if use_confidence else 1.0
    if use_confidence:
        alpha = np.where(np.isfinite(alpha), alpha, 0.0)
    finite = field.power_db[np.isfinite(field.power_db)]
    vmin = float(np.percentile(finite, 2)) if finite.size else 0.0
    vmax = float(np.percentile(finite, 98)) if finite.size else 1.0
    rgba = apply_colormap(values, vmin=vmin, vmax=vmax, name=colormap, alpha=alpha)

    south, west, north, east = field.bounds()
    return encode_png(rgba), {
        "bounds": {"south": south, "west": west, "north": north, "east": east},
        "vmin": vmin,
        "vmax": vmax,
    }


class Handler(BaseHTTPRequestHandler):
    service: SurveyService  # Injected by :func:`serve`.
    server_version = f"SpectraMap/{__version__}"

    def log_message(self, fmt: str, *args: Any) -> None:  # Quiet by default.
        pass

    # -- Plumbing -----------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str, cache: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:  # pragma: no cover - the browser navigated away
            pass

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlparse(self.path).query)

    def _band_args(self) -> tuple[float, float, str, bool]:
        q = self._query()
        center = float(q.get("center", ["0"])[0])
        width = float(q.get("width", ["20000"])[0])
        method = q.get("method", ["peak"])[0]
        if method not in ("peak", "mean", "integrated"):
            raise ValueError(f"unknown method {method!r}")
        include_flagged = q.get("include_flagged", ["0"])[0] in ("1", "true", "yes")
        return center - width / 2, center + width / 2, method, include_flagged

    # -- Routes -------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        route = urlparse(self.path).path
        try:
            if route in ("/", "/index.html"):
                self._send(
                    200, (STATIC_DIR / "app.html").read_bytes(), "text/html; charset=utf-8"
                )
            elif route == "/api/survey":
                self._json(self.service.survey_info())
            elif route == "/api/track":
                self._json({"track": self.service.track()})
            elif route == "/api/band":
                low, high, method, flagged = self._band_args()
                self._json(self.service.band(low, high, method, flagged))
            elif route == "/api/field":
                self._json(self._field_payload())
            elif route == "/api/field.png":
                self._field_png()
            elif route == "/api/hotspot":
                self._json(self._hotspot_payload())
            elif route.startswith("/api/spectrum/"):
                self._json(self.service.spectrum(int(route.rsplit("/", 1)[1])))
            else:
                self._error(404, f"no route {route}")
        except (KeyError, IndexError) as exc:
            self._error(404, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - surfaced to the browser
            self._error(500, f"{type(exc).__name__}: {exc}")

    def _recon(self):
        low, high, method, flagged = self._band_args()
        q = self._query()
        return self.service.reconstruction(
            low,
            high,
            method,
            resolution_m=float(q.get("resolution", ["5"])[0]),
            max_distance_m=float(q.get("max_distance", ["40"])[0]),
            include_flagged=flagged,
        )

    def _field_payload(self) -> dict[str, Any]:
        recon = self._recon()
        south, west, north, east = recon.field.bounds()
        finite = recon.field.power_db[np.isfinite(recon.field.power_db)]
        return {
            "bounds": {"south": south, "west": west, "north": north, "east": east},
            "resolution_m": recon.field.resolution_m,
            "shape": list(recon.field.shape),
            "coverage_fraction": recon.field.coverage_fraction,
            "vmin": float(np.percentile(finite, 2)) if finite.size else None,
            "vmax": float(np.percentile(finite, 98)) if finite.size else None,
            "unit_label": recon.summary.power_unit.label,
            "calibrated": recon.summary.calibrated,
            "n_measurements": recon.n_measurements,
            "excluded": recon.excluded_observations,
            "notes": recon.notes,
            "provenance": recon.field.provenance.to_dict(),
        }

    def _field_png(self) -> None:
        recon = self._recon()
        q = self._query()
        colormap = q.get("colormap", ["viridis"])[0]
        confidence = q.get("confidence", ["1"])[0] in ("1", "true", "yes")
        data, _meta = _field_png_bytes(recon.field, colormap, confidence)
        self._send(200, data, "image/png", cache=True)

    def _hotspot_payload(self) -> dict[str, Any]:
        recon = self._recon()
        return recon.hotspot.to_dict() if recon.hotspot else {}


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(
    survey_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> int:
    """Serve one survey until interrupted."""
    service = SurveyService(survey_path)
    handler = type("BoundHandler", (Handler,), {"service": service})

    with _Server((host, port), handler) as httpd:
        url = f"http://{host}:{port}/"
        summary = service.summary
        print(f"\n  SpectraMap viewer   {url}")
        print(f"  Survey              {summary.name}")
        print(f"  Observations        {summary.n_observations:,}")
        if summary.frequency_spans_hz:
            lo, hi = summary.frequency_spans_hz[0]
            print(f"  Measured span       {lo / 1e6:.4f} - {hi / 1e6:.4f} MHz")
        print(f"  Units               {summary.power_unit.label}")
        if summary.n_positioned == 0:
            print("  (no observation has a position: the map will be empty)")
        print("\n  Ctrl+C to stop\n")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  stopped")
        finally:
            service.close()
    return 0


__all__ = ["Handler", "SurveyService", "serve"]
