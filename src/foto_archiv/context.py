"""Gemeinsamer Laufzeitkontext aller Befehle."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from .config import Config
from .db import Index


@dataclass
class App:
    cfg: Config
    db: Index
    console: Console = field(default_factory=Console)

    def byte_progress(self) -> Progress:
        return Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            DownloadColumn(binary_units=True),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=self.console,
            transient=False,
        )


def human(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
