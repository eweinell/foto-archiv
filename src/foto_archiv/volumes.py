"""Archiv-Volumes (HDDs bzw. Ordner).

Ein Volume ist ein Ordner ``<mount>/<volume_dir>`` (Standard ``foto-archiv``) mit einer
Markierungsdatei ``volume.json`` (id, label, layout). Die Erkennung läuft über alle lokalen
Laufwerke sowie ``cfg.hdd_paths``; gesucht wird unter ``cfg.volume_dir`` und ``foto-archiv``.

``layout`` beschreibt das Ablageformat: 1 = Pakete direkt unter ``<Jahr>/…`` (vor Sammlungen),
2 = ``<archive_dir>/<Jahr>/…``. Volumes im alten Format werden erst nach ``hdd migrate`` benutzt.
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
LAYOUT = 2


class VolumeError(Exception):
    pass


@dataclass
class Volume:
    id: str
    label: str
    root: Path  # <mount>/foto-archiv
    layout: int = LAYOUT

    @property
    def outdated(self) -> bool:
        return self.layout < LAYOUT

    def path_for(self, relpath: str) -> Path:
        return self.root.joinpath(*relpath.split("/"))

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free

    def usage(self) -> tuple[int, int, int]:
        u = shutil.disk_usage(self.root)
        return u.total, u.used, u.free

    def filesystem(self) -> str | None:
        return filesystem_type(self.root)

    def __str__(self) -> str:
        return f"{self.label} ({self.root})"


def filesystem_type(path: Path) -> str | None:
    """Dateisystem des Laufwerks (``NTFS``, ``exFAT``, ``FAT32`` …); None = unbekannt."""
    if sys.platform != "win32":
        return None
    import ctypes

    k32 = ctypes.windll.kernel32
    vol = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(str(Path(path).resolve()), vol, len(vol)):
        return None
    fs = ctypes.create_unicode_buffer(64)
    if not k32.GetVolumeInformationW(vol.value, None, 0, None, None, None, fs, len(fs)):
        return None
    return fs.value or None


def is_fat(fs: str | None) -> bool:
    """FAT12/16/32 (nicht exFAT): Dateien höchstens 4 GiB - 1."""
    return bool(fs) and fs.upper().startswith("FAT")


FAT_MAX_FILE = 4 * 1024**3 - 1


def _read_marker(root: Path) -> Volume | None:
    marker = root / MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        return Volume(id=data["id"], label=data["label"], root=root, layout=int(data.get("layout", 1)))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write_marker(vol: Volume, **changes) -> None:
    marker = vol.root / MARKER
    data = json.loads(marker.read_text(encoding="utf-8"))
    if data.get("id") != vol.id:
        raise VolumeError(f"{marker} gehört nicht zu Volume {vol.id}.")
    data.update(changes)
    tmp = marker.with_name(MARKER + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, marker)


def _dir_names(cfg: Config | None) -> list[str]:
    names = [cfg.volume_dir] if cfg is not None and cfg.volume_dir else []
    return names + [n for n in (VOLUME_DIR,) if n not in names]


def init_volume(mount: Path, label: str, db: Index, dir_name: str = VOLUME_DIR) -> Volume:
    root = Path(mount) / dir_name
    existing = _read_marker(root)
    if existing:
        raise VolumeError(f"{root} ist bereits als Volume '{existing.label}' ({existing.id}) eingerichtet.")
    if db.q1("SELECT 1 FROM volumes WHERE label=? AND kind='hdd'", (label,)):
        raise VolumeError(f"Label '{label}' ist bereits vergeben.")
    root.mkdir(parents=True, exist_ok=True)
    vol = Volume(id=str(uuid.uuid4()), label=label, root=root, layout=LAYOUT)
    (root / MARKER).write_text(
        json.dumps({"id": vol.id, "label": label, "created": utcnow(), "tool": "foto-archiv", "layout": LAYOUT}, indent=2),
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
    data = json.loads((vol.root / MARKER).read_text(encoding="utf-8"))
    _write_marker(vol, label=new_label, previous_labels=[*data.get("previous_labels", []), vol.label])
    db.register_volume(vol.id, "hdd", new_label, str(vol.root))
    return Volume(id=vol.id, label=new_label, root=vol.root, layout=vol.layout)


def set_layout(vol: Volume, layout: int) -> Volume:
    _write_marker(vol, layout=layout)
    return Volume(id=vol.id, label=vol.label, root=vol.root, layout=layout)


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


def _roots_of(mount: Path, names: list[str]):
    """Mögliche Volume-Ordner eines Laufwerks: bekannte Namen, der Pfad selbst, dann jeder
    Unterordner der obersten Ebene (so bleibt ein umbenannter Archivordner auffindbar)."""
    yield from (mount / n for n in names)
    yield mount  # hdd_paths dürfen auch direkt auf .../foto-archiv zeigen
    try:
        subdirs = sorted(e for e in mount.iterdir() if e.is_dir() and e.name not in names)
    except OSError:
        return
    yield from subdirs


def _find_all(cfg: Config, db: Index | None) -> list[Volume]:
    found: dict[str, Volume] = {}
    names = _dir_names(cfg)
    for m in _candidate_mounts(cfg):
        for root in _roots_of(m, names):
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


def find_connected(cfg: Config, db: Index | None = None, include_outdated: bool = False) -> list[Volume]:
    """Angeschlossene Volumes; solche im alten Ablageformat nur mit ``include_outdated``."""
    return [v for v in _find_all(cfg, db) if include_outdated or not v.outdated]


def _outdated_error(v: Volume) -> VolumeError:
    return VolumeError(
        f"Volume '{v.label}' ({v.root}) hat noch das alte Ablageformat - "
        f"zuerst 'foto-archiv hdd migrate {v.label}' ausführen."
    )


def resolve_volume(cfg: Config, db: Index, spec: str | None, allow_outdated: bool = False) -> Volume:
    """Volume per Label, ID oder Pfad. Ohne Angabe: das einzige angeschlossene."""
    def check(v: Volume) -> Volume:
        if v.outdated and not allow_outdated:
            raise _outdated_error(v)
        return v

    all_vols = _find_all(cfg, db)
    if spec:
        p = Path(spec)
        if p.exists():
            for root in [p / n for n in _dir_names(cfg)] + [p]:
                v = _read_marker(root)
                if v:
                    db.register_volume(v.id, "hdd", v.label, str(root))
                    return check(v)
        for v in all_vols:
            if spec in (v.label, v.id):
                return check(v)
        raise VolumeError(f"Volume '{spec}' ist nicht angeschlossen bzw. unbekannt.")
    usable = [v for v in all_vols if allow_outdated or not v.outdated]
    if len(usable) == 1:
        return usable[0]
    if not usable:
        old = [v for v in all_vols if v.outdated]
        if old:
            raise _outdated_error(old[0])
        raise VolumeError("Kein Archiv-Volume angeschlossen (einrichten mit: foto-archiv hdd init <Laufwerk> --label A).")
    labels = ", ".join(v.label for v in usable)
    raise VolumeError(f"Mehrere Volumes angeschlossen ({labels}) - bitte mit --volume wählen.")
