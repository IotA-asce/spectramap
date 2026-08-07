"""The acquisition engine: IQ blocks and position fixes in, a stored survey out.

Responsibilities, in order of importance:

1. **Never silently produce an incomplete survey.** Device faults, GPS loss, dropped
   samples, gain changes and disk pressure are recorded as events and as per-observation
   quality flags. A survey that went wrong says so in its own manifest.
2. **Keep one timeline.** Everything is aligned on the host monotonic clock; UTC is
   carried alongside for human consumption. See docs/architecture.md.
3. **Preserve raw measurements.** Poor-quality observations are flagged, not dropped.
   Filtering is an analysis-time decision, and the analyst gets to make it.

The loop is single threaded and pull-based: read a block, fold it into the current
integration window, drain any new position fixes, and emit an observation when the
window closes. The only thread in the system belongs to the serial GPS reader, which
hands fixes over through a lock.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from spectramap.dsp.detect import estimate_noise_floor
from spectramap.dsp.spectrum import SpectrumAccumulator, SpectrumEstimator, overload_fraction
from spectramap.geospatial.projection import path_length_m
from spectramap.positioning.base import PositionProvider
from spectramap.positioning.track import PositionTrack
from spectramap.sdr.base import IQBlock, SDRDevice, SDRError
from spectramap.storage.store import SurveyWriter
from spectramap.survey.config import SurveyConfig, SurveyMode
from spectramap.types import (
    AntennaInfo,
    DeviceCapabilities,
    DeviceInfo,
    Evidence,
    FFTConfig,
    PowerSpectrum,
    QualityFlag,
    ReceiverConfig,
    RFObservation,
    linear_to_db,
)


@dataclass(frozen=True, slots=True)
class _PendingObservation:
    """An observation waiting for the position fix that will bracket its midpoint."""

    observation_id: int
    t_start_utc_ns: int
    t_end_utc_ns: int
    t_start_mono_ns: int
    t_mid_mono_ns: int
    spectrum: PowerSpectrum
    n_ffts: int
    flags: QualityFlag
    sweep_step: int
    receiver_config: ReceiverConfig
    noise_floor_db: float
    peak_power_db: float
    peak_frequency_hz: float


@dataclass
class RecorderStatus:
    """Live counters, for the CLI display and for the final summary."""

    observations: int = 0
    blocks: int = 0
    dropped_sample_events: int = 0
    flagged_observations: int = 0
    no_position: int = 0
    overloads: int = 0
    started_mono_ns: int = 0
    elapsed_s: float = 0.0
    distance_m: float = 0.0
    last_power_db: float | None = None
    last_noise_floor_db: float | None = None
    latest_accuracy_m: float | None = None
    fixes: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "blocks": self.blocks,
            "dropped_sample_events": self.dropped_sample_events,
            "flagged_observations": self.flagged_observations,
            "observations_without_position": self.no_position,
            "overload_observations": self.overloads,
            "duration_s": self.elapsed_s,
            "distance_m": self.distance_m,
            "position_fixes": self.fixes,
            "errors": list(self.errors),
        }


class SurveyRecorder:
    """Runs one survey from configured hardware (or the simulator) into a ``.smap``."""

    def __init__(
        self,
        config: SurveyConfig,
        device: SDRDevice,
        position_provider: PositionProvider,
        output_path: str | Path,
        antenna: AntennaInfo | None = None,
        extra_manifest: dict[str, Any] | None = None,
        on_status: Callable[[RecorderStatus], None] | None = None,
    ) -> None:
        self.config = config
        self.device = device
        self.positions = position_provider
        self.output_path = Path(output_path)
        self.antenna = antenna if antenna is not None else config.antenna
        self.extra_manifest = dict(extra_manifest or {})
        self.on_status = on_status

        self.status = RecorderStatus()
        self.track = PositionTrack()
        self._writer: SurveyWriter | None = None
        self._estimator: SpectrumEstimator | None = None
        self._accumulator: SpectrumAccumulator | None = None
        self._next_observation_id = 0
        self._window_start_mono_ns: int | None = None
        self._window_start_utc_ns: int | None = None
        self._window_flags = QualityFlag.NONE
        self._last_gain: float | None = None
        self._gain_seen = False
        self._sweep_step = 0
        self._sweep_frequencies: list[float] = []
        self._sweep_step_started_mono_ns: int | None = None
        self._now_mono_ns = 0
        self._pending: list[_PendingObservation] = []
        self._stop_requested = False

    # -- Control ------------------------------------------------------------

    def request_stop(self) -> None:
        """Ask the loop to finish the current observation and close cleanly.

        Wired to SIGINT by the CLI, so Ctrl+C ends a survey rather than corrupting it.
        """
        self._stop_requested = True

    # -- Main loop ----------------------------------------------------------

    def run(self, max_duration_s: float | None = None) -> Path:
        """Record until stopped, and return the path of the finished survey."""
        cfg = self.config
        warnings = cfg.validate()

        # The container is created before the device is touched, so that a receiver
        # which fails to open still leaves a survey directory explaining why.
        self._writer = SurveyWriter(
            self.output_path,
            cfg,
            _placeholder_device_info(cfg),
            _placeholder_capabilities(cfg),
            antenna=self.antenna,
            extra_manifest=self.extra_manifest,
        )
        for warning in warnings:
            self._writer.add_warning(warning)

        try:
            self.device.open()
            self._configure_device()
            self._writer.device_info = self.device.get_device_info()
            self._writer.capabilities = self.device.get_capabilities()

            self.positions.start()
            self._writer.log_event(
                "info",
                "survey_started",
                f"recording {cfg.name}",
                {
                    "device": self._writer.device_info.to_dict(),
                    "position": self.positions.describe(),
                },
            )

            limit_s = max_duration_s if max_duration_s is not None else cfg.quality.max_duration_s
            self._run_loop(limit_s)
        except SDRError as exc:
            # A device that dies mid-survey must leave behind a survey that says so,
            # not an exception and a half-written directory.
            self.status.errors.append(str(exc))
            self._writer.log_event("error", "device_failure", str(exc))
        finally:
            self._flush_window(final=True)
            # Unconditionally drain the buffer. _flush_window returns early when no
            # partial window is in progress, and relying on it to do this would lose
            # every observation still waiting for a position fix.
            if self._writer is not None:
                self._write_pending(force=True)
                self._writer.flush()
            try:
                self.positions.stop()
            finally:
                self.device.close()

        self._finalise_summary()
        return self._writer.close(summary=self._summary())

    def _run_loop(self, limit_s: float | None) -> None:
        cfg = self.config
        block_samples = cfg.capture.block_samples
        interval_ns = int(cfg.aggregation.interval_ms * 1e6)
        disk_check_every = 200

        while not self._stop_requested:
            if self.status.blocks % disk_check_every == 0 and not self._check_disk():
                break

            block = self.device.read_iq(block_samples)
            self.status.blocks += 1
            # Elapsed time comes from the sample timeline, not the host wall clock.
            # That keeps duration limits and sweep dwells correct when the simulator
            # runs the world faster than real time.
            if self.status.started_mono_ns == 0:
                self.status.started_mono_ns = block.t_start_mono_ns
                self._sweep_step_started_mono_ns = block.t_start_mono_ns
            self._now_mono_ns = block.t_end_mono_ns
            self.status.elapsed_s = (block.t_end_mono_ns - self.status.started_mono_ns) / 1e9

            self._drain_positions()

            if block.n_samples == 0 or block.dropped_samples:
                self.status.dropped_sample_events += 1
                self._window_flags |= QualityFlag.DROPPED_SAMPLES
                self._writer.log_event(  # type: ignore[union-attr]
                    "warning",
                    "dropped_samples",
                    f"{block.dropped_samples} samples lost before block {block.sequence}",
                    {"sequence": block.sequence, "dropped": block.dropped_samples},
                    t_mono_ns=block.t_start_mono_ns,
                )
                if block.n_samples == 0:
                    if limit_s is not None and self.status.elapsed_s >= limit_s:
                        break
                    continue

            self._consume_block(block)

            if (
                self._window_start_mono_ns is not None
                and block.t_end_mono_ns - self._window_start_mono_ns >= interval_ns
            ):
                self._flush_window(end_mono_ns=block.t_end_mono_ns, block=block)
                # Checked here, right after a window closes, so that stopping leaves
                # no partial window in progress and the count is exact. Counts
                # observations *emitted*: writes lag by up to one position fix.
                if (
                    cfg.quality.max_observations is not None
                    and self._next_observation_id >= cfg.quality.max_observations
                ):
                    break

            if self._sweep_frequencies and self._should_advance_sweep():
                self._advance_sweep()

            if self.on_status is not None:
                self.on_status(self.status)
            if limit_s is not None and self.status.elapsed_s >= limit_s:
                break

    # -- Setup --------------------------------------------------------------

    def _configure_device(self) -> None:
        cfg = self.config
        rate = self.device.set_sample_rate(cfg.capture.sample_rate_hz)
        if cfg.mode is SurveyMode.SWEEP and cfg.capture.sweep is not None:
            self._sweep_frequencies = cfg.capture.sweep.steps()
            center = self._sweep_frequencies[0]
        else:
            # Tune the LO away from the target so the target does not land in the DC
            # bin. Frequency axes stay absolute, so this is invisible downstream.
            center = cfg.capture.tuned_center_hz
        center = self.device.set_center_frequency(center)
        gain = self.device.set_gain(cfg.receiver.gain_db)
        self._last_gain = gain
        self._gain_seen = True
        self._make_estimator(center, rate)

    def _make_estimator(self, center_hz: float, sample_rate_hz: float) -> None:
        self._estimator = SpectrumEstimator(self.config.fft, sample_rate_hz, center_hz)
        n_bins = self.config.stored_bins()
        self._accumulator = SpectrumAccumulator(n_bins)

    # -- Sweep --------------------------------------------------------------

    def _should_advance_sweep(self) -> bool:
        sweep = self.config.capture.sweep
        if sweep is None or self._sweep_step_started_mono_ns is None:
            return False
        elapsed_ms = (self._now_mono_ns - self._sweep_step_started_mono_ns) / 1e6
        return elapsed_ms >= sweep.dwell_ms

    def _advance_sweep(self) -> None:
        """Retune to the next sweep window, closing the current observation first."""
        self._flush_window(end_mono_ns=self._now_mono_ns)
        self._sweep_step = (self._sweep_step + 1) % len(self._sweep_frequencies)
        target = self._sweep_frequencies[self._sweep_step]
        actual = self.device.set_center_frequency(target)
        self._make_estimator(actual, self.device.config.sample_rate_hz)
        self._sweep_step_started_mono_ns = self._now_mono_ns
        self._window_flags |= QualityFlag.RETUNED
        assert self._writer is not None
        self._writer.log_event(
            "info",
            "retune",
            f"sweep step {self._sweep_step}: {actual / 1e6:.4f} MHz",
            {"step": self._sweep_step, "center_frequency_hz": actual},
        )

    # -- Per-block work -----------------------------------------------------

    def _consume_block(self, block: IQBlock) -> None:
        assert self._estimator is not None and self._accumulator is not None
        if self._window_start_mono_ns is None:
            self._window_start_mono_ns = block.t_start_mono_ns
            self._window_start_utc_ns = block.t_start_utc_ns

        clipped = overload_fraction(block.samples)
        if clipped > self.config.quality.overload_fraction:
            self._window_flags |= QualityFlag.SDR_OVERLOAD

        try:
            linear = self._estimator.linear_power(block.samples)
        except ValueError:
            # Short block (a truncated read at shutdown): count it, do not fake it.
            self._window_flags |= QualityFlag.PARTIAL_INTEGRATION
            return

        if self.config.aggregation.bin_decimation > 1:
            linear = _decimate_bins(linear, self.config.aggregation.bin_decimation)
        self._accumulator.add(linear, weight=self._estimator.segments_in(block.n_samples))

        current = self.device.config
        if self._gain_seen and current.gain_db != self._last_gain:
            self._window_flags |= QualityFlag.GAIN_CHANGED
            assert self._writer is not None
            self._writer.log_event(
                "warning",
                "gain_changed",
                f"gain changed from {self._last_gain} to {current.gain_db} dB; "
                "power is not comparable across this boundary",
                {"from_db": self._last_gain, "to_db": current.gain_db},
                t_mono_ns=block.t_start_mono_ns,
            )
            self._last_gain = current.gain_db
        if current.agc:
            self._window_flags |= QualityFlag.AGC_ACTIVE

    def _drain_positions(self) -> None:
        fixes = self.positions.poll()
        if not fixes:
            return
        assert self._writer is not None
        for fix in fixes:
            self.track.add(fix)
            self._writer.add_position(fix)
        self.status.fixes += len(fixes)
        self.status.latest_accuracy_m = fixes[-1].horizontal_accuracy_m
        # Refresh the live distance periodically rather than per fix: recomputing the
        # smoothed track length is O(n), and the display does not need it more often.
        if self.status.fixes % 10 == 0:
            self._update_distance()
        if self._pending and self._writer is not None:
            self._write_pending()

    # -- Observation emission ------------------------------------------------

    def _flush_window(
        self,
        end_mono_ns: int | None = None,
        end_utc_ns: int | None = None,
        block: IQBlock | None = None,
        final: bool = False,
    ) -> None:
        """Close the current integration window and store it as one observation."""
        if self._accumulator is None or self._accumulator.is_empty:
            return
        if self._writer is None or self._estimator is None:
            return
        if self._window_start_mono_ns is None or self._window_start_utc_ns is None:
            return

        if end_mono_ns is None:
            # No block means this is the final flush at shutdown. The window ends
            # where the last consumed block ended -- using the window's own start
            # would record an observation of zero duration.
            end_mono_ns = block.t_end_mono_ns if block else max(
                self._now_mono_ns, self._window_start_mono_ns
            )
        if end_utc_ns is None:
            duration_ns = end_mono_ns - self._window_start_mono_ns
            end_utc_ns = self._window_start_utc_ns + duration_ns

        mid_mono_ns = (self._window_start_mono_ns + end_mono_ns) // 2
        span_ms = (end_mono_ns - self._window_start_mono_ns) / 1e6
        if span_ms < self.config.aggregation.interval_ms:
            # A truncated window (survey stopped mid-integration) averaged fewer FFTs
            # than every other observation, so its variance is higher. Mark it.
            self._window_flags |= QualityFlag.PARTIAL_INTEGRATION
        mean_linear = self._accumulator.mean_linear()
        power_db = np.asarray(linear_to_db(mean_linear), dtype=np.float64)
        spectrum = PowerSpectrum(
            frequencies_hz=self._stored_frequencies(),
            power_db=power_db,
            unit=self._estimator.unit,
            normalization=self.config.fft.normalization,
            enbw_hz=self._estimator.enbw_hz * self.config.aggregation.bin_decimation,
            bin_width_hz=self._estimator.bin_width_hz * self.config.aggregation.bin_decimation,
            n_averages=self._accumulator.count,
        )

        noise_floor = float(estimate_noise_floor(spectrum))
        peak_index = int(np.argmax(power_db))
        peak_power = float(power_db[peak_index])
        peak_frequency = float(spectrum.frequencies_hz[peak_index])

        self._pending.append(
            _PendingObservation(
                observation_id=self._next_observation_id,
                t_start_utc_ns=self._window_start_utc_ns,
                t_end_utc_ns=end_utc_ns,
                t_start_mono_ns=self._window_start_mono_ns,
                t_mid_mono_ns=mid_mono_ns,
                spectrum=spectrum,
                n_ffts=self._accumulator.count,
                flags=self._window_flags,
                sweep_step=self._sweep_step,
                receiver_config=self._current_receiver_config(),
                noise_floor_db=noise_floor,
                peak_power_db=peak_power,
                peak_frequency_hz=peak_frequency,
            )
        )
        self._next_observation_id += 1

        self.status.last_power_db = peak_power
        self.status.last_noise_floor_db = noise_floor

        self._accumulator.reset()
        self._window_start_mono_ns = None
        self._window_start_utc_ns = None
        # RETUNED is a property of the window that straddled a retune, not a sticky
        # state; everything else is recomputed per block anyway.
        self._window_flags = QualityFlag.NONE

        self._write_pending(force=final)
        if final:
            self._writer.flush()

    def _write_pending(self, force: bool = False) -> None:
        """Write out observations whose position can now be resolved by interpolation.

        An observation is held until a fix arrives *after* its midpoint, so that its
        position can be interpolated between two real fixes rather than extrapolated
        from the last one. With a 1 Hz receiver that costs at most about a second of
        write latency and nothing at all in accuracy -- whereas resolving immediately
        would leave roughly half of all observations extrapolated, simply because the
        next fix had not arrived yet.

        ``force`` flushes the tail at the end of a survey, where there is no later
        fix and extrapolation is genuinely the best available answer. Those
        observations are flagged POSITION_EXTRAPOLATED, as they should be.
        """
        assert self._writer is not None
        latest = self.track.latest
        horizon = latest.t_mono_ns if latest is not None else None

        remaining: list[_PendingObservation] = []
        for pending in self._pending:
            covered = horizon is not None and horizon >= pending.t_mid_mono_ns
            if not (covered or force):
                remaining.append(pending)
                continue

            resolved = self.track.resolve(
                pending.t_mid_mono_ns,
                max_age_ms=self.config.location.max_age_ms,
                accuracy_limit_m=self.config.location.accuracy_limit_m,
            )
            flags = pending.flags | resolved.flags
            observation = RFObservation(
                observation_id=pending.observation_id,
                t_start_utc_ns=pending.t_start_utc_ns,
                t_end_utc_ns=pending.t_end_utc_ns,
                t_start_mono_ns=pending.t_start_mono_ns,
                position=resolved,
                receiver_id=self.device.get_device_info().receiver_id,
                antenna_id=self.antenna.antenna_id,
                receiver_config=pending.receiver_config,
                fft_config=self.config.fft,
                spectrum=pending.spectrum,
                n_ffts=pending.n_ffts,
                flags=flags,
                sweep_step=pending.sweep_step,
            )
            self._writer.append_observation(
                observation,
                noise_floor_db=pending.noise_floor_db,
                peak_power_db=pending.peak_power_db,
                peak_frequency_hz=pending.peak_frequency_hz,
            )
            self.status.observations += 1
            if flags:
                self.status.flagged_observations += 1
            if QualityFlag.NO_LOCATION & flags:
                self.status.no_position += 1
            if QualityFlag.SDR_OVERLOAD & flags:
                self.status.overloads += 1
                self._writer.log_event(
                    "warning",
                    "overload",
                    "receiver front end is clipping; measured power under-reads the true field",
                    {"observation_id": observation.observation_id},
                )
        self._pending = remaining

    def _stored_frequencies(self) -> np.ndarray:
        assert self._estimator is not None
        freqs = self._estimator.frequencies_hz
        factor = self.config.aggregation.bin_decimation
        if factor <= 1:
            return freqs
        usable = (freqs.size // factor) * factor
        return freqs[:usable].reshape(-1, factor).mean(axis=1)

    def _current_receiver_config(self) -> ReceiverConfig:
        try:
            return self.device.config
        except Exception:
            cfg = self.config
            return ReceiverConfig(
                center_frequency_hz=cfg.capture.center_frequency_hz,
                sample_rate_hz=cfg.capture.sample_rate_hz,
                gain_db=cfg.receiver.gain_db,
                agc=cfg.receiver.agc,
            )

    # -- Safety -------------------------------------------------------------

    def _check_disk(self) -> bool:
        """Stop before filling the disk, rather than dying part-way through a write."""
        assert self._writer is not None
        free_mb = self._writer.free_disk_mb()
        if free_mb < self.config.quality.min_free_disk_mb:
            message = (
                f"stopping: {free_mb:.0f} MB free is below the configured minimum of "
                f"{self.config.quality.min_free_disk_mb:.0f} MB"
            )
            self.status.errors.append(message)
            self._writer.log_event("error", "disk_full", message, {"free_mb": free_mb})
            return False
        return True

    # -- Summary ------------------------------------------------------------

    def _update_distance(self) -> None:
        fixes = self.track.fixes
        if len(fixes) >= 2:
            self.status.distance_m = path_length_m(
                np.array([f.latitude for f in fixes]),
                np.array([f.longitude for f in fixes]),
                smooth_window=5,
            )

    def _finalise_summary(self) -> None:
        fixes = self.track.fixes
        if len(fixes) >= 2:
            self.status.distance_m = path_length_m(
                np.array([f.latitude for f in fixes]),
                np.array([f.longitude for f in fixes]),
                smooth_window=5,
            )
        if self.status.started_mono_ns and self._now_mono_ns:
            self.status.elapsed_s = (self._now_mono_ns - self.status.started_mono_ns) / 1e9

    def _summary(self) -> dict[str, Any]:
        summary = self.status.to_dict()
        if self._estimator is not None:
            summary["dsp"] = self._estimator.describe()
        summary["complete"] = not self.status.errors
        return summary


def _decimate_bins(linear: np.ndarray, factor: int) -> np.ndarray:
    """Reduce bin count by integer factor, averaging power in the linear domain.

    Trades frequency resolution for storage. Averaging linear power (not dB) keeps
    the result a correct mean power for the wider bin.
    """
    usable = (linear.size // factor) * factor
    return linear[:usable].reshape(-1, factor).mean(axis=1)


def build_fft_config(size: int, window: str = "hann") -> FFTConfig:
    """Convenience for scripts and tests."""
    return FFTConfig(size=size, window=window)


def _placeholder_device_info(cfg: SurveyConfig) -> DeviceInfo:
    """Stand-in identity used until the real device answers.

    If the receiver never opens, this is what the manifest keeps -- clearly marked as
    unopened rather than pretending a device was present.
    """
    return DeviceInfo(
        driver=cfg.receiver.driver,
        index=cfg.receiver.index,
        serial=cfg.receiver.serial or "",
        product="(device not opened)",
    )


def _placeholder_capabilities(cfg: SurveyConfig) -> DeviceCapabilities:
    span = cfg.capture.span_hz
    return DeviceCapabilities(
        driver=cfg.receiver.driver,
        tuning_range=span,
        sample_rates_hz=(cfg.capture.sample_rate_hz,),
        gains_db=(),
        supports_agc=True,
        max_bandwidth_hz=cfg.capture.sample_rate_hz,
        full_scale_bits=8,
        tuning_evidence=Evidence.ASSUMED,
        notes="Placeholder: the device was not opened, so nothing was read from it.",
    )


__all__ = ["RecorderStatus", "SurveyRecorder", "build_fft_config"]
