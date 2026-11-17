"""Versiegeln: neue/geänderte Dateien einer Serie als Version vN auf ein HDD-Volume packen."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .classify import CLASSES
from .config import Collection, ConfigError
from .context import App, human
from .db import utcnow
from .layout import (
    INDEX_DIR,
    README_NAME,
    bundle_name,
    bundle_relpath,
    collection_year,
    manifest_relpath,
    series_name,
    split_vpath,
)
from .tario import PARTIAL_SUFFIX, TarEntry, sha256_file, write_tar
from .volumes import FAT_MAX_FILE, Volume, is_fat

TAR_OVERHEAD_PER_FILE = 3 * 512  # Header + PAX-Header + Padding (Schätzung)
TAR_OVERHEAD_FIXED = 20 * 512


class SealError(Exception):
    pass


@dataclass
class PlanItem:
    path: str
    cls: str
    size: int
    mtime_ns: int


@dataclass
class SealPlan:
    series: str
    version: int
    items: list[PlanItem]
    collection: Collection
    chunks: dict[str, list[list[PlanItem]]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)

    def chunk_sizes(self) -> list[int]:
        """Geschätzte Größe jedes Pakets (Bytes)."""
        return [
            TAR_OVERHEAD_FIXED + sum(i.size + TAR_OVERHEAD_PER_FILE for i in c)
            for chunks in self.chunks.values()
            for c in chunks
        ]

    def by_class(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for i in self.items:
            n, s = out.get(i.cls, (0, 0))
            out[i.cls] = (n + 1, s + i.size)
        return out


def _chunk(items: list[PlanItem], limit: int) -> list[list[PlanItem]]:
    chunks: list[list[PlanItem]] = []
    cur: list[PlanItem] = []
    cur_size = TAR_OVERHEAD_FIXED
    for it in sorted(items, key=lambda i: i.path):
        need = it.size + TAR_OVERHEAD_PER_FILE
        if cur and cur_size + need > limit:
            chunks.append(cur)
            cur, cur_size = [], TAR_OVERHEAD_FIXED
        cur.append(it)
        cur_size += need
    if cur:
        chunks.append(cur)
    return chunks


def plan_seal(app: App, series: str) -> SealPlan | None:
    """Ermittelt, was in die nächste Version einer Serie gehört (None = nichts zu tun)."""
    db = app.db
    cid, name = split_vpath(series)
    try:
        coll = app.cfg.collection(cid)
    except ConfigError as e:
        raise SealError(f"Serie '{series}': {e}") from None
    if not name:
        raise SealError(f"Serie '{series}': Serienname fehlt.")
    archived = db.latest_archived(series)
    rows = db.q("SELECT * FROM files WHERE series=? AND present=1 ORDER BY path", (series,))
    if not rows and not archived:
        raise SealError(f"Serie '{series}' ist unbekannt (erst 'scan' ausführen).")
    items: list[PlanItem] = []
    for r in rows:
        a = archived.get(r["path"])
        if a is not None:
            sha = r["sha256"]
            if sha is None:
                if a["size"] != r["size"]:
                    sha = "changed"
                else:
                    # unbekannter Stand: hashen und mit Archiv vergleichen
                    sha = sha256_file(app.cfg.source_file(r["path"]))
                    db.x("UPDATE files SET sha256=? WHERE path=? AND mtime_ns=?", (sha, r["path"], r["mtime_ns"]))
            if sha == a["sha256"]:
                continue
        items.append(PlanItem(r["path"], r["cls"], r["size"], r["mtime_ns"]))
    if not items:
        return None
    plan = SealPlan(series=series, version=db.next_version(series), items=items, collection=coll)
    limit = app.cfg.chunk_size_bytes
    order = list(CLASSES) + sorted({i.cls for i in items} - set(CLASSES))
    for cls in order:
        cls_items = [i for i in items if i.cls == cls]
        if cls_items:
            plan.chunks[cls] = _chunk(cls_items, limit)
    return plan


def _manifest(
    coll: Collection, series: str, version: int, sealed_at: str, bundles: list[dict], files: list[dict], removed: list[str]
) -> bytes:
    doc = {
        "format": "foto-archiv/manifest@2",
        "collection": coll.id,
        "series": series_name(series),
        "year": collection_year(coll, series),
        "version": version,
        "sealed_at": sealed_at,
        "paths": "relativ zum Quellordner der Sammlung (wie im Tar)",
        "bundles": bundles,
        "files": files,
        "removed_since_previous": removed,
    }
    raw = json.dumps(doc, ensure_ascii=False, indent=1).encode("utf-8")
    return gzip.compress(raw, mtime=0)


def seal_series(app: App, series: str, volume: Volume, verify: bool = True, dry_run: bool = False) -> SealPlan | None:
    db = app.db
    con = app.console
    plan = plan_seal(app, series)
    if plan is None:
        con.print(f"[green]{series}[/]: nichts Neues zu versiegeln.")
        return None

    con.print(f"[bold]{series}[/] → v{plan.version}: {len(plan.items)} Dateien, {human(plan.total)}")
    for cls, (n, s) in plan.by_class().items():
        con.print(f"   {cls:<13} {n:>6} Dateien {human(s):>10}  in {len(plan.chunks[cls])} Paket(en)")
    if dry_run:
        return plan

    need = plan.total + len(plan.items) * TAR_OVERHEAD_PER_FILE + 50 * 1024 * 1024
    free = volume.free_bytes()
    if free < need:
        raise SealError(f"Zu wenig Platz auf {volume}: frei {human(free)}, benötigt {human(need)}")
    biggest = max(plan.chunk_sizes(), default=0)
    if biggest > FAT_MAX_FILE:
        fs = volume.filesystem()
        if is_fat(fs):
            raise SealError(
                f"{volume.label} ist mit {fs} formatiert (max. 4 GiB je Datei), ein Paket wird {human(biggest)} groß "
                "(Einzeldatei > 4 GiB). Archivplatte mit NTFS oder exFAT verwenden."
            )
        if fs is None:
            con.print("[yellow]Hinweis: Paket > 4 GiB - auf FAT32-Laufwerken nicht speicherbar.")

    coll = plan.collection
    cid = coll.id
    vdir_rel = manifest_relpath(coll, series, plan.version).rsplit("/", 1)[0]
    vdir = volume.path_for(vdir_rel)
    vdir.mkdir(parents=True, exist_ok=True)
    for stale in vdir.glob(f"*{PARTIAL_SUFFIX}"):
        stale.unlink()

    sealed_at = utcnow()
    bundle_docs: list[dict] = []
    file_docs: list[dict] = []
    bundle_rows: list[tuple[dict, list]] = []
    with app.byte_progress() as prog:
        task = prog.add_task(f"{series} v{plan.version}", total=plan.total)
        for cls, chunks in plan.chunks.items():
            for idx, chunk in enumerate(chunks, start=1):
                name = bundle_name(cls, idx)
                rel = bundle_relpath(coll, series, plan.version, name)
                out = volume.path_for(rel)
                # Name im Tar: relativ zum Quellordner der Sammlung
                entries = [TarEntry(split_vpath(i.path)[1], app.cfg.source_file(i.path)) for i in chunk]
                prog.update(task, description=f"{series} {name}")
                res = write_tar(out, entries, lambda d: prog.advance(task, d))
                if verify:
                    prog.update(task, description=f"{series} {name} (prüfen)")
                    if sha256_file(out) != res.sha256:
                        raise SealError(f"Prüfsumme nach Schreiben falsch: {out}")
                bdoc = {"name": name, "class": cls, "relpath": rel, "size": res.size, "sha256": res.sha256, "files": len(res.files)}
                bundle_docs.append(bdoc)
                bundle_rows.append((bdoc, res.files))
                for pf in res.files:
                    file_docs.append(
                        {"path": pf.path, "class": cls, "size": pf.size, "mtime_ns": pf.mtime_ns, "sha256": pf.sha256, "bundle": name}
                    )

    archived = db.latest_archived(series)
    present = {r["path"] for r in db.q("SELECT path FROM files WHERE series=? AND (present=1 OR slimmed=1)", (series,))}
    removed = sorted(p for p in archived if p not in present)
    mbytes = _manifest(coll, series, plan.version, sealed_at, bundle_docs, file_docs, [split_vpath(p)[1] for p in removed])
    mrel = manifest_relpath(coll, series, plan.version)
    mpath = volume.path_for(mrel)
    tmp = mpath.with_name(mpath.name + PARTIAL_SUFFIX)
    tmp.write_bytes(mbytes)
    tmp.replace(mpath)
    msha = hashlib.sha256(mbytes).hexdigest()

    with db.tx() as c:
        c.execute(
            "INSERT INTO versions(series, version, sealed_at, manifest_relpath, manifest_sha256, nfiles, size) VALUES(?,?,?,?,?,?,?)",
            (series, plan.version, sealed_at, mrel, msha, len(file_docs), sum(f["size"] for f in file_docs)),
        )
        for bdoc, files in bundle_rows:
            cur = c.execute(
                "INSERT INTO bundles(series, version, name, cls, relpath, size, sha256, nfiles) VALUES(?,?,?,?,?,?,?,?)",
                (series, plan.version, bdoc["name"], bdoc["class"], bdoc["relpath"], bdoc["size"], bdoc["sha256"], bdoc["files"]),
            )
            bid = cur.lastrowid
            now = utcnow()
            c.execute(
                "INSERT INTO copies(bundle_id, volume_id, stored_at, verified_at, status) VALUES(?,?,?,?, 'ok')",
                (bid, volume.id, now, now if verify else None),
            )
            for pf in files:
                vpath = f"{cid}/{pf.path}"
                c.execute(
                    "INSERT INTO archived(path, version, series, cls, size, mtime_ns, sha256, bundle_id) VALUES(?,?,?,?,?,?,?,?)",
                    (vpath, plan.version, series, bdoc["class"], pf.size, pf.mtime_ns, pf.sha256, bid),
                )
                c.execute(
                    "UPDATE files SET sha256=? WHERE path=? AND size=? AND mtime_ns=?",
                    (pf.sha256, vpath, pf.size, pf.mtime_ns),
                )
        c.execute(
            "INSERT OR REPLACE INTO manifest_copies(series, version, volume_id, stored_at) VALUES(?,?,?,?)",
            (series, plan.version, volume.id, utcnow()),
        )
    con.print(f"[green]✓ {series} v{plan.version} versiegelt auf {volume.label}[/] ({len(bundle_docs)} Paket(e))")
    return plan


def export_index(app: App, volume: Volume) -> Path:
    """Legt eine Kopie des Index und eine Beschreibung (LIESMICH.txt) auf dem Volume ab."""
    dest = volume.path_for(f"{INDEX_DIR}/index.sqlite")
    app.db.backup_to(dest)
    try:
        readme = volume.path_for(README_NAME)
        tmp = readme.with_name(readme.name + PARTIAL_SUFFIX)
        tmp.write_text(readme_text(app), encoding="utf-8")
        tmp.replace(readme)
    except OSError as e:
        app.console.print(f"[yellow]{README_NAME} auf {volume.label} nicht geschrieben: {e}")
    return dest


def readme_text(app: App) -> str:
    """Beschreibung der Ablage - Wiederherstellung muss auch ohne das Tool möglich sein."""
    lines = [
        "foto-archiv - Archiv-Volume",
        "===========================",
        "",
        "Gleiche Struktur auf jeder Archivplatte und in S3 (dort unter dem konfigurierten Präfix).",
        "",
        "Sammlungen (Ordner im Archiv -> Quellordner):",
    ]
    known = {c.archive_dir for c in app.cfg.collections}
    for c in app.cfg.collections:
        layout = "<Serie>/v<N>/" if c.year == "none" else "<Jahr>/<Serie>/v<N>/ (ohne Jahr: _ohne-jahr/)"
        lines.append(f"  {c.archive_dir}/  ({c.id}, {c.profile})  {layout}  <- {c.source_root}")
    for r in app.db.q("SELECT DISTINCT relpath FROM bundles"):
        top = r["relpath"].split("/", 1)[0]
        if top not in known:
            known.add(top)
            lines.append(f"  {top}/  (früherer Ordnername, ältere Versionen)")
    lines += [
        "",
        "Je Version: manifest.json.gz (welche Datei in welchem Paket, SHA-256) und <Klasse>-<NNN>.tar.",
        "Pfade im Tar sind relativ zum Quellordner der Sammlung. Wiederherstellen ohne Tool:",
        "",
        "  tar -xf <Paket>.tar -C <Quellordner der Sammlung>",
        "  tar -xf raw-001.tar -C <Quellordner> \"2026.Kenia/raws/DSC01234.ARW\"   (einzelne Datei)",
        "",
        "_index/index.sqlite: Gesamtindex (SQLite) - welche Pakete auf welchen Volumes/in S3 liegen.",
        "_kataloge/: Sicherungen der digiKam-Datenbanken (gzip).",
        "volume.json: Kennung dieser Platte (nicht löschen).",
        "",
    ]
    return "\n".join(lines)
