"""Archiv-Volumes (HDDs bzw. Ordner).

Ein Volume ist ein Ordner ``<mount>/foto-archiv`` mit einer Markierungsdatei ``volume.json``
(id, label). Die Erkennung läuft über alle lokalen Laufwerke sowie ``cfg.hdd_paths``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import Index, utcnow

VOLUME_DIR = "foto-archiv"
MARKER = "volume.json"


class VolumeError(Exception):
    pass


@dataclass
class Volume:
    id: str
    label: str
    root: Path  # <mount>/foto-archiv

    def path_for(self, relpath: str) -> Path:
        return self.root.joinpath(*relpath.split("/"))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def usage(self) -> tuple[int, int, int]:
        u = shutil.disk_usage(self.root)
        return u.total, u.used, u.free

    def __str__(self) -> str:
        return f"{self.label} ({self.root})"


def _read_marker(root: Path) -> Volume | None:
    marker = root / MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return Volume(id=data["id"], label=data["label"], root=root)
    except (OSError, ValueError, KeyError):
        return None


def init_volume(mount: Path, label: str, db: Index) -> Volume:
    root = Path(mount) / VOLUME_DIR
    existing = _read_marker(root)
    if existing:
        raise VolumeError(f"{root} ist bereits als Volume '{existing.label}' ({existing.id}) eingerichtet.")
    if db.q1("SELECT 1 FROM volumes WHERE label=? AND kind='hdd'", (label,)):
        raise VolumeError(f"Label '{label}' ist bereits vergeben.")
    root.mkdir(parents=True, exist_ok=True)
    vol = Volume(id=str(uuid.uuid4()), label=label, root=root)
    (root / MARKER).write_text(
        json.dumps({"id": vol.id, "label": label, "created": utcnow(), "tool": "foto-archiv"}, indent=2),
        encoding="utf-8",
    )
    db.register_volume(vol.id, "hdd", label, str(root))
    return vol


def relabel_volume(vol: Volume, new_label: str, db: Index) -> Volume:
    """Label ändern. Kopien hängen an der ID, daher bleibt alles andere gültig."""
    new_label = new_label.strip()
    if not new_label:
        raise VolumeError("Label darf nicht leer sein.")
    if new_label == vol.label:
        return vol
    if db.q1("SELECT 1 FROM volumes WHERE label=? AND kind='hdd' AND id<>?", (new_label, vol.id)):
        raise VolumeError(f"Label '{new_label}' ist bereits vergeben.")
    marker = vol.root / MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data.get("id") != vol.id:
        raise VolumeError(f"{marker} gehört nicht zu Volume {vol.id}.")
    data["label"] = new_label
    data.setdefault("previous_labels", []).append(vol.label)
    tmp = marker.with_name(MARKER + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, marker)
    db.register_volume(vol.id, "hdd", new_label, str(vol.root))
    return Volume(id=vol.id, label=new_label, root=vol.root)


def _candidate_mounts(cfg: Config) -> list[Path]:
    mounts: list[Path] = []
    if sys.platform == "win32":
        import ctypes

        get_type = ctypes.windll.kernel32.GetDriveTypeW
        drives = os.listdrives() if hasattr(os, "listdrives") else [f"{c}:\\" for c in "DEFGHIJKLMNOPQRSTUVWXYZ"]
        for d in drives:
            # 2=removable 3=fixed; Netzlaufwerke (4) und CD (5) überspringen
            if get_type(d) in (2, 3):
                mounts.append(Path(d))
    else:
        for base in ("/media", "/mnt", "/Volumes", f"/run/media/{os.environ.get('USER', '')}"):
            p = Path(base)
            if p.is_dir():
                try:
                    mounts.extend(x for x in p.iterdir() if x.is_dir())
                    mounts.extend(y for x in p.iterdir() if x.is_dir() for y in x.iterdir() if y.is_dir())
                except OSError:
                    pass
    mounts.extend(Path(p) for p in cfg.hdd_paths)
    return mounts


def find_connected(cfg: Config, db: Index | None = None) -> list[Volume]:
    found: dict[str, Volume] = {}
    for m in _candidate_mounts(cfg):
        for root in (m / VOLUME_DIR, m):  # hdd_paths dürfen auch direkt auf .../foto-archiv zeigen
            try:
                vol = _read_marker(root)
            except OSError:
                vol = None
            if vol and vol.id not in found:
                found[vol.id] = vol
                if db is not None:
                    db.register_volume(vol.id, "hdd", vol.label, str(root))
                break
    return list(found.values())


def resolve_volume(cfg: Config, db: Index, spec: str | None) -> Volume:
    """Volume per Label, ID oder Pfad. Ohne Angabe: das einzige angeschlossene."""
    connected = find_connected(cfg, db)
    if spec:
        p = Path(spec)
        if p.exists():
            for root in (p / VOLUME_DIR, p):
                v = _read_marker(root)
                if v:
                    db.register_volume(v.id, "hdd", v.label, str(root))
                    return v
        for v in connected:
            if spec in (v.label, v.id):
                return v
        raise VolumeError(f"Volume '{spec}' ist nicht angeschlossen bzw. unbekannt.")
    if len(connected) == 1:
        return connected[0]
    if not connected:
        raise VolumeError("Kein Archiv-Volume angeschlossen (einrichten mit: foto-archiv hdd init <Laufwerk> --label A).")
    labels = ", ".join(v.label for v in connected)
    raise VolumeError(f"Mehrere Volumes angeschlossen ({labels}) - bitte mit --volume wählen.")
