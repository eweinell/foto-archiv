"""Ablagestruktur der Pakete (identisch auf HDD und in S3)."""

from __future__ import annotations

import re

NO_YEAR = "_ohne-jahr"
ROOT_SERIES = "_root"  # Dateien direkt in source_root

_YEAR_RE = re.compile(r"^((?:19|20)\d{2})(?!\d)")


def series_year(series: str) -> int | None:
    m = _YEAR_RE.match(series)
    return int(m.group(1)) if m else None


def series_base(series: str) -> str:
    """``2026/2026.Kenia`` bzw. ``_ohne-jahr/sortiert``."""
    y = series_year(series)
    return f"{y if y else NO_YEAR}/{series}"


def version_dir(series: str, version: int) -> str:
    return f"{series_base(series)}/v{version}"


def bundle_name(cls: str, index: int) -> str:
    return f"{cls}-{index:03d}.tar"


def bundle_relpath(series: str, version: int, name: str) -> str:
    return f"{version_dir(series, version)}/{name}"


def manifest_relpath(series: str, version: int) -> str:
    return f"{version_dir(series, version)}/manifest.json.gz"


INDEX_DIR = "_index"
CATALOG_DIR = "_kataloge"
