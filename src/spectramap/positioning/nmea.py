"""NMEA 0183 position source: the real GPS implementation for V0.

Split deliberately into two pieces:

* :class:`NMEAParser` -- pure, synchronous, no I/O. Sentences in, fixes out. This is
  where every subtle bug lives (ddmm.mmmm coordinates, checksums, UTC date rollover,
  the difference between "no fix" and "fix with bad geometry"), and it is fully unit
  tested in CI with canned sentences and no hardware.
* :class:`SerialNMEAProvider` -- a thin reader that pulls lines off a serial port in
  a background thread and hands them to the parser.

Accuracy
--------
NMEA does not report positional accuracy. It reports *dilution of precision*, a
geometry factor. The estimate here is ``HDOP * UERE``, where UERE is an assumed
user-equivalent range error. That makes reported accuracy an
:attr:`~spectramap.types.Evidence.ASSUMED` quantity derived from a measured DOP --
not a measurement, and it is labelled that way everywhere it surfaces.
"""

from __future__ import annotations

import datetime as dt
import threading
from typing import Any

from spectramap.positioning.base import PositionProvider
from spectramap.sdr.base import now_mono_utc_ns
from spectramap.types import FixQuality, Position

# Assumed 1-sigma user-equivalent range error for a consumer receiver, in metres.
# Multiplied by HDOP to get a horizontal accuracy estimate. Conservative: real
# single-frequency receivers under open sky sit around 3-5 m.
DEFAULT_UERE_M = 4.0

_GGA_QUALITY = {
    0: FixQuality.NONE,
    1: FixQuality.GPS,
    2: FixQuality.DGPS,
    4: FixQuality.RTK_FIXED,
    5: FixQuality.RTK_FLOAT,
    6: FixQuality.ESTIMATED,
}


def nmea_checksum_ok(sentence: str) -> bool:
    """Validate the ``*HH`` XOR checksum. Sentences without one are rejected.

    A corrupted sentence that happens to parse is worse than a dropped one: it puts a
    measurement at the wrong place on the map.
    """
    body = sentence.strip()
    if not body.startswith("$") or "*" not in body:
        return False
    payload, _, given = body[1:].partition("*")
    if len(given.strip()) < 2:
        return False
    checksum = 0
    for ch in payload:
        checksum ^= ord(ch)
    try:
        return checksum == int(given.strip()[:2], 16)
    except ValueError:
        return False


def parse_ddmm(value: str, hemisphere: str) -> float | None:
    """Parse NMEA ``ddmm.mmmm`` / ``dddmm.mmmm`` into signed decimal degrees.

    The degrees field is variable width (2 for latitude, 3 for longitude), so the
    split is taken relative to the decimal point rather than at a fixed offset.
    Treating the whole field as decimal degrees -- a classic error -- puts a fix
    tens of kilometres away.
    """
    if not value or not hemisphere:
        return None
    dot = value.find(".")
    if dot < 3:
        return None
    degrees = int(value[: dot - 2])
    minutes = float(value[dot - 2 :])
    decimal = degrees + minutes / 60.0
    if hemisphere.upper() in ("S", "W"):
        decimal = -decimal
    return decimal


def _f(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class NMEAParser:
    """Accumulates NMEA sentences into complete fixes.

    A fix needs data from more than one sentence (GGA carries altitude, HDOP and
    satellite count; RMC carries the date, speed and course). The parser holds
    partial state and emits a :class:`~spectramap.types.Position` when a GGA arrives,
    which is the sentence that defines the fix instant.
    """

    __slots__ = ("_date", "_speed_mps", "_heading_deg", "_hdop", "_vdop", "uere_m", "source")

    def __init__(self, uere_m: float = DEFAULT_UERE_M, source: str = "nmea") -> None:
        self.uere_m = float(uere_m)
        self.source = source
        self._date: dt.date | None = None
        self._speed_mps: float | None = None
        self._heading_deg: float | None = None
        self._hdop: float | None = None
        self._vdop: float | None = None

    def feed(self, sentence: str, t_mono_ns: int | None = None) -> Position | None:
        """Consume one sentence. Returns a fix when one completes, else ``None``."""
        if not nmea_checksum_ok(sentence):
            return None
        fields = sentence.strip()[1:].split("*")[0].split(",")
        if not fields:
            return None
        talker_type = fields[0][2:] if len(fields[0]) >= 5 else fields[0]

        if talker_type == "RMC":
            self._parse_rmc(fields)
            return None
        if talker_type == "GSA":
            self._parse_gsa(fields)
            return None
        if talker_type == "GGA":
            return self._parse_gga(fields, t_mono_ns)
        return None

    def _parse_rmc(self, f: list[str]) -> None:
        # $..RMC,hhmmss.ss,A,lat,N,lon,E,speed_knots,course,ddmmyy,...
        if len(f) < 10:
            return
        if len(f[9]) == 6:
            try:
                self._date = dt.datetime.strptime(f[9], "%d%m%y").date()
            except ValueError:
                self._date = None
        knots = _f(f[7])
        self._speed_mps = knots * 0.514444 if knots is not None else None
        self._heading_deg = _f(f[8])

    def _parse_gsa(self, f: list[str]) -> None:
        # $..GSA,mode,fixtype,...12 PRNs...,PDOP,HDOP,VDOP
        if len(f) < 18:
            return
        self._hdop = _f(f[16])
        self._vdop = _f(f[17])

    def _parse_gga(self, f: list[str], t_mono_ns: int | None) -> Position | None:
        # $..GGA,hhmmss.ss,lat,N,lon,E,quality,numsats,hdop,alt,M,geoid,M,...
        if len(f) < 10:
            return None
        latitude = parse_ddmm(f[2], f[3])
        longitude = parse_ddmm(f[4], f[5])
        try:
            quality = _GGA_QUALITY.get(int(f[6] or 0), FixQuality.NONE)
        except ValueError:
            quality = FixQuality.NONE
        if latitude is None or longitude is None or quality is FixQuality.NONE:
            return None

        hdop = _f(f[8])
        if hdop is not None:
            self._hdop = hdop
        satellites = None
        try:
            satellites = int(f[7])
        except (TypeError, ValueError):
            pass

        mono_ns, host_utc_ns = now_mono_utc_ns()
        if t_mono_ns is not None:
            mono_ns = t_mono_ns
        utc_ns = self._utc_ns(f[1], host_utc_ns)

        h_acc = self._hdop * self.uere_m if self._hdop is not None else None
        v_acc = self._vdop * self.uere_m if self._vdop is not None else None
        if v_acc is None and h_acc is not None:
            # Vertical geometry is always worse than horizontal for a ground receiver.
            v_acc = h_acc * 1.6

        return Position(
            t_utc_ns=utc_ns,
            t_mono_ns=mono_ns,
            latitude=latitude,
            longitude=longitude,
            altitude_m=_f(f[9]),
            horizontal_accuracy_m=h_acc,
            vertical_accuracy_m=v_acc,
            speed_mps=self._speed_mps,
            heading_deg=self._heading_deg,
            fix_quality=quality,
            satellites=satellites,
            source=self.source,
        )

    def _utc_ns(self, hhmmss: str, fallback_utc_ns: int) -> int:
        """Combine the GGA time-of-day with the date most recently seen in RMC.

        GGA carries no date. Without an RMC the host's own date is used, which is
        recorded as a clock assumption rather than treated as GPS-derived truth.
        """
        if self._date is None or len(hhmmss) < 6:
            return fallback_utc_ns
        try:
            hours, minutes = int(hhmmss[0:2]), int(hhmmss[2:4])
            seconds = float(hhmmss[4:])
        except ValueError:
            return fallback_utc_ns
        base = dt.datetime(
            self._date.year,
            self._date.month,
            self._date.day,
            hours,
            minutes,
            tzinfo=dt.timezone.utc,
        )
        return int(base.timestamp() * 1e9) + int(round(seconds * 1e9))


class SerialNMEAProvider(PositionProvider):
    """Reads NMEA from a serial port (USB GPS dongle, GPS HAT, Bluetooth SPP).

    The serial read runs in a daemon thread so that a stalled or unplugged receiver
    can never block acquisition. Fixes land in a lock-protected list that
    :meth:`poll` drains.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout_s: float = 1.0,
        uere_m: float = DEFAULT_UERE_M,
    ) -> None:
        self.port = port
        self.baudrate = int(baudrate)
        self.timeout_s = float(timeout_s)
        self._parser = NMEAParser(uere_m=uere_m, source=f"nmea:{port}")
        self._serial: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._pending: list[Position] = []
        self._stop = threading.Event()
        self._error: str | None = None
        self._sentences_seen = 0
        self._sentences_bad = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - depends on host packages
            from spectramap.positioning.base import PositionError

            raise PositionError(
                "serial GPS support requires pyserial: pip install 'spectramap[gps]'"
            ) from exc
        try:
            self._serial = serial.Serial(self.port, self.baudrate, timeout=self.timeout_s)
        except Exception as exc:
            from spectramap.positioning.base import PositionError

            raise PositionError(f"could not open GPS on {self.port}: {exc}") from exc
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="spectramap-gps", daemon=True)
        self._thread.start()

    def _run(self) -> None:  # pragma: no cover - requires hardware
        while not self._stop.is_set():
            try:
                raw = self._serial.readline()
            except Exception as exc:
                self._error = str(exc)
                return
            if not raw:
                continue
            self._sentences_seen += 1
            try:
                line = raw.decode("ascii", errors="ignore")
            except Exception:
                self._sentences_bad += 1
                continue
            mono_ns, _ = now_mono_utc_ns()
            fix = self._parser.feed(line, t_mono_ns=mono_ns)
            if fix is not None:
                with self._lock:
                    self._pending.append(fix)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:  # pragma: no cover
                pass
            self._serial = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def last_error(self) -> str | None:
        return self._error

    def poll(self) -> list[Position]:
        with self._lock:
            out, self._pending = self._pending, []
        return out

    def describe(self) -> dict[str, object]:
        return {
            "provider": "nmea-serial",
            "port": self.port,
            "baudrate": self.baudrate,
            "assumed_uere_m": self._parser.uere_m,
            "accuracy_evidence": "assumed: HDOP x UERE, not a receiver-reported accuracy",
            "sentences_seen": self._sentences_seen,
            "sentences_rejected": self._sentences_bad,
        }


__all__ = [
    "DEFAULT_UERE_M",
    "NMEAParser",
    "SerialNMEAProvider",
    "nmea_checksum_ok",
    "parse_ddmm",
]
