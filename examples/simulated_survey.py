#!/usr/bin/env python3
"""The V0 vertical slice, end to end, with no radio hardware.

    virtual transmitter -> MockSDR -> synthetic IQ -> DSP -> spectrum observation
      -> MockGPS route -> geotagged observations -> persistent survey
      -> spatial aggregation -> interpolation -> RF heatmap

The synthetic beacon sits at a known virtual coordinate. The mapping code is never
told where it is; the script recovers a probable region from the measurements alone
and only then compares it against the truth, which is stored separately in the survey
manifest under its own key.

Run it::

    python examples/simulated_survey.py                  # compact, a few seconds
    python examples/simulated_survey.py --scenario full  # a longer walk

Then explore the result::

    spectramap serve surveys/simulated.smap
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

import numpy as np

from spectramap.analysis.pipeline import BandSelection, reconstruct, write_products
from spectramap.analysis.summary import summarise
from spectramap.geospatial.projection import haversine_m
from spectramap.simulation.world import compact_world, default_world
from spectramap.storage import SurveyReader
from spectramap.survey.config import SurveyConfig
from spectramap.survey.recorder import SurveyRecorder
from spectramap.survey.session import build_session

BEACON_HZ = 433_920_000.0

SCENARIOS = {
    # (world factory, sample rate, offset tuning, FFT size, block samples)
    "compact": (compact_world, 250_000, 50_000, 512, 8192),
    "full": (default_world, 2_400_000, 250_000, 2048, 131_072),
}


def rule(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n{'-' * len(title)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="compact")
    parser.add_argument("--output", default="surveys/simulated.smap")
    parser.add_argument("--seed", type=int, default=20260807)
    args = parser.parse_args()

    factory, sample_rate, offset, fft_size, block = SCENARIOS[args.scenario]
    world = factory(seed=args.seed)
    output = Path(args.output)
    shutil.rmtree(output, ignore_errors=True)

    config = SurveyConfig.from_dict(
        {
            "survey": {"name": "simulated", "mode": "fixed"},
            "receiver": {"driver": "mock", "gain": 24.0},
            "capture": {
                "center_frequency_hz": BEACON_HZ,
                "sample_rate_hz": sample_rate,
                "offset_tuning_hz": offset,
                "block_samples": block,
            },
            "fft": {"size": fft_size, "window": "hann"},
            "aggregation": {"interval_ms": 500},
            "location": {"provider": "mock"},
            "antenna": {"id": "sim-whip", "type": "quarter-wave whip",
                        "polarization": "vertical"},
            "simulation": {"seed": args.seed},
        }
    )

    rule("1. Configuration")
    print(f"  Target frequency   {BEACON_HZ / 1e6:.5f} MHz")
    print(f"  LO tuned to        {config.capture.tuned_center_hz / 1e6:.5f} MHz "
          f"(offset {offset / 1e3:.0f} kHz, to keep the target out of the DC bin)")
    print(f"  Sample rate        {sample_rate / 1e6:.3f} MS/s")
    print(f"  FFT                {fft_size} point hann")
    print(f"  Route              {world.route.length_m:.0f} m, "
          f"{world.route.duration_s:.0f} s at {world.route.speed_mps} m/s")
    for warning in config.validate():
        print(f"  ! {warning}")

    rule("2. Recording (MockSDR + MockGPS through the real pipeline)")
    session = build_session(config, world=world)
    recorder = SurveyRecorder(
        config, session.device, session.positions, output,
        extra_manifest=session.manifest_extras(),
    )
    started = time.monotonic()
    path = recorder.run(max_duration_s=world.route.duration_s)
    wall = time.monotonic() - started
    status = recorder.status
    print(f"  Observations       {status.observations:,}")
    print(f"  Simulated duration {status.elapsed_s:.0f} s  (recorded in {wall:.1f} s wall)")
    print(f"  Distance           {status.distance_m:.0f} m")
    print(f"  Position fixes     {status.fixes:,}")
    print(f"  Written to         {path}")

    rule("3. Survey summary")
    with SurveyReader(path) as reader:
        summary = summarise(reader)
        lo, hi = summary.frequency_spans_hz[0]
        print(f"  Measured span      {lo / 1e6:.4f} - {hi / 1e6:.4f} MHz")
        print(f"  Units              {summary.power_unit.label}")
        print(f"  Gain               {summary.gain_db} dB (fixed)")
        print(f"  Noise floor        {summary.noise_floor_db:+.1f} dB")
        print(f"  Position accuracy  median {summary.median_accuracy_m:.1f} m")

        rule("4. Spatial reconstruction at 433.92 MHz")
        recon = reconstruct(reader, BandSelection.around(BEACON_HZ, 20_000.0, "peak"))
        powers = recon.measurements["power_db"]
        print(f"  Measurements used  {recon.n_measurements:,}")
        print(f"  Measured range     {powers.min():+.1f} to {powers.max():+.1f} dB "
              f"(span {np.ptp(powers):.1f} dB)")
        print(f"  Raster             {recon.field.shape[1]} x {recon.field.shape[0]} "
              f"at {recon.field.resolution_m:.0f} m")
        print(f"  Coverage           {recon.field.coverage_fraction * 100:.0f}% "
              "(the rest is masked: nothing was measured there)")

        outputs = write_products(reader, recon)

        rule("5. Strongest-signal region  [ESTIMATED]")
        hotspot = recon.hotspot
        print(f"  {hotspot.describe()}")
        print(f"  Geometry score     {hotspot.geometry_score:.2f}")
        for caveat in hotspot.caveats:
            print(f"    - {caveat}")

        rule("6. Scoring against ground truth (never seen by the analysis)")
        truth = reader.manifest["simulation_ground_truth"]
        beacon = next(t for t in truth["transmitters"] if t["tx_id"].startswith("beacon"))
        error_m = float(
            haversine_m(hotspot.latitude, hotspot.longitude,
                        beacon["latitude"], beacon["longitude"])
        )
        peak_lat, peak_lon, _ = recon.field.argmax_geodetic()
        field_error_m = float(
            haversine_m(peak_lat, peak_lon, beacon["latitude"], beacon["longitude"])
        )
        print(f"  True beacon        {beacon['latitude']:.6f}, {beacon['longitude']:.6f}")
        print(f"  Estimated region   {hotspot.latitude:.6f}, {hotspot.longitude:.6f}")
        print(f"  Localisation error {error_m:.1f} m "
              f"(stated radius {hotspot.radius_m:.1f} m)")
        print(f"  Field peak error   {field_error_m:.1f} m")
        verdict = "PASS" if error_m < 25.0 else "FAIL"
        print(f"  Verdict            \033[1m{verdict}\033[0m "
              "- reconstructed without supplying the transmitter position")

        rule("7. Products")
        for key, value in outputs.items():
            print(f"  {key:<22}{value}")

    print(f"\n  Explore it:  \033[36mspectramap serve {path}\033[0m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
