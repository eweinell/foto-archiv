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
