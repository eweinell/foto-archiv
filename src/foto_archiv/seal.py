"""Versiegeln: neue/geänderte Dateien einer Serie als Version vN auf ein HDD-Volume packen."""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .classify import CLASSES
from .context import App, human
from .db import utcnow
from .layout import INDEX_DIR, bundle_name, bundle_relpath, manifest_relpath, series_year
from .tario import PARTIAL_SUFFIX, TarEntry, sha256_file, write_tar
from .volumes import Volume

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
    chunks: dict[str, list[list[PlanItem]]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(i.size for i in self.items)

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
    archived = db.latest_archived(series)
    rows = db.q("SELECT * FROM files WHERE series=? AND present=1 ORDER BY path", (series,))
    if not rows and not archived:
        raise SealError(f"Serie '{series}' ist unbekannt (erst 'scan' ausführen).")
    root = app.cfg.source_root
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
                    sha = sha256_file(root.joinpath(*r["path"].split("/")))
                    db.x("UPDATE files SET sha256=? WHERE path=? AND mtime_ns=?", (sha, r["path"], r["mtime_ns"]))
            if sha == a["sha256"]:
                continue
        items.append(PlanItem(r["path"], r["cls"], r["size"], r["mtime_ns"]))
    if not items:
        return None
    plan = SealPlan(series=series, version=db.next_version(series), items=items)
    limit = app.cfg.chunk_size_bytes
    for cls in CLASSES:
        cls_items = [i for i in items if i.cls == cls]
        if cls_items:
            plan.chunks[cls] = _chunk(cls_items, limit)
    return plan


def _manifest(series: str, version: int, sealed_at: str, bundles: list[dict], files: list[dict], removed: list[str]) -> bytes:
    doc = {
        "format": "foto-archiv/manifest@1",
        "series": series,
        "year": series_year(series),
        "version": version,
        "sealed_at": sealed_at,
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
    if any(len(c) == 1 and c[0].size > 4 * 1024**3 - 64 * 1024 for chunks in plan.chunks.values() for c in chunks):
        con.print("[yellow]Hinweis: Einzeldatei > 4 GiB - Paket ist nicht FAT32-tauglich.")

    root = app.cfg.source_root
    vdir_rel = manifest_relpath(series, plan.version).rsplit("/", 1)[0]
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
                rel = bundle_relpath(series, plan.version, name)
                out = volume.path_for(rel)
                entries = [TarEntry(i.path, root.joinpath(*i.path.split("/"))) for i in chunk]
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
    mbytes = _manifest(series, plan.version, sealed_at, bundle_docs, file_docs, removed)
    mrel = manifest_relpath(series, plan.version)
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
                c.execute(
                    "INSERT INTO archived(path, version, series, cls, size, mtime_ns, sha256, bundle_id) VALUES(?,?,?,?,?,?,?,?)",
                    (pf.path, plan.version, series, bdoc["class"], pf.size, pf.mtime_ns, pf.sha256, bid),
                )
                c.execute(
                    "UPDATE files SET sha256=? WHERE path=? AND size=? AND mtime_ns=?",
                    (pf.sha256, pf.path, pf.size, pf.mtime_ns),
                )
        c.execute(
            "INSERT OR REPLACE INTO manifest_copies(series, version, volume_id, stored_at) VALUES(?,?,?,?)",
            (series, plan.version, volume.id, utcnow()),
        )
    con.print(f"[green]✓ {series} v{plan.version} versiegelt auf {volume.label}[/] ({len(bundle_docs)} Paket(e))")
    return plan


def export_index(app: App, volume: Volume) -> Path:
    """Legt eine Kopie des Index auf dem Volume ab (macht es selbstbeschreibend)."""
    dest = volume.path_for(f"{INDEX_DIR}/index.sqlite")
    app.db.backup_to(dest)
    return dest
