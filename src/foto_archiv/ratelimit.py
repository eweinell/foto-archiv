"""Bandbreitenbegrenzung für Uploads.

Ein gemeinsamer :class:`RateLimiter` wird von allen Upload-Threads geteilt. Jeder Request-Body
wird in einen :class:`ThrottledBody` gehüllt; botocore/urllib3 lesen ihn blockweise beim Senden,
und jeder gelesene Block wird über den Limiter gebremst. So bleibt die Leitung gleichmäßig
ausgelastet (keine Lastspitzen in voller Geschwindigkeit, wie bei einer Drosselung pro Teil).
"""

from __future__ import annotations

import io
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

_RATE_RE = re.compile(
    r"^\s*(?P<num>\d+(?:[.,]\d+)?)\s*(?P<prefix>[kKmMgG]?)(?P<bi>i?)\s*(?P<unit>bit|Bit|BIT|b|B)?\s*(?:/s|ps)?\s*$"
)


class RateError(ValueError):
    pass


def parse_rate(text: str | int | float | None) -> int:
    """Bandbreite in Bytes/s. Leer oder 0 = unbegrenzt.

    Beispiele: ``"2MB/s"``, ``"2M"``, ``"2"`` (= 2 MB/s), ``"16Mbit"``, ``"16Mbps"``, ``"500k"``, ``"1.5 MiB/s"``.
    Ohne Einheit gilt MB/s; ``bit``/``b``/``bps`` bedeutet Bit, ``B`` Byte.
    """
    if text is None:
        return 0
    if isinstance(text, (int, float)):
        if text < 0:
            raise RateError("Bandbreite darf nicht negativ sein.")
        return int(text * 1000**2)
    s = str(text).strip()
    if s.lower() in ("", "0", "aus", "off", "none", "unbegrenzt"):
        return 0
    m = _RATE_RE.match(s)
    if not m:
        raise RateError(f"Ungültige Bandbreite '{text}' (Beispiele: 2MB/s, 16Mbit, 500k).")
    num = float(m.group("num").replace(",", "."))
    prefix = m.group("prefix").lower()
    unit = m.group("unit") or ""
    if not prefix and not unit:
        prefix = "m"  # reine Zahl = MB/s
    base = 1024 if m.group("bi") else 1000
    factor = base ** "_kmg".index(prefix) if prefix else 1
    is_bits = unit == "b" or unit.lower() == "bit"
    rate = num * factor / (8 if is_bits else 1)
    if rate <= 0:
        return 0
    if rate < 1000:
        raise RateError(f"Bandbreite '{text}' ist unrealistisch klein (mindestens 1 kB/s).")
    return int(rate)


def format_rate(bps: int) -> str:
    if bps <= 0:
        return "unbegrenzt"
    mb = bps / 1000**2
    if mb >= 1:
        return f"{mb:.1f} MB/s (≈ {mb * 8:.0f} Mbit/s)"
    return f"{bps / 1000:.0f} kB/s (≈ {bps * 8 / 1000**2:.1f} Mbit/s)"


# ---------------------------------------------------------------------------- Zeitfenster

_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*=\s*(.+?)\s*$")


@dataclass(frozen=True)
class Window:
    """Tageszeitfenster [start, end) in Minuten seit Mitternacht; darf über Mitternacht gehen."""

    start: int
    end: int
    rate: int
    text: str

    def contains(self, minute: int) -> bool:
        if self.start == self.end:  # 00:00-24:00 bzw. ganzer Tag
            return True
        if self.start < self.end:
            return self.start <= minute < self.end
        return minute >= self.start or minute < self.end


def _minutes(h: str, m: str, text: str, allow_24: bool) -> int:
    hh, mm = int(h), int(m)
    if mm > 59 or hh > 24 or (hh == 24 and (mm or not allow_24)):
        raise RateError(f"Ungültige Uhrzeit in '{text}'.")
    return (hh * 60 + mm) % 1440


def parse_window(text: str) -> Window:
    """``"07:00-22:00=2MB/s"`` -> Window. Über Mitternacht: ``"22:00-07:00=0"`` (0 = unbegrenzt)."""
    m = _WINDOW_RE.match(str(text))
    if not m:
        raise RateError(f"Ungültiges Zeitfenster '{text}' (Format: \"07:00-22:00=2MB/s\").")
    start = _minutes(m.group(1), m.group(2), text, allow_24=False)
    end = _minutes(m.group(3), m.group(4), text, allow_24=True)
    return Window(start, end, parse_rate(m.group(5)), str(text).strip())


class Schedule:
    """Bandbreite je Tageszeit. Das erste passende Fenster gilt; sonst der Standardwert."""

    def __init__(self, windows: list[Window], default: int):
        self.windows = windows
        self.default = default

    @classmethod
    def parse(cls, entries: list[str] | None, default: int) -> "Schedule | None":
        if not entries:
            return None
        return cls([parse_window(e) for e in entries], default)

    def window_at(self, when: datetime) -> Window | None:
        minute = when.hour * 60 + when.minute
        return next((w for w in self.windows if w.contains(minute)), None)

    def rate_at(self, when: datetime) -> int:
        w = self.window_at(when)
        return w.rate if w else self.default

    def describe(self) -> list[str]:
        def hm(x: int) -> str:
            return f"{x // 60:02d}:{x % 60:02d}"

        lines = [f"{hm(w.start)}–{hm(w.end)}: {format_rate(w.rate)}" for w in self.windows]
        lines.append(f"sonst: {format_rate(self.default)}")
        return lines


# ---------------------------------------------------------------------------- Limiter


class RateLimiter:
    """Thread-sicherer Limiter nach dem Prinzip einer virtuellen Uhr.

    Jeder Verbrauch von ``n`` Bytes schiebt den Zeitpunkt, ab dem wieder gesendet werden darf,
    um ``n / rate`` Sekunden nach hinten. Ein kleines Guthaben (``burst_seconds``) gleicht
    Pausen aus, ohne längere Lastspitzen zuzulassen.

    Mit ``schedule`` wird die Rate alle ``check_interval`` Sekunden an die Tageszeit angepasst,
    auch mitten in einem laufenden Upload; ``on_change(rate)`` meldet den Wechsel.
    """

    def __init__(
        self,
        rate: int,
        burst_seconds: float = 0.25,
        clock=time.monotonic,
        sleep=time.sleep,
        schedule: Schedule | None = None,
        now: Callable[[], datetime] = datetime.now,
        check_interval: float = 5.0,
        on_change: Callable[[int], None] | None = None,
    ):
        self.burst = burst_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next = clock()
        self.schedule = schedule
        self._now = now
        self.check_interval = check_interval
        self.on_change = on_change
        self._next_check = clock() + check_interval
        self.rate = max(0, int(schedule.rate_at(now()) if schedule else rate))

    @property
    def enabled(self) -> bool:
        """Müssen Bodies gedrosselt werden? Mit Zeitplan immer (die Rate kann sich ändern)."""
        return self.rate > 0 or self.schedule is not None

    def set_rate(self, rate: int) -> None:
        with self._lock:
            self.rate = max(0, int(rate))
            self._next = self._clock()

    def _refresh_locked(self, now: float) -> int | None:
        if self.schedule is None or now < self._next_check:
            return None
        self._next_check = now + self.check_interval
        rate = max(0, int(self.schedule.rate_at(self._now())))
        if rate == self.rate:
            return None
        self.rate = rate
        self._next = now
        return rate

    def consume(self, n: int) -> None:
        if n <= 0 or (self.rate <= 0 and self.schedule is None):
            return
        delay = 0.0
        with self._lock:
            now = self._clock()
            changed = self._refresh_locked(now)
            if self.rate > 0:
                start = max(self._next, now - self.burst)
                self._next = start + n / self.rate
                delay = self._next - now
        if changed is not None and self.on_change:
            try:
                self.on_change(changed)
            except Exception:
                pass
        if delay > 0:
            self._sleep(delay)


class ThrottledBody(io.RawIOBase):
    """Lesbarer, seekbarer Body über ``bytes``; jedes ``read`` wird vom Limiter gebremst.

    Seekbar, damit botocore die Länge bestimmen und bei Wiederholungen zurückspulen kann.
    """

    CHUNK = 64 * 1024  # größere Leseanforderungen werden in kleine Blöcke zerlegt (gleichmäßiger)

    def __init__(self, data: bytes, limiter: RateLimiter):
        super().__init__()
        self._buf = memoryview(data)
        self._pos = 0
        self._limiter = limiter

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            pos = len(self._buf) + offset
        else:
            raise ValueError(f"whence={whence}")
        self._pos = max(0, min(len(self._buf), pos))
        return self._pos

    def read(self, size: int = -1) -> bytes:
        """Liefert wie eine normale Datei genau ``min(size, Rest)`` Bytes (``-1`` = alles).

        Gebremst wird in Blöcken von ``CHUNK``, damit auch große Leseanforderungen gleichmäßig
        verteilt werden. urllib3 liest beim Senden ohnehin in kleinen Blöcken.
        """
        remaining = len(self._buf) - self._pos
        if remaining <= 0:
            return b""
        n = remaining if size is None or size < 0 else min(size, remaining)
        done = 0
        while done < n:
            step = min(self.CHUNK, n - done)
            self._limiter.consume(step)
            done += step
        out = self._buf[self._pos : self._pos + n].tobytes()
        self._pos += n
        return out

    def readinto(self, b) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)

    def readall(self) -> bytes:
        return self.read(-1)


def throttled(data: bytes, limiter: RateLimiter | None):
    """Body für einen Request: unverändert ohne Limit/Zeitplan, sonst gebremst."""
    if limiter is None or not limiter.enabled:
        return data
    return ThrottledBody(data, limiter)
