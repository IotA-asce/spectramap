"""The SpectraMap command line.

A usable CLI exists before any elaborate UI, because the CLI is what makes the
pipeline testable, scriptable and honest. Commands:

    spectramap devices     list receivers, real and simulated
    spectramap gps         check a position source
    spectramap survey      record a survey
    spectramap status      inspect a recorded survey
    spectramap analyze     reconstruct a frequency band spatially
    spectramap serve       browse a survey on a local map

Output is plain text with optional ANSI colour, and every command takes ``--json``
so it can be consumed by a script. Nothing is printed that the data does not support:
uncalibrated powers are labelled, estimates carry their uncertainty, and filtered
observations are counted rather than silently dropped.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path
from typing import Any

from spectramap.version import __version__

# ---------------------------------------------------------------------------
# Presentation helpers
# ---------------------------------------------------------------------------

_USE_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


def green(t: str) -> str:
    return _c(t, "32")


def cyan(t: str) -> str:
    return _c(t, "36")


def field(label: str, value: Any, width: int = 16) -> str:
    return f"  {label:<{width}}{value}"


def heading(text: str) -> str:
    return f"\n{bold(text)}\n{dim('-' * len(text))}"


def format_hz(hz: float) -> str:
    if hz >= 1e9:
        return f"{hz / 1e9:.6f} GHz"
    if hz >= 1e6:
        return f"{hz / 1e6:.6f} MHz"
    if hz >= 1e3:
        return f"{hz / 1e3:.3f} kHz"
    return f"{hz:.1f} Hz"


def format_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def format_distance(metres: float) -> str:
    return f"{metres / 1000:.2f} km" if metres >= 1000 else f"{metres:.0f} m"


def warn(message: str) -> None:
    print(yellow(f"  ! {message}"), file=sys.stderr)


def fail(message: str, code: int = 1) -> int:
    print(red(f"error: {message}"), file=sys.stderr)
    return code


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------


def cmd_devices(args: argparse.Namespace) -> int:
    from spectramap.sdr.registry import describe_drivers, enumerate_devices, open_device

    devices = enumerate_devices(args.driver)
    if args.json:
        payload = []
        for info in devices:
            entry = info.to_dict()
            try:
                device = open_device(info.driver, index=info.index)
                entry["capabilities"] = device.get_capabilities().to_dict()
            except Exception as exc:
                entry["capabilities_error"] = str(exc)
            payload.append(entry)
        print(json.dumps({"devices": payload, "drivers": describe_drivers()}, indent=2))
        return 0

    if not devices:
        print("No receivers detected.")
        print(dim("\nAvailable drivers:"))
        for spec in describe_drivers():
            needs = f" (requires {', '.join(spec['requires'])})" if spec["requires"] else ""
            print(dim(f"  {spec['driver']:<10}{spec['description']}{needs}"))
        print("\nThe simulator always works: " + cyan("spectramap survey --driver mock"))
        return 0

    print(heading("Receivers"))
    for info in devices:
        label = f"{info.product or info.driver} #{info.index}"
        print(f"\n  {bold(label)}   {dim(info.receiver_id)}")
        try:
            device = open_device(info.driver, index=info.index)
            caps = device.get_capabilities()
        except Exception as exc:
            print(field("", red(f"cannot query: {exc}")))
            continue
        rng = caps.tuning_range
        print(field("Tuner", info.tuner or "unknown", 14))
        suffix = (
            dim("  (assumed from tuner model, not read from the device)")
            if caps.tuning_evidence.value == "assumed"
            else ""
        )
        print(field("Range", f"{format_hz(rng.min_hz)} - {format_hz(rng.max_hz)}{suffix}", 14))
        rates = ", ".join(f"{r / 1e6:.3f}" for r in caps.sample_rates_hz)
        print(field("Sample rates", f"{rates} MS/s", 14))
        if caps.gains_db:
            print(
                field(
                    "Gains",
                    f"{min(caps.gains_db):.1f} - {max(caps.gains_db):.1f} dB "
                    f"({len(caps.gains_db)} steps)",
                    14,
                )
            )
        if caps.notes:
            print(dim(f"    {caps.notes}"))
    return 0


# ---------------------------------------------------------------------------
# gps
# ---------------------------------------------------------------------------


def cmd_gps(args: argparse.Namespace) -> int:
    from spectramap.positioning.mock import MockPositionProvider, StaticPositionProvider
    from spectramap.positioning.nmea import SerialNMEAProvider
    from spectramap.simulation.clock import SimClock
    from spectramap.simulation.world import default_world

    if args.provider == "nmea":
        if not args.port:
            return fail("--port is required for the nmea provider")
        provider: Any = SerialNMEAProvider(port=args.port, baudrate=args.baudrate)
    elif args.provider == "static":
        if args.latitude is None or args.longitude is None:
            return fail("--latitude and --longitude are required for the static provider")
        provider = StaticPositionProvider(args.latitude, args.longitude)
    else:
        clock = SimClock()
        provider = MockPositionProvider(default_world(), clock)

    if not args.json:
        print(heading("Position source"))
        for key, value in provider.describe().items():
            print(field(str(key), value, 26))

    try:
        provider.start()
    except Exception as exc:
        return fail(str(exc))

    if not args.json:
        print(dim(f"\n  waiting up to {args.timeout:.0f} s for a fix..."))
    deadline = time.monotonic() + args.timeout
    fixes: list[Any] = []
    while time.monotonic() < deadline and len(fixes) < args.count:
        if args.provider == "mock":
            provider.clock.advance(1.0)
        fixes.extend(provider.poll())
        if not fixes:
            time.sleep(0.2)
    provider.stop()

    if not fixes:
        if args.json:
            return fail(
                "No fix. Check the antenna sky view, port, baud rate and acquisition timeout.",
                code=2,
            )
        print(red("\n  No fix. "), end="")
        print("Check the antenna has sky view, the port and baud rate are right,")
        print("  and that the receiver has had time to acquire (a cold start can take minutes).")
        return 2

    if args.json:
        print(json.dumps([f.to_dict() for f in fixes[: args.count]], indent=2))
        return 0

    latest = fixes[-1]
    print(green("\n  GPS connected"))
    print(field("Latitude", f"{latest.latitude:.6f}"))
    print(field("Longitude", f"{latest.longitude:.6f}"))
    if latest.altitude_m is not None:
        print(field("Altitude", f"{latest.altitude_m:.1f} m"))
    if latest.horizontal_accuracy_m is not None:
        print(field("Accuracy", f"{latest.horizontal_accuracy_m:.1f} m"))
        if args.provider == "nmea":
            # NMEA reports geometry (HDOP), not accuracy. Say so rather than letting
            # a derived number pass for a measured one.
            print(dim("                  estimated as HDOP x assumed UERE, not reported by the receiver"))
    print(field("Fix quality", latest.fix_quality.value))
    print(field("Satellites", latest.satellites if latest.satellites is not None else "unknown"))
    return 0


# ---------------------------------------------------------------------------
# survey
# ---------------------------------------------------------------------------


def _config_from_args(args: argparse.Namespace) -> Any:
    from spectramap.survey.config import SurveyConfig, parse_frequency

    if args.config:
        config = SurveyConfig.load(args.config)
    else:
        config = SurveyConfig()

    base = config.to_dict()

    if args.name:
        base["survey"]["name"] = args.name
    if args.driver:
        base["receiver"]["driver"] = args.driver
    if args.gain is not None:
        base["receiver"]["gain"] = args.gain
    if args.agc:
        base["receiver"]["gain"] = "auto"
    if args.frequency:
        base["capture"]["center_frequency_hz"] = parse_frequency(args.frequency)
    if args.sample_rate:
        base["capture"]["sample_rate_hz"] = parse_frequency(args.sample_rate)
    if args.offset_tuning is not None:
        base["capture"]["offset_tuning_hz"] = parse_frequency(args.offset_tuning)
    if args.fft_size:
        base["fft"]["size"] = args.fft_size
    if args.interval_ms:
        base["aggregation"]["interval_ms"] = args.interval_ms
    if args.gps:
        base["location"]["provider"] = args.gps
    if args.gps_port:
        base["location"]["port"] = args.gps_port
    if args.seed is not None:
        base["simulation"]["seed"] = args.seed
    if args.realtime:
        base["simulation"]["realtime"] = True
    if args.antenna:
        base["antenna"]["type"] = args.antenna
    return SurveyConfig.from_dict(base)


def cmd_survey(args: argparse.Namespace) -> int:
    from spectramap.survey.recorder import SurveyRecorder
    from spectramap.survey.session import build_session

    try:
        config = _config_from_args(args)
        warnings = config.validate()
    except (ValueError, KeyError) as exc:
        return fail(str(exc))

    output = Path(args.output) if args.output else Path("surveys") / f"{config.name}.smap"
    if output.exists() and not args.force:
        return fail(f"{output} already exists; use --force to overwrite, or pick another --output")

    try:
        session = build_session(config)
    except Exception as exc:
        return fail(str(exc))

    print(heading("SpectraMap Survey"))
    print(field("Name", config.name))
    print(field("Mode", config.mode.value))
    print(field("Driver", config.receiver.driver))
    print(field("Target", format_hz(config.capture.center_frequency_hz)))
    print(
        field(
            "Tuned to",
            f"{format_hz(config.capture.tuned_center_hz)}  "
            + dim(f"(offset {config.capture.offset_tuning_hz / 1e3:.0f} kHz to clear DC)"),
        )
    )
    print(field("Sample rate", f"{config.capture.sample_rate_hz / 1e6:.3f} MS/s"))
    print(field("FFT", f"{config.fft.size} point, {config.fft.window} window"))
    print(
        field(
            "Resolution",
            f"{config.fft.resolution_hz(config.capture.sample_rate_hz):.1f} Hz/bin",
        )
    )
    print(
        field(
            "Gain",
            "AUTO (AGC)" if config.receiver.agc else f"{config.receiver.gain_db:.1f} dB (fixed)",
        )
    )
    print(field("Aggregation", f"{config.aggregation.interval_ms:.0f} ms per observation"))
    print(field("Position", config.location.provider))
    print(field("Output", output))
    print(
        field(
            "Data rate",
            f"~{config.estimated_bytes_per_hour() / 1e6:.0f} MB/hour "
            f"({config.observations_per_second():.1f} obs/s x {config.stored_bins()} bins)",
        )
    )

    for message in warnings:
        warn(message)

    if args.dry_run:
        print(dim("\n  --dry-run: configuration validated, nothing recorded."))
        return 0

    recorder = SurveyRecorder(
        config,
        session.device,
        session.positions,
        output,
        extra_manifest=session.manifest_extras(),
    )

    def handle_sigint(_sig: int, _frame: Any) -> None:
        print(dim("\n  stopping cleanly, finishing the current observation..."))
        recorder.request_stop()

    signal.signal(signal.SIGINT, handle_sigint)

    print()
    if args.duration:
        print(dim(f"  Recording for {args.duration:.0f} s. Ctrl+C to stop early."))
    else:
        print(dim("  Recording. Walk the area, then press Ctrl+C when complete."))
    print()

    last_render = 0.0

    def on_status(status: Any) -> None:
        nonlocal last_render
        # The live line rewrites itself with \r, which only makes sense on a
        # terminal. Piped or redirected, it would emit thousands of junk lines.
        if not _USE_COLOR:
            return
        now = time.monotonic()
        if now - last_render < 0.2:
            return
        last_render = now
        power = (
            f"{status.last_power_db:+7.1f}" if status.last_power_db is not None else "    ---"
        )
        snr = (
            f"{status.last_power_db - status.last_noise_floor_db:+5.1f}"
            if status.last_power_db is not None and status.last_noise_floor_db is not None
            else "  ---"
        )
        accuracy = (
            f"{status.latest_accuracy_m:.1f} m"
            if status.latest_accuracy_m is not None
            else "no fix"
        )
        line = (
            f"  {format_duration(status.elapsed_s):>8}  "
            f"obs {status.observations:>6}  "
            f"{format_distance(status.distance_m):>8}  "
            f"gps {accuracy:>8}  "
            f"peak {power} dB  snr {snr} dB"
        )
        sys.stdout.write("\r" + line[:140])
        sys.stdout.flush()

    recorder.on_status = on_status
    path = recorder.run(max_duration_s=args.duration)
    if _USE_COLOR:
        sys.stdout.write("\r" + " " * 140 + "\r")

    status = recorder.status
    print(heading("Survey complete"))
    print(field("Path", path))
    print(field("Observations", f"{status.observations:,}"))
    print(field("Duration", format_duration(status.elapsed_s)))
    print(field("Distance", format_distance(status.distance_m)))
    print(field("Position fixes", f"{status.fixes:,}"))
    if status.flagged_observations:
        print(
            field(
                "Flagged",
                yellow(
                    f"{status.flagged_observations:,} observation(s) carry quality flags "
                    "(kept, not discarded)"
                ),
            )
        )
    if status.no_position:
        warn(f"{status.no_position} observation(s) have no usable position")
    if status.overloads:
        warn(f"{status.overloads} observation(s) show receiver overload; power under-reads there")
    for error in status.errors:
        print(red(f"  ! {error}"))

    print(f"\n  Next: {cyan(f'spectramap analyze {path} --frequency {args.frequency or 0}')}")
    return 2 if status.errors else 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    from spectramap.analysis.summary import summarise
    from spectramap.storage import SurveyReader

    try:
        reader = SurveyReader(args.survey)
    except (FileNotFoundError, ValueError) as exc:
        return fail(str(exc))

    with reader:
        summary = summarise(reader)
        if args.json:
            print(json.dumps(summary.to_dict(), indent=2))
            return 0

        print(heading(f"Survey: {summary.name}"))
        print(field("Id", summary.survey_id, 20))
        print(field("Observations", f"{summary.n_observations:,}", 20))
        print(
            field(
                "With position",
                f"{summary.n_positioned:,} ({summary.positioned_fraction * 100:.1f}%)",
                20,
            )
        )
        print(field("Duration", format_duration(summary.duration_s), 20))
        print(field("Distance", format_distance(summary.distance_m), 20))
        for lo, hi in summary.frequency_spans_hz:
            print(field("Measured span", f"{format_hz(lo)} - {format_hz(hi)}", 20))

        print(heading("Measurement conditions"))
        print(field("Power unit", summary.power_unit.label, 20))
        if not summary.calibrated:
            print(dim("                      not calibrated: comparable within this survey only"))
        if summary.agc:
            print(field("Gain", red("AUTO (AGC) - powers NOT spatially comparable"), 20))
        elif summary.gain_db is not None:
            print(field("Gain", f"{summary.gain_db:.1f} dB (fixed)", 20))
        else:
            print(field("Gain", yellow("varied during the survey"), 20))
        if summary.median_accuracy_m is not None:
            print(
                field(
                    "Position accuracy",
                    f"median {summary.median_accuracy_m:.1f} m, "
                    f"worst {summary.worst_accuracy_m:.1f} m",
                    20,
                )
            )
        if summary.noise_floor_db is not None:
            print(field("Noise floor", f"{summary.noise_floor_db:+.1f} dB (mean)", 20))
        if summary.peak_power_db is not None:
            print(
                field(
                    "Strongest bin",
                    f"{summary.peak_power_db:+.1f} dB at "
                    f"{format_hz(summary.peak_frequency_hz or 0)}",
                    20,
                )
            )

        if summary.flag_counts:
            print(heading("Quality flags"))
            print(dim("  observations are flagged, never discarded"))
            for name, count in sorted(summary.flag_counts.items(), key=lambda kv: -kv[1]):
                pct = count / summary.n_observations * 100 if summary.n_observations else 0
                print(f"  {name:<24}{count:>8,}  ({pct:.1f}%)")

        if summary.warnings:
            print(heading("Recorded warnings"))
            for message in summary.warnings:
                warn(message)
        if summary.events:
            print(heading("Events"))
            for name, count in sorted(summary.events.items()):
                print(f"  {name:<32}{count:>6}")
    return 0


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------


def cmd_analyze(args: argparse.Namespace) -> int:
    import numpy as np

    from spectramap.analysis.pipeline import (
        DEFAULT_EXCLUDE,
        BandSelection,
        reconstruct,
        write_products,
    )
    from spectramap.storage import SurveyReader
    from spectramap.survey.config import parse_frequency
    from spectramap.types import QualityFlag

    try:
        reader = SurveyReader(args.survey)
    except (FileNotFoundError, ValueError) as exc:
        return fail(str(exc))

    with reader:
        spans = reader.frequency_coverage()
        if args.frequency:
            center = parse_frequency(args.frequency)
        elif spans:
            center = (spans[0][0] + spans[0][1]) / 2
            print(dim("  no --frequency given; using the centre of the measured span"))
        else:
            return fail("this survey contains no spectra")

        width = parse_frequency(args.bandwidth) if args.bandwidth else 20_000.0
        band = BandSelection.around(center, width, args.method)

        try:
            reconstruction = reconstruct(
                reader,
                band,
                resolution_m=args.resolution,
                cell_size_m=args.cell_size,
                idw_power=args.idw_power,
                search_radius_m=args.search_radius,
                max_distance_m=args.max_distance,
                exclude_flags=QualityFlag.NONE if args.include_flagged else DEFAULT_EXCLUDE,
            )
        except ValueError as exc:
            return fail(str(exc))

        outputs = (
            {} if args.no_write else write_products(reader, reconstruction, out_dir=args.out)
        )

        if args.json:
            payload = reconstruction.to_dict()
            payload["outputs"] = {k: str(v) for k, v in outputs.items()}
            print(json.dumps(payload, indent=2))
            return 0

        summary = reconstruction.summary
        powers = reconstruction.measurements["power_db"]
        print(heading(f"Analysis: {summary.name}"))
        print(field("Band", band.label, 22))
        print(field("Aggregation", f"{band.method} power across the band", 22))
        print(field("Observations used", f"{reconstruction.n_measurements:,}", 22))
        if reconstruction.excluded_observations:
            print(
                field(
                    "Excluded",
                    yellow(f"{reconstruction.excluded_observations:,} by quality filter"),
                    22,
                )
            )
        print(field("Unit", summary.power_unit.label, 22))
        print(
            field(
                "Measured range",
                f"{powers.min():+.1f} to {powers.max():+.1f} dB "
                f"(span {np.ptp(powers):.1f} dB)",
                22,
            )
        )

        print(heading("Spatial reconstruction"))
        field_ = reconstruction.field
        print(field("Algorithm", "inverse distance weighting", 22))
        print(field("Grid resolution", f"{field_.resolution_m:.1f} m", 22))
        print(field("Raster", f"{field_.shape[1]} x {field_.shape[0]} cells", 22))
        print(
            field(
                "Coverage",
                f"{field_.coverage_fraction * 100:.0f}% of the raster is within "
                f"{args.max_distance:.0f} m of a measurement",
                22,
            )
        )
        print(dim("                      the rest is masked, not drawn: nothing was measured there"))
        print(field("Cells aggregated", f"{reconstruction.grid.n_cells:,}", 22))

        if reconstruction.hotspot is not None:
            hotspot = reconstruction.hotspot
            colour = {"good": green, "moderate": yellow, "low": red}[hotspot.confidence]
            print(heading("Strongest-signal region  " + dim("[ESTIMATED]")))
            print("  " + colour(hotspot.describe()))
            print(field("Geometry score", f"{hotspot.geometry_score:.2f}", 22))
            print(field("Based on", f"{hotspot.n_used:,} strongest measurements", 22))
            for caveat in hotspot.caveats:
                print(dim(f"  - {caveat}"))

        for note in reconstruction.notes:
            warn(note)

        if outputs:
            print(heading("Written"))
            for key, path in outputs.items():
                print(f"  {key:<22}{path}")
            print(f"\n  Next: {cyan(f'spectramap serve {args.survey}')}")
    return 0


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from spectramap.web.server import serve

    try:
        return serve(args.survey, host=args.host, port=args.port, open_browser=not args.no_browser)
    except (FileNotFoundError, ValueError) as exc:
        return fail(str(exc))


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spectramap",
        description="Map the radio-frequency environment across physical space.",
        epilog="Everything works without hardware: add --driver mock.",
    )
    parser.add_argument("--version", action="version", version=f"spectramap {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # devices
    p = subparsers.add_parser("devices", help="list available receivers")
    p.add_argument("--driver", help="restrict to one driver")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_devices)

    # gps
    p = subparsers.add_parser("gps", help="check a position source")
    p.add_argument("--provider", default="mock", choices=["mock", "nmea", "static"])
    p.add_argument("--port", help="serial port for the nmea provider")
    p.add_argument("--baudrate", type=int, default=9600)
    p.add_argument("--latitude", type=float)
    p.add_argument("--longitude", type=float)
    p.add_argument("--count", type=int, default=1, help="fixes to collect")
    p.add_argument("--timeout", type=float, default=10.0)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_gps)

    # survey
    p = subparsers.add_parser("survey", help="record a survey")
    p.add_argument(
        "action",
        nargs="?",
        default="new",
        choices=["new"],
        help=argparse.SUPPRESS,
    )
    p.add_argument("-c", "--config", help="survey configuration YAML")
    p.add_argument("-o", "--output", help="output .smap directory")
    p.add_argument("-n", "--name", help="survey name")
    p.add_argument("-f", "--frequency", help="target frequency, e.g. 433.92MHz")
    p.add_argument("--bandwidth", help=argparse.SUPPRESS)
    p.add_argument("--driver", help="receiver driver (mock, rtl-sdr)")
    p.add_argument("-g", "--gain", type=float, help="fixed receiver gain in dB (recommended)")
    p.add_argument("--agc", action="store_true", help="use automatic gain (not recommended)")
    p.add_argument("--sample-rate", help="sample rate, e.g. 2.4MHz")
    p.add_argument("--offset-tuning", help="LO offset from the target, e.g. 250kHz")
    p.add_argument("--fft-size", type=int)
    p.add_argument("--interval-ms", type=float, help="integration time per observation")
    p.add_argument("--gps", choices=["mock", "nmea", "static"], help="position provider")
    p.add_argument("--gps-port", help="serial port for the GPS")
    p.add_argument("--antenna", help="antenna description, e.g. 'quarter-wave whip'")
    p.add_argument("-d", "--duration", type=float, help="stop after this many seconds")
    p.add_argument("--seed", type=int, help="simulation seed")
    p.add_argument("--realtime", action="store_true", help="run the simulator at real speed")
    p.add_argument("--dry-run", action="store_true", help="validate the configuration only")
    p.add_argument("--force", action="store_true", help="overwrite an existing survey")
    p.set_defaults(func=cmd_survey)

    # status
    p = subparsers.add_parser("status", help="inspect a recorded survey")
    p.add_argument("survey")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    # analyze
    p = subparsers.add_parser(
        "analyze", aliases=["analyse"], help="reconstruct a frequency band spatially"
    )
    p.add_argument("survey")
    p.add_argument("-f", "--frequency", help="band centre, e.g. 433.92MHz")
    p.add_argument("-b", "--bandwidth", help="band width, e.g. 20kHz", default="20kHz")
    p.add_argument(
        "-m",
        "--method",
        default="peak",
        choices=["peak", "mean", "integrated"],
        help="how to reduce the band to one value per observation",
    )
    p.add_argument("--resolution", type=float, default=5.0, help="grid resolution in metres")
    p.add_argument("--cell-size", type=float, default=10.0, help="aggregation cell size")
    p.add_argument("--idw-power", type=float, default=2.0)
    p.add_argument("--search-radius", type=float, default=60.0)
    p.add_argument(
        "--max-distance",
        type=float,
        default=40.0,
        help="mask cells further than this from any measurement",
    )
    p.add_argument(
        "--include-flagged",
        action="store_true",
        help="include observations that carry quality flags",
    )
    p.add_argument("--out", help="output directory (default: inside the survey)")
    p.add_argument("--no-write", action="store_true", help="analyse without writing products")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analyze)

    # serve
    p = subparsers.add_parser("serve", help="browse a survey on a local map")
    p.add_argument("survey")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print(dim("\ninterrupted"), file=sys.stderr)
        return 130
    except BrokenPipeError:  # pragma: no cover - piping into head, etc.
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
