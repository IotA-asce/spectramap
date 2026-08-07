"""The ``.smap`` survey container.

Layout::

    <name>.smap/
    |-- manifest.json      Format version, configuration, device, antenna, summary
    |-- survey.sqlite      Observation index, raw position track, event log
    |-- spectra/
    |   `-- chunk_000000.f32   Flat little-endian float32, C order, (n_rows, n_bins)
    |-- route.geojson      The survey path and sample points, for any GIS tool
    |-- calibration/       Correction tables (empty in V0)
    `-- processing/        Provenance records for every derived product

Why this split
--------------
Spectra are big and regular; metadata is small and needs querying. Putting a 4096-bin
float32 array in a SQLite BLOB per observation would make the common question -- "what
was the power near 433.92 MHz along the whole route?" -- cost a full read of every FFT
ever recorded. Here that question memory-maps the chunk files and takes a strided
column slice, touching a few hundred kilobytes instead of a few hundred megabytes.

The chunk format is deliberately the dullest thing that could work: a raw C-order
float32 array with its shape recorded in the manifest and in the ``chunks`` table.
Another developer reads it in one line, with no SpectraMap and no exotic library::

    np.memmap("spectra/chunk_000000.f32", dtype="<f4", mode="r").reshape(-1, n_bins)

A chunk is homogeneous: every row shares one frequency axis. Retuning (a sweep step)
starts a new chunk, so a chunk never mixes windows measured at different frequencies.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from spectramap.types import (
    AntennaInfo,
    DeviceCapabilities,
    DeviceInfo,
    Evidence,
    FixQuality,
    Normalization,
    Position,
    PowerSpectrum,
    PowerUnit,
    QualityFlag,
    ResolvedPosition,
    RFObservation,
)
from spectramap.version import FORMAT_VERSION, __version__

if TYPE_CHECKING:  # Imported lazily at runtime: survey.config imports storage back.
    from spectramap.survey.config import SurveyConfig

# Little-endian float32. Explicit byte order so a survey written on one machine
# reads correctly on another.
SPECTRA_DTYPE = np.dtype("<f4")

# Rows per chunk file. 4096 rows x 4096 bins x 4 bytes = 64 MiB, which keeps
# individual files manageable while making column slices efficient.
DEFAULT_CHUNK_ROWS = 4096

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per geotagged spectrum measurement. Summary columns (noise floor, peak)
-- are computed at record time so that maps and status views never need to touch the
-- spectra files.
CREATE TABLE IF NOT EXISTS observations (
    observation_id      INTEGER PRIMARY KEY,
    t_start_utc_ns      INTEGER NOT NULL,
    t_end_utc_ns        INTEGER NOT NULL,
    t_start_mono_ns     INTEGER NOT NULL,
    latitude            REAL,
    longitude           REAL,
    altitude_m          REAL,
    h_accuracy_m        REAL,
    v_accuracy_m        REAL,
    speed_mps           REAL,
    position_evidence   TEXT,
    position_age_ms     REAL,
    center_frequency_hz REAL    NOT NULL,
    sample_rate_hz      REAL    NOT NULL,
    gain_db             REAL,
    agc                 INTEGER NOT NULL,
    n_ffts              INTEGER NOT NULL,
    n_bins              INTEGER NOT NULL,
    sweep_step          INTEGER NOT NULL DEFAULT 0,
    flags               INTEGER NOT NULL DEFAULT 0,
    chunk_id            INTEGER NOT NULL,
    chunk_row           INTEGER NOT NULL,
    noise_floor_db      REAL,
    peak_power_db       REAL,
    peak_frequency_hz   REAL
);
CREATE INDEX IF NOT EXISTS idx_obs_time  ON observations(t_start_utc_ns);
CREATE INDEX IF NOT EXISTS idx_obs_freq  ON observations(center_frequency_hz);
CREATE INDEX IF NOT EXISTS idx_obs_space ON observations(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_obs_chunk ON observations(chunk_id, chunk_row);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id   INTEGER PRIMARY KEY,
    path       TEXT    NOT NULL,
    n_bins     INTEGER NOT NULL,
    n_rows     INTEGER NOT NULL,
    dtype      TEXT    NOT NULL,
    f_start_hz REAL    NOT NULL,
    f_step_hz  REAL    NOT NULL
);

-- The raw position track, every fix as received. Kept separately from the resolved
-- per-observation positions so the original measurements survive independently of
-- how they were interpolated.
CREATE TABLE IF NOT EXISTS positions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    t_utc_ns       INTEGER NOT NULL,
    t_mono_ns      INTEGER NOT NULL,
    latitude       REAL    NOT NULL,
    longitude      REAL    NOT NULL,
    altitude_m     REAL,
    h_accuracy_m   REAL,
    v_accuracy_m   REAL,
    speed_mps      REAL,
    heading_deg    REAL,
    fix_quality    TEXT,
    satellites     INTEGER,
    source         TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_time ON positions(t_mono_ns);

-- Anything that happened which a reader must know about: device faults, gain
-- changes, GPS loss, retunes, disk pressure. A survey with gaps says so.
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    t_utc_ns  INTEGER NOT NULL,
    t_mono_ns INTEGER,
    level     TEXT    NOT NULL,
    kind      TEXT    NOT NULL,
    message   TEXT    NOT NULL,
    detail    TEXT
);
"""


@dataclass(frozen=True, slots=True)
class ChunkInfo:
    chunk_id: int
    path: str
    n_bins: int
    n_rows: int
    dtype: str
    f_start_hz: float
    f_step_hz: float

    def frequencies(self) -> np.ndarray:
        return self.f_start_hz + np.arange(self.n_bins, dtype=np.float64) * self.f_step_hz


class SurveyWriter:
    """Appends observations, positions and events to a ``.smap`` directory.

    Durability: SQLite runs in WAL mode and commits every ``commit_every``
    observations; spectra are flushed to the OS on the same cadence. A survey killed
    mid-walk therefore loses at most one commit interval, and what it does have is a
    valid, readable survey rather than a corrupt one.
    """

    def __init__(
        self,
        path: str | Path,
        config: SurveyConfig,
        device_info: DeviceInfo,
        capabilities: DeviceCapabilities,
        antenna: AntennaInfo | None = None,
        chunk_rows: int = DEFAULT_CHUNK_ROWS,
        commit_every: int = 64,
        extra_manifest: dict[str, Any] | None = None,
        survey_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.config = config
        self.device_info = device_info
        self.capabilities = capabilities
        self.antenna = antenna if antenna is not None else config.antenna
        self.chunk_rows = int(chunk_rows)
        self.commit_every = int(commit_every)
        self.extra_manifest = dict(extra_manifest or {})
        self.survey_id = survey_id or f"{config.name}-{int(time.time())}"

        self.path.mkdir(parents=True, exist_ok=True)
        (self.path / "spectra").mkdir(exist_ok=True)
        (self.path / "calibration").mkdir(exist_ok=True)
        (self.path / "processing").mkdir(exist_ok=True)

        self._db = sqlite3.connect(self.path / "survey.sqlite")
        self._db.executescript(SCHEMA)
        self._db.commit()

        self._chunk_id = -1
        self._chunk_file: Any = None
        self._chunk_rows_written = 0
        self._chunk_key: tuple[int, float, float] | None = None
        self._n_observations = 0
        self._since_commit = 0
        self._started_utc_ns = time.time_ns()
        self._closed = False
        self._warnings: list[str] = []

        self._set_meta("survey_id", self.survey_id)
        self._set_meta("format_version", FORMAT_VERSION)
        self._set_meta("software_version", __version__)
        self._set_meta("started_utc_ns", str(self._started_utc_ns))

    # -- Metadata -----------------------------------------------------------

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def add_warning(self, message: str) -> None:
        """Record a caveat that belongs in the manifest, not just in the terminal."""
        self._warnings.append(message)
        self.log_event("warning", "configuration", message)

    def log_event(
        self,
        level: str,
        kind: str,
        message: str,
        detail: dict[str, Any] | None = None,
        t_mono_ns: int | None = None,
    ) -> None:
        """Record something a later reader must know about."""
        self._db.execute(
            "INSERT INTO events(t_utc_ns, t_mono_ns, level, kind, message, detail) "
            "VALUES(?,?,?,?,?,?)",
            (
                time.time_ns(),
                t_mono_ns,
                level,
                kind,
                message,
                json.dumps(detail) if detail else None,
            ),
        )

    # -- Data ---------------------------------------------------------------

    def add_position(self, fix: Position) -> None:
        """Store a raw position fix exactly as received."""
        self._db.execute(
            "INSERT INTO positions(t_utc_ns, t_mono_ns, latitude, longitude, altitude_m,"
            " h_accuracy_m, v_accuracy_m, speed_mps, heading_deg, fix_quality, satellites,"
            " source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fix.t_utc_ns,
                fix.t_mono_ns,
                fix.latitude,
                fix.longitude,
                fix.altitude_m,
                fix.horizontal_accuracy_m,
                fix.vertical_accuracy_m,
                fix.speed_mps,
                fix.heading_deg,
                fix.fix_quality.value,
                fix.satellites,
                fix.source,
            ),
        )

    def append_observation(
        self,
        obs: RFObservation,
        noise_floor_db: float | None = None,
        peak_power_db: float | None = None,
        peak_frequency_hz: float | None = None,
    ) -> int:
        """Append one observation. Returns its ``observation_id``."""
        if self._closed:
            raise RuntimeError("survey writer is closed")

        spectrum = obs.spectrum
        n_bins = spectrum.n_bins
        f_start = float(spectrum.frequencies_hz[0])
        f_step = float(spectrum.bin_width_hz)
        key = (n_bins, round(f_start, 3), round(f_step, 6))

        if self._chunk_file is None or key != self._chunk_key or (
            self._chunk_rows_written >= self.chunk_rows
        ):
            self._roll_chunk(key)

        row = self._chunk_rows_written
        # float32 gives ~7 significant digits: far finer than the ~0.01 dB
        # repeatability of any RTL-SDR, and half the size of float64.
        self._chunk_file.write(
            np.asarray(spectrum.power_db, dtype=SPECTRA_DTYPE).tobytes(order="C")
        )
        self._chunk_rows_written += 1

        pos = obs.position.position
        # The stored flag set is always the union of the observation's own flags and
        # those describing how its position was resolved. Merging here rather than
        # trusting every caller to do it means a reader can rely on one complete
        # column instead of reconstructing the union itself.
        flags = obs.flags | obs.position.flags
        self._db.execute(
            "INSERT INTO observations(observation_id, t_start_utc_ns, t_end_utc_ns,"
            " t_start_mono_ns, latitude, longitude, altitude_m, h_accuracy_m, v_accuracy_m,"
            " speed_mps, position_evidence, position_age_ms, center_frequency_hz,"
            " sample_rate_hz, gain_db, agc, n_ffts, n_bins, sweep_step, flags, chunk_id,"
            " chunk_row, noise_floor_db, peak_power_db, peak_frequency_hz)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                obs.observation_id,
                obs.t_start_utc_ns,
                obs.t_end_utc_ns,
                obs.t_start_mono_ns,
                pos.latitude if pos else None,
                pos.longitude if pos else None,
                pos.altitude_m if pos else None,
                pos.horizontal_accuracy_m if pos else None,
                pos.vertical_accuracy_m if pos else None,
                pos.speed_mps if pos else None,
                obs.position.evidence.value,
                obs.position.age_ms,
                obs.receiver_config.center_frequency_hz,
                obs.receiver_config.sample_rate_hz,
                obs.receiver_config.gain_db,
                int(obs.receiver_config.agc),
                obs.n_ffts,
                n_bins,
                obs.sweep_step,
                int(flags),
                self._chunk_id,
                row,
                noise_floor_db,
                peak_power_db,
                peak_frequency_hz,
            ),
        )
        self._n_observations += 1
        self._since_commit += 1
        if self._since_commit >= self.commit_every:
            self.flush()
        return obs.observation_id

    def _roll_chunk(self, key: tuple[int, float, float]) -> None:
        """Start a new chunk file, closing any current one."""
        self._finalise_chunk()
        self._chunk_id += 1
        self._chunk_key = key
        self._chunk_rows_written = 0
        rel = f"spectra/chunk_{self._chunk_id:06d}.f32"
        self._chunk_file = open(self.path / rel, "wb")
        n_bins, f_start, f_step = key
        self._db.execute(
            "INSERT INTO chunks(chunk_id, path, n_bins, n_rows, dtype, f_start_hz, f_step_hz)"
            " VALUES(?,?,?,?,?,?,?)",
            (self._chunk_id, rel, n_bins, 0, SPECTRA_DTYPE.str, f_start, f_step),
        )

    def _finalise_chunk(self) -> None:
        if self._chunk_file is None:
            return
        self._chunk_file.flush()
        os.fsync(self._chunk_file.fileno())
        self._chunk_file.close()
        self._chunk_file = None
        self._db.execute(
            "UPDATE chunks SET n_rows=? WHERE chunk_id=?",
            (self._chunk_rows_written, self._chunk_id),
        )

    def flush(self) -> None:
        """Commit the index and push spectra to the OS."""
        if self._chunk_file is not None:
            self._chunk_file.flush()
            self._db.execute(
                "UPDATE chunks SET n_rows=? WHERE chunk_id=?",
                (self._chunk_rows_written, self._chunk_id),
            )
        self._db.commit()
        self._since_commit = 0

    @property
    def n_observations(self) -> int:
        return self._n_observations

    def free_disk_mb(self) -> float:
        """Free space on the survey's filesystem, in MiB."""
        usage = shutil.disk_usage(self.path)
        return usage.free / (1024 * 1024)

    # -- Closing ------------------------------------------------------------

    def close(self, summary: dict[str, Any] | None = None) -> Path:
        """Finalise the container: manifest, route, indices. Safe to call twice."""
        if self._closed:
            return self.path
        self._finalise_chunk()
        self._db.commit()

        manifest = self._build_manifest(summary or {})
        (self.path / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=False), encoding="utf-8"
        )
        self._write_route_geojson()

        self._set_meta("ended_utc_ns", str(time.time_ns()))
        self._set_meta("n_observations", str(self._n_observations))
        self._db.commit()
        self._db.close()
        self._closed = True
        return self.path

    def _build_manifest(self, summary: dict[str, Any]) -> dict[str, Any]:
        cur = self._db.execute(
            "SELECT chunk_id, path, n_bins, n_rows, dtype, f_start_hz, f_step_hz FROM chunks"
            " ORDER BY chunk_id"
        )
        chunks = [
            {
                "chunk_id": r[0],
                "path": r[1],
                "n_bins": r[2],
                "n_rows": r[3],
                "dtype": r[4],
                "layout": "C-order (n_rows, n_bins)",
                "f_start_hz": r[5],
                "f_step_hz": r[6],
            }
            for r in cur.fetchall()
        ]
        ended = time.time_ns()
        return {
            "format": "spectramap-survey",
            "format_version": FORMAT_VERSION,
            "software_version": __version__,
            "survey_id": self.survey_id,
            "name": self.config.name,
            "mode": self.config.mode.value,
            "notes": self.config.notes,
            "started_utc_ns": self._started_utc_ns,
            "ended_utc_ns": ended,
            "duration_s": (ended - self._started_utc_ns) / 1e9,
            "n_observations": self._n_observations,
            "receiver": self.device_info.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "antenna": self.antenna.to_dict(),
            "configuration": self.config.to_dict(),
            "units": {
                "power": (
                    PowerUnit.DBFS_PER_HZ.value
                    if self.config.fft.normalization is Normalization.DENSITY
                    else PowerUnit.DBFS.value
                ),
                "calibrated": False,
                "reference": (
                    "dB relative to a full-scale complex sinusoid at the recorded gain "
                    "setting. NOT absolute field strength and NOT dBm."
                ),
            },
            "spectra": {
                "encoding": "raw little-endian float32, C order, no header",
                "read_recipe": (
                    "np.memmap(path, dtype='<f4', mode='r').reshape(-1, n_bins); "
                    "frequency of bin k is f_start_hz + k * f_step_hz"
                ),
                "chunks": chunks,
            },
            "warnings": list(self._warnings),
            "summary": summary,
            **self.extra_manifest,
        }

    def _write_route_geojson(self) -> None:
        """Write the raw GPS track and the measurement points as GeoJSON.

        Both layers are exported: the track is what the receiver reported, the points
        are where observations were actually placed. Seeing them disagree is often
        the first sign of a timing or interpolation problem.
        """
        track = self._db.execute(
            "SELECT longitude, latitude, altitude_m FROM positions ORDER BY t_mono_ns"
        ).fetchall()
        points = self._db.execute(
            "SELECT observation_id, longitude, latitude, t_start_utc_ns, flags,"
            " position_evidence, peak_power_db FROM observations"
            " WHERE latitude IS NOT NULL AND longitude IS NOT NULL"
            " ORDER BY observation_id"
        ).fetchall()

        features: list[dict[str, Any]] = []
        if len(track) >= 2:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[r[0], r[1]] for r in track],
                    },
                    "properties": {"layer": "gps_track", "evidence": Evidence.MEASURED.value},
                }
            )
        for oid, lon, lat, t_ns, flags, evidence, peak in points:
            features.append(
                {
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                    "properties": {
                        "layer": "observation",
                        "observation_id": oid,
                        "t_utc_ns": t_ns,
                        "flags": QualityFlag.describe(flags),
                        "position_evidence": evidence,
                        "peak_power_db": peak,
                    },
                }
            )
        (self.path / "route.geojson").write_text(
            json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8"
        )

    def __enter__(self) -> SurveyWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class SurveyReader:
    """Read-only access to a ``.smap`` container.

    Spectra are memory-mapped, so extracting one frequency across a whole survey
    reads only the pages that column touches.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        manifest_path = self.path / "manifest.json"
        db_path = self.path / "survey.sqlite"
        if not db_path.exists():
            raise FileNotFoundError(f"{self.path} is not a SpectraMap survey (no survey.sqlite)")
        self.manifest: dict[str, Any] = (
            json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest_path.exists()
            else {}
        )
        if self.manifest and self.manifest.get("format") != "spectramap-survey":
            raise ValueError(f"{self.path} has an unrecognised manifest format")
        # check_same_thread=False because the local viewer serves requests from a
        # thread pool. The connection is read-only, but SQLite still requires that
        # concurrent use be serialised, which the lock below does.
        self._db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._maps: dict[int, np.ndarray] = {}

    def _query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # -- Description --------------------------------------------------------

    @property
    def survey_id(self) -> str:
        return str(self.manifest.get("survey_id", self.path.stem))

    @property
    def name(self) -> str:
        return str(self.manifest.get("name", self.path.stem))

    @property
    def config(self) -> SurveyConfig:
        from spectramap.survey.config import SurveyConfig

        return SurveyConfig.from_dict(self.manifest.get("configuration", {}))

    @property
    def power_unit(self) -> PowerUnit:
        return PowerUnit(self.manifest.get("units", {}).get("power", PowerUnit.DBFS_PER_HZ.value))

    @property
    def is_calibrated(self) -> bool:
        return bool(self.manifest.get("units", {}).get("calibrated", False))

    @property
    def warnings(self) -> list[str]:
        return list(self.manifest.get("warnings", []))

    def chunks(self) -> list[ChunkInfo]:
        rows = self._query("SELECT * FROM chunks ORDER BY chunk_id")
        out: list[ChunkInfo] = []
        for r in rows:
            # Trust the file, not the recorded row count: a survey interrupted by a
            # crash may have more rows on disk than the last commit knew about, and
            # a partial final row must be ignored rather than read as data.
            full = self.path / r["path"]
            n_rows = int(full.stat().st_size // (r["n_bins"] * SPECTRA_DTYPE.itemsize))
            out.append(
                ChunkInfo(
                    chunk_id=r["chunk_id"],
                    path=r["path"],
                    n_bins=r["n_bins"],
                    n_rows=n_rows,
                    dtype=r["dtype"],
                    f_start_hz=r["f_start_hz"],
                    f_step_hz=r["f_step_hz"],
                )
            )
        return out

    def _chunk_map(self, chunk: ChunkInfo) -> np.ndarray:
        cached = self._maps.get(chunk.chunk_id)
        if cached is not None:
            return cached
        if chunk.n_rows == 0:
            mapped = np.empty((0, chunk.n_bins), dtype=SPECTRA_DTYPE)
        else:
            mapped = np.memmap(
                self.path / chunk.path,
                dtype=SPECTRA_DTYPE,
                mode="r",
                shape=(chunk.n_rows, chunk.n_bins),
            )
        self._maps[chunk.chunk_id] = mapped
        return mapped

    # -- Observations -------------------------------------------------------

    @property
    def n_observations(self) -> int:
        return int(self._query("SELECT COUNT(*) FROM observations")[0][0])

    def observation_rows(
        self,
        with_position_only: bool = False,
        exclude_flags: QualityFlag = QualityFlag.NONE,
        sweep_step: int | None = None,
    ) -> list[sqlite3.Row]:
        """Index rows, optionally filtered. Never touches the spectra files."""
        sql = "SELECT * FROM observations"
        clauses: list[str] = []
        params: list[Any] = []
        if with_position_only:
            clauses.append("latitude IS NOT NULL AND longitude IS NOT NULL")
        if exclude_flags:
            clauses.append("(flags & ?) = 0")
            params.append(int(exclude_flags))
        if sweep_step is not None:
            clauses.append("sweep_step = ?")
            params.append(sweep_step)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY observation_id"
        return self._query(sql, params)

    def positions(self) -> list[Position]:
        """The raw position track, exactly as recorded."""
        rows = self._query("SELECT * FROM positions ORDER BY t_mono_ns")
        return [
            Position(
                t_utc_ns=r["t_utc_ns"],
                t_mono_ns=r["t_mono_ns"],
                latitude=r["latitude"],
                longitude=r["longitude"],
                altitude_m=r["altitude_m"],
                horizontal_accuracy_m=r["h_accuracy_m"],
                vertical_accuracy_m=r["v_accuracy_m"],
                speed_mps=r["speed_mps"],
                heading_deg=r["heading_deg"],
                fix_quality=FixQuality(r["fix_quality"] or "none"),
                satellites=r["satellites"],
                source=r["source"] or "unknown",
            )
            for r in rows
        ]

    def events(self, level: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM events"
        params: list[Any] = []
        if level:
            sql += " WHERE level = ?"
            params.append(level)
        sql += " ORDER BY id"
        return [dict(r) for r in self._query(sql, params)]

    def spectrum(self, observation_id: int) -> PowerSpectrum:
        """The full spectrum of one observation."""
        found = self._query(
            "SELECT * FROM observations WHERE observation_id = ?", (observation_id,)
        )
        row = found[0] if found else None
        if row is None:
            raise KeyError(f"no observation {observation_id} in {self.path}")
        chunk = next(c for c in self.chunks() if c.chunk_id == row["chunk_id"])
        data = np.asarray(self._chunk_map(chunk)[row["chunk_row"]], dtype=np.float64)
        return PowerSpectrum(
            frequencies_hz=chunk.frequencies(),
            power_db=data,
            unit=self.power_unit,
            normalization=self.config.fft.normalization,
            enbw_hz=self._enbw_hz(chunk),
            bin_width_hz=chunk.f_step_hz,
            n_averages=int(row["n_ffts"]),
            evidence=Evidence.MEASURED,
        )

    def _enbw_hz(self, chunk: ChunkInfo) -> float:
        from spectramap.dsp.window import window_stats

        cfg = self.config.fft
        return window_stats(cfg.window, chunk.n_bins).enbw_hz(chunk.n_bins * chunk.f_step_hz)

    def iter_observations(
        self, with_position_only: bool = False
    ) -> Iterator[tuple[sqlite3.Row, np.ndarray]]:
        """Stream (index row, spectrum dB array) pairs without loading everything."""
        chunk_by_id = {c.chunk_id: c for c in self.chunks()}
        for row in self.observation_rows(with_position_only=with_position_only):
            chunk = chunk_by_id[row["chunk_id"]]
            yield row, np.asarray(self._chunk_map(chunk)[row["chunk_row"]], dtype=np.float64)

    # -- The query that matters ---------------------------------------------

    def band_power(
        self,
        f_low_hz: float,
        f_high_hz: float,
        method: str = "peak",
        with_position_only: bool = True,
        exclude_flags: QualityFlag = QualityFlag.NONE,
    ) -> dict[str, np.ndarray]:
        """Reduce a frequency band to one power value per observation.

        This is the operation behind the frequency selector: pick a band, get a
        spatial field. Implemented as a column slice over memory-mapped chunks, so
        cost scales with the width of the band, not with the size of the survey.

        ``method`` must be stated explicitly wherever the result is displayed,
        because the three choices answer different questions:

        ``peak``      Strongest bin in the band. Best for finding a known carrier;
                      biased high by noise when nothing is transmitting.
        ``mean``      Mean power across the band, averaged in the linear domain.
                      Best for "how much energy is in this band".
        ``integrated`` Total power in the band: mean multiplied by the bandwidth.
                      Comparable across bands of different widths.

        Returns arrays of equal length: ``observation_id``, ``latitude``,
        ``longitude``, ``power_db``, ``t_utc_ns``, ``flags``, ``n_bins_used``.
        """
        if f_high_hz < f_low_hz:
            f_low_hz, f_high_hz = f_high_hz, f_low_hz
        if method not in ("peak", "mean", "integrated"):
            raise ValueError(f"unknown band power method {method!r}")

        chunk_by_id = {c.chunk_id: c for c in self.chunks()}
        slices: dict[int, tuple[int, int]] = {}
        for cid, chunk in chunk_by_id.items():
            freqs = chunk.frequencies()
            idx = np.flatnonzero((freqs >= f_low_hz) & (freqs <= f_high_hz))
            if idx.size:
                slices[cid] = (int(idx[0]), int(idx[-1]) + 1)

        rows = self.observation_rows(
            with_position_only=with_position_only, exclude_flags=exclude_flags
        )
        ids, lats, lons, powers, times, flags, widths = [], [], [], [], [], [], []
        for row in rows:
            span = slices.get(row["chunk_id"])
            if span is None:
                continue  # This observation's window does not cover the band at all.
            chunk = chunk_by_id[row["chunk_id"]]
            lo, hi = span
            segment = np.asarray(self._chunk_map(chunk)[row["chunk_row"], lo:hi], np.float64)
            if segment.size == 0:
                continue
            linear = np.power(10.0, segment / 10.0)
            if method == "peak":
                value = float(10.0 * np.log10(linear.max()))
            elif method == "mean":
                value = float(10.0 * np.log10(linear.mean()))
            else:
                value = float(10.0 * np.log10(linear.sum() * chunk.f_step_hz))
            ids.append(row["observation_id"])
            lats.append(row["latitude"])
            lons.append(row["longitude"])
            powers.append(value)
            times.append(row["t_start_utc_ns"])
            flags.append(row["flags"])
            widths.append(hi - lo)

        return {
            "observation_id": np.asarray(ids, dtype=np.int64),
            "latitude": np.asarray(lats, dtype=np.float64),
            "longitude": np.asarray(lons, dtype=np.float64),
            "power_db": np.asarray(powers, dtype=np.float64),
            "t_utc_ns": np.asarray(times, dtype=np.int64),
            "flags": np.asarray(flags, dtype=np.int64),
            "n_bins_used": np.asarray(widths, dtype=np.int64),
        }

    def frequency_coverage(self) -> list[tuple[float, float]]:
        """Frequency spans actually measured, one per distinct chunk axis."""
        seen: set[tuple[float, float]] = set()
        for c in self.chunks():
            freqs = c.frequencies()
            if freqs.size:
                seen.add((float(freqs[0]), float(freqs[-1])))
        return sorted(seen)

    def close(self) -> None:
        self._maps.clear()
        with self._lock:
            self._db.close()

    def __enter__(self) -> SurveyReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def make_observation_position(row: sqlite3.Row) -> ResolvedPosition:
    """Rebuild a :class:`ResolvedPosition` from an index row."""
    if row["latitude"] is None:
        return ResolvedPosition(None, Evidence.MEASURED, row["position_age_ms"], QualityFlag(
            int(row["flags"])
        ))
    pos = Position(
        t_utc_ns=row["t_start_utc_ns"],
        t_mono_ns=row["t_start_mono_ns"],
        latitude=row["latitude"],
        longitude=row["longitude"],
        altitude_m=row["altitude_m"],
        horizontal_accuracy_m=row["h_accuracy_m"],
        vertical_accuracy_m=row["v_accuracy_m"],
        speed_mps=row["speed_mps"],
    )
    return ResolvedPosition(
        pos,
        Evidence(row["position_evidence"] or Evidence.MEASURED.value),
        row["position_age_ms"],
        QualityFlag(int(row["flags"])),
    )


__all__ = [
    "DEFAULT_CHUNK_ROWS",
    "SPECTRA_DTYPE",
    "ChunkInfo",
    "SurveyReader",
    "SurveyWriter",
    "make_observation_position",
]
