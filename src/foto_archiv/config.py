"""Konfiguration (TOML) des Foto-Archivs."""

from __future__ import annotations

import os
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
    prefix: str = ""  # optionaler Key-Präfix, z. B. "archiv/" (mit abschließendem /)
    profile: str = "foto-archiv"  # AWS-CLI-Profil; leer = Standardkette (Env-Variablen usw.)
    region: str = "eu-west-1"
    storage_class: str = "DEEP_ARCHIVE"
    part_size_mb: int = 64
    concurrency: int = 4
    upload_limit: str = ""  # Upload-Bandbreite, z. B. "2MB/s" oder "16Mbit"; leer = unbegrenzt
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
class Config:
    source_root: Path
    index_path: Path
    chunk_size_mb: int = 4000  # < 4 GiB -> FAT32-tauglich
    settle_days: int = 60
    hdd_paths: list[str] = field(default_factory=list)  # zusätzliche Volume-Mounts (Ordner)
    exclude_names: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_NAMES))
    exclude_dirs: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDE_DIRS))
    catalog_files: list[str] = field(default_factory=lambda: list(DEFAULT_CATALOG_FILES))
    slim: SlimConfig = field(default_factory=SlimConfig)
    s3: S3Config = field(default_factory=S3Config)
    path: Path | None = None  # woher die Konfiguration geladen wurde

    @property
    def chunk_size_bytes(self) -> int:
        return self.chunk_size_mb * 1024 * 1024


def _default_source_root() -> Path:
    return Path.home() / "Pictures"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    data: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    s3 = S3Config(**data.get("s3", {}))
    slim = SlimConfig(**data.get("slim", {}))
    top = {k: v for k, v in data.items() if k not in ("s3", "slim")}
    source_root = Path(top.pop("source_root", _default_source_root())).expanduser()
    index_path = Path(top.pop("index_path", default_data_dir() / "index.sqlite")).expanduser()
    cfg = Config(source_root=source_root, index_path=index_path, slim=slim, s3=s3, path=path, **top)
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
        f"source_root = {_toml_value(cfg.source_root)}",
        f"index_path = {_toml_value(cfg.index_path)}",
        "# Paketgröße der Tars in MiB (< 4096 für FAT32)",
        f"chunk_size_mb = {cfg.chunk_size_mb}",
        "# Serie gilt als abgeschlossen, wenn so viele Tage unverändert",
        f"settle_days = {cfg.settle_days}",
        "# zusätzliche Archiv-Volumes (Ordner), neben automatisch erkannten Laufwerken",
        f"hdd_paths = {_toml_value(cfg.hdd_paths)}",
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
        f"prefix = {_toml_value(cfg.s3.prefix)}",
        f"profile = {_toml_value(cfg.s3.profile)}",
        f"region = {_toml_value(cfg.s3.region)}",
        f"storage_class = {_toml_value(cfg.s3.storage_class)}",
        f"part_size_mb = {cfg.s3.part_size_mb}",
        f"concurrency = {cfg.s3.concurrency}",
        "# Upload-Bandbreite begrenzen, z. B. \"2MB/s\", \"16Mbit\", \"500k\" (reine Zahl = MB/s); leer = unbegrenzt",
        f"upload_limit = {_toml_value(cfg.s3.upload_limit)}",
        f"restore_tier = {_toml_value(cfg.s3.restore_tier)}",
        f"restore_days = {cfg.s3.restore_days}",
    ]
    if cfg.s3.endpoint_url:
        lines.append(f"endpoint_url = {_toml_value(cfg.s3.endpoint_url)}")
    return "\n".join(lines) + "\n"


def write_config(cfg: Config, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_config(cfg), encoding="utf-8")
