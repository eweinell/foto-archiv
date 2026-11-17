"""Kommandozeile: ``foto-archiv <befehl>``."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import hddops, s3ops
from .config import (
    DEFAULT_COLLECTION,
    Collection,
    ConfigError,
    default_config_path,
    load_config,
    render_config,
    write_config,
)
from .context import App, human
from .db import SCHEMA_VERSION, Index
from .lock import IndexLock, LockedError
from .migrate import MigrationError, migrate_index, migrate_volume, plan_index
from .scan import hash_missing, scan
from .seal import SealError, export_index, seal_series
from .status import compute_status, render_series_detail, render_status
from .volumes import (
    VolumeError,
    find_connected,
    init_volume,
    relabel_volume,
    resolve_volume,
)

app = typer.Typer(
    no_args_is_help=True, add_completion=False,
    help="Foto-Archiv: Serien je Sammlung (Fotos, Videos, Musik …) versiegeln, auf HDD und S3 Deep Archive sichern.",
)
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


def get_app(lock: bool = True, allow_old_index: bool = False) -> App:
    """Kontext laden; schreibende Befehle sperren den Index exklusiv (kein Parallelbetrieb)."""
    try:
        cfg = load_config(_state["config"])
    except ConfigError as e:
        _fail(f"Konfiguration: {e}")
    if lock:
        try:
            lk = IndexLock(cfg.index_path).acquire()
        except LockedError as e:
            _fail(str(e))
        atexit.register(lk.release)
    a = App(cfg=cfg, db=Index(cfg.index_path), console=console)
    if not allow_old_index and a.db.schema_version < SCHEMA_VERSION:
        _fail(
            "Der Index hat noch das Format vor den Sammlungen. Einmalig umstellen mit "
            "'foto-archiv migrate --dry-run' (Vorschau) und 'foto-archiv migrate'."
        )
    return a


def _fail(msg: str) -> None:
    console.print(f"[red]Fehler:[/] {msg}")
    raise typer.Exit(1)


def _known_series(a: App) -> list[str]:
    return sorted(
        {r["series"] for r in a.db.q("SELECT DISTINCT series FROM files")}
        | {r["series"] for r in a.db.q("SELECT DISTINCT series FROM versions")}
    )


def resolve_series(a: App, args: list[str] | None) -> list[str] | None:
    """Serienangaben auflösen: ``fotos/2026.Kenia``, ``2026.Kenia`` (wenn eindeutig) oder
    ``videos/`` bzw. ``videos`` (alle Serien einer Sammlung)."""
    if not args:
        return None
    known = _known_series(a)
    by_name: dict[str, list[str]] = {}
    for s in known:
        by_name.setdefault(s.partition("/")[2], []).append(s)
    out: list[str] = []
    for arg in args:
        arg = arg.strip().replace("\\", "/")
        if "/" in arg:
            cid, _, rest = arg.partition("/")
            if a.cfg.find_collection(cid) is None and not any(s.startswith(cid + "/") for s in known):
                _fail(f"Unbekannte Sammlung '{cid}' (konfiguriert: {', '.join(c.id for c in a.cfg.collections)}).")
            if rest in ("", "*"):
                out += [s for s in known if s.startswith(cid + "/")]
            else:
                out.append(f"{cid}/{rest}")
            continue
        matches = by_name.get(arg, [])
        if len(matches) > 1:
            _fail(f"'{arg}' gibt es in mehreren Sammlungen: {', '.join(matches)} - bitte mit Sammlung angeben.")
        if matches:
            out.append(matches[0])
        elif a.cfg.find_collection(arg) is not None:
            out += [s for s in known if s.startswith(arg + "/")]
        else:
            out.append(f"{a.cfg.collections[0].id}/{arg}")  # unbekannt -> Meldung beim Befehl selbst
    if not out:  # nie still zu "alle" werden lassen
        _fail(f"Keine Serien gefunden für: {', '.join(args)}")
    return list(dict.fromkeys(out))


def resolve_one(a: App, arg: str) -> str:
    res = resolve_series(a, [arg]) or []
    if len(res) != 1:
        _fail(f"'{arg}' bezeichnet {len(res)} Serien - bitte genau eine Serie angeben.")
    return res[0]


SeriesArg = Annotated[
    Optional[list[str]],
    typer.Argument(help="Serien: 2026.Kenia, fotos/2026.Kenia oder videos/ (ganze Sammlung); leer = alle"),
]
VolumeOpt = Annotated[Optional[str], typer.Option("--volume", "-v", help="Volume (Label, ID oder Pfad)")]
DryRun = Annotated[bool, typer.Option("--dry-run", "-n", help="nur anzeigen, nichts ändern")]
ClassOpt = Annotated[
    Optional[list[str]], typer.Option("--class", help="Klassen: raw, img-derived, img-original, video, audio, other")
]


# --------------------------------------------------------------------------- Konfiguration
@app.command()
def init(
    source: Annotated[Optional[Path], typer.Option(help="Foto-Ordner (Standard: ~/Pictures)")] = None,
    bucket: Annotated[str, typer.Option(help="S3-Bucket")] = "",
    prefix: Annotated[str, typer.Option(help="Archivwurzel im Bucket (leer = Bucket-Wurzel)")] = "",
    profile: Annotated[str, typer.Option(help="AWS-Profil")] = "foto-archiv",
    force: Annotated[bool, typer.Option(help="vorhandene Konfiguration überschreiben")] = False,
):
    """Konfigurationsdatei anlegen (mit der Sammlung 'fotos'; weitere in der Datei ergänzen)."""
    path = _state["config"] or default_config_path()
    if path.exists() and not force:
        _fail(f"{path} existiert bereits (--force zum Überschreiben).")
    cfg = load_config(Path("__nicht_vorhanden__.toml"))
    root = source.expanduser().resolve() if source else cfg.collections[0].source_root
    cfg.collections = [Collection(id=DEFAULT_COLLECTION, source_root=root)]
    cfg.s3.bucket, cfg.s3.prefix, cfg.s3.profile = bucket, prefix, profile
    try:
        cfg.validate()
    except ConfigError as e:
        _fail(str(e))
    write_config(cfg, path)
    console.print(f"[green]Konfiguration geschrieben:[/] {path}")
    console.print(render_config(cfg), markup=False, highlight=False)


@app.command("config")
def show_config():
    """Aktive Konfiguration anzeigen."""
    a = get_app(lock=False, allow_old_index=True)
    console.print(f"[bold]Datei:[/] {a.cfg.path}  ({'vorhanden' if a.cfg.path and a.cfg.path.exists() else 'Standardwerte'})")
    console.print(render_config(a.cfg), markup=False, highlight=False)


@app.command("collections")
def collections_cmd():
    """Konfigurierte Sammlungen und ihr Bestand."""
    a = get_app(lock=False)
    s3root = f"s3://{a.cfg.s3.bucket}/{a.cfg.s3.prefix}" if a.cfg.s3.bucket else ""
    t = Table("Sammlung", "Quellordner", "Archivordner", "Profil", "Jahr", "Serien", "lokal", "versiegelt", header_style="bold")
    for c in a.cfg.collections:
        loc = a.db.q1(
            "SELECT COUNT(DISTINCT series) n, COALESCE(SUM(size),0) s FROM files WHERE present=1 AND substr(path,1,?)=?",
            (len(c.id) + 1, c.id + "/"),
        )
        sealed = a.db.q1(
            "SELECT COALESCE(SUM(size),0) s FROM versions WHERE substr(series,1,?)=?", (len(c.id) + 1, c.id + "/")
        )
        root = str(c.source_root) if c.source_root.is_dir() else f"[yellow]{c.source_root} (fehlt)"
        t.add_row(
            c.id, root, f"{c.archive_dir}/", c.profile, "ja" if c.year == "name" else "nein",
            str(loc["n"]), human(loc["s"]), human(sealed["s"]),
        )
    console.print(t)
    for c in a.cfg.collections:
        rules = [*(f"{p}" for p in c.exclude_paths), *(f"{d}/ (überall)" for d in c.exclude_dirs),
                 *(f"{n} (überall)" for n in c.exclude_names)]
        if rules:
            console.print(f"[bold]{c.id}[/] ausgeschlossen: " + escape(", ".join(rules)), highlight=False)
    if s3root:
        console.print(f"S3: {s3root}<Archivordner>/ · HDD: <Laufwerk>\\{a.cfg.volume_dir}\\<Archivordner>\\")


# --------------------------------------------------------------------------- Bestand
@app.command("scan")
def scan_cmd(
    collection: Annotated[Optional[list[str]], typer.Argument(help="nur diese Sammlungen; leer = alle")] = None,
    do_hash: Annotated[bool, typer.Option("--hash", help="fehlende Prüfsummen berechnen (liest alle neuen Dateien)")] = False,
    show_excluded: Annotated[
        bool, typer.Option("--excluded", help="ausgeschlossene Ordner und exclude_paths-Treffer auflisten")
    ] = False,
):
    """Quellordner aller Sammlungen erfassen und Dateien klassifizieren (nur lesend)."""
    a = get_app()
    for cid in collection or []:
        if a.cfg.find_collection(cid) is None:
            _fail(f"Unbekannte Sammlung '{cid}'.")
    try:
        st = scan(a, do_hash=do_hash, only=collection)
    except FileNotFoundError as e:
        _fail(str(e))
    console.print(
        f"{st.files} Dateien, {human(st.bytes)} in {len(st.series)} Serien ({', '.join(st.collections)})  "
        f"(neu {st.new}, geändert {st.changed}, verschwunden {st.gone}, ausgeschlossen {st.excluded})"
    )
    if st.dropped:
        console.print(f"{st.dropped} nie archivierte, jetzt ausgeschlossene Einträge aus dem Index entfernt.")
    if show_excluded:
        for p in st.skipped:
            console.print(f"  [dim]ausgeschlossen:[/] {escape(p)}", highlight=False)


@app.command("hash")
def hash_cmd(series: SeriesArg = None):
    """Fehlende Prüfsummen lokaler Dateien berechnen."""
    a = get_app()
    n = hash_missing(a, resolve_series(a, series))
    console.print(f"{n} Dateien gehasht.")


@app.command()
def status(
    series: SeriesArg = None,
    state: Annotated[Optional[str], typer.Option(help="nur Serien in diesem Stand (aktiv, abschlussreif, versiegelt, geändert)")] = None,
):
    """Übersicht je Serie (bzw. Details zu einer Serie)."""
    a = get_app(lock=False)
    names = resolve_series(a, series)
    rows = compute_status(a, names)
    if state:
        rows = [r for r in rows if r.state == state]
    if names and len(rows) == 1:
        render_series_detail(a, rows[0])
    else:
        render_status(a, rows)


# --------------------------------------------------------------------------- HDD
@hdd_app.command("init")
def hdd_init(mount: Path, label: Annotated[str, typer.Option("--label", "-l", help="z. B. A oder B")]):
    """Laufwerk/Ordner als Archiv-Volume einrichten (legt <mount>/<volume_dir> an, Standard foto-archiv)."""
    a = get_app()
    try:
        v = init_volume(mount, label, a.db, a.cfg.volume_dir)
    except VolumeError as e:
        _fail(str(e))
    console.print(f"[green]Volume '{v.label}' eingerichtet:[/] {v.root}  (id {v.id})")
    fs = v.filesystem()
    if fs and fs.upper().startswith("FAT"):
        console.print(f"[yellow]Hinweis: {fs} erlaubt höchstens 4 GiB je Datei - für große Videos NTFS oder exFAT verwenden.")


def _migrate_volumes(a: App, vols, dry_run: bool, dirname: str | None = None) -> int:
    errors = 0
    for v in vols:
        try:
            nv, res = migrate_volume(a, v, dry_run=dry_run, dirname=dirname)
        except (MigrationError, VolumeError, OSError) as e:
            errors += 1
            console.print(f"[red]✗ {v.label}:[/] {e}")
            continue
        verb = "würde verschieben" if dry_run else "verschoben"
        console.print(f"[bold]{v.label}[/] ({v.root}): {len(res.moved)} Ordner {verb}")
        for m in res.moved:
            console.print(f"   {m}")
        if dry_run:
            continue
        if res.missing:
            errors += 1
            console.print(f"[red]   {len(res.missing)} Paket(e) danach nicht gefunden[/] (z. B. {res.missing[0]}) - 'foto-archiv verify hdd -v {v.label}'")
        else:
            console.print(f"[green]   ✓ {nv.label} im neuen Format, alle Pakete am neuen Ort.")
    return errors


@hdd_app.command("migrate")
def hdd_migrate(
    volume: Annotated[Optional[str], typer.Argument(help="Volume (Label, ID oder Pfad); leer = alle angeschlossenen")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="nur anzeigen, nichts ändern")] = False,
):
    """Volume ins Ablageformat mit Sammlungen umstellen (Jahresordner -> <archive_dir>/<Jahr>)."""
    a = get_app()
    if volume:
        try:
            vols = [resolve_volume(a.cfg, a.db, volume, allow_outdated=True)]
        except VolumeError as e:
            _fail(str(e))
    else:
        vols = find_connected(a.cfg, a.db, include_outdated=True)
    vols = [v for v in vols if v.outdated]
    if not vols:
        console.print("[green]Kein (angegebenes) Volume im alten Format - nichts zu tun.")
        return
    if _migrate_volumes(a, vols, dry_run):
        raise typer.Exit(1)


@app.command()
def migrate(dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="nur anzeigen, nichts ändern")] = False):
    """Einmalige Umstellung auf Sammlungen: Index, Konfiguration und angeschlossene HDDs.

    S3-Keys bleiben unverändert; in S3 wird nichts verschoben oder kopiert.
    """
    a = get_app(allow_old_index=True)
    dirname = None
    if a.db.schema_version >= SCHEMA_VERSION:
        console.print("[green]Index ist bereits umgestellt.")
    else:
        try:
            plan = plan_index(a)
        except MigrationError as e:
            _fail(str(e))
        c = plan.collection
        dirname = c.archive_dir
        console.print(
            f"[bold]Index:[/] {plan.counts['files']} Dateien, {plan.counts['versions']} Versionen, "
            f"{plan.counts['bundles']} Pakete -> Sammlung [bold]{c.id}[/] (Archivordner {c.archive_dir}/, Quelle {c.source_root})"
        )
        for old in plan.s3_ids:
            console.print(
                f"[bold]S3:[/] Präfix {old.partition('/')[2] or '(leer)'} -> {plan.new_prefix or '(leer)'} + "
                f"{c.archive_dir}/  - Keys bleiben gleich"
            )
        if plan.write_config:
            console.print(
                f"[bold]Konfiguration:[/] {a.cfg.path} bekommt \\[\\[collections]] '{c.id}', "
                f"prefix = \"{plan.new_prefix}\" (Sicherung *.bak)"
            )
        if not dry_run:
            try:
                migrate_index(a, plan)
            except (OSError, MigrationError) as e:
                _fail(f"Umstellung abgebrochen, nichts geändert: {e}")
            console.print("[green]✓ Index umgestellt.")
    outdated = [v for v in find_connected(a.cfg, a.db, include_outdated=True) if v.outdated]
    errors = _migrate_volumes(a, outdated, dry_run, dirname) if outdated else 0
    connected = {v.id for v in find_connected(a.cfg, a.db, include_outdated=True)}
    for r in a.db.q("SELECT id, label FROM volumes WHERE kind='hdd' ORDER BY label"):
        if r["id"] not in connected:
            console.print(f"[yellow]{r['label']} nicht angeschlossen[/] - beim nächsten Anschließen: foto-archiv hdd migrate {r['label']}")
    if errors:
        raise typer.Exit(1)


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
    connected = {v.id: v for v in find_connected(a.cfg, a.db, include_outdated=True)}
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
        where = ""
        if v:
            where = f"[yellow]{v.root} (altes Format: hdd migrate)" if v.outdated else f"[green]{v.root}"
        t.add_row(r["label"], r["kind"], where, str(agg["n"]), human(agg["s"]), free, r["last_seen"] or "")
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
    targets = list(resolve_series(a, series) or [])
    if ripe:
        configured = {c.id for c in a.cfg.collections}
        targets += [
            s.name for s in compute_status(a)
            if s.state in ("abschlussreif", "geändert") and s.name not in targets and s.name.partition("/")[0] in configured
        ]
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
    hddops.copy_hdd(a, s, d, resolve_series(a, series), dry_run)


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
    hddops.verify_hdd(a, v, resolve_series(a, series), quick)


@verify_app.command("s3")
def verify_s3_cmd(
    series: SeriesArg = None,
    deep: Annotated[bool, typer.Option("--deep", help="auch gespeicherte Prüfsummen vergleichen")] = False,
):
    """Pakete in S3 gegen den Index prüfen (Existenz, Größe, Prüfsumme)."""
    a = get_app()
    s3ops.verify_s3(a, resolve_series(a, series), deep)


# --------------------------------------------------------------------------- S3
@app.command("push-s3")
def push_s3_cmd(
    series: SeriesArg = None,
    volume: VolumeOpt = None,
    limit: Annotated[
        Optional[str],
        typer.Option(
            "--limit", "-l",
            help="feste Upload-Bandbreite für diesen Lauf, z. B. 2MB/s, 16Mbit, 500k (Zahl = MB/s; 0 = unbegrenzt); "
            "ersetzt upload_limit und upload_schedule aus der Konfiguration",
        ),
    ] = None,
    dry_run: DryRun = False,
):
    """Fehlende Pakete von einer HDD nach S3 (Deep Archive) hochladen."""
    a = get_app()
    if limit is not None:
        a.cfg.s3.upload_limit = limit
        a.cfg.s3.upload_schedule = []
    src = None
    if volume:
        try:
            src = resolve_volume(a.cfg, a.db, volume)
        except VolumeError as e:
            _fail(str(e))
    s3ops.push_s3(a, resolve_series(a, series), src, dry_run)


# --------------------------------------------------------------------------- Ausdünnen
@app.command()
def slim(
    series: SeriesArg = None,
    cls: ClassOpt = None,
    apply: Annotated[bool, typer.Option("--apply", help="wirklich löschen (sonst nur Vorschau)")] = False,
):
    """Lokale Dateien (Standard: RAW) entfernen, die ausreichend archiviert sind."""
    a = get_app()
    hddops.slim(a, resolve_series(a, series), cls, apply)


# --------------------------------------------------------------------------- Wiederherstellen
@app.command()
def restore(
    series: Annotated[str, typer.Argument(help="Serie (2026.Kenia oder fotos/2026.Kenia)")],
    cls: ClassOpt = None,
    source: Annotated[str, typer.Option("--from", help="hdd | s3 | auto")] = "auto",
    volume: VolumeOpt = None,
    target: Annotated[Optional[Path], typer.Option(help="Zielordner (Standard: Quellordner der Sammlung)")] = None,
    tier: Annotated[Optional[str], typer.Option(help="S3-Abruf: Bulk | Standard")] = None,
    staging: Annotated[
        Optional[Path], typer.Option(help="S3: Zwischenablage für Pakete (z. B. externe Platte; Standard: neben dem Ziel)")
    ] = None,
    overwrite: Annotated[bool, typer.Option(help="abweichende vorhandene Dateien überschreiben")] = False,
    dry_run: DryRun = False,
):
    """Serie (optional nur bestimmte Klassen) wiederherstellen - von HDD oder aus S3."""
    a = get_app()
    series = resolve_one(a, series)
    try:
        tgt = target or a.cfg.collection_of(series).source_root
    except ConfigError as e:
        _fail(str(e))
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
    s3ops.restore_s3(a, series, cls, tgt, tier, overwrite, dry_run, staging)


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
