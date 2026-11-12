"""Tests für Klassifizierung, Scan, Versiegeln und Status."""

from __future__ import annotations

import gzip
import json
import os
import tarfile

from foto_archiv.classify import classify, raw_key
from foto_archiv.scan import scan
from foto_archiv.seal import plan_seal, seal_series
from foto_archiv.status import compute_status
from foto_archiv.tario import extract_bundle
from foto_archiv.volumes import find_connected


def test_classify():
    raws = {raw_key("DSC0001.ARW"), raw_key("IMG_1234.CR3")}
    assert classify("DSC0001.ARW", raws) == "raw"
    assert classify("DSC0001.ARW.pp3", raws) == "raw"
    assert classify("DSC0001.xmp", raws) == "raw"
    assert classify("DSC0001.jpg", raws) == "img-derived"
    assert classify("DSC0001-1.jpg", raws) == "img-derived"
    assert classify("DSC0001_01.jpg", raws) == "img-derived"
    assert classify("DSC0001 (2).jpg", raws) == "img-derived"
    assert classify("DSC0001.ARW.jpg", raws) == "img-derived"
    assert classify("IMG_1234.jpg", raws) == "img-derived"
    # gleiches Präfix, aber andere Nummer -> kein Export
    assert classify("IMG_5678.jpg", raws) == "img-original"
    assert classify("PXL_2026.jpg", raws) == "img-original"
    assert classify("C0001.MP4", raws) == "video"
    assert classify("projekt.xcf", raws) == "other"
    assert classify("pano.pto", set()) == "other"


def test_scan_classes_and_excludes(app):
    st = scan(app)
    rows = {r["path"]: r for r in app.db.q("SELECT * FROM files")}
    assert "2026.Kenia/Thumbs.db" not in rows
    assert "digikam4.db" not in rows
    assert not any(p.startswith(".dtrash") for p in rows)
    assert rows["2026.Kenia/raws/DSC01.ARW"]["cls"] == "raw"
    assert rows["2026.Kenia/raws/DSC01.ARW.pp3"]["cls"] == "raw"
    assert rows["2026.Kenia/jpg/DSC01-1.jpg"]["cls"] == "img-derived"
    assert rows["2026.Kenia/pixel/PXL_20260201.jpg"]["cls"] == "img-original"
    assert rows["2026.Kenia/clips/C0001.MP4"]["cls"] == "video"
    assert rows["sortiert/a.png"]["series"] == "sortiert"
    assert st.excluded >= 3
    # zweiter Scan: nichts neu
    st2 = scan(app)
    assert st2.new == 0 and st2.changed == 0


def test_status_states(app):
    scan(app)
    states = {s.name: s.state for s in compute_status(app)}
    assert states["2026.Kenia"] == "abschlussreif"
    assert states["2013.Österreich"] == "abschlussreif"  # keine RAWs -> nur Ruhezeit zählt
    assert states["2026.London"] == "aktiv"


def test_seal_and_extract(app, hdd_a, tmp_path):
    scan(app)
    plan = seal_series(app, "2026.Kenia", hdd_a)
    assert plan is not None and plan.version == 1
    # raw (5*30KB + pp3) in 1 MiB-Paketen -> 1 Paket; alle Klassen vorhanden
    bundles = app.db.bundles_of("2026.Kenia")
    classes = {b["cls"] for b in bundles}
    assert classes == {"raw", "img-derived", "img-original", "video"}
    for b in bundles:
        p = hdd_a.path_for(b["relpath"])
        assert p.exists() and p.stat().st_size == b["size"]
        assert b["relpath"].startswith("2026/2026.Kenia/v1/")
    man = json.loads(gzip.decompress(hdd_a.path_for("2026/2026.Kenia/v1/manifest.json.gz").read_bytes()))
    assert man["series"] == "2026.Kenia" and man["version"] == 1
    assert len(man["files"]) == app.db.q1("SELECT COUNT(*) n FROM archived")["n"] == 18

    # erneutes Versiegeln: nichts zu tun
    assert plan_seal(app, "2026.Kenia") is None
    st = {s.name: s for s in compute_status(app)}["2026.Kenia"]
    assert st.state == "versiegelt" and st.hdd_full == 1 and not st.s3_full

    # Entpacken eines Pakets in ein leeres Ziel mit Hash-Prüfung
    raw = next(b for b in bundles if b["cls"] == "raw")
    expected = {r["path"]: r["sha256"] for r in app.db.q("SELECT path, sha256 FROM archived WHERE bundle_id=?", (raw["id"],))}
    out = tmp_path / "restore"
    stats = extract_bundle(hdd_a.path_for(raw["relpath"]), out, expected)
    assert len(stats.written) == 10 and not stats.hash_errors
    src = app.cfg.source_root / "2026.Kenia" / "raws" / "DSC01.ARW"
    dst = out / "2026.Kenia" / "raws" / "DSC01.ARW"
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns  # exakte mtime über PAX
    # zweites Entpacken: alles identisch -> übersprungen
    stats2 = extract_bundle(hdd_a.path_for(raw["relpath"]), out, expected)
    assert len(stats2.skipped_same) == 10


def test_seal_v2_only_changes(app, hdd_a):
    scan(app)
    seal_series(app, "2026.Kenia", hdd_a)
    root = app.cfg.source_root / "2026.Kenia"
    (root / "jpg" / "DSC02.jpg").write_bytes(b"neu bearbeitet")
    (root / "jpg" / "neu.jpg").write_bytes(b"neuer export")
    os.utime(root / "raws" / "DSC03.ARW")  # nur mtime geändert, Inhalt gleich
    scan(app)
    st = {s.name: s for s in compute_status(app)}["2026.Kenia"]
    assert st.state == "geändert"
    plan = seal_series(app, "2026.Kenia", hdd_a)
    assert plan.version == 2
    assert sorted(i.path for i in plan.items) == ["2026.Kenia/jpg/DSC02.jpg", "2026.Kenia/jpg/neu.jpg"]
    assert {s.name: s for s in compute_status(app)}["2026.Kenia"].state == "versiegelt"


def test_chunking(app, hdd_a, pictures):
    big = pictures / "2025.Gross"
    for i in range(6):
        (big / f"DSC1{i}.ARW").parent.mkdir(parents=True, exist_ok=True)
        (big / f"DSC1{i}.ARW").write_bytes(os.urandom(400_000))
    scan(app)
    seal_series(app, "2025.Gross", hdd_a)
    names = [b["name"] for b in app.db.bundles_of("2025.Gross")]
    assert names == ["raw-001.tar", "raw-002.tar", "raw-003.tar"]  # 1 MiB-Limit -> 2 Dateien je Paket
    for b in app.db.bundles_of("2025.Gross"):
        with tarfile.open(hdd_a.path_for(b["relpath"])) as tf:
            assert len(tf.getnames()) == 2


def test_volume_detection(app, hdd_a, hdd_b, tmp_path):
    # nur Test-Volumes betrachten; echte angeschlossene Archivplatten werden ebenfalls gefunden
    found = find_connected(app.cfg, app.db)
    labels = sorted(v.label for v in found if v.root.is_relative_to(tmp_path))
    assert labels == ["A", "B"]
