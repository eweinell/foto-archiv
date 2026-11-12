"""Tar-Pakete schreiben/lesen mit gleichzeitiger SHA-256-Berechnung."""

from __future__ import annotations

import hashlib
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

BUF = 1024 * 1024
PARTIAL_SUFFIX = ".foto-archiv-partial"

Progress = Callable[[int], None]  # erhält Anzahl verarbeiteter Bytes (Delta)


def sha256_file(path: Path, progress: Progress | None = None) -> str:
    h = hashlib.sha256()
    with open(path, "rb", buffering=0) as fh:
        while True:
            b = fh.read(BUF)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


class _HashingWriter:
    def __init__(self, fh):
        self._fh = fh
        self.h = hashlib.sha256()
        self.size = 0

    def write(self, b) -> int:
        self.h.update(b)
        self.size += len(b)
        return self._fh.write(b)

    def flush(self) -> None:
        self._fh.flush()


class _HashingReader:
    def __init__(self, fh, progress: Progress | None):
        self._fh = fh
        self.h = hashlib.sha256()
        self.n = 0
        self._progress = progress

    def read(self, n: int = -1) -> bytes:
        b = self._fh.read(n)
        self.h.update(b)
        self.n += len(b)
        if self._progress and b:
            self._progress(len(b))
        return b


class FileChangedError(Exception):
    pass


def _ns_to_pax(ns: int) -> str:
    sec, frac = divmod(ns, 1_000_000_000)
    return f"{sec}.{frac:09d}" if frac else str(sec)


def member_mtime_ns(m: tarfile.TarInfo) -> int:
    """Exakte mtime eines Tar-Eintrags in ns (PAX-Dezimalwert, sonst Sekunden)."""
    raw = m.pax_headers.get("mtime") if m.pax_headers else None
    if raw:
        whole, _, frac = raw.partition(".")
        try:
            sign = -1 if whole.startswith("-") else 1
            return int(whole) * 1_000_000_000 + sign * int((frac + "000000000")[:9] or 0)
        except ValueError:
            pass
    return int(round(m.mtime * 1_000_000_000))


@dataclass
class TarEntry:
    path: str  # relativ, "/"-getrennt (Name im Tar)
    source: Path  # absoluter Quellpfad


@dataclass
class PackedFile:
    path: str
    size: int
    mtime_ns: int
    sha256: str


@dataclass
class TarResult:
    sha256: str
    size: int
    files: list[PackedFile] = field(default_factory=list)


def write_tar(out_path: Path, entries: Iterable[TarEntry], progress: Progress | None = None) -> TarResult:
    """Schreibt ein unkomprimiertes PAX-Tar atomar (über .partial) und hasht Tar + Einzeldateien."""
    tmp = out_path.with_name(out_path.name + PARTIAL_SUFFIX)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    packed: list[PackedFile] = []
    try:
        with open(tmp, "wb") as raw:
            hw = _HashingWriter(raw)
            with tarfile.open(fileobj=hw, mode="w|", format=tarfile.PAX_FORMAT, bufsize=BUF) as tf:
                for e in entries:
                    st = os.stat(e.source)
                    ti = tarfile.TarInfo(e.path)
                    ti.size = st.st_size
                    ti.mtime = st.st_mtime_ns // 1_000_000_000
                    # exakte mtime (ns) als PAX-Dezimalwert -> verlustfreie Wiederherstellung
                    ti.pax_headers = {"mtime": _ns_to_pax(st.st_mtime_ns)}
                    ti.mode = 0o644
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with open(e.source, "rb") as src:
                        hr = _HashingReader(src, progress)
                        tf.addfile(ti, hr)
                    st2 = os.stat(e.source)
                    if hr.n != st.st_size or st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
                        raise FileChangedError(f"Datei wurde während des Packens verändert: {e.source}")
                    packed.append(PackedFile(e.path, st.st_size, st.st_mtime_ns, hr.h.hexdigest()))
            raw.flush()
            os.fsync(raw.fileno())
        os.replace(tmp, out_path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return TarResult(sha256=hw.h.hexdigest(), size=hw.size, files=packed)


# ---------------------------------------------------------------------------- Entpacken


@dataclass
class ExtractStats:
    written: list[str] = field(default_factory=list)
    skipped_same: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # existiert mit anderem Inhalt
    hash_errors: list[str] = field(default_factory=list)
    bytes_written: int = 0


def _safe_target(root: Path, name: str) -> Path:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
        raise ValueError(f"Unsicherer Pfad im Paket: {name}")
    return root.joinpath(*p.parts)


def extract_bundle(
    tar_path: Path,
    target_root: Path,
    expected: dict[str, str] | None = None,
    only: set[str] | None = None,
    overwrite: bool = False,
    progress: Progress | None = None,
) -> ExtractStats:
    """Entpackt ein Paket nach ``target_root``.

    expected: Pfad -> erwarteter SHA-256 (aus dem Index/Manifest); Abweichung = hash_error
    only:     nur diese Pfade entpacken
    overwrite: vorhandene Dateien mit anderem Inhalt überschreiben (sonst conflict)
    """
    stats = ExtractStats()
    with tarfile.open(tar_path, mode="r|", bufsize=BUF) as tf:
        for m in tf:
            if not m.isfile():
                continue
            if only is not None and m.name not in only:
                continue
            dest = _safe_target(target_root, m.name)
            want = expected.get(m.name) if expected else None
            if dest.exists():
                if dest.stat().st_size == m.size and want and sha256_file(dest) == want:
                    stats.skipped_same.append(m.name)
                    continue
                if not overwrite:
                    stats.conflicts.append(m.name)
                    continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
            src = tf.extractfile(m)
            assert src is not None
            h = hashlib.sha256()
            with open(tmp, "wb") as out:
                while True:
                    b = src.read(BUF)
                    if not b:
                        break
                    h.update(b)
                    out.write(b)
                    if progress:
                        progress(len(b))
            got = h.hexdigest()
            if want and got != want:
                tmp.unlink()
                stats.hash_errors.append(m.name)
                continue
            os.replace(tmp, dest)
            mt = member_mtime_ns(m)
            os.utime(dest, ns=(mt, mt))
            stats.written.append(m.name)
            stats.bytes_written += m.size
    return stats
