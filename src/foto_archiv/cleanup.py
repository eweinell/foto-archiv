"""Aufräumen auf Archiv-HDDs: Reste abgebrochener Vorgänge (``hdd clean``) und das Entfernen
versiegelter Versionen, die nur auf HDDs liegen (``drop``).

Grundsätze:

- Ohne ``apply`` wird nur angezeigt, was geschehen würde.
- In S3 wird nie etwas gelöscht. Versionen mit einer Kopie (oder einem laufenden Upload) in S3
  lassen sich nicht entfernen.
- ``hdd clean`` löscht nur, was der Index nicht kennt und erkennbar unvollständig ist: Tars in
  Versionsordnern ohne Manifest und ``*.foto-archiv-partial``. Ein unbekanntes Tar neben einem
  Manifest ist eine vollständige Version - dann ist eher der Index veraltet; es bleibt liegen.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape

from .context import App, human
from .layout import CATALOG_DIR, INDEX_DIR, README_NAME, ROOT_SERIES, series_name
from .seal import export_index
from .tario import PARTIAL_SUFFIX
from .volumes import MARKER, Volume

MANIFEST_NAME = "manifest.json.gz"


class DropError(Exception):
    pass


def _rel(volume: Volume, p: Path | str) -> str:
    return Path(p).relative_to(volume.root).as_posix()


def _remove_empty_parents(volume: Volume, d: Path) -> None:
    """Leere Ordner bis unterhalb der obersten Ebene (Archivordner der Sammlung) entfernen."""
    while d != volume.root and d.parent != volume.root:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


# =========================================================================== hdd clean


@dataclass
class CleanItem:
    rel: str
    size: int
    reason: str


@dataclass
class CleanResult:
    remove: list[CleanItem] = field(default_factory=list)  # wird gelöscht
    dirs: list[str] = field(default_factory=list)  # danach leere Ordner
    kept: list[CleanItem] = field(default_factory=list)  # auffällig, bleibt aber liegen
    errors: list[str] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(i.size for i in self.remove)


def plan_clean(app: App, volume: Volume) -> CleanResult:
    db = app.db
    bundles = {r["relpath"] for r in db.q("SELECT relpath FROM bundles")}
    manifests = {r["manifest_relpath"] for r in db.q("SELECT manifest_relpath FROM versions")}
    res = CleanResult()
    removable_dirs: set[Path] = set()
    for d, subdirs, files in os.walk(volume.root, topdown=False):
        dp = Path(d)
        rel_d = "" if dp == volume.root else _rel(volume, dp)
        top = rel_d.split("/", 1)[0]
        special = top in (INDEX_DIR, CATALOG_DIR)
        removed_here = 0
        for name in files:
            p = dp / name
            rel = f"{rel_d}/{name}" if rel_d else name
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if name.endswith(PARTIAL_SUFFIX):
                res.remove.append(CleanItem(rel, size, "Rest eines abgebrochenen Schreibvorgangs"))
                removed_here += 1
            elif special or (not rel_d and name in (MARKER, README_NAME)):
                continue
            elif name.lower().endswith(".tar") and rel not in bundles:
                mrel = f"{rel_d}/{MANIFEST_NAME}"
                if mrel in manifests:
                    res.remove.append(CleanItem(rel, size, "gehört nicht zur versiegelten Version (Rest eines früheren Versuchs)"))
                    removed_here += 1
                elif (dp / MANIFEST_NAME).is_file():
                    res.kept.append(CleanItem(rel, size, "vollständige Version, die der Index nicht kennt (Index veraltet?)"))
                else:
                    res.remove.append(CleanItem(rel, size, "Paket eines abgebrochenen seal (nicht im Index, kein Manifest)"))
                    removed_here += 1
            elif name == MANIFEST_NAME and rel not in manifests:
                res.kept.append(CleanItem(rel, size, "Manifest einer Version, die der Index nicht kennt"))
            elif not name.lower().endswith(".tar") and name != MANIFEST_NAME:
                res.kept.append(CleanItem(rel, size, "unbekannte Datei"))
        # Ordner wird leer, wenn alle Dateien gelöscht werden und alle Unterordner leer werden
        if (
            rel_d
            and "/" in rel_d  # oberste Ebene (Archivordner, _index, _kataloge) bleibt
            and not special
            and removed_here == len(files)
            and all((dp / s) in removable_dirs for s in subdirs)
        ):
            removable_dirs.add(dp)
            res.dirs.append(rel_d)
    return res


def clean_hdd(app: App, volume: Volume, apply: bool) -> CleanResult:
    con = app.console
    res = plan_clean(app, volume)
    if not res.remove and not res.dirs:
        con.print(f"[green]{volume.label}: nichts aufzuräumen.")
    else:
        con.print(f"[bold]{volume.label}[/] ({volume.root}): {len(res.remove)} Dateien, {human(res.bytes)}"
                  + (f", {len(res.dirs)} leere Ordner" if res.dirs else ""))
        for i in res.remove:
            con.print(f"  [red]löschen[/] {escape(i.rel)}  ({human(i.size)}) - {i.reason}", highlight=False)
    for i in res.kept:
        con.print(f"  [yellow]bleibt[/] {escape(i.rel)} - {i.reason}", highlight=False)
    if not apply:
        if res.remove or res.dirs:
            con.print("[dim]Vorschau - mit --apply ausführen.")
        return res
    for i in res.remove:
        try:
            volume.path_for(i.rel).unlink(missing_ok=True)
        except OSError as e:
            res.errors.append(f"{i.rel}: {e}")
    for rel in res.dirs:  # von innen nach außen (os.walk topdown=False)
        try:
            volume.path_for(rel).rmdir()
        except OSError as e:
            res.errors.append(f"{rel}/: {e}")
    for e in res.errors:
        con.print(f"[red]{e}")
    if res.remove or res.dirs:
        con.print(f"[green]✓ {volume.label}: {human(res.bytes)} freigegeben.")
    return res


# =========================================================================== drop


@dataclass
class DropPlan:
    series: str
    versions: list[int]
    bundles: list = field(default_factory=list)  # sqlite3.Row
    manifests: list[str] = field(default_factory=list)
    size: int = 0
    hdd_labels: dict[str, str] = field(default_factory=dict)  # Volume-ID -> Label (laut Index betroffen)
    blockers: list[str] = field(default_factory=list)
    local_files: int = 0


def plan_drop(app: App, series: str, version: int | None, connected: list[Volume]) -> DropPlan:
    db = app.db
    all_versions = [r["version"] for r in db.q("SELECT version FROM versions WHERE series=? ORDER BY version", (series,))]
    if not all_versions:
        raise DropError(f"{series}: keine versiegelte Version.")
    if version is None:
        versions = all_versions
    elif version not in all_versions:
        raise DropError(f"{series}: Version v{version} gibt es nicht (vorhanden: {', '.join(f'v{v}' for v in all_versions)}).")
    else:
        versions = [version]
    plan = DropPlan(series=series, versions=versions)
    newest = max(all_versions)
    if version is not None and version != newest:
        plan.blockers.append(
            f"v{version} ist nicht die neueste Version (v{newest}). Spätere Versionen enthalten nur Änderungen; "
            "unveränderte Dateien lägen dann nirgends mehr. Neuere Versionen zuerst entfernen oder die ganze Serie."
        )
    vin = ",".join("?" * len(versions))
    plan.bundles = db.q(f"SELECT * FROM bundles WHERE series=? AND version IN ({vin}) ORDER BY version, name", (series, *versions))
    plan.manifests = [
        r["manifest_relpath"] for r in db.q(f"SELECT manifest_relpath FROM versions WHERE series=? AND version IN ({vin})", (series, *versions))
    ]
    plan.size = sum(b["size"] for b in plan.bundles)
    ids = [b["id"] for b in plan.bundles]
    bin_ = ",".join("?" * len(ids)) or "NULL"

    s3 = db.q(
        f"""SELECT DISTINCT b.relpath FROM copies c JOIN bundles b ON b.id=c.bundle_id JOIN volumes v ON v.id=c.volume_id
            WHERE v.kind='s3' AND c.bundle_id IN ({bin_})""",
        ids,
    )
    s3m = db.q(
        f"""SELECT m.version FROM manifest_copies m JOIN volumes v ON v.id=m.volume_id
            WHERE v.kind='s3' AND m.series=? AND m.version IN ({vin})""",
        (series, *versions),
    )
    if s3 or s3m:
        plan.blockers.append(
            f"liegt bereits in S3 ({len(s3)} Pakete, {len(s3m)} Manifeste). In S3 wird nicht gelöscht "
            "(kein Löschrecht, Deep Archive berechnet mindestens 180 Tage)."
        )
    if db.q(f"SELECT 1 FROM uploads WHERE bundle_id IN ({bin_})", ids):
        plan.blockers.append("ein Upload nach S3 ist angefangen - erst 'push-s3' abschließen (dann liegt es in S3).")

    slimmed = db.q1(
        f"""SELECT COUNT(*) n, COALESCE(SUM(f.size),0) s FROM files f WHERE f.series=? AND f.slimmed=1
            AND EXISTS (SELECT 1 FROM archived a WHERE a.path=f.path AND a.version IN ({vin}))""",
        (series, *versions),
    )
    if slimmed["n"]:
        plan.blockers.append(
            f"{slimmed['n']} lokal ausgedünnte Dateien ({human(slimmed['s'])}) gibt es nur noch im Archiv - erst 'restore'."
        )

    for r in db.q(
        f"""SELECT DISTINCT v.id, v.label FROM copies c JOIN volumes v ON v.id=c.volume_id
            WHERE v.kind='hdd' AND c.status<>'missing' AND c.bundle_id IN ({bin_})
            UNION SELECT DISTINCT v.id, v.label FROM manifest_copies m JOIN volumes v ON v.id=m.volume_id
            WHERE v.kind='hdd' AND m.series=? AND m.version IN ({vin})""",
        (*ids, series, *versions),
    ):
        plan.hdd_labels[r["id"]] = r["label"]
    have = {v.id for v in connected}
    missing = sorted(lbl for vid, lbl in plan.hdd_labels.items() if vid not in have)
    if missing:
        plan.blockers.append(f"nicht angeschlossen: {', '.join(missing)} - alle Platten mit Kopien anschließen.")

    plan.local_files = db.q1("SELECT COUNT(*) n FROM files WHERE series=? AND present=1", (series,))["n"]
    return plan


def render_drop(app: App, plan: DropPlan, connected: list[Volume]) -> None:
    con = app.console
    labels = ", ".join(plan.hdd_labels.values()) or "-"
    con.print(
        f"[bold]{escape(plan.series)}[/]: {', '.join(f'v{v}' for v in plan.versions)} entfernen - {len(plan.bundles)} Pakete, "
        f"{human(plan.size)}, Kopien auf: {labels}"
    )
    for b in plan.bundles:
        con.print(f"   v{b['version']} {b['name']:<18} {human(b['size']):>10}  {escape(b['relpath'])}", highlight=False)
    for msg in plan.blockers:
        con.print(f"[red]Nicht möglich:[/] {msg}")


def drop_series(app: App, plan: DropPlan, connected: list[Volume]) -> list[str]:
    """Pakete und Manifeste von allen angeschlossenen HDDs löschen, danach aus dem Index entfernen.

    Gelöscht wird auf jeder angeschlossenen Platte, auch ohne registrierte Kopie. Scheitert ein
    Löschvorgang, bleibt der Index unverändert (erneut ausführen)."""
    if plan.blockers:
        raise DropError("; ".join(plan.blockers))
    db = app.db
    con = app.console
    errors: list[str] = []
    rels = [b["relpath"] for b in plan.bundles] + plan.manifests
    for vol in connected:
        dirs: set[Path] = set()
        n = 0
        for rel in rels:
            p = vol.path_for(rel)
            dirs.add(p.parent)
            for q in (p, p.with_name(p.name + PARTIAL_SUFFIX)):
                try:
                    if q.exists():
                        q.unlink()
                        n += 1
                except OSError as e:
                    errors.append(f"{vol.label}: {rel}: {e}")
        for d in sorted(dirs, key=lambda x: len(x.parts), reverse=True):
            if d.is_dir():
                for stale in d.glob(f"*{PARTIAL_SUFFIX}"):
                    stale.unlink(missing_ok=True)
                _remove_empty_parents(vol, d)
        if n:
            con.print(f"  {vol.label}: {n} Dateien gelöscht")
    if errors:
        for e in errors:
            con.print(f"[red]{e}")
        con.print("[red]Index nicht geändert - Ursache beheben und 'drop' erneut ausführen.")
        return errors

    ids = [b["id"] for b in plan.bundles]
    bin_ = ",".join("?" * len(ids)) or "NULL"
    vin = ",".join("?" * len(plan.versions))
    with db.tx() as c:
        c.execute(f"DELETE FROM archived WHERE bundle_id IN ({bin_})", ids)
        c.execute(f"DELETE FROM archived WHERE series=? AND version IN ({vin})", (plan.series, *plan.versions))
        c.execute(f"DELETE FROM copies WHERE bundle_id IN ({bin_})", ids)
        c.execute(f"DELETE FROM uploads WHERE bundle_id IN ({bin_})", ids)
        c.execute(f"DELETE FROM bundles WHERE id IN ({bin_})", ids)
        c.execute(f"DELETE FROM manifest_copies WHERE series=? AND version IN ({vin})", (plan.series, *plan.versions))
        c.execute(f"DELETE FROM versions WHERE series=? AND version IN ({vin})", (plan.series, *plan.versions))
        c.execute(
            """DELETE FROM series WHERE name=? AND NOT EXISTS (SELECT 1 FROM files WHERE series=?)
               AND NOT EXISTS (SELECT 1 FROM versions WHERE series=?)""",
            (plan.series, plan.series, plan.series),
        )
    for vol in connected:
        export_index(app, vol)
    con.print(f"[green]✓ {plan.series}: {', '.join(f'v{v}' for v in plan.versions)} entfernt ({human(plan.size)}).")
    if plan.local_files:
        what = (
            "die Dateien direkt im Quellordner" if series_name(plan.series) == ROOT_SERIES
            else f"den Ordner '{series_name(plan.series)}'"
        )
        con.print(
            f"[yellow]Hinweis:[/] {plan.local_files} Dateien liegen noch lokal; 'seal --ripe' würde sie erneut versiegeln. "
            f"Zum dauerhaften Ausschluss {what} in exclude_paths der Sammlung eintragen."
        )
    return []
