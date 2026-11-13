"""S3-Operationen (Upload nach Deep Archive, Prüfung, Wiederherstellung, Altbestand, Kataloge).

Grundsätze:

* In S3 wird nie etwas überschrieben oder gelöscht. Alle Schreibzugriffe sind bedingt
  (``If-None-Match: *``); existiert ein Objekt bereits, wird es nur über Größe und den in den
  Metadaten gespeicherten SHA-256 als identisch anerkannt, sonst als Konflikt gemeldet.
* Große Pakete werden per eigenem Multipart-Upload hochgeladen. Die UploadId steht in der
  Tabelle ``uploads``; ein abgebrochener Lauf (Strg+C, Netzwerk, Absturz) wird beim nächsten
  ``push-s3`` fortgesetzt, bereits übertragene Teile werden anhand Größe und Prüfsumme erkannt.
* Die Datei wird genau einmal sequenziell gelesen (schonend für HDDs); dabei werden der
  SHA-256 der ganzen Datei und je Teil ein SHA-256 berechnet, den S3 beim Empfang prüft.
  Abgeschlossen wird der Upload nur, wenn der Gesamt-Hash dem Index entspricht.
"""

from __future__ import annotations

import gzip
import hashlib
import math
import os
import re
import shutil
import sqlite3
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import typer
from botocore.exceptions import BotoCoreError, ClientError
from rich.table import Table

from . import s3client
from .context import App, human
from .ratelimit import RateError, RateLimiter, format_rate, parse_rate, throttled
from .db import utcnow
from .layout import CATALOG_DIR, INDEX_DIR, series_year
from .tario import BUF, PARTIAL_SUFFIX, extract_bundle, sha256_file
from .volumes import Volume, find_connected

MiB = 1024 * 1024
GB = 1024**3  # AWS rechnet in GiB ("GB")

# ---------------------------------------------------------------------------- Preise (eu-west-1, USD)
PRICE_STORAGE_GB_MONTH = {
    "DEEP_ARCHIVE": 0.00099,
    "GLACIER": 0.0036,
    "GLACIER_IR": 0.004,
    "STANDARD_IA": 0.0125,
    "ONEZONE_IA": 0.01,
    "INTELLIGENT_TIERING": 0.023,
    "STANDARD": 0.023,
}
PRICE_PUT_PER_1000 = 0.05  # Deep Archive: PUT/POST/LIST je 1.000 Anfragen
PRICE_RETRIEVAL_GB = {"Bulk": 0.003, "Standard": 0.02}
RESTORE_DURATION = {"Bulk": "bis zu 48 Stunden", "Standard": "bis zu 12 Stunden"}
PRICE_EGRESS_GB = 0.09
EGRESS_FREE_GB = 100
PRICE_RESTORED_COPY_GB_MONTH = 0.023
MIN_STORAGE_MONTHS_DEEP_ARCHIVE = 6  # 180 Tage Mindestlaufzeit

# Wiederholungen einzelner Teile (zusätzlich zu den botocore-Retries)
PART_ATTEMPTS = 4
RETRY_DELAY = 2.0  # Sekunden, verdoppelt sich je Versuch (Tests setzen 0)

STAGING_SUFFIX = ".foto-archiv-restore"
TOOL_KEY_RE = re.compile(r"^[^/]+/[^/]+/v\d+/")
SQLITE_MAGIC = b"SQLite format 3\x00"


class UploadError(Exception):
    pass


class UploadConflict(UploadError):
    """Unter dem Key liegt bereits ein anderes Objekt - es wird nicht überschrieben."""


# ============================================================================ Grundlagen


def _fail(app: App, msg: str) -> None:
    app.console.print(f"[red]Fehler:[/] {msg}")
    raise typer.Exit(1)


def _require_bucket(app: App) -> None:
    if not app.cfg.s3.bucket:
        _fail(app, "Kein S3-Bucket konfiguriert (in der config.toml unter [s3] 'bucket' eintragen).")


@dataclass
class S3:
    app: App
    client: Any
    bucket: str
    prefix: str
    volume_id: str
    limiter: RateLimiter | None = None  # gemeinsame Upload-Bremse aller Threads

    def key(self, relpath: str) -> str:
        return self.prefix + relpath


def upload_rate(app: App) -> int:
    """Konfigurierte Upload-Bandbreite in Bytes/s (0 = unbegrenzt)."""
    try:
        return parse_rate(app.cfg.s3.upload_limit)
    except RateError as e:
        _fail(app, f"upload_limit: {e}")
    return 0


def _connect(app: App) -> S3:
    """S3-Client erzeugen, Bucket erreichbar? S3 als Volume registrieren."""
    _require_bucket(app)
    s3c = app.cfg.s3
    rate = upload_rate(app)
    try:
        client = s3client.make_client(s3c)
    except s3client.S3Error as e:
        _fail(app, str(e))
    try:
        client.head_bucket(Bucket=s3c.bucket)
    except (ClientError, BotoCoreError) as e:
        _fail(app, f"Bucket '{s3c.bucket}' nicht erreichbar: {s3client.describe(e)}")
    app.db.register_volume(s3c.volume_id, "s3", f"S3 {s3c.bucket}")
    limiter = RateLimiter(rate) if rate else None
    return S3(app=app, client=client, bucket=s3c.bucket, prefix=s3c.prefix, volume_id=s3c.volume_id, limiter=limiter)


def _part_size(cfg_mb: int, size: int) -> int:
    """Teilgröße: mindestens die konfigurierte, aber so groß, dass höchstens ~9.000 Teile entstehen."""
    base = max(int(cfg_mb), 5) * MiB
    need = math.ceil(size / s3client.TARGET_MAX_PARTS) if size else 0
    ps = max(base, need)
    ps = math.ceil(ps / MiB) * MiB
    return min(ps, s3client.MAX_PART_SIZE)


def _wait_some(futures, return_when=FIRST_COMPLETED) -> set[Future]:
    """Wie ``wait``, aber in kurzen Intervallen - so bleibt Strg+C auch unter Windows wirksam."""
    while True:
        done, _ = wait(list(futures), timeout=0.5, return_when=return_when)
        if done:
            return done


def _gb(n: int | float) -> float:
    return n / GB


def _usd(x: float) -> str:
    return f"{x:.2f} $" if x >= 0.01 or x == 0 else f"{x:.4f} $"


def _bundle_meta(b) -> dict[str, str]:
    return {
        "sha256": b["sha256"],
        "series": s3client.meta_value(b["series"]),
        "version": str(b["version"]),
        "class": b["cls"],
        "tool": "foto-archiv",
    }


# ============================================================================ Upload-Kern


@dataclass
class UploadSpec:
    path: Path
    key: str
    size: int
    sha256: str  # erwarteter SHA-256 (hex) der Datei
    storage_class: str
    metadata: dict[str, str]
    bundle_id: int | None = None


Advance = Callable[[int], None]


def _conflict_text(h: dict, spec: UploadSpec) -> str:
    remote_sha = s3client.metadata_sha256(h) or "unbekannt"
    return (
        f"{spec.key} existiert bereits mit anderem Inhalt "
        f"(S3: {human(h.get('ContentLength'))}, sha256 {remote_sha[:12]}; "
        f"erwartet: {human(spec.size)}, sha256 {spec.sha256[:12]}) - wird nicht überschrieben."
    )


def upload(s3: S3, spec: UploadSpec, advance: Advance) -> str:
    """Lädt eine Datei bedingt hoch. Ergebnis: 'uploaded' oder 'exists' (identisch vorhanden)."""
    db = s3.app.db
    h = s3client.head(s3.client, s3.bucket, spec.key)
    pending = db.q1("SELECT upload_id FROM uploads WHERE key=?", (spec.key,))
    if h is not None:
        if s3client.matches(h, spec.size, spec.sha256):
            if pending:  # liegengebliebener Upload zu einem inzwischen vollständigen Objekt
                _abort_quietly(s3, spec.key, pending["upload_id"])
                db.x("DELETE FROM uploads WHERE key=?", (spec.key,))
            advance(spec.size)
            return "exists"
        raise UploadConflict(_conflict_text(h, spec))
    if not pending and spec.size <= _part_size(s3.app.cfg.s3.part_size_mb, spec.size):
        data = spec.path.read_bytes()
        if len(data) != spec.size or hashlib.sha256(data).hexdigest() != spec.sha256:
            raise UploadError(f"{spec.path}: Inhalt weicht vom Index ab (Größe/SHA-256) - nicht hochgeladen.")
        res = put_small(s3, spec.key, data, spec.storage_class, spec.metadata, spec.sha256)
        advance(spec.size)
    else:
        res = _upload_multipart(s3, spec, advance)
    if res == "uploaded":
        h = s3client.head(s3.client, s3.bucket, spec.key)
        if not s3client.matches(h, spec.size, spec.sha256):
            raise UploadError(f"{spec.key}: nach dem Hochladen stimmen Größe/Metadaten nicht.")
    return res


def put_small(s3: S3, key: str, data: bytes, storage_class: str, metadata: dict[str, str], sha256: str) -> str:
    """Einzelner PUT mit SHA-256-Prüfsumme und ``If-None-Match: *`` (nie überschreiben)."""
    try:
        s3.client.put_object(
            Bucket=s3.bucket,
            Key=key,
            Body=throttled(data, s3.limiter),
            StorageClass=storage_class,
            Metadata=metadata,
            ChecksumAlgorithm="SHA256",
            ChecksumSHA256=s3client.hex_to_b64(sha256),
            IfNoneMatch="*",
        )
    except ClientError as e:
        if s3client.is_precondition_failed(e):
            h = s3client.head(s3.client, s3.bucket, key)
            if s3client.matches(h, len(data), sha256):
                return "exists"
            raise UploadConflict(f"{key} existiert bereits mit anderem Inhalt - wird nicht überschrieben.") from None
        raise
    return "uploaded"


def _list_parts(s3: S3, key: str, upload_id: str) -> dict[int, dict]:
    parts: dict[int, dict] = {}
    marker = 0
    while True:
        resp = s3.client.list_parts(Bucket=s3.bucket, Key=key, UploadId=upload_id, PartNumberMarker=marker)
        for p in resp.get("Parts", []) or []:
            parts[int(p["PartNumber"])] = p
        if not resp.get("IsTruncated"):
            return parts
        nxt = int(resp.get("NextPartNumberMarker") or 0)
        if nxt <= marker:
            return parts
        marker = nxt


def _abort_quietly(s3: S3, key: str, upload_id: str) -> None:
    try:
        s3.client.abort_multipart_upload(Bucket=s3.bucket, Key=key, UploadId=upload_id)
    except (ClientError, BotoCoreError):
        pass


def _part_reusable(prev: dict | None, data: bytes, checksum: str) -> bool:
    """Ist ein bereits hochgeladener Teil identisch? Bevorzugt über ChecksumSHA256, sonst ETag (=MD5 bei SSE-S3)."""
    if not prev or int(prev.get("Size", -1)) != len(data):
        return False
    remote = prev.get("ChecksumSHA256")
    if remote:
        return remote == checksum
    etag = str(prev.get("ETag", "")).strip('"')
    return bool(etag) and etag == hashlib.md5(data).hexdigest()


def _upload_part(s3: S3, key: str, upload_id: str, number: int, data: bytes, checksum: str) -> str:
    for attempt in range(PART_ATTEMPTS):
        try:
            r = s3.client.upload_part(
                Bucket=s3.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumber=number,
                Body=throttled(data, s3.limiter),  # bei Wiederholung neuer Body ab Position 0
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=checksum,
            )
            return r["ETag"]
        except (ClientError, BotoCoreError, OSError) as e:
            if s3client.is_fatal(e) or attempt == PART_ATTEMPTS - 1:
                raise
        time.sleep(RETRY_DELAY * 2**attempt)
    raise AssertionError("unerreichbar")


def _upload_multipart(s3: S3, spec: UploadSpec, advance: Advance, _retry_fresh: bool = False) -> str:
    app, db, client = s3.app, s3.app.db, s3.client
    con = app.console
    part_size = _part_size(app.cfg.s3.part_size_mb, spec.size)
    upload_id: str | None = None
    remote_parts: dict[int, dict] = {}

    row = db.q1("SELECT * FROM uploads WHERE key=?", (spec.key,))
    if row is not None:
        if row["size"] == spec.size and row["sha256"] == spec.sha256:
            try:
                remote_parts = _list_parts(s3, spec.key, row["upload_id"])
                upload_id, part_size = row["upload_id"], row["part_size"]
            except ClientError as e:
                if s3client.error_code(e) != "NoSuchUpload" and not s3client.is_not_found(e):
                    raise
                con.print(f"   [yellow]Früherer Upload von {spec.key} existiert nicht mehr - beginne neu.")
        else:
            _abort_quietly(s3, spec.key, row["upload_id"])
        if upload_id is None:
            db.x("DELETE FROM uploads WHERE key=?", (spec.key,))

    nparts = max(1, math.ceil(spec.size / part_size))
    if nparts > s3client.MAX_PARTS:
        raise UploadError(f"{spec.key}: zu viele Teile ({nparts}) - part_size_mb erhöhen.")

    if upload_id is None:
        meta = dict(spec.metadata)
        meta["part-size"] = str(part_size)
        resp = client.create_multipart_upload(
            Bucket=s3.bucket,
            Key=spec.key,
            StorageClass=spec.storage_class,
            ChecksumAlgorithm="SHA256",
            Metadata=meta,
        )
        upload_id = resp["UploadId"]
        db.x(
            "INSERT OR REPLACE INTO uploads(key, upload_id, bundle_id, part_size, size, sha256, started_at) VALUES(?,?,?,?,?,?,?)",
            (spec.key, upload_id, spec.bundle_id, part_size, spec.size, spec.sha256, utcnow()),
        )
    elif remote_parts:
        con.print(f"   Setze Upload fort: {len(remote_parts)} von {nparts} Teilen bereits in S3.")

    full = hashlib.sha256()
    checksums: dict[int, str] = {}
    etags: dict[int, str] = {}
    inflight: dict[Future, tuple[int, int]] = {}
    concurrency = max(1, int(app.cfg.s3.concurrency))
    pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="s3-teil")

    def collect(done: set[Future]) -> None:
        for f in done:
            number, length = inflight.pop(f)
            etags[number] = f.result()  # wirft den Fehler des Teils weiter
            advance(length)

    try:
        with open(spec.path, "rb") as fh:
            for number in range(1, nparts + 1):
                expected_len = min(part_size, spec.size - (number - 1) * part_size)
                data = fh.read(part_size)
                if len(data) != expected_len:
                    raise UploadError(f"{spec.path}: Datei ist kürzer als im Index ({human(spec.size)}).")
                full.update(data)
                cs = s3client.b64_sha256(data)
                checksums[number] = cs
                prev = remote_parts.get(number)
                if _part_reusable(prev, data, cs):
                    etags[number] = prev["ETag"]
                    advance(len(data))
                    continue
                while len(inflight) >= concurrency:
                    collect(_wait_some(inflight))
                fut = pool.submit(_upload_part, s3, spec.key, upload_id, number, data, cs)
                inflight[fut] = (number, len(data))
                del data
            if fh.read(1):
                raise UploadError(f"{spec.path}: Datei ist größer als im Index ({human(spec.size)}).")
        while inflight:
            collect(_wait_some(inflight))
    except BaseException as e:
        # Upload bleibt in S3 bestehen (fortsetzbar); noch nicht begonnene Teile verwerfen
        for f in inflight:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        if isinstance(e, ClientError) and s3client.error_code(e) == "NoSuchUpload":
            # Upload wurde anderweitig abgeschlossen oder abgebrochen
            db.x("DELETE FROM uploads WHERE key=?", (spec.key,))
            h = s3client.head(client, s3.bucket, spec.key)
            if s3client.matches(h, spec.size, spec.sha256):
                con.print(f"   [yellow]{spec.key} wurde inzwischen vollständig hochgeladen (anderer Lauf).")
                return "exists"
            if h is not None:
                raise UploadConflict(_conflict_text(h, spec)) from None
            if not _retry_fresh:
                con.print(f"   [yellow]Upload von {spec.key} existiert nicht mehr - beginne neu.")
                return _upload_multipart(s3, spec, advance, _retry_fresh=True)
        raise
    pool.shutdown(wait=True)

    if full.hexdigest() != spec.sha256:
        _abort_quietly(s3, spec.key, upload_id)
        db.x("DELETE FROM uploads WHERE key=?", (spec.key,))
        raise UploadError(f"{spec.path}: SHA-256 weicht vom Index ab - Upload verworfen (Paket auf der HDD prüfen).")

    parts = [{"PartNumber": n, "ETag": etags[n], "ChecksumSHA256": checksums[n]} for n in range(1, nparts + 1)]
    try:
        client.complete_multipart_upload(
            Bucket=s3.bucket,
            Key=spec.key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
            IfNoneMatch="*",
        )
    except ClientError as e:
        if s3client.is_precondition_failed(e) or s3client.error_code(e) == "NoSuchUpload":
            # Objekt existiert bereits (z. B. paralleler Lauf oder Wiederholung nach Zeitüberschreitung)
            h = s3client.head(client, s3.bucket, spec.key)
            if s3client.error_code(e) != "NoSuchUpload":
                _abort_quietly(s3, spec.key, upload_id)
            db.x("DELETE FROM uploads WHERE key=?", (spec.key,))
            if s3client.matches(h, spec.size, spec.sha256):
                return "exists"
            if h is not None:
                raise UploadConflict(_conflict_text(h, spec)) from None
        raise
    db.x("DELETE FROM uploads WHERE key=?", (spec.key,))
    return "uploaded"


# ============================================================================ push-s3


def _series_filter(series: list[str] | None, col: str = "series") -> tuple[str, list]:
    if not series:
        return "", []
    return f" AND {col} IN ({','.join('?' * len(series))})", list(series)


def _source_volumes(app: App, source: Volume | None) -> list[Volume]:
    vols = find_connected(app.cfg, app.db)
    if source is None:
        return vols
    return [source] + [v for v in vols if v.id != source.id]


def _find_source(app: App, b, volumes: list[Volume]) -> Volume | None:
    for v in volumes:
        if not app.db.q1("SELECT 1 FROM copies WHERE bundle_id=? AND volume_id=? AND status='ok'", (b["id"], v.id)):
            continue
        p = v.path_for(b["relpath"])
        try:
            if p.stat().st_size == b["size"]:
                return v
            app.console.print(f"[yellow]{p}: Größe weicht vom Index ab - Kopie auf {v.label} wird nicht verwendet.")
        except OSError:
            continue
    return None


def _push_estimate(app: App, sizes: list[int]) -> tuple[float, float]:
    total = sum(sizes)
    price = PRICE_STORAGE_GB_MONTH.get(app.cfg.s3.storage_class, PRICE_STORAGE_GB_MONTH["DEEP_ARCHIVE"])
    monthly = _gb(total) * price
    requests = sum(math.ceil(s / _part_size(app.cfg.s3.part_size_mb, s)) + 2 for s in sizes)
    return monthly, requests / 1000 * PRICE_PUT_PER_1000


def push_s3(app: App, series: list[str] | None, source: Volume | None, dry_run: bool) -> None:
    """Fehlende Pakete (und Manifeste) von einer angeschlossenen HDD nach S3 hochladen."""
    _require_bucket(app)
    db, con, cfg = app.db, app.console, app.cfg
    vol_id = cfg.s3.volume_id
    db.register_volume(vol_id, "s3", f"S3 {cfg.s3.bucket}")

    where, params = _series_filter(series, "b.series")
    bundles = db.q(
        f"""SELECT b.* FROM bundles b
            WHERE NOT EXISTS (SELECT 1 FROM copies c WHERE c.bundle_id=b.id AND c.volume_id=? AND c.status='ok'){where}
            ORDER BY b.series, b.version, b.name""",
        [vol_id, *params],
    )
    vwhere, vparams = _series_filter(series)
    missing_manifests = db.q(
        f"""SELECT * FROM versions WHERE NOT EXISTS
              (SELECT 1 FROM manifest_copies m WHERE m.series=versions.series AND m.version=versions.version AND m.volume_id=?)
            {vwhere} ORDER BY series, version""",
        [vol_id, *vparams],
    )
    if not bundles and not missing_manifests:
        con.print("[green]Alles bereits in S3.[/]")
        return

    volumes = _source_volumes(app, source)
    plan = [(b, _find_source(app, b, volumes)) for b in bundles]
    todo = [(b, v) for b, v in plan if v is not None]
    no_source = [b for b, v in plan if v is None]

    if dry_run:
        t = Table("Serie", "Ver.", "Paket", "Größe", "Quelle", header_style="bold")
        for b, v in plan:
            t.add_row(b["series"], str(b["version"]), b["name"], human(b["size"]), v.label if v else "[red]keine HDD angeschlossen")
        con.print(t)
        sizes = [b["size"] for b, _ in todo]
        monthly, put_cost = _push_estimate(app, sizes)
        con.print(
            f"Würde hochladen: {len(todo)} Paket(e), {human(sum(sizes))} ({cfg.s3.storage_class}) "
            f"+ {len(missing_manifests)} Manifest(e) (STANDARD)."
        )
        con.print(
            f"Geschätzte Kosten: Speicher {_usd(monthly)}/Monat ({_usd(monthly * 12)}/Jahr, "
            f"mindestens {MIN_STORAGE_MONTHS_DEEP_ARCHIVE} Monate), Anfragen einmalig {_usd(put_cost)}."
        )
        rate = upload_rate(app)
        if rate and sizes:
            hours = sum(sizes) / rate / 3600
            con.print(f"Upload-Limit {format_rate(rate)}: Dauer mindestens ≈ {hours:.1f} h.")
        if no_source:
            con.print(f"[yellow]{len(no_source)} Paket(e) ohne angeschlossene HDD-Kopie werden übersprungen.")
        return

    s3 = _connect(app)
    if s3.limiter:
        con.print(f"Upload-Limit: {format_rate(s3.limiter.rate)}")
    uploaded = existed = errors = 0
    uploaded_bytes = 0
    changed = False
    for b in no_source:
        con.print(f"[yellow]Übersprungen (keine HDD mit dem Paket angeschlossen):[/] {b['relpath']}")

    try:
        with app.byte_progress() as prog:
            task = prog.add_task("S3-Upload", total=sum(b["size"] for b, _ in todo) or 1)
            for b, vol in todo:
                prog.update(task, description=f"{b['series']} v{b['version']} {b['name']}")
                start = prog.tasks[task].completed
                spec = UploadSpec(
                    path=vol.path_for(b["relpath"]),
                    key=s3.key(b["relpath"]),
                    size=b["size"],
                    sha256=b["sha256"],
                    storage_class=cfg.s3.storage_class,
                    metadata=_bundle_meta(b),
                    bundle_id=b["id"],
                )
                try:
                    res = upload(s3, spec, lambda n: prog.advance(task, n))
                except Exception as e:  # ein Paket scheitert -> melden, mit dem nächsten weitermachen
                    errors += 1
                    prog.update(task, completed=start + b["size"])
                    msg = str(e) if isinstance(e, UploadError) else s3client.describe(e)
                    con.print(f"[red]✗ {b['relpath']}:[/] {msg}")
                    continue
                db.set_copy(b["id"], s3.volume_id, "ok", verified=True)
                changed = True
                if res == "uploaded":
                    uploaded += 1
                    uploaded_bytes += b["size"]
                    con.print(f"[green]✓[/] {b['relpath']} ({human(b['size'])}, von {vol.label})")
                else:
                    existed += 1
                    con.print(f"[green]✓[/] {b['relpath']} war bereits in S3 - im Index nachgetragen")
    except KeyboardInterrupt:
        con.print(
            "\n[yellow]Abgebrochen.[/] Bereits übertragene Teile bleiben in S3 erhalten; "
            "'foto-archiv push-s3' setzt den Upload fort."
        )
        raise typer.Exit(130)

    m_up, m_err = _push_manifests(s3, series, volumes)
    errors += m_err
    changed = changed or m_up > 0

    if changed:
        try:
            key = upload_index_snapshot(s3)
            con.print(f"Index-Sicherung: s3://{s3.bucket}/{key}")
        except (UploadError, ClientError, BotoCoreError, OSError) as e:
            errors += 1
            con.print(f"[red]Index-Sicherung fehlgeschlagen:[/] {e}")

    con.print(
        f"Hochgeladen: {uploaded} Paket(e), {human(uploaded_bytes)}; bereits vorhanden: {existed}; "
        f"Manifeste: {m_up}; ohne Quelle: {len(no_source)}; Fehler: {errors}"
    )
    _hint_orphan_uploads(s3)
    if errors:
        raise typer.Exit(1)


def _hint_orphan_uploads(s3: S3) -> None:
    """Unvollständige Multipart-Uploads unter dem Präfix, die der Index nicht kennt (kosten Speicher)."""
    known = {r["upload_id"] for r in s3.app.db.q("SELECT upload_id FROM uploads")}
    orphans: list[str] = []
    try:
        for page in s3.client.get_paginator("list_multipart_uploads").paginate(Bucket=s3.bucket, Prefix=s3.prefix):
            orphans += [u["Key"] for u in page.get("Uploads", []) or [] if u["UploadId"] not in known]
    except (ClientError, BotoCoreError):
        return
    if orphans:
        s3.app.console.print(
            f"[yellow]Hinweis:[/] {len(orphans)} unvollständige Multipart-Uploads ohne Index-Eintrag "
            f"(z. B. {orphans[0]}). Ihre Teile verursachen Speicherkosten; abbrechen z. B. mit "
            "'aws s3api abort-multipart-upload' oder per Lifecycle-Regel."
        )


def _push_manifests(s3: S3, series: list[str] | None, volumes: list[Volume]) -> tuple[int, int]:
    """Manifeste (STANDARD) der Versionen hochladen, deren Pakete vollständig in S3 liegen."""
    db, con = s3.app.db, s3.app.console
    vwhere, vparams = _series_filter(series)
    versions = db.q(
        f"""SELECT * FROM versions WHERE NOT EXISTS
              (SELECT 1 FROM manifest_copies m WHERE m.series=versions.series AND m.version=versions.version AND m.volume_id=?)
            {vwhere} ORDER BY series, version""",
        [s3.volume_id, *vparams],
    )
    done = errors = 0
    for v in versions:
        incomplete = db.q1(
            """SELECT 1 FROM bundles b WHERE b.series=? AND b.version=? AND NOT EXISTS
                 (SELECT 1 FROM copies c WHERE c.bundle_id=b.id AND c.volume_id=? AND c.status='ok')""",
            (v["series"], v["version"], s3.volume_id),
        )
        if incomplete:
            continue  # Manifest erst, wenn alle Pakete der Version oben sind
        data = None
        for vol in volumes:
            p = vol.path_for(v["manifest_relpath"])
            try:
                raw = p.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(raw).hexdigest() == v["manifest_sha256"]:
                data = raw
                break
        if data is None:
            con.print(f"[yellow]Manifest {v['manifest_relpath']} auf keiner angeschlossenen HDD gefunden.")
            continue
        meta = {
            "sha256": v["manifest_sha256"],
            "series": s3client.meta_value(v["series"]),
            "version": str(v["version"]),
            "tool": "foto-archiv",
        }
        try:
            put_small(s3, s3.key(v["manifest_relpath"]), data, "STANDARD", meta, v["manifest_sha256"])
        except (UploadError, ClientError, BotoCoreError) as e:
            errors += 1
            con.print(f"[red]✗ Manifest {v['manifest_relpath']}:[/] {e if isinstance(e, UploadError) else s3client.describe(e)}")
            continue
        db.x(
            "INSERT OR REPLACE INTO manifest_copies(series, version, volume_id, stored_at) VALUES(?,?,?,?)",
            (v["series"], v["version"], s3.volume_id, utcnow()),
        )
        done += 1
    return done, errors


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def upload_index_snapshot(s3: S3) -> str:
    """Konsistente, gzip-komprimierte Kopie des Index nach ``_index/index-<UTC>.sqlite.gz`` (STANDARD)."""
    with tempfile.TemporaryDirectory(prefix="foto-archiv-") as td:
        raw = Path(td) / "index.sqlite"
        s3.app.db.backup_to(raw)
        gz = Path(td) / "index.sqlite.gz"
        with open(raw, "rb") as src, gzip.open(gz, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, BUF)
        sha = sha256_file(gz)
        size = gz.stat().st_size
        stamp = _stamp()
        for n in range(1, 100):
            key = s3.key(f"{INDEX_DIR}/index-{stamp}{'' if n == 1 else f'-{n}'}.sqlite.gz")
            spec = UploadSpec(gz, key, size, sha, "STANDARD", {"sha256": sha, "tool": "foto-archiv"})
            try:
                upload(s3, spec, lambda _n: None)
                return key
            except UploadConflict:
                continue  # gleicher Zeitstempel schon vergeben -> Suffix
        raise UploadError("Kein freier Key für die Index-Sicherung gefunden.")


# ============================================================================ verify s3


@dataclass
class _Check:
    bundle: Any
    status: str  # ok | fehlt | abweichend | fehler
    note: str = ""
    head: dict | None = None


def _check_bundle(s3: S3, b, deep: bool) -> _Check:
    key = s3.key(b["relpath"])
    try:
        h = s3client.head(s3.client, s3.bucket, key)
    except (ClientError, BotoCoreError) as e:
        return _Check(b, "fehler", s3client.describe(e))
    if h is None:
        return _Check(b, "fehlt")
    notes = []
    if h.get("ContentLength") != b["size"]:
        notes.append(f"Größe {h.get('ContentLength')} statt {b['size']}")
    remote_sha = s3client.metadata_sha256(h)
    if remote_sha != b["sha256"]:
        notes.append("SHA-256 in Metadaten fehlt" if not remote_sha else f"SHA-256 {remote_sha[:12]}… statt {b['sha256'][:12]}…")
    if not notes and deep:
        notes += _deep_check(s3, key, b, h)
    if notes:
        return _Check(b, "abweichend", "; ".join(notes), h)
    return _Check(b, "ok", s3client.storage_class_of(h), h)


def _deep_check(s3: S3, key: str, b, h: dict) -> list[str]:
    """Plausibilitätsprüfung der von S3 gespeicherten Prüfsummen (Deep Archive ist nicht lesbar)."""
    try:
        attrs = s3.client.get_object_attributes(
            Bucket=s3.bucket, Key=key, ObjectAttributes=["Checksum", "ObjectParts", "ObjectSize"]
        )
    except (ClientError, BotoCoreError) as e:
        return [f"Attribute nicht lesbar: {s3client.describe(e)}"]
    notes = []
    if attrs.get("ObjectSize") not in (None, b["size"]):
        notes.append(f"ObjectSize {attrs.get('ObjectSize')}")
    checksum = (attrs.get("Checksum") or {}).get("ChecksumSHA256")
    if not checksum:
        notes.append("keine SHA-256-Prüfsumme in S3 gespeichert")
    parts = (attrs.get("ObjectParts") or {}).get("TotalPartsCount")
    meta = {k.lower(): v for k, v in (h.get("Metadata") or {}).items()}
    part_size = meta.get("part-size")
    if part_size and parts:
        want = math.ceil(b["size"] / int(part_size))
        if parts != want:
            notes.append(f"{parts} Teile statt {want}")
    if checksum and not part_size and "-" not in checksum and checksum != s3client.hex_to_b64(b["sha256"]):
        notes.append("SHA-256-Prüfsumme in S3 weicht ab")  # Einzel-PUT: Prüfsumme = SHA-256 der Datei
    return notes


def verify_s3(app: App, series: list[str] | None, deep: bool) -> None:
    """Pakete in S3 gegen den Index prüfen (Existenz, Größe, SHA-256 aus den Metadaten)."""
    s3 = _connect(app)
    db, con = app.db, app.console
    where, params = _series_filter(series)
    bundles = db.q(f"SELECT * FROM bundles WHERE 1=1{where} ORDER BY series, version, name", params)
    if not bundles:
        con.print("Keine versiegelten Pakete im Index.")
        return
    rows = {
        r["bundle_id"]: r
        for r in db.q("SELECT * FROM copies WHERE volume_id=?", (s3.volume_id,))
    }
    with con.status(f"Prüfe {len(bundles)} Paket(e) in S3 …"):
        with ThreadPoolExecutor(max_workers=max(4, app.cfg.s3.concurrency * 2)) as pool:
            checks = list(pool.map(lambda b: _check_bundle(s3, b, deep), bundles))

    per_series: dict[str, dict[str, int]] = {}
    problems: list[tuple[_Check, str]] = []
    now = utcnow()
    with db.tx() as c:
        for ch in checks:
            b = ch.bundle
            known = rows.get(b["id"])
            label = ch.status
            if ch.status == "ok":
                if known:
                    c.execute(
                        "UPDATE copies SET verified_at=?, status='ok' WHERE bundle_id=? AND volume_id=?",
                        (now, b["id"], s3.volume_id),
                    )
                else:
                    c.execute(
                        "INSERT INTO copies(bundle_id, volume_id, stored_at, verified_at, status) VALUES(?,?,?,?, 'ok')",
                        (b["id"], s3.volume_id, now, now),
                    )
                    label = "nachgetragen"
                    problems.append((ch, "in S3 vorhanden, war nicht im Index - nachgetragen"))
            elif ch.status == "fehlt":
                if known:
                    c.execute(
                        "UPDATE copies SET verified_at=?, status='missing' WHERE bundle_id=? AND volume_id=?",
                        (now, b["id"], s3.volume_id),
                    )
                    problems.append((ch, "fehlt in S3 (Index: hochgeladen)"))
                else:
                    label = "nicht hochgeladen"
            elif ch.status == "abweichend":
                if known:
                    c.execute(
                        "UPDATE copies SET verified_at=?, status='bad' WHERE bundle_id=? AND volume_id=?",
                        (now, b["id"], s3.volume_id),
                    )
                problems.append((ch, ch.note + ("" if known else " (unbekanntes Objekt unter dem Key)")))
            else:
                problems.append((ch, ch.note))
            cnt = per_series.setdefault(b["series"], {})
            cnt[label] = cnt.get(label, 0) + 1

    cols = ["ok", "nachgetragen", "nicht hochgeladen", "fehlt", "abweichend", "fehler"]
    t = Table("Serie", "Pakete", *cols, header_style="bold")
    for s, cnt in sorted(per_series.items()):
        t.add_row(s, str(sum(cnt.values())), *[str(cnt.get(k, "")) or "" for k in cols])
    con.print(t)
    if problems:
        pt = Table("Paket", "Größe", "Befund", header_style="bold")
        for ch, note in problems:
            pt.add_row(ch.bundle["relpath"], human(ch.bundle["size"]), note)
        con.print(pt)
    bad = sum(1 for ch in checks if ch.status in ("abweichend", "fehler") or (ch.status == "fehlt" and ch.bundle["id"] in rows))
    if bad:
        con.print(f"[red]{bad} Problem(e) gefunden.")
        raise typer.Exit(1)
    con.print("[green]S3-Prüfung ohne Befund.[/]")


# ============================================================================ restore


@dataclass
class _RestoreItem:
    bundle: Any
    entries: list  # archived-Zeilen (jüngste Version je Pfad), die aus diesem Paket kommen
    state: str = ""  # vorhanden | fehlt | anfordern | läuft | bereit | fehler
    note: str = ""
    storage_class: str = ""

    @property
    def key_rel(self) -> str:
        return self.bundle["relpath"]


def _restore_plan(app: App, series: str, classes: list[str] | None) -> list[_RestoreItem]:
    """Je Pfad nur der jüngste archivierte Stand -> nur die nötigen Pakete, je Paket nur diese Pfade."""
    db = app.db
    latest = db.latest_archived(series)
    by_bundle: dict[int, list] = {}
    for a in latest.values():
        if classes and a["cls"] not in classes:
            continue
        by_bundle.setdefault(a["bundle_id"], []).append(a)
    items = []
    for bid, entries in by_bundle.items():
        b = db.q1("SELECT * FROM bundles WHERE id=?", (bid,))
        items.append(_RestoreItem(bundle=b, entries=sorted(entries, key=lambda a: a["path"])))
    items.sort(key=lambda i: (i.bundle["version"], i.bundle["name"]))
    return items


def _target_path(target: Path, rel: str) -> Path:
    return target.joinpath(*rel.split("/"))


def _local_state(app: App, target: Path, entries, in_source: bool) -> tuple[int, int]:
    """(fehlend, abweichend) im Ziel - abweichend nur anhand Größe bzw. bekanntem SHA-256."""
    missing = differing = 0
    for a in entries:
        p = _target_path(target, a["path"])
        try:
            st = p.stat()
        except OSError:
            missing += 1
            continue
        if st.st_size != a["size"]:
            differing += 1
            continue
        if in_source:
            f = app.db.q1("SELECT size, mtime_ns, sha256 FROM files WHERE path=?", (a["path"],))
            if f and f["sha256"] and f["size"] == st.st_size and f["mtime_ns"] == st.st_mtime_ns and f["sha256"] != a["sha256"]:
                differing += 1
    return missing, differing


def _staging_dir(target: Path) -> Path:
    """Zwischenablage für Pakete neben dem Ziel (nicht darin, damit ``scan`` sie nicht erfasst)."""
    t = target.resolve()
    if t.parent != t:
        return t.parent / f".{t.name}{STAGING_SUFFIX}"
    return t / STAGING_SUFFIX


def _norm_tier(app: App, tier: str) -> str:
    for t in PRICE_RETRIEVAL_GB:
        if tier.lower() == t.lower():
            return t
    _fail(app, f"Unbekannter Abruf-Tier '{tier}' (Bulk oder Standard).")
    raise AssertionError


def _restore_cost(retrieve: int, download: int, tier: str, days: int) -> tuple[float, float, float]:
    retrieval = _gb(retrieve) * PRICE_RETRIEVAL_GB[tier]
    egress = max(0.0, _gb(download) - EGRESS_FREE_GB) * PRICE_EGRESS_GB
    temp = _gb(retrieve) * PRICE_RESTORED_COPY_GB_MONTH * days / 30
    return retrieval, egress, temp


def restore_s3(
    app: App, series: str, classes: list[str] | None, target: Path, tier: str | None, overwrite: bool, dry_run: bool
) -> None:
    """Serie aus S3 wiederherstellen - idempotent: so oft ausführen, bis alles da ist.

    Lauf 1 fordert die Pakete aus Deep Archive an (Restore), spätere Läufe laden bereitstehende
    Pakete herunter, prüfen sie (SHA-256) und entpacken sie.
    """
    _require_bucket(app)
    db, con, cfg = app.db, app.console, app.cfg
    tier = _norm_tier(app, tier or cfg.s3.restore_tier)
    days = int(cfg.s3.restore_days)
    target = Path(target)
    in_source = target.resolve() == cfg.source_root.resolve()

    items = _restore_plan(app, series, classes)
    if not items:
        con.print(f"Keine archivierten Dateien für '{series}'" + (f" (Klassen: {', '.join(classes)})" if classes else "") + ".")
        return
    s3 = _connect(app)

    for it in items:
        missing, differing = _local_state(app, target, it.entries, in_source)
        if missing == 0 and (differing == 0 or not overwrite):
            it.state = "vorhanden"
            it.note = f"{differing} abweichend (--overwrite)" if differing else ""
            continue
        try:
            h = s3client.head(s3.client, s3.bucket, s3.key(it.key_rel))
        except (ClientError, BotoCoreError) as e:
            it.state, it.note = "fehler", s3client.describe(e)
            continue
        if h is None:
            it.state, it.note = "fehlt", "nicht in S3"
            continue
        it.storage_class = s3client.storage_class_of(h)
        if h.get("ContentLength") != it.bundle["size"] or s3client.metadata_sha256(h) not in (None, it.bundle["sha256"]):
            it.state, it.note = "fehler", "Objekt in S3 passt nicht zum Index"
            continue
        ongoing, expiry = s3client.parse_restore(h.get("Restore"))
        if it.storage_class in s3client.ARCHIVE_CLASSES:
            if ongoing is None:
                it.state = "anfordern"
            elif ongoing:
                it.state = "läuft"
            else:
                it.state, it.note = "bereit", f"abrufbar bis {expiry}" if expiry else ""
        else:
            it.state = "bereit"

    retrieve = sum(i.bundle["size"] for i in items if i.state == "anfordern")
    download = sum(i.bundle["size"] for i in items if i.state in ("anfordern", "läuft", "bereit"))
    t = Table("Ver.", "Paket", "Größe", "Dateien", "Speicherklasse", "Zustand", "Hinweis", header_style="bold")
    for i in items:
        t.add_row(
            str(i.bundle["version"]), i.bundle["name"], human(i.bundle["size"]), str(len(i.entries)),
            i.storage_class, i.state, i.note,
        )
    con.print(t)
    if retrieve or download:
        r_cost, e_cost, t_cost = _restore_cost(retrieve, download, tier, days)
        con.print(
            f"Kostenschätzung: Abruf {tier} {human(retrieve)} → {_usd(r_cost)}, "
            f"temporäre Kopie {days} Tage → {_usd(t_cost)}, Download {human(download)} → {_usd(e_cost)} "
            f"(bei {EGRESS_FREE_GB} GB/Monat Freikontingent; sonst {_usd(_gb(download) * PRICE_EGRESS_GB)})."
        )
    if dry_run:
        return

    errors = sum(1 for i in items if i.state in ("fehlt", "fehler"))
    requested = pending = restored = 0
    for i in items:
        key = s3.key(i.key_rel)
        if i.state == "anfordern":
            try:
                s3.client.restore_object(
                    Bucket=s3.bucket,
                    Key=key,
                    RestoreRequest={"Days": days, "GlacierJobParameters": {"Tier": tier}},
                )
            except ClientError as e:
                if s3client.error_code(e) != "RestoreAlreadyInProgress":
                    errors += 1
                    con.print(f"[red]✗ Anforderung {i.key_rel}:[/] {s3client.describe(e)}")
                    continue
            _set_restore(db, key, tier, days, "requested", new=True)
            requested += 1
        elif i.state == "läuft":
            if not db.q1("SELECT 1 FROM restores WHERE key=?", (key,)):
                _set_restore(db, key, tier, days, "requested", new=True)
            pending += 1
        elif i.state == "bereit":
            if db.q1("SELECT 1 FROM restores WHERE key=?", (key,)):
                db.x("UPDATE restores SET status='available' WHERE key=?", (key,))
            try:
                ok = _download_and_extract(s3, i, target, overwrite, in_source)
            except KeyboardInterrupt:
                con.print("\n[yellow]Abgebrochen.[/] Der Download wird beim nächsten Lauf fortgesetzt.")
                raise typer.Exit(130)
            except (UploadError, ClientError, BotoCoreError, OSError) as e:
                ok = False
                con.print(f"[red]✗ {i.key_rel}:[/] {e if isinstance(e, UploadError) else s3client.describe(e)}")
            if ok:
                restored += 1
                db.x("UPDATE restores SET status='downloaded' WHERE key=?", (key,))
            else:
                errors += 1

    if requested:
        con.print(
            f"[cyan]{requested} Paket(e) aus dem Archiv angefordert ({tier}, {RESTORE_DURATION[tier]}).[/] "
            "Danach denselben Befehl erneut ausführen."
        )
    if pending:
        con.print(f"[cyan]{pending} Paket(e): Wiederherstellung läuft noch ({tier}, {RESTORE_DURATION[tier]}).[/] Später erneut ausführen.")
    done = sum(1 for i in items if i.state == "vorhanden")
    con.print(f"Entpackt: {restored} Paket(e); bereits vorhanden: {done}; ausstehend: {requested + pending}; Fehler: {errors}")
    if errors:
        raise typer.Exit(1)


def _set_restore(db, key: str, tier: str, days: int, status: str, new: bool = False) -> None:
    db.x(
        """INSERT INTO restores(key, tier, days, requested_at, status) VALUES(?,?,?,?,?)
           ON CONFLICT(key) DO UPDATE SET tier=excluded.tier, days=excluded.days,
               requested_at=excluded.requested_at, status=excluded.status""",
        (key, tier, days, utcnow(), status),
    )


class _Done:
    """Fortschrittsdatei eines Downloads: Nummern fertiger Abschnitte (eine je Zeile)."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> set[int]:
        try:
            return {int(x) for x in self.path.read_text(encoding="ascii").split()}
        except (OSError, ValueError):
            return set()

    def add(self, n: int) -> None:
        with open(self.path, "a", encoding="ascii") as fh:
            fh.write(f"{n}\n")


def download_object(s3: S3, key: str, size: int, dest: Path, advance: Advance) -> None:
    """Paralleler Download per Range-GET nach ``dest`` (über .partial, fortsetzbar)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + PARTIAL_SUFFIX)
    done_file = _Done(dest.with_name(dest.name + ".teile"))
    chunk = _part_size(s3.app.cfg.s3.part_size_mb, size)
    nchunks = max(1, math.ceil(size / chunk))
    done: set[int] = set()
    if partial.exists() and partial.stat().st_size == size:
        done = {n for n in done_file.load() if 0 <= n < nchunks}
    else:
        with open(partial, "wb") as fh:
            fh.truncate(size)
        done_file.path.write_text("", encoding="ascii")
    for n in done:
        advance(min(chunk, size - n * chunk))

    def fetch(n: int) -> int:
        start = n * chunk
        end = min(size, start + chunk) - 1
        for attempt in range(PART_ATTEMPTS):
            got = 0
            try:
                r = s3.client.get_object(Bucket=s3.bucket, Key=key, Range=f"bytes={start}-{end}")
                body = r["Body"]
                with open(partial, "r+b") as fh:
                    fh.seek(start)
                    while True:
                        blk = body.read(BUF)
                        if not blk:
                            break
                        fh.write(blk)
                        got += len(blk)
                        advance(len(blk))
                if got != end - start + 1:
                    raise UploadError(f"{key}: Abschnitt {n} unvollständig ({got} Bytes)")
                return n
            except (ClientError, BotoCoreError, OSError, UploadError) as e:
                advance(-got)
                if s3client.is_fatal(e) or attempt == PART_ATTEMPTS - 1:
                    raise
            time.sleep(RETRY_DELAY * 2**attempt)
        raise AssertionError("unerreichbar")

    todo = [n for n in range(nchunks) if n not in done]
    conc = max(1, int(s3.app.cfg.s3.concurrency))
    pool = ThreadPoolExecutor(max_workers=conc, thread_name_prefix="s3-download")
    futures: set[Future] = set()
    try:
        futures = {pool.submit(fetch, n) for n in todo}
        while futures:
            finished = _wait_some(futures)
            for f in finished:
                futures.discard(f)
                done_file.add(f.result())
    except BaseException:
        for f in futures:
            f.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    os.replace(partial, dest)
    try:
        done_file.path.unlink()
    except OSError:
        pass


def _cleanup_empty_dirs(path: Path, stop: Path) -> None:
    p = path
    while True:
        try:
            p.rmdir()
        except OSError:
            return
        if p == stop:
            return
        p = p.parent


def _download_and_extract(s3: S3, item: _RestoreItem, target: Path, overwrite: bool, in_source: bool) -> bool:
    app, con = s3.app, s3.app.console
    b = item.bundle
    staging = _staging_dir(target)
    staged = staging.joinpath(*b["relpath"].split("/"))
    staged.parent.mkdir(parents=True, exist_ok=True)

    have = staged.exists() and staged.stat().st_size == b["size"]
    need_space = (0 if have else b["size"]) + sum(a["size"] for a in item.entries)
    free = shutil.disk_usage(staged.parent).free
    if free < need_space:
        raise UploadError(f"Zu wenig Platz: frei {human(free)}, benötigt {human(need_space)}")

    with app.byte_progress() as prog:
        if not have:
            t = prog.add_task(f"↓ {b['series']} {b['name']}", total=b["size"])
            download_object(s3, s3.key(b["relpath"]), b["size"], staged, lambda n: prog.advance(t, n))
        t2 = prog.add_task(f"prüfen {b['name']}", total=b["size"])
        sha = sha256_file(staged, lambda n: prog.advance(t2, n))
        if sha != b["sha256"]:
            staged.unlink()
            raise UploadError(f"{b['relpath']}: SHA-256 des heruntergeladenen Pakets falsch - verworfen.")
        only = {a["path"] for a in item.entries}
        expected = {a["path"]: a["sha256"] for a in item.entries}
        t3 = prog.add_task(f"entpacken {b['name']}", total=sum(a["size"] for a in item.entries))
        stats = extract_bundle(staged, target, expected=expected, only=only, overwrite=overwrite, progress=lambda n: prog.advance(t3, n))

    if in_source:
        _update_files(app, target, item, stats.written + stats.skipped_same)
    staged.unlink()
    _cleanup_empty_dirs(staged.parent, staging)

    con.print(
        f"[green]✓[/] {b['relpath']}: {len(stats.written)} geschrieben, {len(stats.skipped_same)} unverändert"
        + (f", [yellow]{len(stats.conflicts)} abweichend vorhanden (--overwrite)[/]" if stats.conflicts else "")
        + (f", [red]{len(stats.hash_errors)} Prüfsummenfehler[/]" if stats.hash_errors else "")
    )
    return not stats.hash_errors


def _update_files(app: App, target: Path, item: _RestoreItem, paths: list[str]) -> None:
    """Wiederhergestellte Dateien im Index als vorhanden (mit bekanntem SHA-256) führen."""
    by_path = {a["path"]: a for a in item.entries}
    now = utcnow()
    with app.db.tx() as c:
        for p in paths:
            a = by_path.get(p)
            if a is None:
                continue
            try:
                st = _target_path(target, p).stat()
            except OSError:
                continue
            c.execute(
                """INSERT INTO files(path, series, cls, size, mtime_ns, sha256, present, slimmed, last_seen)
                   VALUES(?,?,?,?,?,?,1,0,?)
                   ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns,
                       sha256=excluded.sha256, present=1, slimmed=0, last_seen=excluded.last_seen""",
                (p, a["series"], a["cls"], st.st_size, st.st_mtime_ns, a["sha256"], now),
            )


# ============================================================================ Altbestand


def is_tool_key(rel: str) -> bool:
    return bool(TOOL_KEY_RE.match(rel)) or rel.startswith(f"{INDEX_DIR}/") or rel.startswith(f"{CATALOG_DIR}/")


_ARCHIVE_EXT = re.compile(r"\.(tar|tar\.gz|tgz|zip|7z)$", re.IGNORECASE)


def legacy_scan(app: App) -> None:
    """Nicht vom Tool erzeugte Objekte unter dem Präfix in die Tabelle ``legacy`` übernehmen und auflisten."""
    s3 = _connect(app)
    db, con = app.db, app.console
    now = utcnow()
    legacy: list[dict] = []
    tool_n = tool_bytes = 0
    with con.status("Liste Objekte im Bucket …"):
        for page in s3.client.get_paginator("list_objects_v2").paginate(Bucket=s3.bucket, Prefix=s3.prefix):
            for o in page.get("Contents", []) or []:
                rel = o["Key"][len(s3.prefix):]
                if not rel or (rel.endswith("/") and o["Size"] == 0):
                    continue  # "Ordner"-Platzhalter
                if is_tool_key(rel):
                    tool_n += 1
                    tool_bytes += o["Size"]
                else:
                    legacy.append(o)

    with db.tx() as c:
        for o in legacy:
            lm = o.get("LastModified")
            c.execute(
                """INSERT INTO legacy(key, size, last_modified, storage_class, etag, listed_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET size=excluded.size, last_modified=excluded.last_modified,
                       storage_class=excluded.storage_class, etag=excluded.etag, listed_at=excluded.listed_at""",
                (o["Key"], o["Size"], lm.isoformat() if lm else None, o.get("StorageClass") or "STANDARD",
                 str(o.get("ETag", "")).strip('"'), now),
            )
    stale = db.q1("SELECT COUNT(*) n FROM legacy WHERE listed_at<>?", (now,))["n"]

    # lokale Serien (inkl. ausgedünnter Dateien) zum Abgleich
    local: dict[str, int] = {
        r["series"]: r["s"]
        for r in db.q("SELECT series, SUM(size) s FROM files WHERE present=1 OR slimmed=1 GROUP BY series")
    }
    for r in db.q("SELECT series, SUM(size) s FROM versions GROUP BY series"):
        local.setdefault(r["series"], r["s"])
    by_lower = {s.lower(): s for s in local}
    by_year: dict[int, list[str]] = {}
    for s in local:
        y = series_year(s)
        if y:
            by_year.setdefault(y, []).append(s)

    def match(rel: str) -> str:
        name = rel.rsplit("/", 1)[-1]
        stem = _ARCHIVE_EXT.sub("", name)
        if stem.lower() in by_lower:
            s = by_lower[stem.lower()]
            return f"Serie {s} (lokal {human(local[s])})"
        cands = [s for s in local if stem.lower().startswith(s.lower()) or s.lower().startswith(stem.lower())]
        if cands and len(stem) >= 4:
            return ", ".join(f"{s} ({human(local[s])})" for s in sorted(cands)[:3])
        year = series_year(rel.split("/", 1)[0]) or series_year(stem)
        if year and year in by_year:
            ss = by_year[year]
            return f"Jahr {year}: {len(ss)} lokale Serie(n), {human(sum(local[s] for s in ss))}"
        return ""

    groups: dict[str, list[dict]] = {}
    for o in legacy:
        rel = o["Key"][len(s3.prefix):]
        folder = rel.split("/", 1)[0] if "/" in rel else "(ohne Ordner)"
        groups.setdefault(folder, []).append(o)

    t = Table("Objekt", "Größe", "Klasse", "Datum", "mögliche lokale Entsprechung", header_style="bold")
    for folder in sorted(groups):
        for o in sorted(groups[folder], key=lambda x: x["Key"]):
            rel = o["Key"][len(s3.prefix):]
            lm = o.get("LastModified")
            t.add_row(rel, human(o["Size"]), o.get("StorageClass") or "STANDARD", lm.strftime("%Y-%m-%d") if lm else "", match(rel))
    con.print(t)

    yt = Table("Ordner", "Objekte", "Größe", "Klassen", "lokale Serien dieses Jahres", header_style="bold")
    monthly = 0.0
    for folder in sorted(groups):
        objs = groups[folder]
        size = sum(o["Size"] for o in objs)
        classes = sorted({o.get("StorageClass") or "STANDARD" for o in objs})
        for o in objs:
            monthly += _gb(o["Size"]) * PRICE_STORAGE_GB_MONTH.get(o.get("StorageClass") or "STANDARD", 0.023)
        y = series_year(folder)
        loc = ""
        if y and y in by_year:
            ss = by_year[y]
            loc = f"{len(ss)} Serie(n), {human(sum(local[s] for s in ss))}"
        yt.add_row(folder, str(len(objs)), human(size), ", ".join(classes), loc)
    yt.add_section()
    total = sum(o["Size"] for o in legacy)
    yt.add_row("[bold]Summe", str(len(legacy)), human(total), "", "")
    con.print(yt)
    con.print(
        f"Altbestand: {len(legacy)} Objekt(e), {human(total)}, Speicherkosten ≈ {_usd(monthly)}/Monat "
        f"({_usd(monthly * 12)}/Jahr). Vom Tool verwaltet: {tool_n} Objekt(e), {human(tool_bytes)}."
    )
    if stale:
        con.print(f"[yellow]{stale} früher erfasste Altbestands-Objekt(e) sind nicht mehr im Bucket.")


# ============================================================================ Kataloge


def _sqlite_snapshot(src: Path, dst: Path) -> None:
    """Konsistente Kopie per SQLite-Backup-API (funktioniert auch, während digiKam läuft)."""
    last: sqlite3.Error | None = None
    for read_only in (True, False):  # bevorzugt nur lesend; Fallback z. B. bei WAL ohne -shm-Datei
        try:
            if read_only:
                source = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=60)
            else:
                source = sqlite3.connect(src, timeout=60)
            try:
                if dst.exists():
                    dst.unlink()
                target = sqlite3.connect(dst)
                try:
                    source.backup(target)
                finally:
                    target.close()
                return
            finally:
                source.close()
        except sqlite3.Error as e:
            last = e
    assert last is not None
    raise last


def catalog_backup(app: App, volume: Volume | None, to_s3: bool) -> None:
    """Katalog-Datenbanken (digiKam) gzip-komprimiert auf HDD und/oder nach S3 (STANDARD) sichern."""
    db, con, cfg = app.db, app.console, app.cfg
    if volume is None and not to_s3:
        _fail(app, "Weder HDD noch S3 als Ziel - nichts zu tun.")
    s3 = _connect(app) if to_s3 else None
    stamp = _stamp()
    taken_at = utcnow()
    errors = saved = 0
    with tempfile.TemporaryDirectory(prefix="foto-archiv-katalog-") as td:
        for name in cfg.catalog_files:
            src = cfg.source_root / name
            if not src.is_file():
                continue
            try:
                with open(src, "rb") as fh:
                    if fh.read(16) != SQLITE_MAGIC:
                        con.print(f"[yellow]{name}: keine SQLite-Datenbank - übersprungen.")
                        continue
            except OSError as e:
                con.print(f"[yellow]{name}: nicht lesbar ({e}) - übersprungen.")
                continue
            snap = Path(td) / name
            gz = Path(td) / f"{name}.gz"
            try:
                _sqlite_snapshot(src, snap)
                with open(snap, "rb") as fi, gzip.open(gz, "wb", compresslevel=6) as fo:
                    shutil.copyfileobj(fi, fo, BUF)
            except (sqlite3.Error, OSError) as e:
                errors += 1
                con.print(f"[red]✗ {name}: Sicherung fehlgeschlagen ({e})")
                continue
            sha = sha256_file(gz)
            size = gz.stat().st_size
            rel = f"{CATALOG_DIR}/{stamp}/{name}.gz"

            if volume is not None:
                dest = volume.path_for(rel)
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + PARTIAL_SUFFIX)
                    shutil.copyfile(gz, tmp)
                    if sha256_file(tmp) != sha:
                        raise OSError("Prüfsumme nach dem Kopieren falsch")
                    os.replace(tmp, dest)
                except OSError as e:
                    errors += 1
                    con.print(f"[red]✗ {name} → {volume.label}: {e}")
                else:
                    db.x(
                        "INSERT OR REPLACE INTO catalogs(name, taken_at, sha256, size, volume_id, location) VALUES(?,?,?,?,?,?)",
                        (name, taken_at, sha, size, volume.id, rel),
                    )
                    saved += 1
                    con.print(f"[green]✓[/] {name} → {volume.label}: {rel} ({human(size)})")

            if s3 is not None:
                key = s3.key(rel)
                spec = UploadSpec(gz, key, size, sha, "STANDARD", {"sha256": sha, "tool": "foto-archiv", "catalog": name})
                try:
                    upload(s3, spec, lambda _n: None)
                except (UploadError, ClientError, BotoCoreError, OSError) as e:
                    errors += 1
                    con.print(f"[red]✗ {name} → S3: {e if isinstance(e, UploadError) else s3client.describe(e)}")
                else:
                    db.x(
                        "INSERT OR REPLACE INTO catalogs(name, taken_at, sha256, size, volume_id, location) VALUES(?,?,?,?,?,?)",
                        (name, taken_at, sha, size, s3.volume_id, key),
                    )
                    saved += 1
                    con.print(f"[green]✓[/] {name} → s3://{s3.bucket}/{key} ({human(size)})")
    if saved == 0 and errors == 0:
        con.print(f"Keine Katalog-Datenbanken in {cfg.source_root} gefunden ({', '.join(cfg.catalog_files)}).")
    if errors:
        raise typer.Exit(1)
