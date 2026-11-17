"""Bestandsaufnahme der Quellordner aller Sammlungen in den Index."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field

from .classify import classify_for, is_raw_name, raw_key
from .config import Collection
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
    collections: list[str] = field(default_factory=list)  # erfasste Sammlungen
    missing: list[str] = field(default_factory=list)  # Sammlungen, deren Quellordner fehlt


@dataclass
class _Found:
    rel: str  # Index-Pfad (mit Sammlung)
    series: str
    name: str
    size: int
    mtime_ns: int
    profile: str


def _excluded(name: str, patterns_lower: list[str]) -> bool:
    n = name.lower()
    return any(fnmatch.fnmatchcase(n, p) for p in patterns_lower)


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(p))


def walk_collection(app: App, coll: Collection) -> tuple[list[_Found], int]:
    cfg = app.cfg
    name_pats = [p.lower() for p in [*cfg.exclude_names, *coll.exclude_names]]
    dir_pats = [p.lower() for p in [*cfg.exclude_dirs, *coll.exclude_dirs]]
    catalogs = {c.lower() for c in cfg.catalog_files} if coll.profile == "photo" else set()
    # Quellordner anderer Sammlungen innerhalb dieses Ordners gehören dorthin, nicht hierher
    own = _norm(coll.source_root)
    nested = {_norm(c.source_root) for c in cfg.collections if c.id != coll.id and _norm(c.source_root) != own}
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
                        if _excluded(e.name, dir_pats) or _norm(e.path) in nested:
                            excluded += 1
                            continue
                        rec(e.path, r, series or e.name)
                    elif e.is_file(follow_symlinks=False):
                        if _excluded(e.name, name_pats) or (series is None and e.name.lower() in catalogs):
                            excluded += 1
                            continue
                        st = e.stat(follow_symlinks=False)
                        found.append(
                            _Found(
                                f"{coll.id}/{r}", f"{coll.id}/{series or ROOT_SERIES}", e.name, st.st_size, st.st_mtime_ns,
                                coll.profile,
                            )
                        )
                except OSError as ex:
                    app.console.print(f"[yellow]Übersprungen: {e.path} ({ex})")

    rec(str(coll.source_root), "", None)
    return found, excluded


def scan(app: App, do_hash: bool = False, only: list[str] | None = None) -> ScanStats:
    """Alle (bzw. die genannten) Sammlungen erfassen.

    Fehlt der Quellordner einer Sammlung (z. B. externe Platte nicht angeschlossen), wird sie
    übersprungen; ihre Dateien bleiben im Index unverändert und gelten nicht als verschwunden.
    """
    db = app.db
    now = utcnow()
    stats = ScanStats()
    found: list[_Found] = []
    for coll in app.cfg.collections:
        if only and coll.id not in only:
            continue
        if not coll.source_root.is_dir():
            stats.missing.append(coll.id)
            app.console.print(f"[yellow]Sammlung {coll.id}: Quellordner fehlt ({coll.source_root}) - übersprungen.")
            continue
        with app.console.status(f"Durchsuche {coll.source_root} …"):
            f, ex = walk_collection(app, coll)
        found += f
        stats.excluded += ex
        stats.collections.append(coll.id)
    if not stats.collections:
        roots = ", ".join(str(c.source_root) for c in app.cfg.collections if not only or c.id in only)
        raise FileNotFoundError(f"Kein Quellordner vorhanden: {roots}")

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
            cls = classify_for(f.profile, f.name, raw_keys.get(f.series, set()))
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
        for cid in stats.collections:
            cur = c.execute(
                """UPDATE files SET present=0 WHERE present=1 AND (last_seen IS NULL OR last_seen<>?)
                   AND substr(path, 1, ?)=?""",
                (now, len(cid) + 1, cid + "/"),
            )
            stats.gone += cur.rowcount
        for s in stats.series:
            coll = app.cfg.collection_of(s)
            c.execute(
                "INSERT OR IGNORE INTO series(name, year, first_seen) VALUES(?,?,?)",
                (s, series_year(s) if coll.year == "name" else None, now),
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
    rows = [r for r in db.q(sql, params) if app.cfg.find_collection(r["path"].partition("/")[0])]
    total = sum(r["size"] for r in rows)
    n = 0
    with app.byte_progress() as prog:
        task = prog.add_task("Hashen", total=total)
        for r in rows:
            p = app.cfg.source_file(r["path"])
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
