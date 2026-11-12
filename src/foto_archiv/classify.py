"""Zuordnung von Dateien zu Archivklassen anhand von Endung und Dateiname."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

RAW_EXT = {
    ".arw", ".srf", ".sr2", ".pef", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".dng",
    ".raf", ".orf", ".rw2", ".rwl", ".srw", ".x3f", ".3fr", ".iiq", ".erf", ".mrw", ".kdc",
}
SIDECAR_EXT = {".pp3", ".xmp", ".dop", ".pto", ".arp", ".cos", ".on1"}
IMG_EXT = {".jpg", ".jpeg", ".jpe", ".tif", ".tiff", ".png", ".heic", ".heif", ".webp", ".avif", ".jxl", ".gif", ".bmp"}
VIDEO_EXT = {
    ".mp4", ".m4v", ".mov", ".mpg", ".mpeg", ".avi", ".mts", ".m2ts", ".mkv", ".3gp", ".wmv", ".webm", ".insv", ".lrv",
}

RAW = "raw"
IMG_DERIVED = "img-derived"
IMG_ORIGINAL = "img-original"
VIDEO = "video"
OTHER = "other"
CLASSES = (RAW, IMG_DERIVED, IMG_ORIGINAL, VIDEO, OTHER)

# typische Suffixe von Exporten: DSC0001-1.jpg, DSC0001_01.jpg, DSC0001_edit.jpg, DSC0001 (2).jpg …
_SUFFIX_RE = re.compile(
    r"(?:[-_ .~]+(?:\(?\d{1,3}\)?|edit(?:ed)?|bearb\w*|export\w*|hdr|pano\w*|v\d+|copy|kopie|web|small|klein|final|sw|bw))$",
    re.IGNORECASE,
)


def _ext(name: str) -> str:
    return PurePosixPath(name).suffix.lower()


def raw_key(name: str) -> str:
    """Schlüssel eines RAW-Bildes für den Abgleich mit Exporten (kleingeschriebener Stamm)."""
    return PurePosixPath(name).stem.lower()


def _derived_candidates(name: str):
    stem = PurePosixPath(name).stem.lower()
    # "DSC0001.ARW.jpg" -> "DSC0001"
    inner = PurePosixPath(stem)
    if inner.suffix.lower() in RAW_EXT:
        stem = inner.stem
    yield stem
    for _ in range(3):
        new = _SUFFIX_RE.sub("", stem)
        if new == stem or not new:
            break
        stem = new
        yield stem


def classify(name: str, raw_keys: set[str]) -> str:
    """Klasse einer Datei. ``raw_keys``: Menge der RAW-Stämme derselben Serie."""
    ext = _ext(name)
    if ext in RAW_EXT:
        return RAW
    if ext in VIDEO_EXT:
        return VIDEO
    if ext in SIDECAR_EXT:
        # "DSC0001.ARW.pp3" / "DSC0001.xmp" gehören zum RAW, sonst "other"
        inner = PurePosixPath(PurePosixPath(name).stem)
        if inner.suffix.lower() in RAW_EXT or inner.name.lower() in raw_keys:
            return RAW
        return OTHER
    if ext in IMG_EXT:
        if raw_keys and any(c in raw_keys for c in _derived_candidates(name)):
            return IMG_DERIVED
        return IMG_ORIGINAL
    return OTHER


def is_raw_name(name: str) -> bool:
    return _ext(name) in RAW_EXT
