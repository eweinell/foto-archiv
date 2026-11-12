"""Tests für HDD-Operationen: copy-hdd, verify hdd, slim, restore (HDD)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foto_archiv.hddops import OpFailed, copy_hdd, restore_hdd, slim, verify_hdd
from foto_archiv.scan import scan
from foto_archiv.seal import seal_series
from foto_archiv.status import compute_status
from foto_archiv.tario import PARTIAL_SUFFIX

KENIA = "2026.Kenia"


# --------------------------------------------------------------------------- Hilfen
def _seal(app, vol, series=KENIA):
    scan(app)
    seal_series(app, series, vol)


def _add_s3(app):
    """Simuliert eine S3-Kopie aller Pakete (ohne echten Upload)."""
    vid = app.cfg.s3.volume_id
    app.db.register_volume(vid, "s3", "S3")
    for b in app.db.q("SELECT id FROM bundles"):
        app.db.set_copy(b["id"], vid, "ok", verified=True)


def _flip(p: Path, offset: int = 1000) -> None:
    data = bytearray(p.read_bytes())
    data[offset] ^= 0xFF
    p.write_bytes(data)


def _copy_row(app, bundle_id, vol):
    return app.db.q1("SELECT * FROM copies WHERE bundle_id=? AND volume_id=?", (bundle_id, vol.id))


def _files(app):
    return {r["path"]: r for r in app.db.q("SELECT * FROM files")}


def _raw_paths(app):
    return sorted(p for p, r in _files(app).items() if r["series"] == KENIA and r["cls"] == "raw")


# --------------------------------------------------------------------------- copy-hdd
def test_copy_hdd(app, hdd_a, hdd_b):
    _seal(app, hdd_a)
    cat = hdd_a.root / "_kataloge" / "2026-11-12" / "digikam4.db"
    cat.parent.mkdir(parents=True)
    cat.write_bytes(b"katalog")

    # Trockenlauf ändert nichts
    copy_hdd(app, hdd_a, hdd_b, None, dry_run=True)
    assert not (hdd_b.root / "2026").exists()

    res = copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    bundles = app.db.bundles_of(KENIA)
    assert len(res.copied) == len(bundles) and res.manifests == 1 and res.catalogs == 1
    for b in bundles:
        assert hdd_b.path_for(b["relpath"]).read_bytes() == hdd_a.path_for(b["relpath"]).read_bytes()
        row = _copy_row(app, b["id"], hdd_b)
        assert row["status"] == "ok" and row["verified_at"]
    assert hdd_b.path_for("2026/2026.Kenia/v1/manifest.json.gz").is_file()
    assert app.db.q1("SELECT 1 FROM manifest_copies WHERE series=? AND volume_id=?", (KENIA, hdd_b.id))
    assert (hdd_b.root / "_kataloge" / "2026-11-12" / "digikam4.db").read_bytes() == b"katalog"
    assert (hdd_b.root / "_index" / "index.sqlite").is_file()
    assert not list(hdd_b.root.rglob(f"*{PARTIAL_SUFFIX}"))

    # idempotent
    res2 = copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    assert not res2.copied and not res2.registered and res2.manifests == 0 and res2.catalogs == 0

    # Datei liegt korrekt auf dem Ziel, Index-Eintrag fehlt -> nur registrieren
    b0 = bundles[0]
    app.db.x("DELETE FROM copies WHERE bundle_id=? AND volume_id=?", (b0["id"], hdd_b.id))
    res3 = copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    assert res3.registered == [b0["relpath"]] and not res3.copied
    assert _copy_row(app, b0["id"], hdd_b)["status"] == "ok"


def test_copy_hdd_source_corrupt(app, hdd_a, hdd_b):
    _seal(app, hdd_a)
    raw = next(b for b in app.db.bundles_of(KENIA) if b["cls"] == "raw")
    _flip(hdd_a.path_for(raw["relpath"]))
    with pytest.raises(OpFailed) as ei:
        copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    assert ei.value.result.source_bad == [raw["relpath"]]
    assert _copy_row(app, raw["id"], hdd_a)["status"] == "bad"
    assert _copy_row(app, raw["id"], hdd_b) is None
    assert not hdd_b.path_for(raw["relpath"]).exists()
    assert not list(hdd_b.root.rglob(f"*{PARTIAL_SUFFIX}"))
    # die übrigen Pakete wurden trotzdem kopiert
    assert len(ei.value.result.copied) == len(app.db.bundles_of(KENIA)) - 1


# --------------------------------------------------------------------------- verify
def test_verify_hdd(app, hdd_a, hdd_b):
    _seal(app, hdd_a)
    copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    res = verify_hdd(app, hdd_b, None, quick=False)
    bundles = app.db.bundles_of(KENIA)
    assert len(res.ok) == len(bundles) and not res.failed

    raw, vid, img = (next(b for b in bundles if b["cls"] == c) for c in ("raw", "video", "img-original"))
    # Bitfehler: nur volle Prüfung erkennt ihn
    _flip(hdd_b.path_for(raw["relpath"]))
    assert not verify_hdd(app, hdd_b, None, quick=True).failed
    with pytest.raises(OpFailed) as ei:
        verify_hdd(app, hdd_b, None, quick=False)
    assert ei.value.result.bad == [raw["relpath"]]
    assert _copy_row(app, raw["id"], hdd_b)["status"] == "bad"

    # abgeschnittene / fehlende Datei: Schnellprüfung reicht
    p = hdd_b.path_for(vid["relpath"])
    p.write_bytes(p.read_bytes()[:1000])
    hdd_b.path_for(img["relpath"]).unlink()
    (hdd_b.root / "2026" / f"x.tar{PARTIAL_SUFFIX}").write_bytes(b"rest")
    with pytest.raises(OpFailed) as ei:
        verify_hdd(app, hdd_b, [KENIA], quick=True)
    r = ei.value.result
    assert vid["relpath"] in r.bad and r.missing == [img["relpath"]]
    assert raw["relpath"] in r.unverified  # Status bad bleibt, Größe passt
    assert r.partials == [f"2026/x.tar{PARTIAL_SUFFIX}"]
    assert (hdd_b.root / "2026" / f"x.tar{PARTIAL_SUFFIX}").exists()  # verify löscht nichts
    assert _copy_row(app, vid["id"], hdd_b)["status"] == "bad"
    assert _copy_row(app, img["id"], hdd_b)["status"] == "missing"

    # kaputte Pakete von A neu kopieren -> wieder alles ok
    copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    assert not verify_hdd(app, hdd_b, None, quick=False).failed


def test_verify_registers_unknown_copy(app, hdd_a, hdd_b):
    _seal(app, hdd_a)
    copy_hdd(app, hdd_a, hdd_b, None, dry_run=False)
    b0 = app.db.bundles_of(KENIA)[0]
    app.db.x("DELETE FROM copies WHERE bundle_id=? AND volume_id=?", (b0["id"], hdd_b.id))
    assert verify_hdd(app, hdd_b, None, quick=True).unregistered == [b0["relpath"]]
    res = verify_hdd(app, hdd_b, None, quick=False)
    assert res.registered == [b0["relpath"]]
    row = _copy_row(app, b0["id"], hdd_b)
    assert row["status"] == "ok" and row["verified_at"]


def test_verify_missing_manifest(app, hdd_a):
    _seal(app, hdd_a)
    hdd_a.path_for("2026/2026.Kenia/v1/manifest.json.gz").unlink()
    with pytest.raises(OpFailed) as ei:
        verify_hdd(app, hdd_a, None, quick=True)
    assert ei.value.result.manifests_missing == ["2026/2026.Kenia/v1/manifest.json.gz"]


# --------------------------------------------------------------------------- slim
def test_slim_protection_and_apply(app, hdd_a):
    _seal(app, hdd_a)
    root = app.cfg.source_root
    raws = _raw_paths(app)
    assert len(raws) == 10

    # require_s3=True (Standard) und keine S3-Kopie -> nichts ausdünnbar
    assert slim(app, None, None, apply=True).candidates == 0
    assert all(root.joinpath(p).exists() for p in raws)

    _add_s3(app)
    prev = slim(app, None, None, apply=False)
    assert prev.candidates == 10 and not prev.deleted
    assert all(root.joinpath(p).exists() for p in raws)

    # eine Datei nach dem scan verändert -> darf nicht gelöscht werden
    changed = root / KENIA / "raws" / "DSC05.ARW.pp3"
    os.utime(changed, (1_700_000_000, 1_700_000_000))
    others = {p: root.joinpath(p).stat().st_mtime_ns for p, r in _files(app).items() if r["cls"] != "raw"}

    res = slim(app, None, None, apply=True)
    assert len(res.deleted) == 9 and res.skipped == [f"{KENIA}/raws/DSC05.ARW.pp3"]
    assert changed.exists()
    assert (root / KENIA / "raws").is_dir()  # nicht leer -> bleibt
    for p, mt in others.items():  # JPG, Video usw. unberührt
        assert root.joinpath(p).stat().st_mtime_ns == mt
    rows = _files(app)
    for p in res.deleted:
        assert not root.joinpath(p).exists()
        assert rows[p]["present"] == 0 and rows[p]["slimmed"] == 1

    # scan danach: entfernte RAWs bleiben "slimmed", Exporte bleiben img-derived
    st = scan(app)
    assert st.new == 0
    rows = _files(app)
    for p in res.deleted:
        assert rows[p]["present"] == 0 and rows[p]["slimmed"] == 1
    assert rows[f"{KENIA}/jpg/DSC01.jpg"]["cls"] == "img-derived"
    assert rows[f"{KENIA}/jpg/DSC01-1.jpg"]["cls"] == "img-derived"
    assert rows[f"{KENIA}/pixel/PXL_20260201.jpg"]["cls"] == "img-original"


def test_slim_requires_hdd_verification(app, hdd_a):
    _seal(app, hdd_a)
    _add_s3(app)
    app.db.x("UPDATE copies SET verified_at=NULL WHERE volume_id=?", (hdd_a.id,))
    assert slim(app, None, None, apply=True).candidates == 0


def test_slim_refuses_without_source_root(app, hdd_a, tmp_path):
    _seal(app, hdd_a)
    _add_s3(app)
    app.cfg.source_root = tmp_path / "gibt-es-nicht"
    with pytest.raises(OpFailed):
        slim(app, None, None, apply=True)


# --------------------------------------------------------------------------- restore
def test_slim_then_restore_raw(app, hdd_a):
    _seal(app, hdd_a)
    _add_s3(app)
    root = app.cfg.source_root
    raws = _raw_paths(app)
    original = {p: root.joinpath(p).read_bytes() for p in raws}

    slim(app, [KENIA], ["raw"], apply=True)
    assert not (root / KENIA / "raws").exists()  # durch slim leer geworden -> entfernt
    assert (root / KENIA).is_dir()

    res = restore_hdd(app, KENIA, ["raw"], root, hdd_a, overwrite=False, dry_run=False)
    assert sorted(res.written) == raws and not res.conflicts and not res.hash_errors
    for p, data in original.items():
        assert root.joinpath(p).read_bytes() == data
    rows = _files(app)
    for p in raws:
        assert rows[p]["present"] == 1 and rows[p]["slimmed"] == 0

    st = scan(app)
    assert st.new == 0 and st.changed == 0
    s = {x.name: x for x in compute_status(app, [KENIA])}[KENIA]
    assert s.state == "versiegelt" and s.pending_files == 0

    # nochmal: alles identisch
    res2 = restore_hdd(app, KENIA, ["raw"], root, hdd_a, overwrite=False, dry_run=False)
    assert sorted(res2.skipped_same) == raws and not res2.written


def test_restore_conflicts(app, hdd_a, tmp_path):
    _seal(app, hdd_a)
    target = tmp_path / "ziel"
    clash = target / KENIA / "raws" / "DSC01.ARW"
    clash.parent.mkdir(parents=True)
    clash.write_bytes(b"anderer Inhalt")

    res = restore_hdd(app, KENIA, ["raw"], target, hdd_a, overwrite=False, dry_run=False)
    assert res.conflicts == [f"{KENIA}/raws/DSC01.ARW"] and len(res.written) == 9
    assert clash.read_bytes() == b"anderer Inhalt"

    res2 = restore_hdd(app, KENIA, ["raw"], target, hdd_a, overwrite=True, dry_run=False)
    assert res2.written == [f"{KENIA}/raws/DSC01.ARW"] and len(res2.skipped_same) == 9
    assert clash.read_bytes() == (app.cfg.source_root / KENIA / "raws" / "DSC01.ARW").read_bytes()
    # Ziel != source_root -> Index unverändert
    assert _files(app)[f"{KENIA}/raws/DSC01.ARW"]["present"] == 1


def test_restore_latest_version(app, hdd_a, tmp_path):
    _seal(app, hdd_a)
    root = app.cfg.source_root / KENIA
    (root / "jpg" / "DSC02.jpg").write_bytes(b"v2-Inhalt")
    scan(app)
    seal_series(app, KENIA, hdd_a)

    target = tmp_path / "ziel"
    res = restore_hdd(app, KENIA, ["img-derived"], target, hdd_a, overwrite=False, dry_run=False)
    assert len(res.written) == 6 and not res.hash_errors
    assert (target / KENIA / "jpg" / "DSC02.jpg").read_bytes() == b"v2-Inhalt"
    assert (target / KENIA / "jpg" / "DSC01.jpg").read_bytes() == (root / "jpg" / "DSC01.jpg").read_bytes()
    assert not (target / KENIA / "raws").exists()


def test_restore_bundle_not_on_volume(app, hdd_a, hdd_b, tmp_path, capsys):
    _seal(app, hdd_a)
    target = tmp_path / "ziel"
    restore_hdd(app, KENIA, ["raw"], target, hdd_b, overwrite=False, dry_run=True)
    assert not target.exists()
    with pytest.raises(OpFailed) as ei:
        restore_hdd(app, KENIA, ["raw"], target, hdd_b, overwrite=False, dry_run=False)
    assert len(ei.value.result.unavailable) == 1 and not ei.value.result.written
    assert "HDD A" in capsys.readouterr().out


# --------------------------------------------------------------------------- Label ändern
def test_relabel_keeps_copies(app, hdd_a, hdd_b):
    import json

    from foto_archiv.volumes import VolumeError, find_connected, relabel_volume

    _seal(app, hdd_a)
    n_before = app.db.q1("SELECT COUNT(*) n FROM copies WHERE volume_id=?", (hdd_a.id,))["n"]
    with pytest.raises(VolumeError):
        relabel_volume(hdd_a, "B", app.db)  # schon vergeben
    v = relabel_volume(hdd_a, "A-2025", app.db)
    assert v.id == hdd_a.id and v.label == "A-2025"
    marker = json.loads((hdd_a.root / "volume.json").read_text(encoding="utf-8"))
    assert marker["label"] == "A-2025" and marker["previous_labels"] == ["A"]
    assert app.db.q1("SELECT label FROM volumes WHERE id=?", (hdd_a.id,))["label"] == "A-2025"
    assert app.db.q1("SELECT COUNT(*) n FROM copies WHERE volume_id=?", (hdd_a.id,))["n"] == n_before
    res = verify_hdd(app, v, None, quick=False)
    assert res.ok and not (res.bad or res.missing)
    assert [x.label for x in find_connected(app.cfg, app.db) if x.id == hdd_a.id] == ["A-2025"]
