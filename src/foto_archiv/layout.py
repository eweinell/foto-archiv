"""Ablagestruktur der Pakete (identisch auf HDD und in S3) und Index-Pfade.

Index-Pfade und Seriennamen tragen die Sammlung als erste Ebene:
``fotos/2026.Kenia/raws/DSC01.ARW`` bzw. Serie ``fotos/2026.Kenia``. Im Tar steht der Pfad
relativ zum Quellordner der Sammlung (``2026.Kenia/raws/DSC01.ARW``).

Archiv (HDD-Volume bzw. ``<prefix>`` in S3)::

    <archive_dir>/<Jahr|_ohne-jahr>/<Serie>/v<N>/…   (year = "name")
    <archive_dir>/<Serie>/v<N>/…                      (year = "none")
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Collection

NO_YEAR = "_ohne-jahr"
ROOT_SERIES = "_root"  # Dateien direkt im Quellordner einer Sammlung

_YEAR_RE = re.compile(r"^((?:19|20)\d{2})(?!\d)")
# oberste Ordner des alten Ablageformats (vor Sammlungen): Jahr bzw. _ohne-jahr
LEGACY_TOP_RE = re.compile(r"^(?:(?:19|20)\d{2}|_ohne-jahr)$")


def split_vpath(vpath: str) -> tuple[str, str]:
    """``fotos/2026.Kenia/x.jpg`` -> (``fotos``, ``2026.Kenia/x.jpg``)."""
    cid, _, rest = vpath.partition("/")
    return cid, rest


def join_vpath(cid: str, rel: str) -> str:
    return f"{cid}/{rel}"


def series_name(series: str) -> str:
    """Ordnername einer Serie ohne Sammlung."""
    return series.rsplit("/", 1)[-1]


def series_year(series: str) -> int | None:
    m = _YEAR_RE.match(series_name(series))
    return int(m.group(1)) if m else None


def collection_year(coll: Collection, series: str) -> int | None:
    return series_year(series) if coll.year == "name" else None


def series_base(coll: Collection, series: str) -> str:
    """``fotos/2026/2026.Kenia``, ``fotos/_ohne-jahr/sortiert`` bzw. ``musik/Beatles``."""
    name = series_name(series)
    if coll.year == "none":
        return f"{coll.archive_dir}/{name}"
    y = series_year(series)
    return f"{coll.archive_dir}/{y if y else NO_YEAR}/{name}"


def version_dir(coll: Collection, series: str, version: int) -> str:
    return f"{series_base(coll, series)}/v{version}"


def bundle_name(cls: str, index: int) -> str:
    return f"{cls}-{index:03d}.tar"


def bundle_relpath(coll: Collection, series: str, version: int, name: str) -> str:
    return f"{version_dir(coll, series, version)}/{name}"


def manifest_relpath(coll: Collection, series: str, version: int) -> str:
    return f"{version_dir(coll, series, version)}/manifest.json.gz"


INDEX_DIR = "_index"
CATALOG_DIR = "_kataloge"
README_NAME = "LIESMICH.txt"
