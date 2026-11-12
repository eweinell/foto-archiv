"""Schutzstatus der Pakete und Regeln für das lokale Ausdünnen (slim)."""

from __future__ import annotations

from dataclasses import dataclass

from .context import App


@dataclass
class Protection:
    hdd_ok: set[str]  # Volume-IDs (hdd) mit Status ok
    hdd_verified: set[str]  # davon mit verified_at gesetzt
    s3_ok: bool
    s3_verified: bool


def bundle_protection(app: App, series: str | None = None) -> dict[int, Protection]:
    s3_id = app.cfg.s3.volume_id if app.cfg.s3.bucket else None
    sql = """SELECT b.id, c.volume_id, c.status, c.verified_at, v.kind
             FROM bundles b LEFT JOIN copies c ON c.bundle_id=b.id LEFT JOIN volumes v ON v.id=c.volume_id"""
    params: tuple = ()
    if series:
        sql += " WHERE b.series=?"
        params = (series,)
    out: dict[int, Protection] = {}
    for r in app.db.q(sql, params):
        p = out.setdefault(r["id"], Protection(set(), set(), False, False))
        if r["volume_id"] is None or r["status"] != "ok":
            continue
        if r["kind"] == "hdd":
            p.hdd_ok.add(r["volume_id"])
            if r["verified_at"]:
                p.hdd_verified.add(r["volume_id"])
        elif r["kind"] == "s3" and r["volume_id"] == s3_id:
            p.s3_ok = True
            p.s3_verified = p.s3_verified or bool(r["verified_at"])
    return out


def is_protected_for_slim(app: App, p: Protection) -> bool:
    cfg = app.cfg.slim
    if len(p.hdd_verified) < cfg.min_hdd_copies:
        return False
    if cfg.require_s3 and not p.s3_ok:
        return False
    return True


@dataclass
class SlimCandidate:
    path: str
    series: str
    cls: str
    size: int
    sha256: str
    bundle_id: int


def slim_candidates(app: App, series: list[str] | None = None, classes: list[str] | None = None) -> list[SlimCandidate]:
    """Lokal vorhandene Dateien, deren aktueller Stand (gleicher SHA-256) archiviert und
    gemäß [slim]-Regeln ausreichend geschützt ist."""
    classes = classes or app.cfg.slim.classes
    db = app.db
    sql = f"""SELECT f.path, f.series, f.cls, f.size, f.sha256, a.bundle_id
              FROM files f
              JOIN archived a ON a.path=f.path AND a.sha256=f.sha256
              WHERE f.present=1 AND f.sha256 IS NOT NULL AND f.cls IN ({','.join('?' * len(classes))})"""
    params: list = list(classes)
    if series:
        sql += f" AND f.series IN ({','.join('?' * len(series))})"
        params += list(series)
    rows = db.q(sql, params)
    prot_cache: dict[str, dict[int, Protection]] = {}
    out: list[SlimCandidate] = []
    seen: set[str] = set()
    for r in rows:
        if r["path"] in seen:
            continue
        prot = prot_cache.setdefault(r["series"], bundle_protection(app, r["series"]))
        p = prot.get(r["bundle_id"])
        if p and is_protected_for_slim(app, p):
            seen.add(r["path"])
            out.append(SlimCandidate(r["path"], r["series"], r["cls"], r["size"], r["sha256"], r["bundle_id"]))
    return out
