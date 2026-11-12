"""HDD-Operationen: Kopieren zwischen Volumes, Prüfen, lokales Ausdünnen, Wiederherstellen.

Alle Befehle geben ein Ergebnisobjekt zurück. Liegt ein Fehler vor, der einen Exit-Code ≠ 0
rechtfertigt (defekte Pakete, Hash-Fehler beim Wiederherstellen …), wird nach der vollständigen
Ausgabe :class:`OpFailed` ausgelöst; die Ausnahme trägt das Ergebnis in ``result``.
"""

from __future__ import annotations

import hashlib
import os
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import typer
from rich.table import Table

from .classify import CLASSES
from .context import App, human
from .db import utcnow
from .layout import CATALOG_DIR, INDEX_DIR
from .policy import bundle_protection, slim_candidates
from .seal import export_index
from .tario import BUF, PARTIAL_SUFFIX, extract_bundle, sha256_file
from .volumes import Volume

SPACE_MARGIN = 50 * 1024 * 1024  # Reserve für Index-Export u. Ä.
USAGE_WARN = 0.80


class OpFailed(typer.Exit):
    """Befehl ist durchgelaufen, aber mit Fehlern (Exit-Code 1). ``result`` enthält die Details."""

    def __init__(self, result=None, message: str | None = None):
        super().__init__(1)
        self.result = result
        self.message = message


# =========================================================================== Hilfsfunktionen


def _ensure_registered(app: App, vol: Volume) -> None:
    app.db.register_volume(vol.id, "hdd", vol.label, str(vol.root))


def _in_clause(values) -> str:
    return ",".join("?" * len(values))


def _check_classes(app: App, classes: list[str] | None) -> None:
    unknown = [c for c in classes or [] if c not in CLASSES]
    if unknown:
        app.console.print(f"[red]Unbekannte Klasse(n): {', '.join(unknown)}[/] (erlaubt: {', '.join(CLASSES)})")
        raise OpFailed(message="unbekannte Klasse")


def _copy_stream(src: Path, dst: Path, progress: Callable[[int], None] | None = None) -> tuple[Path, str, int]:
    """Kopiert ``src`` nach ``<dst>.foto-archiv-partial`` (gehasht, fsync). Gibt (tmp, sha256, bytes) zurück.

    Das Umbenennen auf ``dst`` übernimmt der Aufrufer, nachdem er die Prüfsumme verglichen hat.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + PARTIAL_SUFFIX)
    h = hashlib.sha256()
    n = 0
    try:
        with open(src, "rb", buffering=0) as fi, open(tmp, "wb") as fo:
            while True:
                b = fi.read(BUF)
                if not b:
                    break
                h.update(b)
                fo.write(b)
                n += len(b)
                if progress:
                    progress(len(b))
            fo.flush()
            os.fsync(fo.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp, h.hexdigest(), n


def _commit(tmp: Path, dst: Path, src: Path) -> None:
    """Atomares Umbenennen; Änderungszeit der Quelle übernehmen."""
    os.replace(tmp, dst)
    try:
        st = src.stat()
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def _bytes_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _other_locations(app: App, bundle_id: int, exclude_volume: str) -> list[str]:
    rows = app.db.q(
        """SELECT v.label, v.kind FROM copies c JOIN volumes v ON v.id=c.volume_id
           WHERE c.bundle_id=? AND c.status='ok' AND c.volume_id<>? ORDER BY v.kind, v.label""",
        (bundle_id, exclude_volume),
    )
    return ["S3" if r["kind"] == "s3" else f"HDD {r['label']}" for r in rows]


# =========================================================================== copy-hdd


@dataclass
class CopyResult:
    copied: list[str] = field(default_factory=list)  # relpaths
    registered: list[str] = field(default_factory=list)  # lag schon korrekt auf dem Ziel
    source_bad: list[str] = field(default_factory=list)  # Quelle defekt oder fehlend
    errors: list[str] = field(default_factory=list)
    manifests: int = 0
    catalogs: int = 0
    bytes_copied: int = 0


def _bundles_to_copy(app: App, src: Volume, dst: Volume, series: list[str] | None):
    sql = """SELECT b.* FROM bundles b
             JOIN copies cs ON cs.bundle_id=b.id AND cs.volume_id=? AND cs.status='ok'
             WHERE NOT EXISTS (SELECT 1 FROM copies cd
                               WHERE cd.bundle_id=b.id AND cd.volume_id=? AND cd.status='ok')"""
    params: list = [src.id, dst.id]
    if series:
        sql += f" AND b.series IN ({_in_clause(series)})"
        params += list(series)
    return app.db.q(sql + " ORDER BY b.series, b.version, b.name", params)


def _manifests_to_copy(app: App, src: Volume, dst: Volume, series: list[str] | None):
    """Versionen, deren Manifest auf der Quelle liegt, auf dem Ziel aber fehlt (Datei oder Index-Eintrag)."""
    sql = "SELECT * FROM versions"
    params: list = []
    if series:
        sql += f" WHERE series IN ({_in_clause(series)})"
        params = list(series)
    out = []
    for v in app.db.q(sql + " ORDER BY series, version", params):
        if not src.path_for(v["manifest_relpath"]).is_file():
            continue
        registered = app.db.q1(
            "SELECT 1 FROM manifest_copies WHERE series=? AND version=? AND volume_id=?", (v["series"], v["version"], dst.id)
        )
        if registered and dst.path_for(v["manifest_relpath"]).is_file():
            continue
        out.append(v)
    return out


def _catalogs_to_copy(src: Volume, dst: Volume) -> list[tuple[Path, Path]]:
    base = src.root / CATALOG_DIR
    if not base.is_dir():
        return []
    out = []
    for f in sorted(base.rglob("*")):
        if not f.is_file() or f.name.endswith(PARTIAL_SUFFIX):
            continue
        target = dst.root / f.relative_to(src.root)
        if not target.exists():
            out.append((f, target))
    return out


def copy_hdd(app: App, src: Volume, dst: Volume, series: list[str] | None, dry_run: bool) -> CopyResult:
    """Fehlende Pakete, Manifeste und Katalog-Sicherungen von ``src`` nach ``dst`` kopieren und prüfen."""
    con = app.console
    res = CopyResult()
    if src.id == dst.id:
        con.print("[red]Quelle und Ziel sind dasselbe Volume.")
        raise OpFailed(res)
    _ensure_registered(app, src)
    _ensure_registered(app, dst)

    bundles = _bundles_to_copy(app, src, dst, series)
    manifests = _manifests_to_copy(app, src, dst, series)
    catalogs = _catalogs_to_copy(src, dst)

    # Platzbedarf: Pakete, die nicht schon in passender Größe auf dem Ziel liegen
    def already_there(b) -> bool:
        p = dst.path_for(b["relpath"])
        return p.is_file() and p.stat().st_size == b["size"]

    need_bundles = sum(b["size"] for b in bundles if not already_there(b))
    need = (
        need_bundles
        + sum(src.path_for(m["manifest_relpath"]).stat().st_size for m in manifests)
        + sum(s.stat().st_size for s, _ in catalogs)
    )
    free = dst.free_bytes()

    con.print(
        f"[bold]{src.label} → {dst.label}[/]: {len(bundles)} Paket(e) ({human(sum(b['size'] for b in bundles))}), "
        f"{len(manifests)} Manifest(e), {len(catalogs)} Katalog-Datei(en)"
    )
    if dry_run:
        if bundles:
            t = Table("Serie", "Version", "Paket", "Größe", header_style="bold")
            for b in bundles:
                t.add_row(b["series"], f"v{b['version']}", b["name"], human(b["size"]))
            con.print(t)
        con.print(f"Zu kopieren: {human(need)} · frei auf {dst.label}: {human(free)}")
        if need + SPACE_MARGIN > free:
            con.print("[red]Platz auf dem Ziel reicht nicht aus.")
        return res

    if not (bundles or manifests or catalogs):
        con.print(f"[green]{dst.label} ist auf dem Stand von {src.label}.")
    elif need + SPACE_MARGIN > free:
        con.print(
            f"[red]Zu wenig Platz auf {dst}:[/] benötigt {human(need + SPACE_MARGIN)} "
            f"(inkl. {human(SPACE_MARGIN)} Reserve), frei {human(free)}"
        )
        raise OpFailed(res)

    if bundles:
        with app.byte_progress() as prog:
            task = prog.add_task(f"{src.label} → {dst.label}", total=sum(b["size"] for b in bundles))
            for b in bundles:
                prog.update(task, description=f"{b['series']} v{b['version']} {b['name']}")
                _copy_bundle(app, src, dst, b, res, lambda d: prog.advance(task, d))

    for m in manifests:
        _copy_manifest(app, src, dst, m, res)

    for s, d in catalogs:
        try:
            tmp, _, _ = _copy_stream(s, d)
            _commit(tmp, d, s)
            res.catalogs += 1
        except OSError as e:
            res.errors.append(f"{s}: {e}")

    export_index(app, dst)
    export_index(app, src)

    con.print(
        f"[green]✓ {len(res.copied)} Paket(e) kopiert ({human(res.bytes_copied)})[/], "
        f"{len(res.registered)} bereits vorhanden und registriert, "
        f"{res.manifests} Manifest(e), {res.catalogs} Katalog-Datei(en)"
    )
    if res.source_bad or res.errors:
        for p in res.source_bad:
            con.print(f"[red]Quelle defekt/fehlend:[/] {p} auf {src.label} – als 'bad'/'missing' markiert")
        for e in res.errors:
            con.print(f"[red]Fehler:[/] {e}")
        raise OpFailed(res)
    return res


def _copy_bundle(app: App, src: Volume, dst: Volume, b, res: CopyResult, progress: Callable[[int], None]) -> None:
    db = app.db
    rel = b["relpath"]
    sp, dp = src.path_for(rel), dst.path_for(rel)
    # schon (korrekt) auf dem Ziel? -> nur registrieren
    if dp.is_file() and dp.stat().st_size == b["size"]:
        if sha256_file(dp, progress) == b["sha256"]:
            db.set_copy(b["id"], dst.id, "ok", verified=True)
            res.registered.append(rel)
            return
    if not sp.is_file():
        db.set_copy(b["id"], src.id, "missing")
        res.source_bad.append(rel)
        return
    try:
        tmp, sha, n = _copy_stream(sp, dp, progress)
    except OSError as e:
        res.errors.append(f"{rel}: {e}")
        return
    if sha != b["sha256"] or n != b["size"]:
        tmp.unlink(missing_ok=True)
        db.mark_verified(b["id"], src.id, ok=False)
        res.source_bad.append(rel)
        return
    _commit(tmp, dp, sp)
    db.set_copy(b["id"], dst.id, "ok", verified=True)
    res.copied.append(rel)
    res.bytes_copied += n


def _copy_manifest(app: App, src: Volume, dst: Volume, m, res: CopyResult) -> None:
    rel = m["manifest_relpath"]
    sp, dp = src.path_for(rel), dst.path_for(rel)
    try:
        if not (dp.is_file() and _bytes_hash(dp) == m["manifest_sha256"]):
            tmp, sha, _ = _copy_stream(sp, dp)
            if sha != m["manifest_sha256"]:
                tmp.unlink(missing_ok=True)
                res.errors.append(f"Manifest {rel} auf {src.label} weicht vom Index ab")
                return
            _commit(tmp, dp, sp)
    except OSError as e:
        res.errors.append(f"{rel}: {e}")
        return
    app.db.x(
        "INSERT OR REPLACE INTO manifest_copies(series, version, volume_id, stored_at) VALUES(?,?,?,?)",
        (m["series"], m["version"], dst.id, utcnow()),
    )
    res.manifests += 1


# =========================================================================== verify hdd


@dataclass
class VerifyResult:
    ok: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)  # quick: Größe passt, Status war aber nicht ok
    registered: list[str] = field(default_factory=list)  # gefunden, ohne Index-Eintrag, jetzt registriert
    unregistered: list[str] = field(default_factory=list)  # gefunden, ohne Index-Eintrag (quick bzw. Hash falsch)
    unknown: list[str] = field(default_factory=list)  # *.tar, die keinem bekannten Paket entsprechen
    partials: list[str] = field(default_factory=list)
    manifests_missing: list[str] = field(default_factory=list)
    manifests_bad: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)  # Status -> Bytes

    @property
    def failed(self) -> bool:
        return bool(self.bad or self.missing or self.manifests_missing or self.manifests_bad)


def verify_hdd(app: App, volume: Volume, series: list[str] | None, quick: bool) -> VerifyResult:
    """Pakete auf ``volume`` gegen den Index prüfen (voll: SHA-256, quick: Existenz und Größe)."""
    con, db = app.console, app.db
    _ensure_registered(app, volume)
    res = VerifyResult()
    sizes = {"ok": 0, "bad": 0, "missing": 0}

    sql = """SELECT b.*, c.status AS copy_status FROM copies c JOIN bundles b ON b.id=c.bundle_id
             WHERE c.volume_id=?"""
    params: list = [volume.id]
    if series:
        sql += f" AND b.series IN ({_in_clause(series)})"
        params += list(series)
    rows = db.q(sql + " ORDER BY b.series, b.version, b.name", params)

    # Pakete auf der Platte ohne copies-Eintrag (bekannt per relpath)
    listed = {
        r["relpath"]
        for r in db.q("SELECT b.relpath FROM copies c JOIN bundles b ON b.id=c.bundle_id WHERE c.volume_id=?", (volume.id,))
    }
    orphans = []
    for p in sorted(volume.root.rglob("*.tar")):
        rel = p.relative_to(volume.root).as_posix()
        if rel.split("/", 1)[0] in (INDEX_DIR, CATALOG_DIR) or rel in listed:
            continue
        b = db.q1("SELECT * FROM bundles WHERE relpath=?", (rel,))
        if b is None:
            res.unknown.append(rel)
        elif not series or b["series"] in series:
            orphans.append(b)

    def to_hash(b) -> bool:
        p = volume.path_for(b["relpath"])
        return p.is_file() and p.stat().st_size == b["size"]

    # voll: gehashte Bytes; quick: Fortschritt je geprüftem Paket (nach Paketgröße)
    if quick:
        total = sum(b["size"] for b in rows)
    else:
        total = sum(b["size"] for b in (*rows, *orphans) if to_hash(b))
    with app.byte_progress() as prog:
        task = prog.add_task(f"Prüfe {volume.label}", total=total)
        for b in rows:
            rel = b["relpath"]
            p = volume.path_for(rel)
            prog.update(task, description=f"{b['series']} v{b['version']} {b['name']}")
            if quick:
                prog.advance(task, b["size"])
            if not p.is_file():
                db.set_copy(b["id"], volume.id, "missing")
                res.missing.append(rel)
                sizes["missing"] += b["size"]
                continue
            if p.stat().st_size != b["size"]:
                if quick:
                    db.set_copy(b["id"], volume.id, "bad")
                else:
                    db.mark_verified(b["id"], volume.id, ok=False)
                res.bad.append(rel)
                sizes["bad"] += b["size"]
                continue
            if quick:
                if b["copy_status"] == "ok":
                    res.ok.append(rel)
                    sizes["ok"] += b["size"]
                else:
                    res.unverified.append(rel)  # Status bleibt, bis eine volle Prüfung ihn bestätigt
                continue
            good = sha256_file(p, lambda d: prog.advance(task, d)) == b["sha256"]
            db.mark_verified(b["id"], volume.id, ok=good)
            (res.ok if good else res.bad).append(rel)
            sizes["ok" if good else "bad"] += b["size"]

        for b in orphans:
            rel = b["relpath"]
            if quick or not to_hash(b):
                res.unregistered.append(rel)
                continue
            if sha256_file(volume.path_for(rel), lambda d: prog.advance(task, d)) == b["sha256"]:
                db.set_copy(b["id"], volume.id, "ok", verified=True)
                res.registered.append(rel)
                sizes["ok"] += b["size"]
            else:
                res.unregistered.append(rel)

    _verify_manifests(app, volume, series, quick, res)
    res.partials = sorted(p.relative_to(volume.root).as_posix() for p in volume.root.rglob(f"*{PARTIAL_SUFFIX}"))
    res.sizes = sizes

    _render_verify(app, volume, quick, res)
    export_index(app, volume)
    if res.failed:
        raise OpFailed(res)
    return res


def _verify_manifests(app: App, volume: Volume, series: list[str] | None, quick: bool, res: VerifyResult) -> None:
    """Manifeste aller Versionen, die laut Index (Pakete oder Manifest) auf dem Volume liegen."""
    db = app.db
    sql = """SELECT DISTINCT v.* FROM versions v
             WHERE (EXISTS (SELECT 1 FROM copies c JOIN bundles b ON b.id=c.bundle_id
                            WHERE c.volume_id=? AND b.series=v.series AND b.version=v.version)
                    OR EXISTS (SELECT 1 FROM manifest_copies m
                               WHERE m.volume_id=? AND m.series=v.series AND m.version=v.version))"""
    params: list = [volume.id, volume.id]
    if series:
        sql += f" AND v.series IN ({_in_clause(series)})"
        params += list(series)
    for v in db.q(sql + " ORDER BY v.series, v.version", params):
        rel = v["manifest_relpath"]
        p = volume.path_for(rel)
        if not p.is_file():
            res.manifests_missing.append(rel)
            db.x("DELETE FROM manifest_copies WHERE series=? AND version=? AND volume_id=?", (v["series"], v["version"], volume.id))
            continue
        if not quick and _bytes_hash(p) != v["manifest_sha256"]:
            res.manifests_bad.append(rel)
            continue
        db.x(
            "INSERT OR IGNORE INTO manifest_copies(series, version, volume_id, stored_at) VALUES(?,?,?,?)",
            (v["series"], v["version"], volume.id, utcnow()),
        )


def _render_verify(app: App, volume: Volume, quick: bool, res: VerifyResult) -> None:
    con = app.console
    t = Table("Status", "Pakete", "Größe", title=f"{volume.label}: {'Schnellprüfung' if quick else 'Prüfung (SHA-256)'}", header_style="bold")
    t.add_row("[green]ok", str(len(res.ok) + len(res.registered)), human(res.sizes.get("ok", 0)))
    t.add_row("[red]bad", str(len(res.bad)), human(res.sizes.get("bad", 0)))
    t.add_row("[red]missing", str(len(res.missing)), human(res.sizes.get("missing", 0)))
    if res.unverified:
        t.add_row("[yellow]ungeprüft", str(len(res.unverified)), "")
    con.print(t)

    problems: list[str] = []
    problems += [f"[red]defekt:[/] {p}" for p in res.bad]
    problems += [f"[red]fehlt:[/] {p}" for p in res.missing]
    problems += [f"[red]Manifest fehlt:[/] {p}" for p in res.manifests_missing]
    problems += [f"[red]Manifest defekt:[/] {p}" for p in res.manifests_bad]
    problems += [f"[yellow]Status nicht ok, Größe passt – volle Prüfung nötig:[/] {p}" for p in res.unverified]
    problems += [f"[yellow]nicht im Index registriert:[/] {p}" for p in res.unregistered]
    problems += [f"[yellow]unbekanntes Tar:[/] {p}" for p in res.unknown]
    problems += [f"[yellow]Rest eines abgebrochenen Schreibvorgangs:[/] {p}" for p in res.partials]
    for p in res.registered:
        con.print(f"[green]neu registriert:[/] {p}")
    if problems:
        con.print("[bold]Auffälligkeiten:")
        for p in problems:
            con.print(f"  {p}")
    elif not quick:
        con.print("[green]✓ Alle Pakete in Ordnung.")

    try:
        total, used, _ = volume.usage()
        if total and used / total > USAGE_WARN:
            con.print(f"[yellow]Warnung: {volume.label} ist zu {used / total:.0%} belegt – neue Platte einplanen.")
    except OSError:
        pass


# =========================================================================== slim


@dataclass
class SlimResult:
    candidates: int = 0
    deleted: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # geändert oder unsicher -> nicht gelöscht
    errors: list[str] = field(default_factory=list)
    bytes_freed: int = 0
    dirs_removed: list[str] = field(default_factory=list)


def slim(app: App, series: list[str] | None, classes: list[str] | None, apply: bool) -> SlimResult:
    """Lokale Dateien (Standard: Klassen aus [slim]) entfernen, die ausreichend archiviert sind.

    Ohne ``apply`` nur Vorschau.
    """
    con, db, cfg = app.console, app.db, app.cfg
    res = SlimResult()
    root = cfg.source_root
    if not root.is_dir():
        con.print(f"[red]Foto-Ordner existiert nicht:[/] {root} – Abbruch.")
        raise OpFailed(res)
    _check_classes(app, classes)
    classes = classes or cfg.slim.classes

    cands = slim_candidates(app, series, classes)
    res.candidates = len(cands)
    _render_slim_preview(app, cands, series, classes)
    if not cands or not apply:
        if cands:
            con.print("Vorschau – zum Löschen [bold]--apply[/] angeben.")
        return res

    root_res = root.resolve()
    touched_dirs: set[Path] = set()
    for c in cands:
        p = root.joinpath(*c.path.split("/"))
        reason = _unsafe_reason(db, c, p, root_res)
        if reason:
            res.skipped.append(c.path)
            con.print(f"[yellow]Übersprungen:[/] {c.path} ({reason})")
            continue
        try:
            os.remove(p)
        except OSError as e:
            res.errors.append(f"{c.path}: {e}")
            continue
        db.x("UPDATE files SET present=0, slimmed=1 WHERE path=?", (c.path,))
        res.deleted.append(c.path)
        res.bytes_freed += c.size
        touched_dirs.add(p.parent)

    res.dirs_removed = _remove_empty_dirs(root, touched_dirs)
    con.print(
        f"[green]✓ {len(res.deleted)} Datei(en) entfernt, {human(res.bytes_freed)} frei geworden[/]"
        + (f", {len(res.dirs_removed)} leere(r) Ordner entfernt" if res.dirs_removed else "")
    )
    if res.skipped:
        con.print(f"[yellow]{len(res.skipped)} Datei(en) übersprungen.")
    if res.errors:
        for e in res.errors:
            con.print(f"[red]Fehler:[/] {e}")
        raise OpFailed(res)
    return res


def _unsafe_reason(db, c, p: Path, root_res: Path) -> str | None:
    """Grund, eine Kandidatendatei *nicht* zu löschen (None = sicher)."""
    try:
        rp = p.resolve()
    except OSError as e:
        return f"Pfad nicht auflösbar: {e}"
    if rp == root_res or not rp.is_relative_to(root_res):
        return "liegt außerhalb des Foto-Ordners"
    if p.is_symlink() or not p.is_file():
        return "keine reguläre Datei"
    row = db.q1("SELECT size, mtime_ns, sha256, present FROM files WHERE path=?", (c.path,))
    if row is None or not row["present"] or row["sha256"] != c.sha256:
        return "Index-Eintrag hat sich geändert"
    st = p.stat()
    if st.st_size != row["size"] or st.st_mtime_ns != row["mtime_ns"]:
        return "Datei wurde seit dem letzten scan verändert"
    return None


def _remove_empty_dirs(root: Path, dirs: set[Path]) -> list[str]:
    """Durch slim leer gewordene Ordner entfernen – nie den Foto-Ordner oder einen Serien-Ordner."""
    removed: list[str] = []
    for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
        while True:
            try:
                rel = d.relative_to(root)
            except ValueError:
                break
            if len(rel.parts) < 2:  # root selbst oder Serienordner
                break
            try:
                os.rmdir(d)  # schlägt fehl, wenn nicht leer
            except OSError:
                break
            removed.append(rel.as_posix())
            d = d.parent
    return removed


def _render_slim_preview(app: App, cands, series: list[str] | None, classes: list[str]) -> None:
    con, db, cfg = app.console, app.db, app.cfg
    labels = {r["id"]: r["label"] for r in db.q("SELECT id, label FROM volumes")}
    by_series: dict[str, list] = {}
    for c in cands:
        by_series.setdefault(c.series, []).append(c)

    if by_series:
        t = Table("Serie", "Dateien", "frei werdend", "geprüft auf HDD", "S3", title=f"Ausdünnen ({', '.join(classes)})", header_style="bold")
        total_n = total_b = 0
        for s in sorted(by_series):
            items = by_series[s]
            prot = bundle_protection(app, s)
            ps = [prot[i] for i in {c.bundle_id for c in items} if i in prot]
            hdd = set.intersection(*(p.hdd_verified for p in ps)) if ps else set()
            s3 = "[green]ja" if ps and all(p.s3_ok for p in ps) else "nein"
            if ps and all(p.s3_verified for p in ps):
                s3 += " (geprüft)"
            n, b = len(items), sum(c.size for c in items)
            total_n += n
            total_b += b
            t.add_row(s, str(n), human(b), ", ".join(sorted(labels.get(v, v[:8]) for v in hdd)), s3)
        t.add_section()
        t.add_row("[bold]Summe", str(total_n), human(total_b), "", "")
        con.print(t)
    else:
        con.print("Keine Dateien ausdünnbar.")

    # Hinweis auf nicht ausreichend geschützte Dateien dieser Klassen
    sql = f"SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files WHERE present=1 AND cls IN ({_in_clause(classes)})"
    params: list = list(classes)
    if series:
        sql += f" AND series IN ({_in_clause(series)})"
        params += list(series)
    r = db.q1(sql, params)
    rest_n, rest_b = r["n"] - len(cands), r["s"] - sum(c.size for c in cands)
    if rest_n > 0:
        cond = f"≥ {cfg.slim.min_hdd_copies} geprüfte HDD-Kopie(n)" + (" und eine S3-Kopie" if cfg.slim.require_s3 else "")
        con.print(
            f"[dim]{rest_n} weitere Datei(en) ({human(rest_b)}) noch nicht ausdünnbar – nötig: archivierter, "
            f"gehashter Stand mit {cond}."
        )


# =========================================================================== restore (HDD)


@dataclass
class RestoreResult:
    written: list[str] = field(default_factory=list)
    skipped_same: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    hash_errors: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)  # Pakete, die auf dem Volume nicht (ok) liegen
    errors: list[str] = field(default_factory=list)
    bytes_written: int = 0

    @property
    def failed(self) -> bool:
        return bool(self.hash_errors or self.unavailable or self.errors)


def restore_hdd(
    app: App, series: str, classes: list[str] | None, target: Path, volume: Volume, overwrite: bool, dry_run: bool
) -> RestoreResult:
    """Serie (optional nur bestimmte Klassen) von einem HDD-Volume nach ``target`` entpacken.

    Je Pfad wird nur der Stand der jüngsten Version wiederhergestellt.
    """
    con, db, cfg = app.console, app.db, app.cfg
    res = RestoreResult()
    _check_classes(app, classes)

    latest = db.latest_archived(series)
    if classes:
        latest = {p: a for p, a in latest.items() if a["cls"] in classes}
    if not latest:
        what = f" (Klassen: {', '.join(classes)})" if classes else ""
        con.print(f"[red]Für '{series}'{what} ist nichts archiviert.")
        raise OpFailed(res)

    # Pfade je Paket (jeweils jüngste Version)
    wanted: dict[int, dict[str, str]] = {}
    for path, a in latest.items():
        wanted.setdefault(a["bundle_id"], {})[path] = a["sha256"]
    bundles = db.q(
        f"SELECT * FROM bundles WHERE id IN ({_in_clause(wanted)}) ORDER BY version, name", list(wanted)
    )
    ok_here = {
        r["bundle_id"]
        for r in db.q("SELECT bundle_id FROM copies WHERE volume_id=? AND status='ok'", (volume.id,))
    }
    available, missing = [], []
    for b in bundles:
        if b["id"] in ok_here and volume.path_for(b["relpath"]).is_file():
            available.append(b)
        else:
            missing.append(b)

    if dry_run:
        t = Table("Version", "Paket", "Klasse", "Dateien", "Größe", f"auf {volume.label}", header_style="bold",
                  title=f"Wiederherstellen {series} → {target}")
        available_ids = {b["id"] for b in available}
        for b in bundles:
            where = "[green]✓" if b["id"] in available_ids else "[red]✗ " + ", ".join(_other_locations(app, b["id"], volume.id))
            t.add_row(f"v{b['version']}", b["name"], b["cls"], f"{len(wanted[b['id']])}/{b['nfiles']}", human(b["size"]), where)
        con.print(t)
        con.print(
            f"{sum(len(wanted[b['id']]) for b in bundles)} Datei(en), "
            f"zu lesen: {human(sum(b['size'] for b in available))}"
        )
        return res

    for b in missing:
        where = _other_locations(app, b["id"], volume.id)
        res.unavailable.append(b["relpath"])
        con.print(
            f"[red]{b['relpath']} liegt nicht (geprüft ok) auf {volume.label}[/] – "
            + (f"vorhanden auf: {', '.join(where)}" if where else "keine andere Kopie bekannt")
        )

    target.mkdir(parents=True, exist_ok=True)
    to_source = target.resolve() == cfg.source_root.resolve()
    done = 0
    with app.byte_progress() as prog:
        task = prog.add_task(f"{series}", total=sum(b["size"] for b in available))
        for b in available:
            prog.update(task, description=f"{series} v{b['version']} {b['name']}")
            exp = wanted[b["id"]]
            try:
                st = extract_bundle(
                    volume.path_for(b["relpath"]), target, expected=exp, only=set(exp), overwrite=overwrite,
                    progress=lambda d: prog.advance(task, d),
                )
            except (OSError, tarfile.TarError, ValueError) as e:
                res.errors.append(f"{b['relpath']}: {e}")
                continue
            finally:
                done += b["size"]
                prog.update(task, completed=done)
            res.written += st.written
            res.skipped_same += st.skipped_same
            res.conflicts += st.conflicts
            res.hash_errors += st.hash_errors
            res.bytes_written += st.bytes_written
            if to_source:
                _update_files_rows(app, target, st.written + st.skipped_same, latest)

    _render_restore(app, series, target, res)
    if res.failed:
        raise OpFailed(res)
    return res


def _update_files_rows(app: App, root: Path, paths: list[str], latest: dict) -> None:
    """Index-Zeilen wiederhergestellter Dateien aktualisieren, damit ``scan`` sie als unverändert sieht."""
    now = utcnow()
    with app.db.tx() as c:
        for path in paths:
            a = latest[path]
            try:
                st = root.joinpath(*path.split("/")).stat()
            except OSError:
                continue
            c.execute(
                """INSERT INTO files(path, series, cls, size, mtime_ns, sha256, present, slimmed, last_seen)
                   VALUES(?,?,?,?,?,?,1,0,?)
                   ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                       sha256=excluded.sha256, present=1, slimmed=0, last_seen=excluded.last_seen""",
                (path, a["series"], a["cls"], st.st_size, st.st_mtime_ns, a["sha256"], now),
            )


def _render_restore(app: App, series: str, target: Path, res: RestoreResult) -> None:
    con = app.console
    con.print(
        f"[bold]{series}[/] → {target}: [green]{len(res.written)} geschrieben ({human(res.bytes_written)})[/], "
        f"{len(res.skipped_same)} identisch übersprungen, "
        f"{len(res.conflicts)} Konflikt(e), {len(res.hash_errors)} Prüfsummenfehler"
    )
    if res.conflicts:
        con.print("[yellow]Vorhanden mit anderem Inhalt (nicht überschrieben, ggf. --overwrite):")
        for p in res.conflicts[:20]:
            con.print(f"  {p}")
        if len(res.conflicts) > 20:
            con.print(f"  … und {len(res.conflicts) - 20} weitere")
    if res.hash_errors:
        con.print("[bold red]Prüfsummenfehler – diese Dateien wurden NICHT geschrieben:")
        for p in res.hash_errors:
            con.print(f"  [red]{p}")
    for e in res.errors:
        con.print(f"[red]Fehler:[/] {e}")
