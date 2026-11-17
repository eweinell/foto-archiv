"""Umstellung auf Sammlungen.

* Index (Schema 1 -> 2): Pfade und Serien bekommen die Sammlung vorangestellt
  (``2026.Kenia/…`` -> ``fotos/2026.Kenia/…``), Ablagepfade den Ordner der Sammlung
  (``2026/2026.Kenia/v1/…`` -> ``fotos/2026/2026.Kenia/v1/…``).
* Konfiguration: ``[s3] prefix`` wird zur Archivwurzel, der bisherige Präfix-Ordner zum
  ``archive_dir`` der Sammlung (``prefix = "fotos/"`` -> ``prefix = ""`` + ``archive_dir = "fotos"``).
  Damit bleibt jeder S3-Key **unverändert** (``prefix + relpath`` ist vorher und nachher gleich) -
  in S3 wird nichts verschoben, kopiert oder gelöscht.
* HDD-Volumes (layout 1 -> 2): die Jahresordner wandern in den Ordner der Sammlung
  (``foto-archiv/2025`` -> ``foto-archiv/fotos/2025``) - Umbenennen auf derselben Platte.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import DEFAULT_COLLECTION, Collection, write_config
from .context import App
from .db import SCHEMA_VERSION, utcnow
from .layout import LEGACY_TOP_RE
from .seal import export_index
from .volumes import LAYOUT, Volume, VolumeError, set_layout


class MigrationError(Exception):
    pass


@dataclass
class IndexPlan:
    collection: Collection  # Sammlung, der der bisherige Bestand zugeordnet wird
    new_prefix: str
    s3_ids: dict[str, str]  # alte -> neue S3-Volume-ID
    write_config: bool
    counts: dict[str, int] = field(default_factory=dict)


def plan_index(app: App) -> IndexPlan:
    db, cfg = app.db, app.cfg
    if db.schema_version >= SCHEMA_VERSION:
        raise MigrationError("Der Index ist bereits auf Sammlungen umgestellt.")

    if cfg.collections_explicit:
        coll = cfg.collections[0]
        new_prefix = cfg.s3.prefix
        write = False
    else:
        old = cfg.s3.prefix
        if old:
            base, _, adir = old.rstrip("/").rpartition("/")
            new_prefix = f"{base}/" if base else ""
        else:
            adir, new_prefix = DEFAULT_COLLECTION, ""
        coll = Collection(id=DEFAULT_COLLECTION, source_root=cfg.collections[0].source_root, archive_dir=adir)
        write = True

    # S3: jeder bisherige Key muss gleich bleiben -> neuer Präfix + Sammlungsordner = alter Präfix
    s3_ids: dict[str, str] = {}
    used = {
        r["volume_id"]
        for t in ("copies", "manifest_copies", "catalogs")
        for r in db.q(f"SELECT DISTINCT volume_id FROM {t} WHERE volume_id LIKE 's3:%'")
    }
    for r in db.q("SELECT id FROM volumes WHERE kind='s3'"):
        used.add(r["id"])
    for vid in sorted(used):
        bucket, _, old_prefix = vid[3:].partition("/")
        has_data = db.q1(
            "SELECT 1 FROM copies WHERE volume_id=? UNION SELECT 1 FROM manifest_copies WHERE volume_id=?", (vid, vid)
        )
        if bucket != cfg.s3.bucket:
            if has_data:
                raise MigrationError(f"Index enthält Kopien in einem anderen Bucket ({vid}) - bitte [s3] bucket prüfen.")
            continue
        if has_data and new_prefix + coll.archive_dir + "/" != old_prefix:
            raise MigrationError(
                f"S3-Keys würden sich ändern: bisher '{old_prefix}<Jahr>/…', neu "
                f"'{new_prefix}{coll.archive_dir}/<Jahr>/…'. [s3] prefix + archive_dir der Sammlung "
                f"'{coll.id}' müssen zusammen '{old_prefix}' ergeben."
            )
        s3_ids[vid] = f"s3:{bucket}/{new_prefix}"

    counts = {t: db.q1(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("files", "versions", "bundles", "archived")}
    return IndexPlan(collection=coll, new_prefix=new_prefix, s3_ids=s3_ids, write_config=write, counts=counts)


def migrate_index(app: App, plan: IndexPlan) -> None:
    """Index (und ggf. Konfiguration) umstellen. Vorher wird eine Sicherung des Index angelegt."""
    db, cfg, con = app.db, app.cfg, app.console
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = cfg.index_path.with_name(f"index-schema1-{stamp}.sqlite")
    db.backup_to(backup)
    con.print(f"Sicherung des bisherigen Index: {backup}")

    coll = plan.collection
    p, d = coll.id + "/", coll.archive_dir + "/"
    with db.tx() as c:
        c.execute("UPDATE files SET path=?||path, series=?||series", (p, p))
        c.execute("UPDATE series SET name=?||name", (p,))
        c.execute("UPDATE versions SET series=?||series, manifest_relpath=?||manifest_relpath", (p, d))
        c.execute("UPDATE bundles SET series=?||series, relpath=?||relpath", (p, d))
        c.execute("UPDATE archived SET path=?||path, series=?||series", (p, p))
        c.execute("UPDATE manifest_copies SET series=?||series", (p,))
        for old, new in plan.s3_ids.items():
            if old == new:
                continue
            c.execute(
                """INSERT OR IGNORE INTO volumes(id, kind, label, created, last_root, last_seen)
                   SELECT ?, kind, label, created, last_root, last_seen FROM volumes WHERE id=?""",
                (new, old),
            )
            for t in ("copies", "manifest_copies", "catalogs"):
                c.execute(f"UPDATE {t} SET volume_id=? WHERE volume_id=?", (new, old))
            c.execute("DELETE FROM volumes WHERE id=?", (old,))
        db.meta_set("schema_version", str(SCHEMA_VERSION), c)
        db.meta_set("legacy_dir", coll.archive_dir, c)
        db.meta_set("legacy_collection", coll.id, c)
        db.meta_set("migrated_at", utcnow(), c)

        # Konfiguration im selben Schritt: schlägt das Schreiben fehl, wird der Index zurückgerollt
        if plan.write_config:
            cfg.collections = [coll]
            cfg.collections_explicit = True
            cfg.s3.prefix = plan.new_prefix
            if cfg.path is not None:
                if cfg.path.exists():
                    shutil.copy2(cfg.path, cfg.path.with_name(f"{cfg.path.name}.schema1-{stamp}.bak"))
                write_config(cfg, cfg.path)
                con.print(f"Konfiguration umgestellt: {cfg.path}")


@dataclass
class VolumeMigration:
    moved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # Pakete laut Index auf dem Volume, aber nicht gefunden


def legacy_dir(app: App) -> str:
    return app.db.meta_get("legacy_dir") or app.cfg.collections[0].archive_dir


def migrate_volume(
    app: App, vol: Volume, dry_run: bool = False, dirname: str | None = None
) -> tuple[Volume, VolumeMigration]:
    """Jahresordner eines Volumes im alten Format in den Ordner der Sammlung verschieben."""
    res = VolumeMigration()
    if not vol.outdated:
        return vol, res
    dest = vol.root / (dirname or legacy_dir(app))
    tops = sorted(e for e in vol.root.iterdir() if e.is_dir() and LEGACY_TOP_RE.match(e.name))
    if dry_run:
        res.moved = [f"{t.name} -> {dest.name}/{t.name}" for t in tops]
        return vol, res
    if app.db.schema_version < SCHEMA_VERSION:
        raise MigrationError("Zuerst den Index umstellen: 'foto-archiv migrate'.")

    dest.mkdir(exist_ok=True)
    for top in tops:
        target = dest / top.name
        if not target.exists():
            os.rename(top, target)
        else:  # z. B. nach einem abgebrochenen Lauf: Serienordner einzeln nachziehen
            for child in sorted(top.iterdir()):
                t2 = target / child.name
                if t2.exists():
                    raise VolumeError(f"{t2} existiert bereits - {child} bitte von Hand prüfen.")
                os.rename(child, t2)
            top.rmdir()
        res.moved.append(f"{top.name} -> {dest.name}/{top.name}")

    rows = app.db.q(
        """SELECT b.relpath FROM copies c JOIN bundles b ON b.id=c.bundle_id
           WHERE c.volume_id=? AND c.status='ok'""",
        (vol.id,),
    )
    res.missing = [r["relpath"] for r in rows if not vol.path_for(r["relpath"]).is_file()]
    vol = set_layout(vol, LAYOUT)
    export_index(app, vol)
    return vol, res
