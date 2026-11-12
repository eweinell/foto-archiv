"""Dünne Hilfsschicht um boto3: Client-Erzeugung, Fehlerklassifizierung, kleine Helfer.

Das Tool löscht in S3 nie etwas (der IAM-Benutzer darf es auch nicht). Erlaubt sind nur:
ListBucket, GetBucketLocation, GetBucketVersioning, ListBucketMultipartUploads, PutObject,
GetObject, GetObjectAttributes, RestoreObject, AbortMultipartUpload, ListMultipartUploadParts.
"""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any
from urllib.parse import quote, unquote

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

from .config import S3Config

# Speicherklassen, deren Objekte vor dem Lesen per RestoreObject zurückgeholt werden müssen
ARCHIVE_CLASSES = {"GLACIER", "DEEP_ARCHIVE"}

MIN_PART_SIZE = 5 * 1024 * 1024  # S3-Minimum je Teil (außer dem letzten)
MAX_PART_SIZE = 5 * 1024**3
MAX_PARTS = 10_000
TARGET_MAX_PARTS = 9_000  # Reserve unter dem S3-Limit

_PRECONDITION_CODES = {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}

# Fehler, bei denen eine Wiederholung sinnlos ist
FATAL_CODES = {
    "NoSuchUpload",
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "NoSuchBucket",
    "InvalidObjectState",
    "ExpiredToken",
    "InvalidPart",
    "EntityTooSmall",
}


class S3Error(Exception):
    """Fehler mit deutscher, für den Benutzer bestimmter Meldung."""


def make_client(cfg: S3Config):
    """Erzeugt einen S3-Client (Profil leer = Standard-Anmeldekette, z. B. Env-Variablen)."""
    try:
        session = boto3.session.Session(profile_name=cfg.profile or None, region_name=cfg.region or None)
    except ProfileNotFound:
        raise S3Error(
            f"AWS-Profil '{cfg.profile}' nicht gefunden (in ~/.aws/credentials anlegen "
            "oder profile in der Konfiguration anpassen)."
        ) from None
    conc = max(1, int(cfg.concurrency))
    bc = BotoConfig(
        retries={"max_attempts": 10, "mode": "adaptive"},
        max_pool_connections=conc + 4,
        connect_timeout=30,
        read_timeout=300,  # große Teile über langsame Leitungen
        tcp_keepalive=True,
    )
    return session.client("s3", endpoint_url=cfg.endpoint_url or None, config=bc)


# ---------------------------------------------------------------------------- Fehler


def error_code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {}) or {}
        code = str(err.get("Code") or "")
        if not code:
            code = str(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
        return code
    return ""


def http_status(exc: BaseException) -> int | None:
    if isinstance(exc, ClientError):
        return exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return None


def is_not_found(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and (error_code(exc) in _NOT_FOUND_CODES or http_status(exc) == 404)


def is_precondition_failed(exc: BaseException) -> bool:
    """Bedingtes Schreiben (If-None-Match: *) gescheitert -> Objekt existiert bereits (bzw. parallel)."""
    return isinstance(exc, ClientError) and (error_code(exc) in _PRECONDITION_CODES or http_status(exc) in (409, 412))


def is_fatal(exc: BaseException) -> bool:
    return isinstance(exc, ClientError) and error_code(exc) in FATAL_CODES


def describe(exc: BaseException) -> str:
    """Kurze deutsche Beschreibung eines AWS-Fehlers."""
    if isinstance(exc, NoCredentialsError):
        return "Keine AWS-Zugangsdaten gefunden (Profil bzw. Umgebungsvariablen prüfen)."
    if isinstance(exc, ClientError):
        code = error_code(exc)
        msg = exc.response.get("Error", {}).get("Message", "")
        op = getattr(exc, "operation_name", "")
        if code in ("AccessDenied", "403"):
            return f"Zugriff verweigert bei {op} ({msg or code}) - IAM-Rechte prüfen."
        if code == "NoSuchBucket":
            return "Bucket existiert nicht."
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
            return "AWS-Zugangsdaten ungültig."
        return f"{op}: {code} {msg}".strip()
    if isinstance(exc, BotoCoreError):
        return f"Verbindungs-/SDK-Fehler: {exc}"
    return f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------- Helfer


def head(client, bucket: str, key: str) -> dict[str, Any] | None:
    """head_object; None, wenn das Objekt nicht existiert."""
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        if is_not_found(e):
            return None
        raise


def storage_class_of(head_resp: dict[str, Any]) -> str:
    return head_resp.get("StorageClass") or "STANDARD"


_RESTORE_RE = re.compile(r'ongoing-request="(true|false)"(?:,\s*expiry-date="([^"]*)")?')


def parse_restore(header: str | None) -> tuple[bool | None, str | None]:
    """``x-amz-restore`` auswerten: (laufend?, Ablaufdatum). (None, None) = kein Restore."""
    if not header:
        return None, None
    m = _RESTORE_RE.search(header)
    if not m:
        return None, None
    return m.group(1) == "true", m.group(2)


def b64_digest(digest: bytes) -> str:
    return base64.b64encode(digest).decode("ascii")


def b64_sha256(data: bytes) -> str:
    return b64_digest(hashlib.sha256(data).digest())


def hex_to_b64(hexdigest: str) -> str:
    return b64_digest(bytes.fromhex(hexdigest))


def meta_value(value: str) -> str:
    """Metadaten müssen ASCII sein -> Prozent-Kodierung (z. B. Umlaute in Seriennamen)."""
    return quote(str(value), safe=" ._-:/()+,=@")


def meta_decode(value: str) -> str:
    return unquote(value)


def metadata_sha256(head_resp: dict[str, Any]) -> str | None:
    meta = {k.lower(): v for k, v in (head_resp.get("Metadata") or {}).items()}
    return meta.get("sha256")


def matches(head_resp: dict[str, Any] | None, size: int, sha256: str) -> bool:
    """Gilt ein vorhandenes Objekt als identisch (Größe + gespeicherter SHA-256)?"""
    return bool(head_resp) and head_resp.get("ContentLength") == size and metadata_sha256(head_resp) == sha256
