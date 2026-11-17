"""Aufräumen: hdd clean (Reste abgebrochener Vorgänge) und drop (Versionen nur auf HDDs entfernen)."""

from __future__ import annotations

import pytest

from foto_archiv import cleanup
from foto_archiv.hddops import copy_hdd, verify_hdd
from foto_archiv.scan import scan
from foto_archiv.seal import export_index, plan_seal, seal_series
from foto_archiv.status import compute_status

KENIA = "fotos/2026.Kenia"


def _kenia_dir(vol, version=1):
    return vol.root / "fotos" / "2026" / "2026.Kenia" / f"v{version}"


def _put(p, data=b"x" * 100):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def test_clean_removes_only_incomplete_leftovers(app, hdd_a):
    scan(app)
    seal_series(app, KENIA, hdd_a)
    export_index(app, hdd_a)
    r = hdd_a.root
    _put(r / "_index" / "index.sqlite.foto-archiv-partial")  # auch in _index: Teildatei weg, Rest bleibt
    # abgebrochenes seal: Paket ohne Manifest + Teildatei
    _put(r / "videos" / "_ohne-jahr" / "_root" / "v1" / "video-001.tar", b"a" * 5000)
    _put(r / "videos" / "_ohne-jahr" / "_root" / "v1" / "video-002.tar.foto-archiv-partial", b"")
    # Rest eines früheren Versuchs neben einer versiegelten Version
    _put(_kenia_dir(hdd_a) / "raw-009.tar")
    # vollständige Version, die der Index nicht kennt -> bleibt
    _put(r / "fotos" / "2020" / "2020.X" / "v1" / "raw-001.tar")
    _put(r / "fotos" / "2020" / "2020.X" / "v1" / "manifest.json.gz")
    _put(r / "fotos" / "notiz.txt")  # fremde Datei -> bleibt
    known = sorted(p for p in _kenia_dir(hdd_a).iterdir() if p.name != "raw-009.tar")

    res = cleanup.clean_hdd(app, hdd_a, apply=False)
    assert sorted(i.rel for i in res.remove) == [
        "_index/index.sqlite.foto-archiv-partial",
        "fotos/2026/2026.Kenia/v1/raw-009.tar",
        "videos/_ohne-jahr/_root/v1/video-001.tar",
        "videos/_ohne-jahr/_root/v1/video-002.tar.foto-archiv-partial",
    ]
    assert res.dirs == ["videos/_ohne-jahr/_root/v1", "videos/_ohne-jahr/_root", "videos/_ohne-jahr"]
    assert {i.rel for i in res.kept} == {"fotos/2020/2020.X/v1/raw-001.tar", "fotos/2020/2020.X/v1/manifest.json.gz", "fotos/notiz.txt"}
    assert (r / "videos" / "_ohne-jahr" / "_root" / "v1" / "video-001.tar").exists()  # Vorschau ändert nichts

    res = cleanup.clean_hdd(app, hdd_a, apply=True)
    assert not res.errors
    assert not (r / "videos" / "_ohne-jahr").exists() and (r / "videos").is_dir()  # oberste Ebene bleibt
    assert not (_kenia_dir(hdd_a) / "raw-009.tar").exists()
    assert sorted(_kenia_dir(hdd_a).iterdir()) == known
    assert (r / "fotos" / "2020" / "2020.X" / "v1" / "raw-001.tar").exists()
    assert (r / "volume.json").exists() and (r / "_index" / "index.sqlite").exists()
    assert not verify_hdd(app, hdd_a, None, quick=False).failed
    assert not cleanup.plan_clean(app, hdd_a).remove


def test_drop_series_from_all_hdds(app, hdd_a, hdd_b, capsys):
    scan(app)
    seal_series(app, KENIA, hdd_a)
    copy_hdd(app, hdd_a, hdd_b, None, False)
    assert _kenia_dir(hdd_b).is_dir()

    plan = cleanup.plan_drop(app, KENIA, None, [hdd_a])  # B nicht angeschlossen
    assert any("nicht angeschlossen: B" in b for b in plan.blockers)
    with pytest.raises(cleanup.DropError):
        cleanup.drop_series(app, plan, [hdd_a])
    assert _kenia_dir(hdd_a).is_dir()

    plan = cleanup.plan_drop(app, KENIA, None, [hdd_a, hdd_b])
    assert not plan.blockers and plan.versions == [1] and plan.local_files > 0
    assert cleanup.drop_series(app, plan, [hdd_a, hdd_b]) == []
    for v in (hdd_a, hdd_b):
        assert not (v.root / "fotos" / "2026" / "2026.Kenia").exists()
        assert (v.root / "fotos").is_dir()
    db = app.db
    assert not db.q("SELECT 1 FROM versions WHERE series=?", (KENIA,))
    assert not db.q("SELECT 1 FROM bundles WHERE series=?", (KENIA,))
    assert not db.q("SELECT 1 FROM archived WHERE series=?", (KENIA,))
    assert not db.q("SELECT 1 FROM manifest_copies WHERE series=?", (KENIA,))
    assert "exclude_paths" in capsys.readouterr().out
    (st,) = compute_status(app, [KENIA])
    assert st.versions == 0 and st.state in ("aktiv", "abschlussreif")
    # andere Serien unberührt, erneutes Versiegeln beginnt wieder bei v1
    assert not verify_hdd(app, hdd_a, None, quick=False).failed
    assert plan_seal(app, KENIA).version == 1
    seal_series(app, KENIA, hdd_a)
    assert _kenia_dir(hdd_a).is_dir()


def test_drop_blockers(app, hdd_a):
    scan(app)
    seal_series(app, KENIA, hdd_a)
    (app.cfg.source_root / "2026.Kenia" / "jpg" / "neu.jpg").write_bytes(b"neu")
    scan(app)
    seal_series(app, KENIA, hdd_a)

    # ältere Version: spätere bauen darauf auf
    plan = cleanup.plan_drop(app, KENIA, 1, [hdd_a])
    assert any("nicht die neueste" in b for b in plan.blockers)
    # neueste Version allein geht
    plan = cleanup.plan_drop(app, KENIA, 2, [hdd_a])
    assert not plan.blockers
    with pytest.raises(cleanup.DropError, match="gibt es nicht"):
        cleanup.plan_drop(app, KENIA, 7, [hdd_a])
    with pytest.raises(cleanup.DropError, match="keine versiegelte"):
        cleanup.plan_drop(app, "fotos/sortiert", None, [hdd_a])

    # in S3 -> nie
    b = app.db.q1("SELECT id FROM bundles WHERE series=? AND version=2", (KENIA,))
    app.db.register_volume("s3:bucket/", "s3", "S3")
    app.db.set_copy(b["id"], "s3:bucket/")
    plan = cleanup.plan_drop(app, KENIA, 2, [hdd_a])
    assert any("S3" in x for x in plan.blockers)
    app.db.x("DELETE FROM copies WHERE volume_id='s3:bucket/'")

    # lokal ausgedünnte Dateien gäbe es danach nirgends mehr
    app.db.x("UPDATE files SET present=0, slimmed=1 WHERE path=?", (f"{KENIA}/jpg/neu.jpg",))
    plan = cleanup.plan_drop(app, KENIA, 2, [hdd_a])
    assert any("ausgedünnte" in x for x in plan.blockers)


def test_drop_newest_version_only(app, hdd_a):
    scan(app)
    seal_series(app, KENIA, hdd_a)
    (app.cfg.source_root / "2026.Kenia" / "jpg" / "neu.jpg").write_bytes(b"neu")
    scan(app)
    seal_series(app, KENIA, hdd_a)
    plan = cleanup.plan_drop(app, KENIA, 2, [hdd_a])
    assert cleanup.drop_series(app, plan, [hdd_a]) == []
    assert _kenia_dir(hdd_a, 1).is_dir() and not _kenia_dir(hdd_a, 2).exists()
    assert [r["version"] for r in app.db.q("SELECT version FROM versions WHERE series=?", (KENIA,))] == [1]
    assert plan_seal(app, KENIA).version == 2  # neu.jpg ist wieder offen
    assert not verify_hdd(app, hdd_a, None, quick=False).failed
