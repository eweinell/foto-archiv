"""Gemeinsame Test-Fixtures: synthetischer Foto-Ordner, Archiv-Volumes, App-Kontext."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from rich.console import Console

from foto_archiv.config import Config, S3Config
from foto_archiv.context import App
from foto_archiv.db import Index
from foto_archiv.volumes import init_volume

OLD = time.time() - 200 * 86400  # "lange unverändert"


def _write(p: Path, size: int, seed: int, mtime: float | None = None) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    block = (f"{p.name}-{seed}-".encode() * 64)[:4096]
    with open(p, "wb") as fh:
        remaining = size
        while remaining > 0:
            fh.write(block[: min(remaining, len(block))])
            remaining -= len(block)
    if mtime:
        os.utime(p, (mtime, mtime))


@pytest.fixture
def pictures(tmp_path: Path) -> Path:
    root = tmp_path / "Pictures"
    # Serie mit RAW, Exporten, Sidecars, Video, Handy-JPG (alt -> abschlussreif)
    k = root / "2026.Kenia"
    for i in range(1, 6):
        _write(k / "raws" / f"DSC0{i}.ARW", 30_000 + i, i, OLD)
        _write(k / "raws" / f"DSC0{i}.ARW.pp3", 300, i, OLD)
        _write(k / "jpg" / f"DSC0{i}.jpg", 8_000 + i, i, OLD)
    _write(k / "jpg" / "DSC01-1.jpg", 7_000, 99, OLD)
    _write(k / "pixel" / "PXL_20260201.jpg", 9_000, 7, OLD)
    _write(k / "clips" / "C0001.MP4", 50_000, 8, OLD)
    _write(k / "Thumbs.db", 100, 1, OLD)
    # alte Serie nur mit Original-JPGs, Umlaut im Namen
    s = root / "2013.Österreich"
    for i in range(1, 4):
        _write(s / f"IMG_{i:04d}.JPG", 12_000 + i, i, OLD)
    # aktive Serie (frisch)
    _write(root / "2026.London" / "DSC09.ARW", 40_000, 9)
    # Ordner ohne Jahr + Katalog im Wurzelordner + Papierkorb
    _write(root / "sortiert" / "a.png", 5_000, 3, OLD)
    _write(root / "digikam4.db", 2_000, 1)
    _write(root / ".dtrash" / "x.jpg", 1_000, 1)
    return root


@pytest.fixture
def app(tmp_path: Path, pictures: Path) -> App:
    cfg = Config(
        source_root=pictures,
        index_path=tmp_path / "data" / "index.sqlite",
        chunk_size_mb=1,  # kleine Pakete -> mehrere Chunks testbar
        hdd_paths=[str(tmp_path / "hddA"), str(tmp_path / "hddB")],
        s3=S3Config(bucket="test-bucket", prefix="fotos/", profile="", region="eu-west-1"),
    )
    cfg.chunk_size_mb = 1
    a = App(cfg=cfg, db=Index(cfg.index_path), console=Console(width=160, force_terminal=False))
    yield a
    a.db.close()


@pytest.fixture
def hdd_a(app: App, tmp_path: Path):
    return init_volume(tmp_path / "hddA", "A", app.db)


@pytest.fixture
def hdd_b(app: App, tmp_path: Path):
    return init_volume(tmp_path / "hddB", "B", app.db)
