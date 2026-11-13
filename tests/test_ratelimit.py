"""Bandbreitenbegrenzung: Parser, Limiter, gedrosselter Body, Upload mit Limit."""

from __future__ import annotations

import io
import threading
import time

import pytest

from foto_archiv.ratelimit import RateError, RateLimiter, ThrottledBody, format_rate, parse_rate, throttled


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", 0),
        ("0", 0),
        ("aus", 0),
        (None, 0),
        ("2", 2_000_000),
        (2, 2_000_000),
        (1.5, 1_500_000),
        ("2MB/s", 2_000_000),
        ("2 MB/s", 2_000_000),
        ("2,5MB", 2_500_000),
        ("2M", 2_000_000),
        ("500k", 500_000),
        ("500kB/s", 500_000),
        ("16Mbit", 2_000_000),
        ("16Mbit/s", 2_000_000),
        ("16Mbps", 2_000_000),
        ("16 Mb/s", 2_000_000),
        ("1MiB/s", 1_048_576),
        ("1G", 1_000_000_000),
    ],
)
def test_parse_rate(text, expected):
    assert parse_rate(text) == expected


@pytest.mark.parametrize("text", ["schnell", "2 MB/h", "-1", "10B/s", "1Mbitx"])
def test_parse_rate_invalid(text):
    with pytest.raises(RateError):
        parse_rate(text)


def test_format_rate():
    assert format_rate(0) == "unbegrenzt"
    assert format_rate(2_000_000).startswith("2.0 MB/s")
    assert "kB/s" in format_rate(500_000)


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.slept = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.slept += d
        self.t += d


def test_limiter_paces_with_fake_clock():
    clk = FakeClock()
    lim = RateLimiter(1_000_000, burst_seconds=0.0, clock=clk, sleep=clk.sleep)
    for _ in range(10):
        lim.consume(100_000)
    assert clk.slept == pytest.approx(1.0)  # 1 MB bei 1 MB/s


def test_limiter_burst_credit_is_bounded():
    clk = FakeClock()
    lim = RateLimiter(1_000_000, burst_seconds=0.25, clock=clk, sleep=clk.sleep)
    clk.t += 3600  # lange Pause: Guthaben trotzdem nur 0,25 s
    lim.consume(1_000_000)
    assert clk.slept == pytest.approx(0.75)


def test_limiter_disabled_and_rate_change():
    clk = FakeClock()
    lim = RateLimiter(0, clock=clk, sleep=clk.sleep)
    lim.consume(10**9)
    assert clk.slept == 0
    lim.set_rate(2_000_000)
    lim.consume(1_000_000)
    assert clk.slept == pytest.approx(0.5, abs=0.26)


def test_limiter_shared_between_threads():
    lim = RateLimiter(4_000_000, burst_seconds=0.0)
    t0 = time.monotonic()

    def work():
        for _ in range(10):
            lim.consume(40_000)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 4 × 400 kB = 1,6 MB bei 4 MB/s ≈ 0,4 s (gemeinsam, nicht je Thread)
    assert time.monotonic() - t0 >= 0.35


def test_throttled_body_reads_seeks_and_counts():
    clk = FakeClock()
    lim = RateLimiter(1_000_000, burst_seconds=0.0, clock=clk, sleep=clk.sleep)
    data = bytes(range(256)) * 1000  # 256 kB
    body = ThrottledBody(data, lim)
    assert body.seek(0, io.SEEK_END) == len(data)
    assert body.tell() == len(data)
    body.seek(0)
    out = b""
    while chunk := body.read(8192):
        out += chunk
    assert out == data
    assert clk.slept == pytest.approx(len(data) / 1_000_000)
    body.seek(0)  # Wiederholung durch botocore
    before = clk.slept
    assert body.read() == data  # read() ohne Größe liefert alles
    assert clk.slept - before == pytest.approx(len(data) / 1_000_000)
    body.seek(1000)
    assert body.read(100_000) == data[1000:101_000]  # volle Menge, nicht gekappt
    assert body.read(10**9) == data[101_000:]
    assert throttled(data, None) is data
    assert throttled(data, RateLimiter(0)) is data


# ---------------------------------------------------------------------------- Zeitfenster

from datetime import datetime as _dt

from foto_archiv.ratelimit import Schedule, Window, parse_window


def _at(hh, mm=0):
    return _dt(2026, 11, 13, hh, mm)


def test_parse_window_and_midnight_wrap():
    w = parse_window("22:00-07:00=0")
    assert (w.start, w.end, w.rate) == (22 * 60, 7 * 60, 0)
    assert w.contains(23 * 60) and w.contains(0) and w.contains(6 * 60 + 59)
    assert not w.contains(7 * 60) and not w.contains(12 * 60)
    d = parse_window(" 07:00 - 22:00 = 16Mbit ")
    assert d.rate == 2_000_000 and d.contains(7 * 60) and not d.contains(22 * 60)
    assert parse_window("18:00-24:00=1MB/s").contains(23 * 60 + 59)
    assert parse_window("00:00-24:00=1M").contains(12 * 60)  # ganzer Tag


@pytest.mark.parametrize("text", ["7-22=2M", "07:00-22:00", "25:00-07:00=1M", "07:60-08:00=1M", "24:00-07:00=1M", "07:00-08:00=schnell"])
def test_parse_window_invalid(text):
    with pytest.raises(RateError):
        parse_window(text)


def test_schedule_first_match_and_default():
    s = Schedule.parse(["07:00-18:00=2MB/s", "17:00-23:00=1MB/s"], default=500_000)
    assert s.rate_at(_at(8)) == 2_000_000
    assert s.rate_at(_at(17, 30)) == 2_000_000  # Überlappung: erstes Fenster gewinnt
    assert s.rate_at(_at(20)) == 1_000_000
    assert s.rate_at(_at(3)) == 500_000  # außerhalb: Standard
    assert Schedule.parse([], 0) is None
    assert s.describe()[-1].startswith("sonst: 500 kB/s")


def test_limiter_follows_schedule_mid_upload():
    clk = FakeClock()
    wall = {"t": _at(22, 59)}
    changes = []
    s = Schedule.parse(["07:00-23:00=1MB/s", "23:00-07:00=0"], default=0)
    lim = RateLimiter(0, burst_seconds=0.0, clock=clk, sleep=clk.sleep, schedule=s,
                      now=lambda: wall["t"], check_interval=5.0, on_change=changes.append)
    assert lim.rate == 1_000_000 and lim.enabled
    lim.consume(1_000_000)
    assert clk.slept == pytest.approx(1.0)
    wall["t"] = _at(23, 0)  # Nacht beginnt
    clk.t += 10  # Prüfintervall abgelaufen
    before = clk.slept
    lim.consume(50_000_000)
    assert lim.rate == 0 and changes == [0]
    assert clk.slept == before  # unbegrenzt: keine Pause
    assert lim.enabled  # trotz Rate 0 weiter drosselbar (Zeitplan kann wieder umschalten)
    wall["t"] = _at(7, 0)
    clk.t += 10
    lim.consume(500_000)
    assert changes == [0, 1_000_000]
    assert clk.slept - before == pytest.approx(0.5)


def test_throttled_wraps_when_schedule_present():
    s = Schedule.parse(["00:00-24:00=0"], default=0)
    lim = RateLimiter(0, schedule=s)
    assert isinstance(throttled(b"x" * 10, lim), ThrottledBody)
