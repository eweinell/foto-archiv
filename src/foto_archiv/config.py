"""Konfiguration (TOML) des Foto-Archivs."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import platformdirs

APP_NAME = "foto-archiv"

DEFAULT_EXCLUDE_NAMES = [
    "Thumbs.db",
    "thumbnails-digikam.db",
    "desktop.ini",
    ".directory",
    "*.dtrashinfo",
    ".DS_Store",
    "._*",
    "*.foto-archiv-partial",
]
DEFAULT_EXCLUDE_DIRS = [".dtrash", "@eaDir", ".thumbnails", "$RECYCLE.BIN", "*.foto-archiv-restore"]
DEFAULT_CATALOG_FILES = ["digikam4.db", "recognition.db", "similarity.db"]

DEFAULT_COLLECTION = "fotos"
PROFILES = ("photo", "media")  # photo: RAW/Export-Abgleich; media: nur nach Dateiendung
YEAR_MODES = ("name", "none")  # name: Jahr aus dem Ordnernamen (sonst _ohne-jahr); none: keine Jahresebene

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_DIR_RE = re.compile(r"^[^/\\:*?\"<>|_.][^/\\:*?\"<>|]{0,63}$")
_YEARLIKE_RE = re.compile(r"^\d{4}$")


class ConfigError(Exception):
    pass


def normalize_exclude_path(p: str) -> str:
    """``\\`` → ``/``, führende ``./`` und ``/`` sowie abschließende ``/`` entfernen."""
    s = str(p).replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    s = s.strip("/")
    return "/".join(seg for seg in s.split("/") if seg not in ("", "."))


def default_config_path() -> Path:
    env = os.environ.get("FOTO_ARCHIV_CONFIG")
    if env:
        return Path(env)
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False)) / "config.toml"


def default_data_dir() -> Path:
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


@dataclass
class S3Config:
    bucket: str = ""
    prefix: str = ""  # Archivwurzel im Bucket, z. B. "archiv/" (mit abschließendem /); leer = Bucket-Wurzel
    profile: str = "foto-archiv"  # AWS-CLI-Profil; leer = Standardkette (Env-Variablen usw.)
    region: str = "eu-west-1"
    storage_class: str = "DEEP_ARCHIVE"
    part_size_mb: int = 64
    concurrency: int = 4
    upload_limit: str = ""  # Upload-Bandbreite, z. B. "2MB/s" oder "16Mbit"; leer = unbegrenzt
    # Zeitfenster, z. B. ["07:00-23:00=2MB/s", "23:00-07:00=0"]; erstes passendes gilt, sonst upload_limit
    upload_schedule: list[str] = field(default_factory=list)
    restore_tier: str = "Bulk"  # Bulk | Standard
    restore_days: int = 7
    endpoint_url: str = ""  # nur für Tests (z. B. moto-Server)

    @property
    def volume_id(self) -> str:
        return f"s3:{self.bucket}/{self.prefix}"


@dataclass
class SlimConfig:
    classes: list[str] = field(default_factory=lambda: ["raw"])
    min_hdd_copies: int = 1
    require_s3: bool = True


@dataclass
class Collection:
    """Eine Sammlung: ein Quellordner mit eigenem Ordner im Archiv (HDD und S3)."""

    id: str  # stabil, steht im Index vor jedem Pfad ("fotos/2026.Kenia/…") - nicht mehr ändern
    source_root: Path
    archive_dir: str = ""  # Ordnername im Archiv; leer = id. Änderung gilt nur für neue Versionen.
    profile: str = "photo"
    year: str = "name"
    settle_days: int | None = None  # leer = globaler Wert
    exclude_names: list[str] = field(default_factory=list)  # zusätzlich zu den globalen Mustern
    exclude_dirs: list[str] = field(default_factory=list)
    # Pfade relativ zum Quellordner ("tmp", "2024.Hochzeit/veraltet", "*/proxy"); samt Inhalt ausgeschlossen
    exclude_paths: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.source_root = Path(self.source_root).expanduser()
        if not self.archive_dir:
            self.archive_dir = self.id
        self.exclude_paths = [normalize_exclude_path(p) for p in self.exclude_paths]

    def source_path(self, rel: str) -> Path:
        return self.source_root.joinpath(*rel.split("/")) if rel else self.source_root


@dataclass
class Config:
    index_path: Path
    source_root: Path | None = None  # nur noch für die implizite Sammlung "fotos" (alte Konfigurationen)
    chunk_size_mb: int = 4000  # < 4 GiB -> FAT32-tauglich
    settle_days: int = 60
    hdd_paths: list[str] = field(default_factory=list)  # zusätzliche Volume-Mounts (Ordner)
    volume_dir: str = "foto-archiv"  # Ordnername des Archivs auf neu eingerichteten HDDs
    exclude_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES))
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    catalog_files: list[str] = field(default_factory=lambda: list(DEFAULT_CATALOG_FILES))
    collections: list[Collection] = field(default_factory=list)
    slim: SlimConfig = field(default_factory=SlimConfig)
    s3: S3Config = field(default_factory=S3Config)
    path: Path | None = None  # woher die Konfiguration geladen wurde
    collections_explicit: bool = False  # [[collections]] stand in der Datei

    def __post_init__(self) -> None:
        self.index_path = Path(self.index_path)
        if self.collections:
            self.collections = [c if isinstance(c, Collection) else Collection(**c) for c in self.collections]
        else:
            root = Path(self.source_root).expanduser() if self.source_root else _default_source_root()
            self.collections = [Collection(id=DEFAULT_COLLECTION, source_root=root)]
        if self.source_root is None:
            self.source_root = self.collections[0].source_root

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024

    # ------------------------------------------------------------------ Sammlungen
    def find_collection(self, cid: str) -> Collection | None:
        return next((c for c in self.collections if c.id == cid), None)

    def collection(self, cid: str) -> Collection:
        c = self.find_collection(cid)
        if c is None:
            known = ", ".join(c.id for c in self.collections)
            raise ConfigError(f"Sammlung '{cid}' ist nicht konfiguriert (bekannt: {known}).")
        return c

    def collection_of(self, vpath: str) -> Collection:
        """Sammlung zu einem Index-Pfad bzw. einer Serie (``<sammlung>/<rest>``)."""
        return self.collection(vpath.partition("/")[0])

    def source_file(self, vpath: str) -> Path:
        cid, _, rel = vpath.partition("/")
        return self.collection(cid).source_path(rel)

    def settle_days_for(self, cid: str) -> int:
        c = self.find_collection(cid)
        return c.settle_days if c is not None and c.settle_days is not None else self.settle_days

    def catalog_root(self) -> Path | None:
        """Ordner der Katalog-Datenbanken: Wurzel der ersten Foto-Sammlung."""
        return next((c.source_root for c in self.collections if c.profile == "photo"), None)

    def validate(self) -> None:
        ids: set[str] = set()
        dirs: set[str] = set()
        roots: dict[str, str] = {}
        for c in self.collections:
            if not _ID_RE.match(c.id):
                raise ConfigError(f"Sammlung '{c.id}': id nur aus a-z, 0-9, - und _ (max. 32 Zeichen).")
            if c.id in ids:
                raise ConfigError(f"Sammlung '{c.id}' ist doppelt konfiguriert.")
            ids.add(c.id)
            if not _DIR_RE.match(c.archive_dir) or _YEARLIKE_RE.match(c.archive_dir):
                raise ConfigError(
                    f"Sammlung '{c.id}': archive_dir '{c.archive_dir}' ungültig "
                    "(ein Ordnername, nicht mit _ oder . beginnend, keine Jahreszahl)."
                )
            if c.archive_dir.lower() in dirs:
                raise ConfigError(f"archive_dir '{c.archive_dir}' wird von mehreren Sammlungen verwendet.")
            dirs.add(c.archive_dir.lower())
            if c.profile not in PROFILES:
                raise ConfigError(f"Sammlung '{c.id}': profile muss {' oder '.join(PROFILES)} sein.")
            if c.year not in YEAR_MODES:
                raise ConfigError(f"Sammlung '{c.id}': year muss {' oder '.join(YEAR_MODES)} sein.")
            key = os.path.normcase(os.path.abspath(c.source_root))
            if key in roots:
                raise ConfigError(f"Sammlungen '{roots[key]}' und '{c.id}' haben denselben Quellordner.")
            roots[key] = c.id
            for p in c.exclude_paths:
                if not p or ".." in p.split("/") or ":" in p or "**" in p:
                    raise ConfigError(
                        f"Sammlung '{c.id}': exclude_paths-Eintrag '{p}' ungültig "
                        "(Pfad relativ zum Quellordner, z. B. \"tmp\" oder \"2024.Hochzeit/veraltet\"; * nur innerhalb eines Ordnernamens)."
                    )
        if self.s3.prefix and not self.s3.prefix.endswith("/"):
            raise ConfigError("[s3] prefix muss mit / enden (oder leer sein).")


def _default_source_root() -> Path:
    return Path.home() / "Pictures"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    data: dict = {}
    if path.exists():
        try:
            # utf-8-sig: unter Windows gespeicherte Dateien beginnen oft mit einer BOM
            data = tomllib.loads(path.read_bytes().decode("utf-8-sig"))
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            raise ConfigError(f"{path}: {e}") from None
    s3 = S3Config(**data.get("s3", {}))
    slim = SlimConfig(**data.get("slim", {}))
    raw_cols = data.get("collections", [])
    top = {k: v for k, v in data.items() if k not in ("s3", "slim", "collections")}
    source_root = top.pop("source_root", None)
    index_path = Path(top.pop("index_path", default_data_dir() / "index.sqlite")).expanduser()
    try:
        collections = [Collection(**c) for c in raw_cols]
    except TypeError as e:
        raise ConfigError(f"[[collections]]: {e}") from None
    cfg = Config(
        index_path=index_path,
        source_root=Path(source_root).expanduser() if source_root else None,
        collections=collections,
        slim=slim,
        s3=s3,
        path=path,
        collections_explicit=bool(collections),
        **top,
    )
    cfg.validate()
    return cfg


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v).replace("\\", "/").replace('"', '\\"')
    return f'"{s}"'


def render_config(cfg: Config) -> str:
    lines = [
        "# foto-archiv Konfiguration",
        f"index_path = {_toml_value(cfg.index_path)}",
        "# Paketgröße der Tars in MiB (< 4096 für FAT32)",
        f"chunk_size_mb = {cfg.chunk_size_mb}",
        "# Serie gilt als abgeschlossen, wenn so viele Tage unverändert (je Sammlung überschreibbar)",
        f"settle_days = {cfg.settle_days}",
        "# zusätzliche Archiv-Volumes (Ordner), neben automatisch erkannten Laufwerken",
        f"hdd_paths = {_toml_value(cfg.hdd_paths)}",
        "# Ordnername des Archivs auf neu eingerichteten HDDs (vorhandene werden über volume.json erkannt)",
        f"volume_dir = {_toml_value(cfg.volume_dir)}",
        f"exclude_names = {_toml_value(cfg.exclude_names)}",
        f"exclude_dirs = {_toml_value(cfg.exclude_dirs)}",
        f"catalog_files = {_toml_value(cfg.catalog_files)}",
        "",
        "[slim]",
        "# Klassen, die lokal entfernt werden dürfen, und nötige geprüfte Kopien",
        f"classes = {_toml_value(cfg.slim.classes)}",
        f"min_hdd_copies = {cfg.slim.min_hdd_copies}",
        f"require_s3 = {_toml_value(cfg.slim.require_s3)}",
        "",
        "[s3]",
        f"bucket = {_toml_value(cfg.s3.bucket)}",
        "# Archivwurzel im Bucket (leer = Bucket-Wurzel); darunter liegt je Sammlung ihr archive_dir",
        f"prefix = {_toml_value(cfg.s3.prefix)}",
        f"profile = {_toml_value(cfg.s3.profile)}",
        f"region = {_toml_value(cfg.s3.region)}",
        f"storage_class = {_toml_value(cfg.s3.storage_class)}",
        f"part_size_mb = {cfg.s3.part_size_mb}",
        f"concurrency = {cfg.s3.concurrency}",
        "# Upload-Bandbreite begrenzen, z. B. \"2MB/s\", \"16Mbit\", \"500k\" (reine Zahl = MB/s); leer = unbegrenzt",
        f"upload_limit = {_toml_value(cfg.s3.upload_limit)}",
        "# Zeitfenster (lokale Uhrzeit), z. B. [\"07:00-23:00=2MB/s\", \"23:00-07:00=0\"]; 0 = unbegrenzt.",
        "# Das erste passende Fenster gilt, außerhalb aller Fenster upload_limit. Wechsel greifen auch mitten im Upload.",
        f"upload_schedule = {_toml_value(cfg.s3.upload_schedule)}",
        f"restore_tier = {_toml_value(cfg.s3.restore_tier)}",
        f"restore_days = {cfg.s3.restore_days}",
    ]
    if cfg.s3.endpoint_url:
        lines.append(f"endpoint_url = {_toml_value(cfg.s3.endpoint_url)}")
    lines += [
        "",
        "# Sammlungen: je Quellordner ein Ordner im Archiv (HDD und S3 unter <prefix><archive_dir>/).",
        "# id steht im Index und bleibt fest; archive_dir lässt sich ändern (gilt für neue Versionen).",
        "# profile: photo (RAW/Export-Abgleich) | media (nur Dateiendung, z. B. Videos, Musik)",
        "# year: name (Jahresebene aus dem Ordnernamen, sonst _ohne-jahr) | none (keine Jahresebene)",
        "# exclude_paths: Pfade relativ zum Quellordner, samt Inhalt ausgeschlossen, z. B. [\"tmp\", \"2024.Hochzeit/veraltet\"];",
        "#   * gilt innerhalb eines Namens (\"*/veraltet\" = in jeder Serie). exclude_dirs/exclude_names: Namen in jeder Tiefe.",
    ]
    for c in cfg.collections:
        lines += [
            "",
            "[[collections]]",
            f"id = {_toml_value(c.id)}",
            f"source_root = {_toml_value(c.source_root)}",
            f"archive_dir = {_toml_value(c.archive_dir)}",
            f"profile = {_toml_value(c.profile)}",
            f"year = {_toml_value(c.year)}",
        ]
        if c.settle_days is not None:
            lines.append(f"settle_days = {c.settle_days}")
        if c.exclude_names:
            lines.append(f"exclude_names = {_toml_value(c.exclude_names)}")
        if c.exclude_dirs:
            lines.append(f"exclude_dirs = {_toml_value(c.exclude_dirs)}")
        if c.exclude_paths:
            lines.append(f"exclude_paths = {_toml_value(c.exclude_paths)}")
    return "\n".join(lines) + "\n"


def write_config(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(cfg), encoding="utf-8")
