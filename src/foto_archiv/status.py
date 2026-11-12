"""Übersicht je Serie: lokaler Bestand, Versiegelung, Kopien, Ausdünn-Potenzial."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from rich import box
from rich.table import Table

from .classify import IMG_DERIVED, RAW
from .context import App, human
from .layout import series_year
from .policy import bundle_protection, slim_candidates

STATE_LABEL = {
    "aktiv": "[cyan]aktiv",
    "abschlussreif": "[yellow]abschlussreif",
    "versiegelt": "[green]versiegelt",
    "geändert": "[magenta]geändert",
    "nur-archiv": "[dim]nur Archiv",
}


@dataclass
class SeriesStatus:
    name: str
    year: int | None
    local_files: int = 0
    local_bytes: int = 0
    by_class: dict[str, list[int]] = field(default_factory=dict)  # cls -> [n, bytes] (lokal)
    last_change_ns: int = 0
    versions: int = 0
    pending_files: int = 0
    pending_bytes: int = 0
    bundles: int = 0
    hdd_full: int = 0  # Volumes, die alle Pakete der Serie (ok) haben
    hdd_labels: list[str] = field(default_factory=list)
    s3_full: bool = False
    slimmable_bytes: int = 0
    slimmed_bytes: int = 0
    state: str = "aktiv"

    @property
    def idle_days(self) -> float:
        if not self.last_change_ns:
            return 0
        return (time.time() - self.last_change_ns / 1e9) / 86400


def compute_status(app: App, only: list[str] | None = None) -> list[SeriesStatus]:
    db = app.db
    names = sorted(
        {r["series"] for r in db.q("SELECT DISTINCT series FROM files")}
        | {r["series"] for r in db.q("SELECT DISTINCT series FROM versions")}
    )
    if only:
        names = [n for n in names if n in set(only)]
    labels = {r["id"]: r["label"] for r in db.q("SELECT id, label FROM volumes")}
    slim_by_series: dict[str, int] = {}
    for c in slim_candidates(app, names or None):
        slim_by_series[c.series] = slim_by_series.get(c.series, 0) + c.size

    out: list[SeriesStatus] = []
    for name in names:
        st = SeriesStatus(name=name, year=series_year(name))
        archived = db.latest_archived(name)
        for r in db.q("SELECT path, cls, size, mtime_ns, sha256, present, slimmed FROM files WHERE series=?", (name,)):
            if not r["present"]:
                if r["slimmed"]:
                    st.slimmed_bytes += r["size"]
                continue
            st.local_files += 1
            st.local_bytes += r["size"]
            st.by_class.setdefault(r["cls"], [0, 0])
            st.by_class[r["cls"]][0] += 1
            st.by_class[r["cls"]][1] += r["size"]
            st.last_change_ns = max(st.last_change_ns, r["mtime_ns"])
            a = archived.get(r["path"])
            if a is None or (r["sha256"] is not None and r["sha256"] != a["sha256"]) or (
                r["sha256"] is None and (a["size"] != r["size"] or a["mtime_ns"] != r["mtime_ns"])
            ):
                st.pending_files += 1
                st.pending_bytes += r["size"]
        v = db.q1("SELECT COUNT(*) n FROM versions WHERE series=?", (name,))
        st.versions = v["n"]
        prot = bundle_protection(app, name)
        st.bundles = len(prot)
        if prot:
            common = set.intersection(*(p.hdd_ok for p in prot.values()))
            st.hdd_full = len(common)
            st.hdd_labels = sorted(labels.get(i, i[:8]) for i in common)
            st.s3_full = all(p.s3_ok for p in prot.values())
        st.slimmable_bytes = slim_by_series.get(name, 0)

        has_raw = RAW in st.by_class or st.slimmed_bytes > 0
        has_derived = IMG_DERIVED in st.by_class
        ripe = st.idle_days >= app.cfg.settle_days and (has_derived or not has_raw)
        if st.local_files == 0 and st.versions:
            st.state = "nur-archiv"
        elif st.versions == 0:
            st.state = "abschlussreif" if ripe else "aktiv"
        else:
            st.state = "geändert" if st.pending_files else "versiegelt"
        out.append(st)
    return out


def _cls_bytes(st: SeriesStatus, *classes: str) -> int:
    return sum(st.by_class.get(c, [0, 0])[1] for c in classes)


def render_status(app: App, rows: list[SeriesStatus]) -> None:
    # Schmales Terminal: weniger Spalten, sonst quetscht rich die Serienspalte auf 0
    compact = app.console.width < 130
    hidden = {"Bilder", "Video", "Ver."} if compact else set()
    cols = [
        ("Serie", "left"), ("lokal", "right"), ("RAW", "right"), ("Bilder", "right"), ("Video", "right"),
        ("Stand", "left"), ("Ver.", "right"), ("offen", "right"), ("HDD", "left"), ("S3", "center"), ("ausdünnbar", "right"),
    ]
    t = Table(show_lines=False, header_style="bold", box=box.SIMPLE_HEAD if compact else box.HEAVY_HEAD, pad_edge=not compact)
    for col, j in cols:
        if col in hidden:
            continue
        if col == "Serie":
            t.add_column(col, justify=j, min_width=16, overflow="fold")
        else:
            t.add_column(col, justify=j, no_wrap=True)

    def add(values: list[str]) -> None:
        t.add_row(*(v for (col, _), v in zip(cols, values) if col not in hidden))

    tot = SeriesStatus(name="Summe", year=None)
    for st in rows:
        add([
            st.name,
            human(st.local_bytes),
            human(_cls_bytes(st, "raw")) if "raw" in st.by_class else "",
            human(_cls_bytes(st, "img-derived", "img-original")),
            human(_cls_bytes(st, "video")) if "video" in st.by_class else "",
            STATE_LABEL.get(st.state, st.state),
            str(st.versions or ""),
            human(st.pending_bytes) if st.pending_files else "",
            ",".join(st.hdd_labels) if st.bundles else "",
            ("[green]✓" if st.s3_full else "[red]✗") if st.bundles else "",
            human(st.slimmable_bytes) if st.slimmable_bytes else "",
        ])
        tot.local_bytes += st.local_bytes
        tot.pending_bytes += st.pending_bytes
        tot.slimmable_bytes += st.slimmable_bytes
        for c, (n, b) in st.by_class.items():
            tot.by_class.setdefault(c, [0, 0])
            tot.by_class[c][1] += b
    t.add_section()
    add([
        "[bold]Summe", human(tot.local_bytes), human(_cls_bytes(tot, "raw")),
        human(_cls_bytes(tot, "img-derived", "img-original")), human(_cls_bytes(tot, "video")),
        "", "", human(tot.pending_bytes), "", "", human(tot.slimmable_bytes),
    ])
    app.console.print(t)
    counts: dict[str, int] = {}
    for st in rows:
        counts[st.state] = counts.get(st.state, 0) + 1
    app.console.print("  " + "  ".join(f"{STATE_LABEL.get(k, k)}[/]: {v}" for k, v in sorted(counts.items())))


def render_series_detail(app: App, st: SeriesStatus) -> None:
    con = app.console
    db = app.db
    con.print(f"[bold]{st.name}[/]  Stand: {STATE_LABEL.get(st.state, st.state)}[/]  "
              f"unverändert seit {st.idle_days:.0f} Tagen")
    t = Table("Klasse", "Dateien", "Größe", header_style="bold")
    for c, (n, b) in sorted(st.by_class.items()):
        t.add_row(c, str(n), human(b))
    con.print(t)
    labels = {r["id"]: r["label"] for r in db.q("SELECT id, label FROM volumes")}
    for v in db.q("SELECT * FROM versions WHERE series=? ORDER BY version", (st.name,)):
        con.print(f"v{v['version']}  versiegelt {v['sealed_at']}  {v['nfiles']} Dateien  {human(v['size'])}")
        bt = Table("Paket", "Größe", "Kopien", header_style="bold")
        for b in db.bundles_of(st.name, v["version"]):
            cps = db.q("SELECT volume_id, status, verified_at FROM copies WHERE bundle_id=?", (b["id"],))
            desc = ", ".join(
                f"{labels.get(c['volume_id'], c['volume_id'])}:{c['status']}{'✓' if c['verified_at'] else ''}" for c in cps
            )
            bt.add_row(b["name"], human(b["size"]), desc)
        con.print(bt)
    if st.pending_files:
        con.print(f"[magenta]Noch nicht archiviert: {st.pending_files} Dateien, {human(st.pending_bytes)}")
    if st.slimmable_bytes:
        con.print(f"[yellow]Lokal ausdünnbar: {human(st.slimmable_bytes)}")
