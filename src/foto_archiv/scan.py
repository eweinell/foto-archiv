"""Bestandsaufnahme von ``source_root`` in den Index."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path

from .classify import classify, is_raw_name, raw_key
from .context import App
from .db import utcnow
from .layout import ROOT_SERIES, series_year
from .tario import sha256_file


@dataclass
class ScanStats:
    files: int = 0
    bytes: int = 0
    new: int = 0
    changed: int = 0
    gone: int = 0
    excluded: int = 0
    series: set[str] = field(default_factory=set)


@dataclass
class _Found:
    rel: str
    series: str
    name: str
    size: int
    mtime_ns: int


def _excluded(name: str, patterns_lower: list[str]) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatchcase(n, p) for p in patterns_lower)


def walk_source(app: App) -> tuple[list[_Found], int]:
    cfg = app.cfg
    root = cfg.source_root
    name_pats = [p.lower() for p in cfg.exclude_names]
    dir_pats = [p.lower() for p in cfg.exclude_dirs]
    catalogs = {c.lower() for c in cfg.catalog_files}
    found: list[_Found] = []
    excluded = 0

    def rec(d: str, rel: str, series: str | None):
        nonlocal excluded
        try:
            it = os.scandir(d)
        except OSError as e:
            app.console.print(f"[yellow]Nicht lesbar: {d} ({e})")
            return
        with it:
            for e in it:
                r = f"{rel}/{e.name}" if rel else e.name
                try:
                    if e.is_dir(follow_symlinks=False):
                        if _excluded(e.name, dir_pats):
                            excluded += 1
                            continue
                        rec(e.path, r, series or e.name)
                    elif e.is_file(follow_symlinks=False):
                        if _excluded(e.name, name_pats) or (series is None and e.name.lower() in catalogs):
                            excluded += 1
                            continue
                        st = e.stat(follow_symlinks=False)
                        found.append(_Found(r, series or ROOT_SERIES, e.name, st.st_size, st.st_mtime_ns))
                except OSError as ex:
                    app.console.print(f"[yellow]Übersprungen: {e.path} ({ex})")

    rec(str(root), "", None)
    return found, excluded


def scan(app: App, do_hash: bool = False) -> ScanStats:
    db = app.db
    root = app.cfg.source_root
    if not root.is_dir():
        raise FileNotFoundError(f"source_root existiert nicht: {root}")
    now = utcnow()
    with app.console.status(f"Durchsuche {root} …"):
        found, excluded = walk_source(app)

    stats = ScanStats(excluded=excluded)
    # RAW-Stämme je Serie (inkl. bereits lokal entfernter RAWs aus dem Index)
    raw_keys: dict[str, set[str]] = {}
    for f in found:
        if is_raw_name(f.name):
            raw_keys.setdefault(f.series, set()).add(raw_key(f.name))
    for r in db.q("SELECT series, path FROM files WHERE cls='raw' AND present=0"):
        name = r["path"].rsplit("/", 1)[-1]
        if is_raw_name(name):
            raw_keys.setdefault(r["series"], set()).add(raw_key(name))

    existing = {r["path"]: r for r in db.q("SELECT path, size, mtime_ns, sha256, cls FROM files")}
    with db.tx() as c:
        for f in found:
            stats.files += 1
            stats.bytes += f.size
            stats.series.add(f.series)
            cls = classify(f.name, raw_keys.get(f.series, set()))
            old = existing.get(f.rel)
            if old is None:
                stats.new += 1
                c.execute(
                    """INSERT INTO files(path, series, cls, size, mtime_ns, sha256, present, slimmed, last_seen)
                       VALUES(?,?,?,?,?,NULL,1,0,?)""",
                    (f.rel, f.series, cls, f.size, f.mtime_ns, now),
                )
            else:
                unchanged = old["size"] == f.size and old["mtime_ns"] == f.mtime_ns
                if not unchanged:
                    stats.changed += 1
                c.execute(
                    """UPDATE files SET series=?, cls=?, size=?, mtime_ns=?, sha256=?, present=1, slimmed=0,
                       last_seen=? WHERE path=?""",
                    (f.series, cls, f.size, f.mtime_ns, old["sha256"] if unchanged else None, now, f.rel),
                )
        cur = c.execute("UPDATE files SET present=0 WHERE present=1 AND (last_seen IS NULL OR last_seen<>?)", (now,))
        stats.gone = cur.rowcount
        for s in stats.series:
            c.execute(
                "INSERT OR IGNORE INTO series(name, year, first_seen) VALUES(?,?,?)", (s, series_year(s), now)
            )

    if do_hash:
        hash_missing(app)
    return stats


def hash_missing(app: App, series: list[str] | None = None) -> int:
    """Berechnet fehlende SHA-256 lokal vorhandener Dateien."""
    db = app.db
    sql = "SELECT path, size, mtime_ns FROM files WHERE present=1 AND sha256 IS NULL"
    params: list = []
    if series:
        sql += f" AND series IN ({','.join('?' * len(series))})"
        params = list(series)
    rows = db.q(sql, params)
    total = sum(r["size"] for r in rows)
    root = app.cfg.source_root
    n = 0
    with app.byte_progress() as prog:
        task = prog.add_task("Hashen", total=total)
        for r in rows:
            p = root.joinpath(*r["path"].split("/"))
            try:
                st = p.stat()
                if st.st_size != r["size"] or st.st_mtime_ns != r["mtime_ns"]:
                    continue  # inzwischen geändert -> nächster scan
                h = sha256_file(p, lambda d: prog.advance(task, d))
            except OSError as e:
                app.console.print(f"[yellow]{p}: {e}")
                continue
            db.x("UPDATE files SET sha256=? WHERE path=? AND size=? AND mtime_ns=?", (h, r["path"], r["size"], r["mtime_ns"]))
            n += 1
    return n
