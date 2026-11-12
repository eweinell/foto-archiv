"""Kommandozeile: ``foto-archiv <befehl>``."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import hddops, s3ops
from .config import default_config_path, load_config, render_config, write_config
from .context import App, human
from .db import Index
from .lock import IndexLock, LockedError
from .scan import hash_missing, scan
from .seal import SealError, export_index, seal_series
from .status import compute_status, render_series_detail, render_status
from .volumes import VolumeError, find_connected, init_volume, relabel_volume, resolve_volume

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Foto-Archiv: Serien versiegeln, auf HDD und S3 Deep Archive sichern.")
hdd_app = typer.Typer(no_args_is_help=True, help="Archiv-Volumes (HDDs) verwalten.")
verify_app = typer.Typer(no_args_is_help=True, help="Kopien prüfen.")
legacy_app = typer.Typer(no_args_is_help=True, help="Altbestand im Bucket.")
catalog_app = typer.Typer(no_args_is_help=True, help="Katalog-Datenbanken (digiKam) sichern.")
app.add_typer(hdd_app, name="hdd")
app.add_typer(verify_app, name="verify")
app.add_typer(legacy_app, name="legacy")
app.add_typer(catalog_app, name="catalog")

console = Console()
_state: dict = {"config": None}


@app.callback()
def _main(config: Annotated[Optional[Path], typer.Option("--config", "-c", help="Pfad zur config.toml")] = None):
    _state["config"] = config


def get_app(lock: bool = True) -> App:
    """Kontext laden; schreibende Befehle sperren den Index exklusiv (kein Parallelbetrieb)."""
    cfg = load_config(_state["config"])
    if lock:
        try:
            lk = IndexLock(cfg.index_path).acquire()
        except LockedError as e:
            _fail(str(e))
        atexit.register(lk.release)
    return App(cfg=cfg, db=Index(cfg.index_path), console=console)


def _fail(msg: str) -> None:
    console.print(f"[red]Fehler:[/] {msg}")
    raise typer.Exit(1)


SeriesArg = Annotated[Optional[list[str]], typer.Argument(help="Serien (Ordnernamen); leer = alle")]
VolumeOpt = Annotated[Optional[str], typer.Option("--volume", "-v", help="Volume (Label, ID oder Pfad)")]
DryRun = Annotated[bool, typer.Option("--dry-run", "-n", help="nur anzeigen, nichts ändern")]
ClassOpt = Annotated[Optional[list[str]], typer.Option("--class", help="Klassen: raw, img-derived, img-original, video, other")]


# --------------------------------------------------------------------------- Konfiguration
@app.command()
def init(
    source: Annotated[Optional[Path], typer.Option(help="Foto-Ordner (Standard: ~/Pictures)")] = None,
    bucket: Annotated[str, typer.Option(help="S3-Bucket")] = "",
    prefix: Annotated[str, typer.Option(help="Key-Präfix im Bucket")] = "fotos/",
    profile: Annotated[str, typer.Option(help="AWS-Profil")] = "foto-archiv",
    force: Annotated[bool, typer.Option(help="vorhandene Konfiguration überschreiben")] = False,
):
    """Konfigurationsdatei anlegen."""
    path = _state["config"] or default_config_path()
    if path.exists() and not force:
        _fail(f"{path} existiert bereits (--force zum Überschreiben).")
    cfg = load_config(Path("__nicht_vorhanden__.toml"))
    if source:
        cfg.source_root = source.expanduser().resolve()
    cfg.s3.bucket, cfg.s3.prefix, cfg.s3.profile = bucket, prefix, profile
    write_config(cfg, path)
    console.print(f"[green]Konfiguration geschrieben:[/] {path}")
    console.print(render_config(cfg), markup=False, highlight=False)


@app.command("config")
def show_config():
    """Aktive Konfiguration anzeigen."""
    a = get_app(lock=False)
    console.print(f"[bold]Datei:[/] {a.cfg.path}  ({'vorhanden' if a.cfg.path and a.cfg.path.exists() else 'Standardwerte'})")
    console.print(render_config(a.cfg), markup=False, highlight=False)


# --------------------------------------------------------------------------- Bestand
@app.command("scan")
def scan_cmd(
    do_hash: Annotated[bool, typer.Option("--hash", help="fehlende Prüfsummen berechnen (liest alle neuen Dateien)")] = False,
):
    """Foto-Ordner erfassen und Dateien klassifizieren (nur lesend)."""
    a = get_app()
    st = scan(a, do_hash=do_hash)
    console.print(
        f"{st.files} Dateien, {human(st.bytes)} in {len(st.series)} Serien  "
        f"(neu {st.new}, geändert {st.changed}, verschwunden {st.gone}, ausgeschlossen {st.excluded})"
    )


@app.command("hash")
def hash_cmd(series: SeriesArg = None):
    """Fehlende Prüfsummen lokaler Dateien berechnen."""
    a = get_app()
    n = hash_missing(a, series)
    console.print(f"{n} Dateien gehasht.")


@app.command()
def status(
    series: SeriesArg = None,
    state: Annotated[Optional[str], typer.Option(help="nur Serien in diesem Stand (aktiv, abschlussreif, versiegelt, geändert)")] = None,
):
    """Übersicht je Serie (bzw. Details zu einer Serie)."""
    a = get_app(lock=False)
    rows = compute_status(a, series)
    if state:
        rows = [r for r in rows if r.state == state]
    if series and len(rows) == 1:
        render_series_detail(a, rows[0])
    else:
        render_status(a, rows)


# --------------------------------------------------------------------------- HDD
@hdd_app.command("init")
def hdd_init(mount: Path, label: Annotated[str, typer.Option("--label", "-l", help="z. B. A oder B")]):
    """Laufwerk/Ordner als Archiv-Volume einrichten (legt <mount>/foto-archiv an)."""
    a = get_app()
    try:
        v = init_volume(mount, label, a.db)
    except VolumeError as e:
        _fail(str(e))
    console.print(f"[green]Volume '{v.label}' eingerichtet:[/] {v.root}  (id {v.id})")


@hdd_app.command("relabel")
def hdd_relabel(
    volume: Annotated[str, typer.Argument(help="Volume (altes Label, ID oder Pfad); muss angeschlossen sein")],
    new_label: Annotated[str, typer.Argument(help="neues Label")],
):
    """Label eines Volumes ändern (Kopien und Prüfstatus bleiben erhalten)."""
    a = get_app()
    try:
        v = resolve_volume(a.cfg, a.db, volume)
        old = v.label
        v = relabel_volume(v, new_label, a.db)
    except VolumeError as e:
        _fail(str(e))
    export_index(a, v)
    console.print(f"[green]Volume umbenannt:[/] {old} → {v.label}  ({v.root})")


@hdd_app.command("list")
def hdd_list():
    """Bekannte und angeschlossene Volumes."""
    a = get_app(lock=False)
    connected = {v.id: v for v in find_connected(a.cfg, a.db)}
    t = Table("Label", "Art", "angeschlossen", "Pakete", "Größe", "frei", "zuletzt gesehen", header_style="bold")
    for r in a.db.q("SELECT * FROM volumes ORDER BY kind, label"):
        agg = a.db.q1(
            "SELECT COUNT(*) n, COALESCE(SUM(b.size),0) s FROM copies c JOIN bundles b ON b.id=c.bundle_id WHERE c.volume_id=? AND c.status='ok'",
            (r["id"],),
        )
        v = connected.get(r["id"])
        free = ""
        if v:
            total, used, fr = v.usage()
            free = f"{human(fr)} ({used / total:.0%} belegt)"
        t.add_row(r["label"], r["kind"], f"[green]{v.root}" if v else "", str(agg["n"]), human(agg["s"]), free, r["last_seen"] or "")
    console.print(t)


# --------------------------------------------------------------------------- Versiegeln
@app.command()
def seal(
    series: SeriesArg = None,
    ripe: Annotated[bool, typer.Option("--ripe", help="alle abschlussreifen und geänderten Serien")] = False,
    volume: VolumeOpt = None,
    no_verify: Annotated[bool, typer.Option("--no-verify", help="Pakete nach dem Schreiben nicht erneut lesen")] = False,
    dry_run: DryRun = False,
):
    """Serien als neue Version auf ein HDD-Volume packen (Tars je Klasse + Manifest)."""
    a = get_app()
    if not series and not ripe:
        _fail("Serie(n) angeben oder --ripe verwenden.")
    targets = list(series or [])
    if ripe:
        targets += [s.name for s in compute_status(a) if s.state in ("abschlussreif", "geändert") and s.name not in targets]
    if not targets:
        console.print("Nichts zu versiegeln.")
        return
    try:
        vol = resolve_volume(a.cfg, a.db, volume)
    except VolumeError as e:
        _fail(str(e))
    errors = 0
    for s in targets:
        try:
            seal_series(a, s, vol, verify=not no_verify, dry_run=dry_run)
        except (SealError, OSError) as e:
            errors += 1
            console.print(f"[red]{s}: {e}")
    if not dry_run:
        export_index(a, vol)
    if errors:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- Kopieren / Prüfen
@app.command("copy-hdd")
def copy_hdd_cmd(
    src: Annotated[str, typer.Option("--from", help="Quell-Volume")],
    dst: Annotated[str, typer.Option("--to", help="Ziel-Volume")],
    series: SeriesArg = None,
    dry_run: DryRun = False,
):
    """Fehlende Pakete von einem Volume auf ein anderes kopieren und prüfen."""
    a = get_app()
    try:
        s, d = resolve_volume(a.cfg, a.db, src), resolve_volume(a.cfg, a.db, dst)
    except VolumeError as e:
        _fail(str(e))
    hddops.copy_hdd(a, s, d, series, dry_run)


@verify_app.command("hdd")
def verify_hdd_cmd(
    volume: VolumeOpt = None,
    series: SeriesArg = None,
    quick: Annotated[bool, typer.Option("--quick", help="nur Existenz und Größe prüfen")] = False,
):
    """Pakete auf einem Volume gegen den Index prüfen (SHA-256)."""
    a = get_app()
    try:
        v = resolve_volume(a.cfg, a.db, volume)
    except VolumeError as e:
        _fail(str(e))
    hddops.verify_hdd(a, v, series, quick)


@verify_app.command("s3")
def verify_s3_cmd(
    series: SeriesArg = None,
    deep: Annotated[bool, typer.Option("--deep", help="auch gespeicherte Prüfsummen vergleichen")] = False,
):
    """Pakete in S3 gegen den Index prüfen (Existenz, Größe, Prüfsumme)."""
    s3ops.verify_s3(get_app(), series, deep)


# --------------------------------------------------------------------------- S3
@app.command("push-s3")
def push_s3_cmd(series: SeriesArg = None, volume: VolumeOpt = None, dry_run: DryRun = False):
    """Fehlende Pakete von einer HDD nach S3 (Deep Archive) hochladen."""
    a = get_app()
    src = None
    if volume:
        try:
            src = resolve_volume(a.cfg, a.db, volume)
        except VolumeError as e:
            _fail(str(e))
    s3ops.push_s3(a, series, src, dry_run)


# --------------------------------------------------------------------------- Ausdünnen
@app.command()
def slim(
    series: SeriesArg = None,
    cls: ClassOpt = None,
    apply: Annotated[bool, typer.Option("--apply", help="wirklich löschen (sonst nur Vorschau)")] = False,
):
    """Lokale Dateien (Standard: RAW) entfernen, die ausreichend archiviert sind."""
    hddops.slim(get_app(), series, cls, apply)


# --------------------------------------------------------------------------- Wiederherstellen
@app.command()
def restore(
    series: Annotated[str, typer.Argument(help="Serie")],
    cls: ClassOpt = None,
    source: Annotated[str, typer.Option("--from", help="hdd | s3 | auto")] = "auto",
    volume: VolumeOpt = None,
    target: Annotated[Optional[Path], typer.Option(help="Zielordner (Standard: source_root)")] = None,
    tier: Annotated[Optional[str], typer.Option(help="S3-Abruf: Bulk | Standard")] = None,
    overwrite: Annotated[bool, typer.Option(help="abweichende vorhandene Dateien überschreiben")] = False,
    dry_run: DryRun = False,
):
    """Serie (optional nur bestimmte Klassen) wiederherstellen - von HDD oder aus S3."""
    a = get_app()
    tgt = target or a.cfg.source_root
    if source in ("hdd", "auto"):
        try:
            vol = resolve_volume(a.cfg, a.db, volume)
        except VolumeError as e:
            if source == "hdd":
                _fail(str(e))
            vol = None
        if vol is not None:
            try:
                hddops.restore_hdd(a, series, cls, tgt, vol, overwrite, dry_run)
                return
            except hddops.OpFailed as e:
                res = e.result
                if source == "hdd" or res is None or not res.unavailable or res.hash_errors or res.errors:
                    raise
                console.print(f"[yellow]{len(res.unavailable)} Paket(e) fehlen auf {vol.label} - weiter mit S3 …")
    if source not in ("s3", "auto"):
        _fail("--from muss hdd, s3 oder auto sein.")
    s3ops.restore_s3(a, series, cls, tgt, tier, overwrite, dry_run)


# --------------------------------------------------------------------------- Altbestand / Kataloge
@legacy_app.command("scan")
def legacy_scan_cmd():
    """Nicht vom Tool erzeugte Objekte unter dem Präfix erfassen und auflisten."""
    s3ops.legacy_scan(get_app())


@catalog_app.command("backup")
def catalog_backup_cmd(
    volume: VolumeOpt = None,
    no_s3: Annotated[bool, typer.Option("--no-s3", help="nicht nach S3 hochladen")] = False,
):
    """digiKam-Datenbanken konsistent sichern (HDD und/oder S3 Standard)."""
    a = get_app()
    vol = None
    try:
        vol = resolve_volume(a.cfg, a.db, volume)
    except VolumeError as e:
        if volume:
            _fail(str(e))
        console.print(f"[yellow]{e} - nur S3.")
    s3ops.catalog_backup(a, vol, not no_s3)


def main() -> None:
    try:
        app()
    except (NotImplementedError,) as e:
        console.print(f"[red]Noch nicht implementiert.[/] {e}")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
