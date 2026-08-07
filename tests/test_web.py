"""The local viewer's API.

Served over a real socket rather than by calling handlers directly, so the threaded
server path -- the one that caught a cross-thread SQLite bug -- is genuinely exercised.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest
from tests.conftest import BEACON_HZ, CONTROL_HZ

from spectramap.web.server import Handler, SurveyService, _Server


@pytest.fixture(scope="module")
def server(recorded_survey):
    service = SurveyService(recorded_survey)
    httpd = _Server(("127.0.0.1", 0), type("Bound", (Handler,), {"service": service}))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    service.close()


def get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=20) as response:
        return response.status, response.read(), response.headers.get("Content-Type")


def get_json(base: str, path: str):
    _, body, _ = get(base, path)
    return json.loads(body)


def band_query(center_hz: float, width_hz: float = 20_000.0, method: str = "peak") -> str:
    return f"center={center_hz}&width={width_hz}&method={method}"


def test_index_is_served_and_self_contained(server):
    status, body, content_type = get(server, "/")
    assert status == 200
    assert "text/html" in content_type
    html = body.decode()
    assert "<title>SpectraMap</title>" in html
    # No CDN dependency: the viewer must work in a field with no internet.
    assert "cdn." not in html
    assert "<script src=" not in html


def test_survey_endpoint_describes_the_survey_honestly(server):
    payload = get_json(server, "/api/survey")
    assert payload["summary"]["observations"] > 20
    assert payload["units"]["calibrated"] is False
    assert payload["spans_hz"]
    assert payload["has_positions"] is True
    assert payload["bounds"]["north"] > payload["bounds"]["south"]


def test_track_endpoint_returns_the_raw_gps_path(server):
    payload = get_json(server, "/api/track")
    assert len(payload["track"]) > 10
    assert len(payload["track"][0]) == 2


def test_band_endpoint_returns_a_spatial_field(server):
    payload = get_json(server, "/api/band?" + band_query(BEACON_HZ))
    assert payload["n"] > 20
    assert payload["range_db"] > 15
    assert payload["calibrated"] is False
    assert "relative, uncalibrated" in payload["unit_label"]
    assert len(payload["latitude"]) == payload["n"]


def test_frequency_selection_changes_the_result(server):
    beacon = get_json(server, "/api/band?" + band_query(BEACON_HZ))
    control = get_json(server, "/api/band?" + band_query(CONTROL_HZ))
    assert beacon["range_db"] > 5 * control["range_db"]


@pytest.mark.parametrize("method", ["peak", "mean", "integrated"])
def test_all_band_methods_are_served(server, method):
    payload = get_json(server, "/api/band?" + band_query(BEACON_HZ, method=method))
    assert payload["method"] == method
    assert payload["n"] > 0


def test_field_endpoint_reports_coverage_and_provenance(server):
    payload = get_json(server, "/api/field?" + band_query(BEACON_HZ))
    assert 0 < payload["coverage_fraction"] <= 1
    assert payload["provenance"]["evidence"] == "interpolated"
    assert payload["provenance"]["algorithm"] == "inverse-distance-weighting"
    assert payload["unit_label"].endswith("(relative, uncalibrated)")
    assert payload["bounds"]["east"] > payload["bounds"]["west"]


def test_field_png_is_a_valid_image(server):
    status, body, content_type = get(server, "/api/field.png?" + band_query(BEACON_HZ))
    assert status == 200
    assert content_type == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(body) > 200


def test_field_png_honours_the_confidence_toggle(server):
    _, with_confidence, _ = get(
        server, "/api/field.png?" + band_query(BEACON_HZ) + "&confidence=1"
    )
    _, without, _ = get(server, "/api/field.png?" + band_query(BEACON_HZ) + "&confidence=0")
    assert with_confidence != without


def test_spectrum_endpoint_carries_everything_needed_to_read_it(server):
    payload = get_json(server, "/api/spectrum/3")
    assert len(payload["power_db"]) == len(payload["frequencies_hz"]) == 512
    assert payload["calibrated"] is False
    assert payload["enbw_hz"] > payload["bin_width_hz"]
    assert payload["n_ffts"] > 0
    assert "flags" in payload
    assert payload["latitude"] is not None


def test_hotspot_endpoint_carries_its_caveats(server):
    payload = get_json(server, "/api/hotspot?" + band_query(BEACON_HZ))
    assert payload["evidence"] == "estimated"
    assert payload["radius_m"] > 0
    assert payload["caveats"]
    assert "not where a transmitter is" in " ".join(payload["caveats"])


def test_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/does-not-exist")
    assert exc.value.code == 404


def test_unknown_observation_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/spectrum/999999")
    assert exc.value.code == 404


def test_invalid_method_is_400(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(server, "/api/band?" + band_query(BEACON_HZ, method="bogus"))
    assert exc.value.code == 400


def test_concurrent_requests_are_served(server):
    """The reader is shared across handler threads; this is what caught that."""
    results: list[int] = []
    errors: list[Exception] = []

    def worker(offset: int) -> None:
        try:
            payload = get_json(
                server, "/api/band?" + band_query(BEACON_HZ + offset * 100)
            )
            results.append(payload["n"])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(results) == 8
    assert all(n > 0 for n in results)
