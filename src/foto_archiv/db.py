"""Lokaler Index (SQLite).

Alle Pfade sind relativ zu ``source_root`` und mit ``/`` getrennt.
Ein *Volume* ist ein Speicherort für Pakete: eine HDD (``kind='hdd'``) oder S3 (``kind='s3'``,
id = ``s3:<bucket>/<prefix>``). ``copies`` hält fest, welches Paket auf welchem Volume liegt.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- lokaler Bestand (Ergebnis von scan)
CREATE TABLE IF NOT EXISTS files(
    path      TEXT PRIMARY KEY,
    series    TEXT NOT NULL,
    cls       TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    sha256    TEXT,                      -- NULL = (noch) unbekannt bzw. seit letztem Hash geändert
    present   INTEGER NOT NULL DEFAULT 1, -- liegt lokal vor
    slimmed   INTEGER NOT NULL DEFAULT 0, -- bewusst lokal entfernt (slim)
    last_seen TEXT
);
CREATE INDEX IF NOT EXISTS files_series ON files(series);

CREATE TABLE IF NOT EXISTS series(
    name       TEXT PRIMARY KEY,
    year       INTEGER,
    first_seen TEXT
);

-- Versiegelungen
CREATE TABLE IF NOT EXISTS versions(
    series          TEXT NOT NULL,
    version         INTEGER NOT NULL,
    sealed_at       TEXT NOT NULL,
    manifest_relpath TEXT NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    nfiles          INTEGER NOT NULL,
    size            INTEGER NOT NULL,
    PRIMARY KEY(series, version)
);

CREATE TABLE IF NOT EXISTS bundles(
    id      INTEGER PRIMARY KEY,
    series  TEXT NOT NULL,
    version INTEGER NOT NULL,
    name    TEXT NOT NULL,        -- raw-001.tar
    cls     TEXT NOT NULL,
    relpath TEXT NOT NULL UNIQUE, -- 2026/2026.Kenia/v1/raw-001.tar
    size    INTEGER NOT NULL,
    sha256  TEXT NOT NULL,
    nfiles  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS bundles_series ON bundles(series, version);

-- Dateien je Version (Inhalt der Manifeste)
CREATE TABLE IF NOT EXISTS archived(
    path      TEXT NOT NULL,
    version   INTEGER NOT NULL,
    series    TEXT NOT NULL,
    cls       TEXT NOT NULL,
    size      INTEGER NOT NULL,
    mtime_ns  INTEGER NOT NULL,
    sha256    TEXT NOT NULL,
    bundle_id INTEGER NOT NULL REFERENCES bundles(id),
    PRIMARY KEY(path, version)
);
CREATE INDEX IF NOT EXISTS archived_series ON archived(series);
CREATE INDEX IF NOT EXISTS archived_bundle ON archived(bundle_id);

CREATE TABLE IF NOT EXISTS volumes(
    id        TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,          -- hdd | s3
    label     TEXT NOT NULL,
    created   TEXT NOT NULL,
    last_root TEXT,                   -- zuletzt gesehener Pfad (hdd)
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS copies(
    bundle_id   INTEGER NOT NULL REFERENCES bundles(id),
    volume_id   TEXT NOT NULL REFERENCES volumes(id),
    stored_at   TEXT NOT NULL,
    verified_at TEXT,
    status      TEXT NOT NULL,         -- ok | bad | missing
    PRIMARY KEY(bundle_id, volume_id)
);

-- Manifeste je Volume (werden zusammen mit den Paketen abgelegt)
CREATE TABLE IF NOT EXISTS manifest_copies(
    series    TEXT NOT NULL,
    version   INTEGER NOT NULL,
    volume_id TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    PRIMARY KEY(series, version, volume_id)
);

-- laufende Multipart-Uploads (zum Fortsetzen)
CREATE TABLE IF NOT EXISTS uploads(
    key        TEXT PRIMARY KEY,
    upload_id  TEXT NOT NULL,
    bundle_id  INTEGER,
    part_size  INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    sha256     TEXT NOT NULL,
    started_at TEXT NOT NULL
);

-- angestoßene Wiederherstellungen aus Deep Archive
CREATE TABLE IF NOT EXISTS restores(
    key          TEXT PRIMARY KEY,
    tier         TEXT NOT NULL,
    days         INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    status       TEXT NOT NULL        -- requested | available | downloaded | expired
);

-- Altbestand im Bucket (nicht vom Tool erzeugt)
CREATE TABLE IF NOT EXISTS legacy(
    key           TEXT PRIMARY KEY,
    size          INTEGER NOT NULL,
    last_modified TEXT,
    storage_class TEXT,
    etag          TEXT,
    listed_at     TEXT NOT NULL,
    note          TEXT
);

-- gesicherte Katalog-Datenbanken
CREATE TABLE IF NOT EXISTS catalogs(
    name      TEXT NOT NULL,
    taken_at  TEXT NOT NULL,
    sha256    TEXT NOT NULL,
    size      INTEGER NOT NULL,
    volume_id TEXT NOT NULL,
    location  TEXT NOT NULL,
    PRIMARY KEY(name, taken_at, volume_id)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Index:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),)
        )

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def tx(self):
        """Transaktion; bei Ausnahme Rollback."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def q1(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def x(self, sql: str, params=()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def backup_to(self, dest: Path) -> None:
        """Konsistente Kopie des Index (z. B. auf ein Archiv-Volume)."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".foto-archiv-partial")
        if tmp.exists():
            tmp.unlink()
        target = sqlite3.connect(tmp)
        try:
            self.conn.backup(target)
        finally:
            target.close()
        tmp.replace(dest)

    # ------------------------------------------------------------------ volumes
    def register_volume(self, vol_id: str, kind: str, label: str, root: str | None = None) -> None:
        now = utcnow()
        self.x(
            """INSERT INTO volumes(id, kind, label, created, last_root, last_seen) VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET label=excluded.label,
                   last_root=COALESCE(excluded.last_root, volumes.last_root), last_seen=excluded.last_seen""",
            (vol_id, kind, label, now, root, now),
        )

    def set_copy(self, bundle_id: int, volume_id: str, status: str = "ok", verified: bool = False) -> None:
        now = utcnow()
        self.x(
            """INSERT INTO copies(bundle_id, volume_id, stored_at, verified_at, status) VALUES(?,?,?,?,?)
               ON CONFLICT(bundle_id, volume_id) DO UPDATE SET status=excluded.status,
                   verified_at=COALESCE(excluded.verified_at, copies.verified_at)""",
            (bundle_id, volume_id, now, now if verified else None, status),
        )

    def mark_verified(self, bundle_id: int, volume_id: str, ok: bool) -> None:
        self.x(
            "UPDATE copies SET verified_at=?, status=? WHERE bundle_id=? AND volume_id=?",
            (utcnow(), "ok" if ok else "bad", bundle_id, volume_id),
        )

    # ------------------------------------------------------------------ queries
    def latest_archived(self, series: str) -> dict[str, sqlite3.Row]:
        """Pro Pfad der jüngste archivierte Eintrag einer Serie."""
        rows = self.q(
            """SELECT a.* FROM archived a
               JOIN (SELECT path, MAX(version) v FROM archived WHERE series=? GROUP BY path) m
                 ON a.path=m.path AND a.version=m.v""",
            (series,),
        )
        return {r["path"]: r for r in rows}

    def bundles_of(self, series: str, version: int | None = None) -> list[sqlite3.Row]:
        if version is None:
            return self.q("SELECT * FROM bundles WHERE series=? ORDER BY version, name", (series,))
        return self.q("SELECT * FROM bundles WHERE series=? AND version=? ORDER BY name", (series, version))

    def ok_volumes_for_bundle(self, bundle_id: int) -> list[sqlite3.Row]:
        return self.q(
            """SELECT v.* FROM copies c JOIN volumes v ON v.id=c.volume_id
               WHERE c.bundle_id=? AND c.status='ok'""",
            (bundle_id,),
        )

    def next_version(self, series: str) -> int:
        r = self.q1("SELECT MAX(version) m FROM versions WHERE series=?", (series,))
        return (r["m"] or 0) + 1
